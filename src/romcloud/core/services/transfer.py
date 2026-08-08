"""Transfer service — staged, resumable file transfers.

Transfer lifecycle
------------------

1. Create a staging directory: ``{cache_root}/.partial/{game_id}/``
2. Transfer each asset from the provider into staging (resumable per-file).
3. Validate that all staging files match expected sizes.
4. Atomically promote the staging directory to the final cache path.
5. Return the final cache path.

A power-loss or interruption leaves the staging directory in place.
The next call picks up where it left off (resume logic is in the provider).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

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
        """Transfer all game assets and return the final cache container path.

        The returned path is ``{cache_root}/{system}/{game_id}/``.
        Assets live inside this directory under their original filenames.

        Resume: if a previous transfer was interrupted, only missing/incomplete
        assets are re-transferred.
        """
        staging = self._partial_root / game.id
        staging.mkdir(parents=True, exist_ok=True)

        log.info("Starting transfer for %r (%s) → %s", game.title, game.id, staging)

        try:
            for asset in game.assets:
                src = str(Path(game.source_root) / asset.relative_path)
                # Preserve the original filename verbatim.
                # Batocera's configgen matches per-game settings by filename
                # (e.g. snes["Some Game.sfc"].*); renaming here would silently
                # break any game-specific emulator/core overrides.
                dst = str(staging / asset.filename)

                log.debug("Transferring asset %s → %s", asset.relative_path, dst)
                self._provider.transfer_to(src, dst, on_progress)

            self._validate(game, staging)

            final = self._promote(game, staging)
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

    def staging_size(self, game_id: str) -> int:
        """Return the byte total of whatever is currently in the staging area."""
        staging = self._partial_root / game_id
        if not staging.exists():
            return 0
        return sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())

    def discard_staging(self, game_id: str) -> None:
        """Remove the staging directory for *game_id* (e.g. after a failed cancel)."""
        staging = self._partial_root / game_id
        if staging.exists():
            shutil.rmtree(staging)
            log.debug("Discarded staging for %s", game_id)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _validate(self, game: Game, staging: Path) -> None:
        """Check that staged files match expected sizes where known."""
        for asset in game.assets:
            staged = staging / asset.filename
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

    def _promote(self, game: Game, staging: Path) -> str:
        """Move the staging directory to the final cache location atomically."""
        final = self._cache_root / game.system / game.id
        final.parent.mkdir(parents=True, exist_ok=True)

        if final.exists():
            shutil.rmtree(final)

        shutil.move(str(staging), str(final))
        return str(final)
