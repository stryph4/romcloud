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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from romcloud.core.exceptions import GameNotFoundError, ProxyError, ProxyNotOwnedError
from romcloud.core.models.game import Game, GameAsset, derive_title
from romcloud.core.models.proxy import ProxyRecord
from romcloud.core.providers.base import RemoteEntry, StorageProvider
from romcloud.core.systems import BATOCERA_SYSTEMS
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository

log = get_logger("catalog")

_PROXY_VERSION = "1"

# Extensions that indicate the *entry point* for a game rather than a support file.
# Bare files with unknown extensions are also treated as games.
_DIRECTORY_EXTENSIONS: frozenset[str] = frozenset()  # directories are always entry points

# Extensions whose files are *secondary* when a same-stem primary exists.
_SECONDARY_EXTENSIONS: frozenset[str] = frozenset({".bin", ".img", ".sub", ".raw"})


@dataclass
class CatalogRefreshResult:
    added: int
    skipped: int
    removed: int
    errors: list[tuple[str, str]]  # (system, error_message)

    def __str__(self) -> str:
        lines = [
            f"Added:   {self.added}",
            f"Skipped: {self.skipped}",
            f"Removed: {self.removed}",
        ]
        if self.errors:
            lines.append(f"Errors:  {len(self.errors)}")
            for sys_, msg in self.errors:
                lines.append(f"  [{sys_}] {msg}")
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

        Already-tracked games are skipped (not duplicated).
        Games whose source has disappeared are *not* automatically removed —
        use ``prune()`` for that.
        """
        added = skipped = removed = 0
        errors: list[tuple[str, str]] = []

        try:
            remote_systems = self._provider.list_systems(self._source_root)
        except Exception as exc:  # noqa: BLE001
            errors.append(("(root)", str(exc)))
            return CatalogRefreshResult(added, skipped, removed, errors)

        matched = [s for s in remote_systems if s in self._known_systems]
        unmatched = [s for s in remote_systems if s not in self._known_systems]
        if unmatched:
            log.debug("Ignoring unrecognised system folders: %s", unmatched)

        for system in matched:
            try:
                entries = self._provider.list_entries(self._source_root, system)
                games = self._group_entries(system, entries)

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
                        skipped += 1
                        continue

                    self._game_repo.save(game)
                    self._write_proxy(game)
                    added += 1
                    log.info("Catalogued %r [%s]", game.title, system)

            except Exception as exc:  # noqa: BLE001
                log.exception("Error scanning system %r", system)
                errors.append((system, str(exc)))

        return CatalogRefreshResult(added, skipped, removed, errors)

    def resolve_proxy(self, proxy_path: str) -> Game:
        """Resolve a ``.romcloud`` proxy file to its :class:`~romcloud.core.models.game.Game`.

        First looks up the game_id in SQLite.  If the database entry is
        missing, falls back to reconstructing a :class:`Game` from the proxy
        file's JSON payload.

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
        if game is not None:
            return game

        # Fallback: reconstruct from proxy payload (DB may be missing/rebuilt).
        log.warning(
            "game_id %s not in DB — reconstructing from proxy file %s",
            game_id,
            proxy_path,
        )
        return self._game_from_proxy_payload(game_id, payload)

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

    def _group_entries(self, system: str, entries: list[RemoteEntry]) -> list[Game]:
        """Convert raw directory entries into logical Game objects."""
        games: list[Game] = []
        skip_names: set[str] = set()   # secondary files to suppress

        # First pass: identify .cue primaries so we can suppress their .bin tracks.
        cue_stems: set[str] = set()
        m3u_names: set[str] = set()
        for entry in entries:
            if entry.is_directory or entry.name.startswith("."):
                continue
            stem = Path(entry.name).stem
            ext = Path(entry.name).suffix.lower()
            if ext == ".cue":
                cue_stems.add(stem)
            elif ext == ".m3u":
                m3u_names.add(entry.name)

        # Build skip set: .bin/.img files whose stem matches a .cue.
        for entry in entries:
            if entry.is_directory:
                continue
            stem = Path(entry.name).stem
            ext = Path(entry.name).suffix.lower()
            if ext in _SECONDARY_EXTENSIONS and stem in cue_stems:
                skip_names.add(entry.name)

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.name.endswith(".romcloud"):
                continue  # never re-catalog existing proxies
            if entry.name in skip_names:
                continue

            if entry.is_directory:
                games.append(self._make_directory_game(system, entry))
            else:
                games.append(self._make_file_game(system, entry))

        return games

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
