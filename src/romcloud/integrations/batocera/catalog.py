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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from romcloud.core.cue_parser import resolve_cue_dependencies
from romcloud.core.exceptions import GameNotFoundError, ProxyError, ProxyNotOwnedError
from romcloud.core.models.game import Game, GameAsset, derive_title
from romcloud.core.models.proxy import ProxyRecord
from romcloud.core.storage import RemoteEntry, StorageProvider
from romcloud.integrations.batocera.systems import BATOCERA_SYSTEMS
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
        known_systems: Optional[frozenset[str]] = None,
    ) -> None:
        self._provider = provider
        self._game_repo = game_repo
        self._proxy_repo = proxy_repo
        self._local_roms_root = Path(local_roms_root)
        self._source_root = source_root
        self._known_systems = known_systems or BATOCERA_SYSTEMS

    # ── public API ────────────────────────────────────────────────────────────

    def refresh(self) -> CatalogRefreshResult:
        """Scan the remote ROM root; create proxy files for new games.

        Already-tracked games are skipped (not duplicated). An existing game
        whose companion-asset set has changed (e.g. a ``.bin`` that used to
        be catalogued independently is now recognised as part of a ``.cue``
        set) is updated in place, preserving its ``id``/pin state/history.
        Games whose source has disappeared entirely are *not* automatically
        removed — only stale entries superseded by a cue/directory grouping
        change are pruned here (see ``removed``).
        """
        added = skipped = removed = updated = 0
        errors: list[tuple[str, str]] = []
        warnings: list[str] = []

        try:
            remote_systems = self._provider.list_systems(self._source_root)
        except Exception as exc:  # noqa: BLE001
            errors.append(("(root)", str(exc)))
            return CatalogRefreshResult(added, skipped, removed, errors, warnings=warnings, updated=updated)

        matched = [s for s in remote_systems if s in self._known_systems]
        unmatched = [s for s in remote_systems if s not in self._known_systems]
        if unmatched:
            log.debug("Ignoring unrecognised system folders: %s", unmatched)

        for system in matched:
            try:
                entries = self._provider.list_entries(self._source_root, system)
                games, consumed_paths, group_warnings = self._group_entries(system, entries)
                warnings.extend(group_warnings)

                # Prune stale entries *before* adding new ones: a superseded
                # game can derive the exact same proxy filename as its
                # replacement (e.g. both titled "Game"), and freeing that
                # filename/ownership record first avoids a spurious
                # collision inside `_write_proxy`.
                removed += self._prune_stale_entries(system, consumed_paths)

                for game in games:
                    primary = game.primary_asset
                    if primary is None:
                        continue

                    existing = self._game_repo.find_by_source_path(
                        game.source_provider,
                        game.source_root,
                        primary.relative_path,
                    )
                    if existing is not None:
                        if _asset_paths_differ(existing.assets, game.assets):
                            game.id = existing.id
                            game.added_at = existing.added_at
                            game.last_played = existing.last_played
                            self._game_repo.save(game)
                            updated += 1
                            log.info(
                                "Updated companion assets for %r [%s]", game.title, system
                            )
                        else:
                            skipped += 1
                        continue

                    self._game_repo.save(game)
                    self._write_proxy(game)
                    added += 1
                    log.info("Catalogued %r [%s]", game.title, system)

            except Exception as exc:  # noqa: BLE001
                log.exception("Error scanning system %r", system)
                errors.append((system, str(exc)))

        return CatalogRefreshResult(added, skipped, removed, errors, warnings=warnings, updated=updated)

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

    def _group_entries(
        self, system: str, entries: list[RemoteEntry]
    ) -> tuple[list[Game], set[str], list[str]]:
        """Convert raw directory entries into logical Game objects.

        Returns ``(games, consumed_paths, warnings)``:

        - ``consumed_paths`` — asset ``relative_path`` values that are now
          required companions of a cue-based game (or a directory now
          represented by cue-based game(s) instead of one opaque blob).
          These must never become independent games, and any *existing*
          catalog entry at one of these exact paths is stale (see
          ``_prune_stale_entries``). Matching is always by full relative
          path, never by bare filename — two cue sets may legally share a
          companion filename in different directories without colliding.
        - ``warnings`` — human-readable, non-fatal issues (malformed cue
          lines, rejected path-traversal references, missing referenced
          assets) surfaced for visibility without aborting the scan.
        """
        games: list[Game] = []
        consumed_paths: set[str] = set()
        warnings: list[str] = []
        handled_root_cue_names: set[str] = set()
        superseded_dirs: set[str] = set()

        # Pass 1: top-level .cue files are their own logical game.
        for entry in entries:
            if entry.is_directory or entry.name.startswith("."):
                continue
            if Path(entry.name).suffix.lower() != _CUE_EXTENSION:
                continue

            game, cue_warnings = self._build_cue_game(system, entry.relative_path)
            warnings.extend(cue_warnings)
            if game is None:
                continue  # unreadable cue — falls through to plain file cataloguing below

            games.append(game)
            handled_root_cue_names.add(entry.name)
            for asset in game.assets:
                if not asset.is_primary:
                    consumed_paths.add(asset.relative_path)

        # Pass 2: directories containing their own .cue(s) — the "directory
        # isolation" case (e.g. psx/Game A/Game A.cue). Each nested .cue
        # becomes its own logical game instead of the whole directory being
        # one opaque blob (which would cache/launch the directory itself,
        # not the .cue Batocera actually needs).
        for entry in entries:
            if not entry.is_directory or entry.name.startswith("."):
                continue
            try:
                nested = self._provider.list_entries(
                    self._source_root, f"{system}/{entry.name}"
                )
            except Exception:  # noqa: BLE001 — fall back to opaque directory game
                nested = []

            nested_cue_entries = [
                n for n in nested
                if not n.is_directory and Path(n.name).suffix.lower() == _CUE_EXTENSION
            ]
            if not nested_cue_entries:
                continue

            produced: list[Game] = []
            for cue_entry in nested_cue_entries:
                game, cue_warnings = self._build_cue_game(system, cue_entry.relative_path)
                warnings.extend(cue_warnings)
                if game is not None:
                    produced.append(game)

            if not produced:
                continue  # every nested cue was unreadable — keep old opaque behaviour

            games.extend(produced)
            superseded_dirs.add(entry.relative_path)
            for game in produced:
                for asset in game.assets:
                    if not asset.is_primary:
                        consumed_paths.add(asset.relative_path)

        # Pass 3: everything else — unrelated standalone files/directories
        # keep the pre-existing discovery behaviour untouched.
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.name.endswith(".romcloud"):
                continue  # never re-catalog existing proxies

            if entry.is_directory:
                if entry.relative_path in superseded_dirs:
                    continue
                games.append(self._make_directory_game(system, entry))
            else:
                if entry.name in handled_root_cue_names:
                    continue
                if entry.relative_path in consumed_paths:
                    continue
                games.append(self._make_file_game(system, entry))

        consumed_paths |= superseded_dirs
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
        title = entry.name  # directory name is the title (e.g. BCES00000)
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

    # ── proxy I/O ─────────────────────────────────────────────────────────────

    def _write_proxy(self, game: Game) -> None:
        """Create the .romcloud file and record it in the ownership DB."""
        proxy_dir = self._local_roms_root / game.system
        proxy_dir.mkdir(parents=True, exist_ok=True)

        safe_title = _safe_filename(game.title)
        proxy_path = proxy_dir / f"{safe_title}.romcloud"

        # Resolve filename collision by appending a short id suffix.
        if proxy_path.exists() and not self._proxy_repo.owns_path(str(proxy_path)):
            proxy_path = proxy_dir / f"{safe_title}.{game.id[:8]}.romcloud"

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

        record = ProxyRecord.create(game_id=game.id, proxy_path=str(proxy_path))
        self._proxy_repo.save(record)

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
    """True if the *set* of (relative_path, is_primary) pairs differs.

    Used to detect that an existing catalog entry needs its companion
    assets updated (e.g. a cue's referenced tracks were just discovered),
    without triggering churn from mere size-field fluctuations.
    """
    old_keys = {(a.relative_path, a.is_primary) for a in old}
    new_keys = {(a.relative_path, a.is_primary) for a in new}
    return old_keys != new_keys
