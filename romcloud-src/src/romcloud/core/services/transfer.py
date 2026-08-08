"""Transfer service — staged, resumable file transfers.

Transfer lifecycle
------------------

1. For each asset, stage it at
   ``{cache_root}/.partial/{system}/{asset path relative to system root}``.
2. Transfer each asset from the provider into staging (resumable per-file).
3. Validate that all staging files match expected sizes.
4. Atomically promote each staged asset to its final cache path:
   ``{cache_root}/{system}/{asset path relative to system root}``.
5. Return the final cache path of the game's primary asset.

The final (and staging) layout mirrors the source's relative path under the
system, so the original basename is always preserved and identical filenames
in different systems or subdirectories never collide — see
:mod:`romcloud.core.cache_paths`.

A power-loss or interruption leaves the staging path(s) in place.
The next call picks up where it left off (resume logic is in the provider).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

from romcloud.core.cache_paths import resolve_cache_path
from romcloud.core.exceptions import TransferError, TransferValidationError
from romcloud.core.models.game import Game
from romcloud.core.providers.base import StorageProvider
from romcloud.infrastructure.logging import get_logger

log = get_logger("transfer")


class TransferService:
    """Orchestrates staged, resumable transfers for a single game."""

    def __init__(self, provider: StorageProvider, cache_root: str) -> None:
        self._provider = provider
        self._cache_root = Path(cache_root)
        self._partial_root = self._cache_root / ".partial"

    # ── public API ────────────────────────────────────────────────────────────

    def transfer(
        self,
        game: Game,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """Transfer all game assets and return the final cache path.

        The returned path is the primary asset's final location:
        ``{cache_root}/{system}/{relative path within that system}``.

        Resume: if a previous transfer was interrupted, only missing/incomplete
        assets are re-transferred.
        """
        if not game.assets:
            raise TransferError(f"Game {game.id!r} has no assets to transfer")

        log.info("Starting transfer for %r (%s)", game.title, game.id)

        try:
            for asset in game.assets:
                src = str(Path(game.source_root) / asset.relative_path)
                # Preserve the original filename verbatim, and mirror its
                # relative location under the system — never flatten to a
                # bare filename. Batocera's configgen matches per-game
                # settings by filename (e.g. snes["Some Game.sfc"].*);
                # renaming here would silently break any game-specific
                # emulator/core overrides.
                dst = self._staging_path(game.system, asset.relative_path)
                dst.parent.mkdir(parents=True, exist_ok=True)

                log.debug("Transferring asset %s → %s", asset.relative_path, dst)
                self._provider.transfer_to(src, str(dst), on_progress)

            self._validate(game)

            final = self._promote(game)
            log.info("Transfer complete for %r → %s", game.title, final)
            return final

        except Exception as exc:
            log.warning(
                "Transfer failed for %r (%s): %s — staging preserved for resume",
                game.title,
                game.id,
                exc,
            )
            raise

    def staging_size(self, game: Game) -> int:
        """Return the byte total of whatever is currently staged for *game*."""
        total = 0
        for asset in game.assets:
            staged = self._staging_path(game.system, asset.relative_path)
            if staged.is_file():
                total += staged.stat().st_size
            elif staged.is_dir():
                total += sum(f.stat().st_size for f in staged.rglob("*") if f.is_file())
        return total

    def discard_staging(self, game: Game) -> None:
        """Remove any staged (partial) data for *game* (e.g. after a failed cancel)."""
        for asset in game.assets:
            staged = self._staging_path(game.system, asset.relative_path)
            if staged.is_dir():
                shutil.rmtree(staged)
                log.debug("Discarded staging dir for %s", staged)
            elif staged.exists():
                staged.unlink()
                log.debug("Discarded staging file for %s", staged)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _staging_path(self, system: str, relative_path: str) -> Path:
        return resolve_cache_path(self._partial_root, system, relative_path)

    def _final_path(self, system: str, relative_path: str) -> Path:
        return resolve_cache_path(self._cache_root, system, relative_path)

    def _validate(self, game: Game) -> None:
        """Check that staged files match expected sizes where known."""
        for asset in game.assets:
            staged = self._staging_path(game.system, asset.relative_path)
            if not staged.exists():
                raise TransferValidationError(
                    f"Asset missing after transfer: {asset.filename}"
                )
            if asset.size_bytes is not None:
                actual = (
                    staged.stat().st_size
                    if staged.is_file()
                    else sum(f.stat().st_size for f in staged.rglob("*") if f.is_file())
                )
                if actual != asset.size_bytes:
                    raise TransferValidationError(
                        f"Size mismatch for {asset.filename}: "
                        f"expected {asset.size_bytes}, got {actual}"
                    )

    def _promote(self, game: Game) -> str:
        """Move each staged asset to its final cache location atomically."""
        final_primary: Optional[str] = None

        for asset in game.assets:
            staged = self._staging_path(game.system, asset.relative_path)
            final = self._final_path(game.system, asset.relative_path)
            final.parent.mkdir(parents=True, exist_ok=True)

            if final.exists():
                if final.is_dir():
                    shutil.rmtree(final)
                else:
                    final.unlink()

            shutil.move(str(staged), str(final))

            if asset.is_primary or final_primary is None:
                final_primary = str(final)

        assert final_primary is not None  # game.assets is non-empty (checked above)
        return final_primary
