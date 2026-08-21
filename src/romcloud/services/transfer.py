"""Transfer service — staged, resumable file transfers.

Transfer lifecycle
------------------

1. For each asset, stage it at
   ``{cache_root}/.partial/{system}/{asset path relative to system root}``.
2. Expand directory assets through the provider's recursive tree API and
   create their complete staging layout.
3. Transfer each required file into staging (resumable per-file).
4. Validate exact directory membership and every known file size.
5. Atomically promote each staged asset to its final cache path:
   ``{cache_root}/{system}/{asset path relative to system root}``.
6. Return the final cache path of the game's primary asset.

The final (and staging) layout mirrors the source's relative path under the
system, so the original basename is always preserved and identical filenames
in different systems or subdirectories never collide — see
:mod:`romcloud.core.cache_paths`.

A power-loss or interruption leaves the staging path(s) in place.
The next call picks up where it left off (resume logic is in the provider).

Multi-asset games (e.g. .cue + .bin tracks)
--------------------------------------------
An asset already present and correctly sized at its *final* cache path is
never re-staged/re-transferred — only genuinely missing or incomplete
assets are fetched. This means a repair of a partially-cached logical game
(e.g. the .cue exists but one .bin track was deleted) only downloads what's
actually missing, and ``on_progress`` reports bytes/percentage aggregated
across *all* of the game's assets (not reset to 0 for each new asset) so a
UI session represents the whole logical-game transfer, not one track at a
time.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from romcloud.core.cache_paths import resolve_cache_path
from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import (
    TransferCancelledError,
    TransferError,
    TransferValidationError,
)
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.storage import RemoteEntry, StorageProvider
from romcloud.infrastructure.logging import get_logger

log = get_logger("transfer")


@dataclass(frozen=True)
class _PlannedFile:
    relative_path: str
    package_relative_path: str
    size_bytes: Optional[int]


@dataclass(frozen=True)
class _AssetPlan:
    asset: GameAsset
    is_directory: bool
    directories: tuple[str, ...] = ()
    files: tuple[_PlannedFile, ...] = ()

    @property
    def total_size_bytes(self) -> int:
        return sum(file.size_bytes or 0 for file in self.files)


class TransferService:
    """Orchestrates staged, resumable transfers for a single game."""

    def __init__(
        self,
        provider: StorageProvider,
        cache_root: str,
        source_root: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._cache_root = Path(cache_root)
        self._partial_root = self._cache_root / ".partial"
        # The currently configured source root, if known — takes priority
        # over a game's persisted `source_root` (see `_asset_source` below),
        # since that is catalog data written when the game was last scanned
        # and does not track later source-path reconfiguration/migration.
        self._source_root = source_root

    @property
    def provider(self) -> StorageProvider:
        return self._provider

    @property
    def source_root(self) -> Optional[str]:
        return self._source_root

    # ── public API ────────────────────────────────────────────────────────────

    def transfer(
        self,
        game: Game,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancellation: Optional[TransferCancellationToken] = None,
    ) -> str:
        """Transfer one logical game within the provider's bounded session."""
        with self._provider.transfer_session():
            return self._transfer_in_session(game, on_progress, cancellation)

    def _transfer_in_session(
        self,
        game: Game,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancellation: Optional[TransferCancellationToken] = None,
    ) -> str:
        """Transfer all game assets and return the final cache path.

        The returned path is the primary asset's final location:
        ``{cache_root}/{system}/{relative path within that system}``.

        Resume/repair: an asset already complete at its final cache path is
        skipped entirely; only missing/incomplete assets are (re-)staged
        and transferred. ``on_progress`` receives cumulative
        ``(bytes_done, bytes_total)`` across the whole game, not per asset.
        """
        if not game.assets:
            raise TransferError(f"Game {game.id!r} has no assets to transfer")

        log.info("Starting transfer for %r (%s)", game.title, game.id)

        plans: list[_AssetPlan] = []
        grand_total = game.total_size_bytes or 0
        cumulative_done = 0

        try:
            _check_cancelled(cancellation)
            for asset in game.assets:
                _check_cancelled(cancellation)
                plan = self._plan_asset(game, asset, cancellation)
                plans.append(plan)
                if game.total_size_bytes is None:
                    grand_total += plan.total_size_bytes
                final = self._final_path(game.system, asset.relative_path)
                final_size = self._validated_size(final, plan)

                if final_size is not None:
                    # Already fully cached (from a previous run, or another
                    # asset of this same game) — repair only what's missing.
                    cumulative_done += final_size
                    _check_cancelled(cancellation)
                    if on_progress:
                        on_progress(cumulative_done, grand_total or cumulative_done)
                    _check_cancelled(cancellation)
                    continue

                # Preserve the original filename verbatim, and mirror its
                # relative location under the system — never flatten to a
                # bare filename. Batocera's configgen matches per-game
                # settings by filename (e.g. snes["Some Game.sfc"].*);
                # renaming here would silently break any game-specific
                # emulator/core overrides.
                dst = self._staging_path(game.system, asset.relative_path)
                dst.parent.mkdir(parents=True, exist_ok=True)

                if plan.is_directory:
                    cumulative_done = self._transfer_directory(
                        game,
                        plan,
                        dst,
                        cumulative_done,
                        grand_total,
                        on_progress,
                        cancellation,
                    )
                else:
                    cumulative_done = self._transfer_file(
                        game,
                        plan.files[0],
                        dst,
                        cumulative_done,
                        grand_total,
                        on_progress,
                        cancellation,
                    )

            _check_cancelled(cancellation)
            self._validate(game, plans)
            _check_cancelled(cancellation)

            final = self._promote(game)
            _check_cancelled(cancellation)
            log.info("Transfer complete for %r → %s", game.title, final)
            return final

        except TransferCancelledError:
            log.info(
                "Transfer cancelled for %r (%s); staging preserved for retry",
                game.title,
                game.id,
            )
            raise
        except Exception as exc:
            log.warning(
                "Transfer failed for %r (%s): %s — staging preserved for resume",
                game.title,
                game.id,
                exc,
            )
            raise

    def _asset_source_root(self, game: Game) -> str:
        """The root to read *game*'s assets from: the live configured root
        when known, else the game's own persisted (possibly historical)
        value — see the `source_root` constructor parameter."""
        return self._source_root if self._source_root is not None else game.source_root

    def staging_size(self, game: Game) -> int:
        """Return the byte total of whatever is currently staged for *game*."""
        total = 0
        for asset in game.assets:
            staged = self._staging_path(game.system, asset.relative_path)
            total += _existing_size(staged) or 0
        return total

    def estimate_size(self, game: Game) -> int:
        """Return the best available source-size estimate for *game*.

        Directory packages deliberately have no catalog-time size: recursively
        sizing every candidate would duplicate the discovery walk.  Resolve
        those unknowns only when the user actually requests a transfer, where
        the value is needed for quota enforcement.
        """
        return sum(self.estimate_asset_size(game, asset) for asset in game.assets)

    def estimate_asset_size(self, game: Game, asset: GameAsset) -> int:
        """Resolve one lazy asset size for deduplicated batch planning."""
        if asset.size_bytes is not None:
            return asset.size_bytes
        source = self._provider.resolve_path(
            self._asset_source_root(game), asset.relative_path
        )
        return self._provider.get_size(source) or 0

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

    def _plan_asset(
        self,
        game: Game,
        asset: GameAsset,
        cancellation: Optional[TransferCancellationToken],
    ) -> _AssetPlan:
        """Snapshot every required source path for one logical asset."""
        _check_cancelled(cancellation)
        entry = self._source_entry(game, asset.relative_path)
        if entry.is_symlink:
            raise TransferError(
                f"Refusing to transfer symlink asset: {asset.relative_path}"
            )
        if entry.is_directory:
            return self._plan_directory(game, asset, cancellation)
        return _AssetPlan(
            asset=asset,
            is_directory=False,
            files=(
                _PlannedFile(
                    relative_path=asset.relative_path,
                    package_relative_path="",
                    size_bytes=(
                        asset.size_bytes
                        if asset.size_bytes is not None
                        else entry.size_bytes
                    ),
                ),
            ),
        )

    def _source_entry(self, game: Game, relative_path: str) -> RemoteEntry:
        relative = PurePosixPath(str(relative_path).replace("\\", "/"))
        if len(relative.parts) < 2:
            raise TransferError(f"Invalid source asset path: {relative_path!r}")
        parent = PurePosixPath(*relative.parts[:-1]).as_posix()
        expected = relative.as_posix()
        for entry in self._provider.list_entries(self._asset_source_root(game), parent):
            if entry.relative_path.replace("\\", "/") == expected:
                return entry
        raise TransferError(f"Source does not exist: {relative_path}")

    def _plan_directory(
        self,
        game: Game,
        asset: GameAsset,
        cancellation: Optional[TransferCancellationToken],
    ) -> _AssetPlan:
        root = PurePosixPath(asset.relative_path.replace("\\", "/"))
        directories: list[str] = []
        files: list[_PlannedFile] = []
        source = self._provider.resolve_path(
            self._asset_source_root(game), asset.relative_path
        )

        for entry in self._provider.walk(source):
            _check_cancelled(cancellation)
            package_path = PurePosixPath(entry.relative_path.replace("\\", "/"))
            if (
                package_path.is_absolute()
                or not package_path.parts
                or any(part in ("", ".", "..") for part in package_path.parts)
                or entry.name != package_path.name
            ):
                raise TransferError(
                    "Provider returned an invalid directory-package path: "
                    f"{entry.relative_path!r}"
                )
            package_relative = package_path.as_posix()
            relative = PurePosixPath(root, package_path).as_posix()
            if entry.is_symlink:
                raise TransferError(
                    f"Refusing to transfer package symlink: {relative}"
                )
            if entry.is_directory:
                directories.append(package_relative)
            else:
                files.append(
                    _PlannedFile(
                        relative_path=relative,
                        package_relative_path=package_relative,
                        size_bytes=entry.size_bytes,
                    )
                )

        return _AssetPlan(
            asset=asset,
            is_directory=True,
            directories=tuple(sorted(directories)),
            files=tuple(sorted(files, key=lambda file: file.relative_path)),
        )

    def _transfer_directory(
        self,
        game: Game,
        plan: _AssetPlan,
        staging: Path,
        cumulative_done: int,
        grand_total: int,
        on_progress: Optional[Callable[[int, int], None]],
        cancellation: Optional[TransferCancellationToken],
    ) -> int:
        if staging.exists() and not staging.is_dir():
            staging.unlink()
        staging.mkdir(parents=True, exist_ok=True)
        expected_dirs = set(plan.directories)
        expected_files = {file.package_relative_path for file in plan.files}
        for candidate in sorted(
            staging.rglob("*"), key=lambda path: len(path.parts), reverse=True
        ):
            relative = candidate.relative_to(staging).as_posix()
            if candidate.is_symlink():
                candidate.unlink()
            elif candidate.is_file() and relative not in expected_files:
                candidate.unlink()
            elif candidate.is_dir() and relative not in expected_dirs:
                shutil.rmtree(candidate)
        for relative in plan.directories:
            (staging / Path(*PurePosixPath(relative).parts)).mkdir(
                parents=True, exist_ok=True
            )

        for file in plan.files:
            _check_cancelled(cancellation)
            destination = staging / Path(
                *PurePosixPath(file.package_relative_path).parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            existing = _existing_size(destination)
            if file.size_bytes is not None and existing == file.size_bytes:
                cumulative_done += existing
                if on_progress:
                    on_progress(cumulative_done, grand_total or cumulative_done)
                continue
            cumulative_done = self._transfer_file(
                game,
                file,
                destination,
                cumulative_done,
                grand_total,
                on_progress,
                cancellation,
            )
        return cumulative_done

    def _transfer_file(
        self,
        game: Game,
        file: _PlannedFile,
        destination: Path,
        cumulative_done: int,
        grand_total: int,
        on_progress: Optional[Callable[[int, int], None]],
        cancellation: Optional[TransferCancellationToken],
    ) -> int:
        source = self._provider.resolve_path(
            self._asset_source_root(game), file.relative_path
        )
        base_done = cumulative_done

        def _file_progress(done: int, total: int) -> None:
            _check_cancelled(cancellation)
            if on_progress:
                on_progress(base_done + done, grand_total or (base_done + total))
            _check_cancelled(cancellation)

        log.debug("Transferring asset %s to %s", file.relative_path, destination)
        self._provider.transfer_to(source, str(destination), _file_progress)
        _check_cancelled(cancellation)
        return base_done + (_existing_size(destination) or 0)

    def _validate(self, game: Game, plans: list[_AssetPlan]) -> None:
        """Check that every asset ended up complete — either already
        correct at its final path, or freshly staged with the expected size."""
        for plan in plans:
            asset = plan.asset
            final = self._final_path(game.system, asset.relative_path)
            if self._validated_size(final, plan) is not None:
                continue  # already promoted from a previous run

            staged = self._staging_path(game.system, asset.relative_path)
            if self._validated_size(staged, plan) is None:
                raise TransferValidationError(
                    f"Asset incomplete after transfer: {asset.filename}"
                )

    @staticmethod
    def _validated_size(path: Path, plan: _AssetPlan) -> Optional[int]:
        if not plan.is_directory:
            if not path.is_file() or path.is_symlink():
                return None
            actual = path.stat().st_size
            expected = plan.files[0].size_bytes
            return actual if expected is None or actual == expected else None

        if not path.is_dir() or path.is_symlink():
            return None
        expected_dirs = set(plan.directories)
        expected_files = {
            file.package_relative_path: file.size_bytes for file in plan.files
        }
        actual_dirs: set[str] = set()
        actual_files: dict[str, int] = {}
        for candidate in path.rglob("*"):
            if candidate.is_symlink():
                return None
            relative = candidate.relative_to(path).as_posix()
            if candidate.is_dir():
                actual_dirs.add(relative)
            elif candidate.is_file():
                actual_files[relative] = candidate.stat().st_size
            else:
                return None
        if actual_dirs != expected_dirs or set(actual_files) != set(expected_files):
            return None
        if any(
            expected is not None and actual_files[relative] != expected
            for relative, expected in expected_files.items()
        ):
            return None
        return sum(actual_files.values())

    def _promote(self, game: Game) -> str:
        """Move each freshly-staged asset to its final cache location.

        An asset already complete at its final path (skipped during
        transfer) is left untouched.
        """
        final_primary: Optional[str] = None

        for asset in game.assets:
            staged = self._staging_path(game.system, asset.relative_path)
            final = self._final_path(game.system, asset.relative_path)

            if staged.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    if final.is_dir():
                        shutil.rmtree(final)
                    else:
                        final.unlink()
                shutil.move(str(staged), str(final))
            # else: asset was already complete at `final` — nothing to promote.

            if asset.is_primary or final_primary is None:
                final_primary = str(final)

        assert final_primary is not None  # game.assets is non-empty (checked above)
        return final_primary


def _existing_size(path: Path) -> Optional[int]:
    """Return the on-disk size of *path* (file or directory tree), or None
    if it does not exist."""
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return None


def _check_cancelled(cancellation: Optional[TransferCancellationToken]) -> None:
    if cancellation is not None:
        cancellation.raise_if_cancelled()
