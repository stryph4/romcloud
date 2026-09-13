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

import hashlib
import json
import os
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
from romcloud.infrastructure.repositories.download import StagingRepository

log = get_logger("transfer")


@dataclass(frozen=True)
class _PlannedFile:
    relative_path: str
    package_relative_path: str
    size_bytes: Optional[int]
    object_id: Optional[str] = None
    revision: Optional[str] = None
    checksum: Optional[str] = None
    modified_epoch: Optional[float] = None


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
        staging_repository: Optional[StagingRepository] = None,
    ) -> None:
        self._provider = provider
        self._cache_root = Path(cache_root)
        self._partial_root = self._cache_root / ".partial"
        # The currently configured source root, if known — takes priority
        # over a game's persisted `source_root` (see `_asset_source` below),
        # since that is catalog data written when the game was last scanned
        # and does not track later source-path reconfiguration/migration.
        self._source_root = source_root
        self._staging_repo = staging_repository

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
                self._persist_plan(game, plan)
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
            if not staged.is_dir():
                total += _existing_size(staged.with_name(staged.name + ".part")) or 0
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

    def discard_staging(
        self, game: Game, *, preserve_relative_paths: Optional[set[str]] = None
    ) -> None:
        """Remove any staged (partial) data for *game* (e.g. after a failed cancel)."""
        for asset in game.assets:
            if asset.relative_path in (preserve_relative_paths or set()):
                continue
            staged = self._staging_path(game.system, asset.relative_path)
            if staged.is_dir():
                shutil.rmtree(staged)
                log.debug("Discarded staging dir for %s", staged)
            elif staged.exists():
                staged.unlink()
                log.debug("Discarded staging file for %s", staged)
            part = staged.with_name(staged.name + ".part")
            if part.exists() and not part.is_symlink():
                part.unlink()
            if self._staging_repo is not None:
                self._staging_repo.delete_asset(asset.relative_path)

    def finalize_staging_records(self, game: Game) -> None:
        """Drop recovery metadata only after CacheService commits COMPLETE."""
        if self._staging_repo is not None:
            for asset in game.assets:
                self._staging_repo.delete_asset(asset.relative_path)

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
                    object_id=entry.object_id,
                    revision=entry.revision,
                    checksum=entry.checksum,
                    modified_epoch=entry.modified_epoch,
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
                        object_id=entry.object_id,
                        revision=entry.revision,
                        checksum=entry.checksum,
                        modified_epoch=entry.modified_epoch,
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
        expected_parts = {relative + ".part" for relative in expected_files}
        for candidate in sorted(
            staging.rglob("*"), key=lambda path: len(path.parts), reverse=True
        ):
            relative = candidate.relative_to(staging).as_posix()
            if candidate.is_symlink():
                candidate.unlink()
            elif candidate.is_file() and relative not in expected_files | expected_parts:
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
        asset_path, member_path = self._record_keys(game, file)
        record = (
            self._staging_repo.get_file(asset_path, member_path)
            if self._staging_repo is not None
            else None
        )
        part = destination.with_name(destination.name + ".part")

        # A clean staging name is reusable only after content equivalence is
        # established.  Size equality alone is deliberately insufficient.
        if destination.is_file() and not destination.is_symlink():
            clean_size = destination.stat().st_size
            if (file.size_bytes is None or clean_size == file.size_bytes) and self._source_matches(
                source, destination, clean_size, cancellation
            ):
                digest = _hash_prefix(destination, clean_size)
                if self._staging_repo is not None:
                    self._staging_repo.complete(asset_path, member_path, clean_size, digest)
                if on_progress:
                    on_progress(base_done + clean_size, grand_total or base_done + clean_size)
                return base_done + clean_size
            destination.unlink()

        checkpoint = 0
        if part.is_file() and not part.is_symlink() and record is not None:
            actual = part.stat().st_size
            checkpoint = record.checkpoint_bytes
            if checkpoint > actual or checkpoint < 0 or not record.checkpoint_sha256:
                checkpoint = 0
            elif actual > checkpoint:
                with part.open("r+b") as handle:
                    handle.truncate(checkpoint)
                    handle.flush()
                    os.fsync(handle.fileno())
            if checkpoint and _hash_prefix(part, checkpoint) != record.checkpoint_sha256:
                checkpoint = 0
            if checkpoint and not self._source_matches(
                source, part, checkpoint, cancellation
            ):
                checkpoint = 0
        if checkpoint == 0:
            part.parent.mkdir(parents=True, exist_ok=True)
            with part.open("wb") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            if self._staging_repo is not None:
                self._staging_repo.checkpoint(asset_path, member_path, 0, hashlib.sha256().hexdigest(), state="transferring")

        if not callable(getattr(self._provider, "open_binary", None)):
            return self._transfer_without_resume(
                source, file, part, destination, asset_path, member_path,
                base_done, grand_total, on_progress, cancellation,
            )

        # Seed the running digest from the exact durable local prefix.  The
        # source prefix has already been independently hashed and compared.
        digest = hashlib.sha256()
        if checkpoint:
            with part.open("rb") as retained:
                for chunk in iter(lambda: retained.read(_CHUNK), b""):
                    digest.update(chunk)

        log.debug("Transferring asset %s to %s from byte %d", file.relative_path, part, checkpoint)
        bytes_done = checkpoint
        next_checkpoint = checkpoint + _CHECKPOINT_INTERVAL
        try:
            with self._provider.open_binary(source) as source_handle:  # type: ignore[attr-defined]
                source_handle.seek(checkpoint)
                with part.open("ab") as output:
                    while True:
                        _check_cancelled(cancellation)
                        chunk = source_handle.read(_CHUNK)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        bytes_done += len(chunk)
                        if bytes_done >= next_checkpoint:
                            _durable_flush(output)
                            if self._staging_repo is not None:
                                self._staging_repo.checkpoint(
                                    asset_path, member_path, bytes_done,
                                    digest.hexdigest(), state="transferring",
                                )
                            next_checkpoint = bytes_done + _CHECKPOINT_INTERVAL
                        if on_progress:
                            on_progress(
                                base_done + bytes_done,
                                grand_total or base_done + (file.size_bytes or bytes_done),
                            )
                        _check_cancelled(cancellation)
                    _durable_flush(output)
        except TransferCancelledError:
            if part.exists():
                size = part.stat().st_size
                with part.open("r+b") as checkpoint_file:
                    _durable_flush(checkpoint_file)
                digest_value = (
                    digest.hexdigest()
                    if size == bytes_done
                    else _hash_prefix(part, size)
                )
                if self._staging_repo is not None:
                    self._staging_repo.checkpoint(
                        asset_path, member_path, size, digest_value, state="partial"
                    )
            raise

        if file.size_bytes is not None and bytes_done != file.size_bytes:
            if self._staging_repo is not None:
                self._staging_repo.checkpoint(
                    asset_path, member_path, bytes_done, digest.hexdigest(), state="partial"
                )
            raise TransferValidationError(
                f"Asset has {bytes_done} bytes but expected {file.size_bytes}: {file.relative_path}"
            )
        os.replace(part, destination)
        _fsync_directory(destination.parent)
        if self._staging_repo is not None:
            self._staging_repo.complete(
                asset_path, member_path, bytes_done, digest.hexdigest()
            )
        _check_cancelled(cancellation)
        return base_done + bytes_done

    def _transfer_without_resume(
        self,
        source: str,
        file: _PlannedFile,
        part: Path,
        destination: Path,
        asset_path: str,
        member_path: str,
        base_done: int,
        grand_total: int,
        on_progress: Optional[Callable[[int, int], None]],
        cancellation: Optional[TransferCancellationToken],
    ) -> int:
        """Safe fallback for legacy providers: retain partials, never append."""
        part.unlink(missing_ok=True)

        def progress(done: int, total: int) -> None:
            _check_cancelled(cancellation)
            if on_progress:
                on_progress(base_done + done, grand_total or base_done + total)
            _check_cancelled(cancellation)

        try:
            self._provider.transfer_to(source, str(part), progress)
        except TransferCancelledError:
            if part.exists() and self._staging_repo is not None:
                size = part.stat().st_size
                self._staging_repo.checkpoint(
                    asset_path, member_path, size, _hash_prefix(part, size), state="partial"
                )
            raise
        size = part.stat().st_size
        digest = _hash_prefix(part, size)
        if file.size_bytes is not None and size != file.size_bytes:
            if self._staging_repo is not None:
                self._staging_repo.checkpoint(
                    asset_path, member_path, size, digest, state="partial"
                )
            raise TransferValidationError(
                f"Asset has {size} bytes but expected {file.size_bytes}: {file.relative_path}"
            )
        with part.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(part, destination)
        _fsync_directory(destination.parent)
        if self._staging_repo is not None:
            self._staging_repo.complete(asset_path, member_path, size, digest)
        return base_done + size

    def _source_matches(
        self,
        source: str,
        retained: Path,
        length: int,
        cancellation: Optional[TransferCancellationToken],
    ) -> bool:
        if length == 0:
            return True
        local_digest = _hash_prefix(retained, length)
        source_digest = hashlib.sha256()
        remaining = length
        try:
            with self._provider.open_binary(source) as source_handle:  # type: ignore[attr-defined]
                while remaining:
                    _check_cancelled(cancellation)
                    chunk = source_handle.read(min(_CHUNK, remaining))
                    if not chunk:
                        return False
                    source_digest.update(chunk)
                    remaining -= len(chunk)
        except (AttributeError, OSError):
            return False
        return source_digest.hexdigest() == local_digest

    @staticmethod
    def _record_keys(game: Game, file: _PlannedFile) -> tuple[str, str]:
        for asset in game.assets:
            prefix = asset.relative_path.rstrip("/")
            if file.relative_path == prefix:
                return prefix, ""
            if file.relative_path.startswith(prefix + "/"):
                return prefix, file.relative_path[len(prefix) + 1 :]
        raise TransferError(f"Staged file is outside the game asset closure: {file.relative_path}")

    def _persist_plan(self, game: Game, plan: _AssetPlan) -> None:
        if self._staging_repo is None:
            return
        manifest_payload = [
            {
                "path": item.package_relative_path,
                "size": item.size_bytes,
                "object_id": item.object_id,
                "revision": item.revision,
                "checksum": item.checksum,
            }
            for item in plan.files
        ]
        manifest_payload.extend(
            {"path": directory, "kind": "directory"}
            for directory in plan.directories
        )
        manifest = hashlib.sha256(
            json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._staging_repo.replace_plan(
            relative_path=plan.asset.relative_path,
            system=game.system,
            asset_kind="directory" if plan.is_directory else "file",
            source_provider=self._provider.provider_id,
            source_root=self._asset_source_root(game),
            expected_size=plan.total_size_bytes,
            source_manifest_sha256=manifest,
            files=[
                {
                    "member_relative_path": item.package_relative_path,
                    "expected_size": item.size_bytes,
                    "source_object_id": item.object_id,
                    "source_revision": item.revision,
                    "source_checksum": item.checksum,
                    "source_modified_epoch": item.modified_epoch,
                }
                for item in plan.files
            ],
        )

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


_CHUNK = 1024 * 1024
_CHECKPOINT_INTERVAL = 8 * 1024 * 1024


def _hash_prefix(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _durable_flush(handle) -> None:  # type: ignore[no-untyped-def]
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some mounted filesystems do not support directory fsync. File data
        # was still flushed before rename; recovery treats the DB as advisory.
        pass
