"""Catalog service — scan a remote ROM root and manage proxy files.

Responsibilities
----------------
- Scan the storage provider for Batocera system folders.
- Group discovered entries into logical :class:`~romcloud.core.models.game.Game` objects.
- Write ``.romcloud`` proxy files into the local Batocera ROM directories.
- Track ownership of generated proxies in SQLite.
- Resolve a ``.romcloud`` proxy path back to a :class:`~romcloud.core.models.game.Game`.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional
from xml.etree import ElementTree as ET

from romcloud.core.cue_parser import resolve_cue_dependencies
from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.exceptions import (
    GameNotFoundError,
    ProviderError,
    ProxyError,
    ProxyNotOwnedError,
)
from romcloud.core.models.game import Game, GameAsset, derive_title
from romcloud.core.models.proxy import ProxyRecord
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.core.storage import RemoteEntry, StorageProvider
from romcloud.integrations.batocera.system_registry import (
    EffectiveSystemRegistry,
    SystemLaunchSpec,
    SystemRegistryError,
)
from romcloud.integrations.batocera.proxy_ownership import remove_owned_proxy_files
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository

log = get_logger("catalog")

_PROXY_VERSION = "1"

_CUE_EXTENSION = ".cue"


@dataclass
class CatalogRefreshResult:
    added: int
    skipped: int
    removed: int
    errors: list[tuple[str, str]]  # (system, error_message)
    warnings: list[str] = field(default_factory=list)
    """Non-fatal issues surfaced for visibility (e.g. a cue referencing a
    missing companion file, a rejected path-traversal reference, a malformed
    cue line) — these never block a refresh, unlike ``errors``."""
    updated: int = 0
    """Existing games whose companion-asset set changed (e.g. a
    previously-independent .bin now recognised as part of a .cue set)."""

    def __str__(self) -> str:
        lines = [
            f"Added:   {self.added}",
            f"Updated: {self.updated}",
            f"Skipped: {self.skipped}",
            f"Removed: {self.removed}",
        ]
        if self.errors:
            lines.append(f"Errors:  {len(self.errors)}")
            for sys_, msg in self.errors:
                lines.append(f"  [{sys_}] {msg}")
        if self.warnings:
            lines.append(f"Warnings: {len(self.warnings)}")
            for msg in self.warnings:
                lines.append(f"  {msg}")
        return "\n".join(lines)


class CatalogService:
    """Scans a remote ROM source and maintains the local proxy catalog."""

    def __init__(
        self,
        provider: StorageProvider,
        game_repo: GameRepository,
        proxy_repo: ProxyRepository,
        local_roms_root: str,
        source_root: str,
        system_registry: Optional[EffectiveSystemRegistry] = None,
        registry_loader: Optional[Callable[[], EffectiveSystemRegistry]] = None,
        write_proxies: bool = True,
        capability_policy: Optional[CapabilityPolicy] = None,
    ) -> None:
        self._provider = provider
        self._game_repo = game_repo
        self._proxy_repo = proxy_repo
        self._local_roms_root = Path(local_roms_root)
        self._source_root = source_root
        self._system_registry = system_registry
        self._registry_loader = registry_loader
        self._write_proxies_enabled = write_proxies
        self._capabilities = capability_policy or CapabilityPolicy("smart_cache")

    # ── public API ────────────────────────────────────────────────────────────

    def refresh(self, progress: ProgressSink = None) -> CatalogRefreshResult:
        """Scan the remote ROM root; create proxy files for new games.

        Already-tracked games are skipped (not duplicated). An existing game
        whose companion-asset set has changed (e.g. a ``.bin`` that used to
        be catalogued independently is now recognised as part of a ``.cue``
        set) is updated in place, preserving its ``id``/pin state/history.
        Games whose source has disappeared entirely are *not* automatically
        removed — only stale entries superseded by a cue/directory grouping
        change are pruned here (see ``removed``).
        """
        self._capabilities.require(Capability.CATALOG_REFRESH, "Catalog refresh")
        added = skipped = removed = updated = 0
        errors: list[tuple[str, str]] = []
        warnings: list[str] = []

        emit_progress(
            progress,
            "catalog_refresh",
            "refresh_started",
            "running",
            "Starting catalog refresh",
        )

        try:
            registry = (
                self._registry_loader()
                if self._registry_loader is not None
                else self._system_registry
            )
            if registry is None:
                raise SystemRegistryError(
                    "No Batocera launch registry was configured; discovery was skipped"
                )
            remote_systems = self._provider.list_systems(self._source_root)
        except Exception as exc:  # noqa: BLE001
            errors.append(("(root)", str(exc)))
            emit_progress(
                progress,
                "catalog_refresh",
                "refresh_completed",
                "error",
                "Catalog refresh failed before systems could be discovered",
                detail=str(exc),
                current=0,
                total=0,
                metadata={"succeeded": 0, "failed": 1},
            )
            return CatalogRefreshResult(added, skipped, removed, errors, warnings=warnings, updated=updated)

        matched = [s for s in remote_systems if s in registry.names]
        unmatched = [s for s in remote_systems if s not in registry.names]
        if unmatched:
            log.debug("Ignoring systems absent from effective Batocera config: %s", unmatched)
            # A complete live registry positively proves that retained rows
            # for these present source systems are no longer launchable. An
            # LKG registry is intentionally non-authoritative for removals.
            if not registry.from_last_known_good:
                for system in unmatched:
                    try:
                        self._suppress_entire_system(system)
                    except Exception as exc:  # noqa: BLE001
                        log.exception(
                            "Could not suppress catalog rows for removed system %r",
                            system,
                        )
                        errors.append((system, str(exc)))

        system_total = len(matched)
        for system in matched:
            emit_progress(
                progress,
                "catalog_refresh",
                "system_queued",
                "queued",
                f"{system} queued",
                metadata={"system": system},
            )
        emit_progress(
            progress,
            "catalog_refresh",
            "systems_discovered",
            "running",
            f"{system_total} system{'s' if system_total != 1 else ''} queued",
            current=0,
            total=system_total,
            metadata={"systems": matched},
        )

        completed_systems = 0
        failed_systems = 0
        for system in matched:
            emit_progress(
                progress,
                "catalog_refresh",
                "system_started",
                "running",
                f"Refreshing {system}",
                metadata={"system": system},
            )
            try:
                spec = registry.get(system)
                if spec is None:  # guarded by `matched`; keeps narrowing explicit
                    continue
                entries, known_ineligible, ordinary_directories = (
                    self._discover_entries(system, spec)
                )
                games, consumed_paths, group_warnings = self._group_entries(system, entries)
                warnings.extend(group_warnings)
                legacy_matches = self._legacy_container_matches(
                    system, games, ordinary_directories
                )

                game_total = len(games)
                emit_progress(
                    progress,
                    "catalog_refresh",
                    "system_progress",
                    "running",
                    f"Refreshing {system}: {game_total} entries discovered",
                    current=0,
                    total=game_total,
                    metadata={"system": system, "discovered": game_total},
                )

                # Prune stale entries *before* adding new ones: a superseded
                # game can derive the exact same proxy filename as its
                # replacement (e.g. both titled "Game"), and freeing that
                # filename/ownership record first avoids a spurious
                # collision inside `_write_proxy`.
                removed += self._prune_stale_entries(system, consumed_paths)

                progress_interval = max(1, game_total // 100)
                for game_index, game in enumerate(games, start=1):
                    primary = game.primary_asset
                    if primary is None:
                        self._emit_system_progress(
                            progress, system, game_index, game_total, progress_interval
                        )
                        continue

                    existing = self._game_repo.find_by_source_path(
                        game.source_provider,
                        game.source_root,
                        primary.relative_path,
                    )
                    if existing is None:
                        existing = legacy_matches.get(primary.relative_path)
                    if existing is not None:
                        changed = (
                            _asset_paths_differ(existing.assets, game.assets)
                            or not existing.is_eligible
                        )
                        if changed:
                            game.id = existing.id
                            game.added_at = existing.added_at
                            game.last_played = existing.last_played
                            game.is_eligible = True
                            self._game_repo.save(game)
                            self._rewrite_owned_proxy(game)
                            updated += 1
                            log.info(
                                "Updated asset metadata for %r [%s]", game.title, system
                            )
                        else:
                            skipped += 1
                        self._emit_system_progress(
                            progress, system, game_index, game_total, progress_interval
                        )
                        continue

                    self._game_repo.save(game)
                    self._write_proxy(game)
                    added += 1
                    log.info("Catalogued %r [%s]", game.title, system)

                    self._emit_system_progress(
                        progress, system, game_index, game_total, progress_interval
                    )

                # Only paths positively observed and rejected during this
                # complete system traversal are suppressed. Missing source
                # paths and failed scans retain their previous exposure.
                if not registry.from_last_known_good:
                    self._suppress_ineligible_paths(system, known_ineligible)

                # Empty systems and all-skipped systems still need an explicit
                # terminal state; percentages are only based on the known
                # grouped-game denominator above.
                completed_systems += 1
                emit_progress(
                    progress,
                    "catalog_refresh",
                    "system_completed",
                    "success",
                    f"{system} complete",
                    current=game_total,
                    total=game_total,
                    metadata={"system": system},
                )

            except Exception as exc:  # noqa: BLE001
                log.exception("Error scanning system %r", system)
                errors.append((system, str(exc)))
                failed_systems += 1
                emit_progress(
                    progress,
                    "catalog_refresh",
                    "system_failed",
                    "error",
                    f"{system} failed",
                    detail=str(exc),
                    metadata={"system": system},
                )

            emit_progress(
                progress,
                "catalog_refresh",
                "overall_progress",
                "running",
                f"{completed_systems + failed_systems} of {system_total} systems finished",
                current=completed_systems + failed_systems,
                total=system_total,
                metadata={"succeeded": completed_systems, "failed": failed_systems},
            )

        final_status = "error" if errors else "success"
        summary = f"Catalog refresh complete: {completed_systems} succeeded"
        if failed_systems:
            summary += f", {failed_systems} failed"
        emit_progress(
            progress,
            "catalog_refresh",
            "refresh_completed",
            final_status,
            summary,
            current=system_total,
            total=system_total,
            metadata={"succeeded": completed_systems, "failed": failed_systems},
        )

        return CatalogRefreshResult(added, skipped, removed, errors, warnings=warnings, updated=updated)

    @staticmethod
    def _emit_system_progress(
        progress: ProgressSink,
        system: str,
        current: int,
        total: int,
        interval: int,
    ) -> None:
        if current != total and current % interval:
            return
        emit_progress(
            progress,
            "catalog_refresh",
            "system_progress",
            "running",
            f"Refreshing {system}",
            current=current,
            total=total,
            metadata={"system": system},
        )

    def resolve_proxy(self, proxy_path: str) -> Game:
        """Resolve a ``.romcloud`` proxy file to its :class:`~romcloud.core.models.game.Game`.

        First looks up the game_id in SQLite.  If the database entry is
        missing, falls back to reconstructing a :class:`Game` from the proxy
        file's JSON payload. Either way, if the resolved game's launch asset
        is a ``.cue``, its companion-asset list is reconciled against the
        *current* source before being returned — see
        :meth:`_reconcile_cue_assets` for why this can't wait for the next
        ``romcloud refresh``.

        Raises :class:`~romcloud.core.exceptions.ProxyError` if the file is
        not a valid ROMCloud proxy.
        """
        path = Path(proxy_path)
        if not path.exists():
            raise ProxyError(f"Proxy file not found: {proxy_path}")
        if path.suffix.lower() != ".romcloud":
            raise ProxyError(f"Not a .romcloud file: {proxy_path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProxyError(f"Cannot read proxy {proxy_path}: {exc}") from exc

        game_id = payload.get("game_id")
        if not game_id:
            raise ProxyError(f"Missing game_id in proxy: {proxy_path}")

        # Primary: SQLite lookup.
        game = self._game_repo.get(game_id)
        if game is None:
            # Fallback: reconstruct from proxy payload (DB may be missing/rebuilt).
            log.warning(
                "game_id %s not in DB — reconstructing from proxy file %s",
                game_id,
                proxy_path,
            )
            game = self._game_from_proxy_payload(game_id, payload)

        return self._reconcile_cue_assets(game)

    def _reconcile_cue_assets(self, game: Game) -> Game:
        """Self-heal a legacy/stale companion-asset list at resolve time.

        A catalog row created before cue-dependency parsing existed (or one
        a `romcloud refresh` simply hasn't reprocessed yet since upgrading)
        may still list the ``.cue`` as its *only* asset. Cache-hit
        completeness only ever checks the assets a `Game` currently claims
        to have (see `CacheService.is_cached`) — so if the catalog itself
        doesn't yet know about the required companion tracks, a genuinely
        incomplete legacy cache would be silently treated as a full hit at
        launch, without waiting for a `romcloud refresh` to have run first.

        This re-derives the cue's companions from the *current* source
        (identical logic to fresh discovery — see `_build_cue_assets`) every
        time a proxy is resolved, and persists the corrected asset list
        in place (same `game.id` — pinning/cache history untouched) whenever
        it differs from what's already catalogued. Any failure degrades to
        returning the catalog's existing (possibly stale) assets rather than
        blocking resolution — "ROMCloud may fail; Batocera must not".
        """
        primary = game.primary_asset
        if primary is None or Path(primary.filename).suffix.lower() != _CUE_EXTENSION:
            return game

        try:
            assets, _warnings = self._build_cue_assets(game.system, primary.relative_path)
        except Exception:  # noqa: BLE001
            log.exception(
                "Cue reconciliation failed for %s (%s) — using existing catalog assets",
                game.id,
                primary.relative_path,
            )
            return game

        if assets is None or not _asset_paths_differ(game.assets, assets):
            return game

        game.assets = assets
        try:
            self._game_repo.save(game)
            log.info(
                "Reconciled stale legacy asset list for %r (%s) at resolve time",
                game.title,
                game.id,
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist reconciled asset list for %s", game.id)
        return game

    def remove_proxy(self, game_id: str) -> None:
        """Remove the proxy file and catalog entry for *game_id*.

        Safety: only removes proxy files that are recorded in the ownership DB.
        """
        record = self._proxy_repo.get(game_id)
        if record is None:
            return

        proxy_path = Path(record.proxy_path)
        if proxy_path.exists():
            if not self._proxy_repo.owns_path(str(proxy_path)):
                raise ProxyNotOwnedError(
                    f"Refusing to delete unowned proxy: {proxy_path}"
                )
            proxy_path.unlink()

        self._proxy_repo.delete(game_id)
        self._game_repo.delete(game_id)
        log.info("Removed proxy and catalog entry for %s", game_id)

    def list_games(self, system: Optional[str] = None) -> list[Game]:
        if system:
            return self._game_repo.find_by_system(system)
        return self._game_repo.list_all()

    # ── grouping ──────────────────────────────────────────────────────────────

    def _source_metadata_entries(self, system: str) -> set[str]:
        """Exclude source gamelist/media containers from ROM discovery.

        Media directory names are derived from safe references in the source
        gamelist rather than from one scraper's fixed directory convention.
        """
        gamelist_rel = PurePosixPath(system, "gamelist.xml").as_posix()
        excluded = {gamelist_rel}
        try:
            text = self._provider.read_text(
                str(Path(self._source_root) / system / "gamelist.xml")
            )
            root = ET.fromstring(text)
        except Exception:  # noqa: BLE001 - absent/malformed metadata is non-fatal
            return excluded
        if root.tag != "gameList":
            return excluded
        for game in root.findall("game"):
            for tag in (
                "image", "thumbnail", "video", "marquee", "fanart",
                "manual", "boxback", "bezel", "wheel",
            ):
                raw = (game.findtext(tag) or "").strip().replace("\\", "/")
                while raw.startswith("./"):
                    raw = raw[2:]
                normalized = posixpath.normpath(raw)
                parts = PurePosixPath(normalized).parts
                if (
                    not raw
                    or raw.startswith(("/", "~"))
                    or normalized in (".", "..")
                    or normalized.startswith("../")
                    or not parts
                ):
                    continue
                excluded.add(PurePosixPath(system, parts[0]).as_posix())
        return excluded

    def _discover_entries(
        self, system: str, spec: SystemLaunchSpec
    ) -> tuple[list[RemoteEntry], set[str], set[str]]:
        """Recursively find only entries Batocera can launch for *system*."""
        candidates: list[RemoteEntry] = []
        known_ineligible: set[str] = set()
        ordinary_directories: set[str] = set()
        excluded = self._source_metadata_entries(system)
        pending = [system]
        visited: set[str] = set()

        while pending:
            relative_dir = pending.pop()
            if relative_dir in visited:
                raise ProviderError(
                    f"Provider returned a recursive directory cycle at {relative_dir!r}"
                )
            visited.add(relative_dir)
            for raw_entry in self._provider.list_entries(
                self._source_root, relative_dir
            ):
                entry = self._validated_entry(system, relative_dir, raw_entry)
                path = entry.relative_path
                if any(path == root or path.startswith(root + "/") for root in excluded):
                    known_ineligible.add(path)
                    continue
                if entry.is_symlink:
                    known_ineligible.add(path)
                    log.warning("Ignoring symlink during source discovery: %s", path)
                    continue
                if spec.accepts(entry.name):
                    candidates.append(entry)
                    continue
                known_ineligible.add(path)
                if entry.is_directory:
                    ordinary_directories.add(path)
                    pending.append(path)

        candidates.sort(key=lambda entry: entry.relative_path.casefold())
        return candidates, known_ineligible, ordinary_directories

    @staticmethod
    def _validated_entry(
        system: str, parent: str, entry: RemoteEntry
    ) -> RemoteEntry:
        """Reject malformed provider paths before they enter catalog identity."""
        if (
            not entry.name
            or entry.name in (".", "..")
            or "/" in entry.name
            or "\\" in entry.name
        ):
            raise ProviderError(f"Provider returned unsafe entry name: {entry.name!r}")
        normalized_parent = PurePosixPath(parent.replace("\\", "/")).as_posix()
        normalized_path = PurePosixPath(
            entry.relative_path.replace("\\", "/")
        ).as_posix()
        expected = PurePosixPath(normalized_parent, entry.name).as_posix()
        parts = PurePosixPath(normalized_path).parts
        if (
            normalized_path != expected
            or not parts
            or parts[0] != system
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ProviderError(
                "Provider entry escapes or disagrees with source directory: "
                f"{entry.relative_path!r}"
            )
        return RemoteEntry(
            name=entry.name,
            relative_path=normalized_path,
            is_directory=entry.is_directory,
            size_bytes=entry.size_bytes,
            is_symlink=entry.is_symlink,
        )

    def _group_entries(
        self, system: str, entries: list[RemoteEntry]
    ) -> tuple[list[Game], set[str], list[str]]:
        """Convert already-eligible recursive candidates into logical games."""
        games: list[Game] = []
        consumed_paths: set[str] = set()
        warnings: list[str] = []
        handled_cues: set[str] = set()

        for entry in entries:
            if entry.is_directory or Path(entry.name).suffix.lower() != _CUE_EXTENSION:
                continue
            game, cue_warnings = self._build_cue_game(system, entry.relative_path)
            warnings.extend(cue_warnings)
            if game is None:
                continue
            games.append(game)
            handled_cues.add(entry.relative_path)
            consumed_paths.update(
                asset.relative_path for asset in game.assets if not asset.is_primary
            )

        for entry in entries:
            if entry.relative_path in handled_cues or entry.relative_path in consumed_paths:
                continue
            games.append(
                self._make_directory_game(system, entry)
                if entry.is_directory
                else self._make_file_game(system, entry)
            )
        return games, consumed_paths, warnings

    def _build_cue_game(
        self, system: str, cue_relative_path: str
    ) -> tuple[Optional[Game], list[str]]:
        """Parse one ``.cue`` file into a brand-new logical Game (launch
        asset + every referenced track as a required companion asset).

        Returns ``(None, warnings)`` if the cue itself cannot even be read —
        callers should fall back to cataloguing it as an ordinary single
        file rather than losing it entirely ("ROMCloud may fail; Batocera
        must not").
        """
        assets, warnings = self._build_cue_assets(system, cue_relative_path)
        if assets is None:
            return None, warnings

        cue_name = Path(cue_relative_path).name
        game = Game.create(
            system=system,
            title=derive_title(cue_name),
            source_provider=self._provider.provider_id,
            source_root=self._source_root,
            assets=assets,
        )
        return game, warnings

    def _build_cue_assets(
        self, system: str, cue_relative_path: str
    ) -> tuple[Optional[list[GameAsset]], list[str]]:
        """Parse one ``.cue`` file into ``[primary, *companions]`` — the pure
        asset-list computation shared by fresh discovery (:meth:`_build_cue_game`)
        and stale-catalog reconciliation at resolve/launch time
        (:meth:`_reconcile_cue_assets`), so both always derive companions the
        same way instead of one path silently drifting from the other.

        Always re-reads the cue and re-queries sizes from the *current*
        source state — never trusts a caller-supplied/previously-catalogued
        size, so a stale legacy record is never used to decide completeness.

        Returns ``(None, warnings)`` if the cue itself cannot even be read.
        """
        warnings: list[str] = []
        source_path = str(Path(self._source_root) / cue_relative_path)

        try:
            cue_text = self._provider.read_text(source_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"[{system}] could not read cue {cue_relative_path!r}: {exc} "
                "— cataloguing as a plain file instead"
            )
            return None, warnings

        cue_size = self._provider.get_size(source_path)
        result = resolve_cue_dependencies(cue_relative_path, cue_text)

        for w in result.warnings:
            warnings.append(
                f"[{system}] {cue_relative_path}: {w.reason} "
                f"(line {w.line_number}: {w.line.strip()!r})"
            )
        for r in result.rejected:
            warnings.append(
                f"[{system}] {cue_relative_path}: rejected reference "
                f"{r.raw_reference!r} — {r.reason}"
            )

        companions: list[GameAsset] = []
        seen_paths: set[str] = {cue_relative_path}
        for dep in result.dependencies:
            if dep.relative_path in seen_paths:
                continue
            seen_paths.add(dep.relative_path)

            size = self._provider.get_size(str(Path(self._source_root) / dep.relative_path))
            if size is None:
                warnings.append(
                    f"[{system}] {cue_relative_path}: referenced asset missing: "
                    f"{dep.relative_path}"
                )
            companions.append(
                GameAsset(
                    filename=Path(dep.relative_path).name,
                    relative_path=dep.relative_path,
                    size_bytes=size,
                    is_primary=False,
                )
            )

        primary = GameAsset(
            filename=Path(cue_relative_path).name,
            relative_path=cue_relative_path,
            size_bytes=cue_size,
            is_primary=True,
        )
        return [primary, *companions], warnings

    def _prune_stale_entries(self, system: str, consumed_paths: set[str]) -> int:
        """Remove catalog/proxy entries that are now stale because their
        sole asset path is a required companion of a cue set (or a
        directory now represented by cue-based game(s)).

        Only ever removes ROMCloud-owned proxy files and DB rows — never
        touches the real source ROM files.
        """
        if not consumed_paths:
            return 0

        removed = 0
        for existing_game in self._game_repo.find_by_system(system):
            primary = existing_game.primary_asset
            if primary is None or primary.relative_path not in consumed_paths:
                continue
            try:
                self.remove_proxy(existing_game.id)
                if self._game_repo.get(existing_game.id) is None:
                    removed += 1
                    log.info(
                        "Pruned stale catalog entry %r (%s) [%s] — now part of a cue set",
                        existing_game.title,
                        primary.relative_path,
                        system,
                    )
                else:
                    log.warning(
                        "Stale entry %s had no owned proxy to remove — "
                        "deleting catalog row directly",
                        existing_game.id,
                    )
                    self._game_repo.delete(existing_game.id)
                    removed += 1
            except ProxyNotOwnedError as exc:
                log.warning("Could not prune stale entry %s: %s", existing_game.id, exc)
        return removed

    def _make_file_game(self, system: str, entry: RemoteEntry) -> Game:
        title = derive_title(entry.name)
        asset = GameAsset(
            filename=entry.name,
            relative_path=entry.relative_path,
            size_bytes=entry.size_bytes,
            is_primary=True,
        )
        return Game.create(
            system=system,
            title=title,
            source_provider=self._provider.provider_id,
            source_root=self._source_root,
            assets=[asset],
        )

    def _make_directory_game(self, system: str, entry: RemoteEntry) -> Game:
        title = derive_title(entry.name)
        asset = GameAsset(
            filename=entry.name,
            relative_path=entry.relative_path,
            size_bytes=entry.size_bytes,
            is_primary=True,
        )
        return Game.create(
            system=system,
            title=title,
            source_provider=self._provider.provider_id,
            source_root=self._source_root,
            assets=[asset],
        )

    def _legacy_container_matches(
        self,
        system: str,
        games: list[Game],
        ordinary_directories: set[str],
    ) -> dict[str, Game]:
        """Map one nested launchable to an old opaque-directory identity.

        A legacy directory is adopted only when it contains exactly one
        discovered game. Multiple descendants leave the old row retained but
        ineligible; none is allowed to guess which cache/pin/history owns it.
        """
        primaries = {
            game.primary_asset.relative_path: game
            for game in games
            if game.primary_asset is not None
        }
        legacy_dirs: dict[str, Game] = {}
        for existing in self._game_repo.find_by_system(
            system, include_ineligible=True
        ):
            primary = existing.primary_asset
            if (
                primary is not None
                and len(existing.assets) == 1
                and primary.relative_path in ordinary_directories
            ):
                legacy_dirs[primary.relative_path] = existing

        matches: dict[str, Game] = {}
        for directory, existing in sorted(
            legacy_dirs.items(), key=lambda item: len(PurePosixPath(item[0]).parts),
            reverse=True,
        ):
            descendants = [
                path for path in primaries if path.startswith(directory.rstrip("/") + "/")
            ]
            if len(descendants) == 1 and descendants[0] not in matches:
                matches[descendants[0]] = existing
        return matches

    def _suppress_ineligible_paths(
        self, system: str, known_ineligible: set[str]
    ) -> None:
        if not known_ineligible:
            return
        games = [
            game
            for game in self._game_repo.find_by_system(
                system, include_ineligible=True
            )
            if game.primary_asset is not None
            and game.primary_asset.relative_path in known_ineligible
        ]
        self._suppress_games(games)

    def _suppress_entire_system(self, system: str) -> None:
        self._suppress_games(
            self._game_repo.find_by_system(system, include_ineligible=True)
        )

    def _suppress_games(self, games: list[Game]) -> None:
        """Hide retained rows and remove only their owned presentation."""
        if not games:
            return
        game_ids = {game.id for game in games}
        manifest_records = [
            (record.game_id, Path(record.proxy_path))
            for record in self._proxy_repo.list_all()
            if record.game_id in game_ids
        ]
        remove_owned_proxy_files(
            self._local_roms_root,
            manifest_records=manifest_records,
            remove_game_ids=game_ids,
        )
        for game in games:
            self._proxy_repo.delete(game.id)
            if game.is_eligible:
                self._game_repo.set_eligible(game.id, False)
                log.info(
                    "Retained but hid ineligible catalog row %r (%s)",
                    game.title,
                    game.primary_asset.relative_path if game.primary_asset else game.id,
                )

    # ── proxy I/O ─────────────────────────────────────────────────────────────

    def ensure_proxy(self, game: Game) -> ProxyRecord:
        """Register and materialize a proxy for a game that has none yet.

        Used to recover games whose registration was never created (e.g. an
        interrupted catalog refresh) — always writes the file, independent
        of the ``write_proxies`` setting used for full catalog refreshes,
        since the caller (mode-presentation reconciliation) always needs
        the actual file to exist once a game is selected for exposure.
        """
        existing = self._proxy_repo.get(game.id)
        if existing is not None:
            return existing

        proxy_dir = self._local_roms_root / game.system
        proxy_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = self._allocate_proxy_path(game)

        self._write_proxy_payload(proxy_path, game)
        record = ProxyRecord.create(game_id=game.id, proxy_path=str(proxy_path))
        self._proxy_repo.save(record)
        return record

    def _write_proxy(self, game: Game) -> None:
        """Record proxy ownership and materialize it when presentation allows."""
        proxy_dir = self._local_roms_root / game.system
        if self._write_proxies_enabled:
            proxy_dir.mkdir(parents=True, exist_ok=True)

        proxy_path = self._allocate_proxy_path(game)

        if self._write_proxies_enabled:
            self._write_proxy_payload(proxy_path, game)

        record = ProxyRecord.create(game_id=game.id, proxy_path=str(proxy_path))
        self._proxy_repo.save(record)

    def _allocate_proxy_path(self, game: Game) -> Path:
        """Return a path that does not steal another game's durable identity.

        Connected and Offline presentation may intentionally remove a proxy
        *file* while retaining its ownership row.  Collision detection must
        therefore consult both states: filesystem existence alone cannot tell
        whether a title-derived path is available.  This also protects an
        unregistered/foreign file and handles the unlikely case that two game
        IDs share the same eight-character suffix.
        """
        proxy_dir = self._local_roms_root / game.system
        safe_title = _safe_filename(game.title)
        default = proxy_dir / f"{safe_title}.romcloud"
        if not self._proxy_path_conflicts(default, game.id):
            return default

        safe_id = _safe_filename(game.id[:8]) or "game"
        candidate = proxy_dir / f"{safe_title}.{safe_id}.romcloud"
        collision = 2
        while self._proxy_path_conflicts(candidate, game.id):
            candidate = proxy_dir / f"{safe_title}.{safe_id}.{collision}.romcloud"
            collision += 1
        return candidate

    def _proxy_path_conflicts(self, path: Path, game_id: str) -> bool:
        owner = self._proxy_repo.get_by_path(str(path))
        if owner is not None:
            return owner.game_id != game_id
        # An existing path with no ownership row is user/foreign state and
        # must never be adopted or overwritten.
        return path.exists()

    def _rewrite_owned_proxy(self, game: Game) -> None:
        """Refresh an existing owned proxy without changing its path."""
        record = self._proxy_repo.get(game.id)
        if record is None:
            self._write_proxy(game)
            return
        if self._write_proxies_enabled:
            self._write_proxy_payload(Path(record.proxy_path), game)

    def _write_proxy_payload(self, proxy_path: Path, game: Game) -> None:
        payload = {
            "romcloud_version": _PROXY_VERSION,
            "game_id": game.id,
            "title": game.title,
            "system": game.system,
            "source_provider": game.source_provider,
            "source_root": game.source_root,
            "assets": [
                {
                    "filename": a.filename,
                    "relative_path": a.relative_path,
                    "is_primary": a.is_primary,
                }
                for a in game.assets
            ],
        }
        proxy_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _game_from_proxy_payload(self, game_id: str, payload: dict) -> Game:
        assets = [
            GameAsset(
                filename=a["filename"],
                relative_path=a["relative_path"],
                is_primary=a.get("is_primary", False),
            )
            for a in payload.get("assets", [])
        ]
        return Game(
            id=game_id,
            system=payload.get("system", "unknown"),
            title=payload.get("title", "Unknown"),
            source_provider=payload.get("source_provider", "local"),
            source_root=payload.get("source_root", ""),
            assets=assets,
            added_at=datetime.now(timezone.utc),
        )


def _safe_filename(title: str) -> str:
    """Strip characters that are problematic in filenames."""
    bad = r'\/:*?"<>|'
    return "".join(c if c not in bad else "_" for c in title)


def _asset_paths_differ(old: list[GameAsset], new: list[GameAsset]) -> bool:
    """True if the identifying asset metadata differs.

    Used to detect that an existing catalog entry needs its companion
    assets or exact source filename/path updated, without triggering churn
    from mere size-field fluctuations.
    """
    old_keys = {(a.filename, a.relative_path, a.is_primary) for a in old}
    new_keys = {(a.filename, a.relative_path, a.is_primary) for a in new}
    return old_keys != new_keys
