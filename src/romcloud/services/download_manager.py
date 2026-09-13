"""Provider-neutral durable browser download queue and serial worker."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import TransferCancelledError
from romcloud.core.models.download import DownloadOrigin, DownloadState
from romcloud.infrastructure.logging import get_logger
from romcloud.infrastructure.repositories.download import (
    DownloadRepository,
    StagingRepository,
)
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.services.cache import CacheService

log = get_logger("download_manager")

HISTORY_LIMIT = 200
INTERRUPTED_RETENTION = timedelta(days=30)
FAILED_RETENTION = timedelta(days=30)
CANCELLED_RETENTION = timedelta(days=7)
GC_INTERVAL_SECONDS = 6 * 60 * 60


class DownloadManagerService:
    """Owns persistent queue operations and one resident background worker."""

    def __init__(
        self,
        *,
        repository: DownloadRepository,
        staging_repository: StagingRepository,
        game_repo: GameRepository,
        cache: CacheService,
        cache_root: str,
    ) -> None:
        self._repo = repository
        self._staging = staging_repository
        self._games = game_repo
        self._cache = cache
        self._cache_root = Path(cache_root)
        self.instance_id = uuid.uuid4().hex
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._control_lock = threading.Lock()
        self._active_item_id: Optional[str] = None
        self._active_token: Optional[TransferCancellationToken] = None
        self._requested_stop: dict[str, str] = {}
        self._speed: dict[str, tuple[float, int, float]] = {}
        # Startup recovery is DB/filesystem-local only: no source access,
        # hashing, package scans, or automatic resume occurs here.
        self._repo.recover_interrupted()
        self._repo.prune_terminal(keep=HISTORY_LIMIT)
        self._last_gc = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._worker, name="romcloud-download-manager", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        with self._control_lock:
            if self._active_item_id and self._active_token:
                self._requested_stop[self._active_item_id] = "interrupted"
                self._active_token.cancel()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def enqueue(
        self,
        game_ids: Iterable[str],
        *,
        origin: DownloadOrigin = DownloadOrigin.MANUAL,
        batch_id: Optional[str] = None,
    ) -> dict[str, object]:
        items = []
        created = 0
        for game_id in tuple(dict.fromkeys(str(item) for item in game_ids if item)):
            game = self._games.get(game_id)
            if game is None or not game.is_eligible:
                raise ValueError(f"Game not found in eligible catalog: {game_id}")
            self._cache.prepare_download_membership(game.id)
            item, was_created = self._repo.enqueue(
                game_id=game.id,
                game_title=game.title,
                system=game.system,
                origin=origin,
                batch_id=batch_id,
            )
            items.append(item.as_dict())
            created += int(was_created)
        if not items:
            raise ValueError("Select at least one game to download.")
        self._wake.set()
        return {"items": items, "created": created, "count": len(items)}

    def status(self) -> dict[str, object]:
        items = []
        now = time.monotonic()
        for item in self._repo.list(terminal_limit=HISTORY_LIMIT):
            payload = item.as_dict()
            sample = self._speed.get(item.id)
            speed = sample[2] if sample and now - sample[0] < 10 else 0.0
            payload["speed_bytes_per_second"] = speed
            remaining = max(0, item.bytes_total - item.bytes_present)
            payload["eta_seconds"] = remaining / speed if speed > 0 else None
            payload["has_partial"] = item.bytes_present > 0
            payload.update(
                self._staging.stats_for_game(item.game_id)
                if item.game_id is not None else {
                    "total_files": 0, "retained_files": 0,
                    "interrupted_files": 0, "remaining_files": 0,
                }
            )
            items.append(payload)
        active = [item for item in items if item["state"] in {"running", "verifying"}]
        grouped = {
            state: [item for item in items if item["state"] == state]
            for state in (
                "queued", "paused", "interrupted", "failed", "cancelled", "complete"
            )
        }
        return {
            "items": items,
            "active": active[0] if active else None,
            **grouped,
            "retained_partial_bytes": self._staging.bytes_present(),
            "history_limit": HISTORY_LIMIT,
        }

    def pause(self, item_id: str) -> None:
        item = self._require(item_id)
        if item.state not in {DownloadState.RUNNING, DownloadState.VERIFYING}:
            raise ValueError("Only a running or verifying download can be paused.")
        self._request_active_stop(item_id, "paused")

    def cancel(self, item_id: str) -> None:
        item = self._require(item_id)
        if item.state is DownloadState.QUEUED:
            if not self._repo.transition(
                item_id, from_states=[DownloadState.QUEUED], to_state=DownloadState.CANCELLED
            ):
                raise ValueError("Download state changed; refresh and try again.")
            return
        if item.state not in {DownloadState.RUNNING, DownloadState.VERIFYING}:
            raise ValueError("Only a queued or active download can be cancelled.")
        self._request_active_stop(item_id, "cancelled")

    def resume(self, item_id: str) -> None:
        if not self._repo.transition(
            item_id,
            from_states=[DownloadState.PAUSED, DownloadState.INTERRUPTED, DownloadState.CANCELLED],
            to_state=DownloadState.QUEUED,
        ):
            raise ValueError("Only paused, interrupted, or cancelled downloads can resume.")
        self._wake.set()

    def retry(self, item_id: str) -> None:
        if not self._repo.transition(
            item_id, from_states=[DownloadState.FAILED], to_state=DownloadState.QUEUED
        ):
            raise ValueError("Only a failed download can be retried.")
        self._wake.set()

    def remove_queued(self, item_id: str) -> None:
        if not self._repo.delete_queued(item_id):
            raise ValueError("Only a queued download can be removed.")

    def discard_partial(self, item_id: str) -> None:
        item = self._require(item_id)
        if item.state not in {
            DownloadState.PAUSED, DownloadState.INTERRUPTED,
            DownloadState.FAILED, DownloadState.CANCELLED,
        }:
            raise ValueError("Partial data cannot be discarded in this state.")
        if item.game_id is not None:
            self._cache.discard_staging(item.game_id)
        self._repo.update_progress(item.id, 0, item.bytes_total)

    def cancel_all(self) -> int:
        count = self._repo.queued_cancel_all()
        with self._control_lock:
            if self._active_item_id and self._active_token:
                self._requested_stop[self._active_item_id] = "cancelled"
                self._active_token.cancel()
                count += 1
        return count

    def retry_all_failed(self) -> int:
        count = self._repo.retry_all_failed()
        if count:
            self._wake.set()
        return count

    def cleanup_stale_partials(self) -> int:
        cleaned = 0
        now = datetime.now(timezone.utc)
        retention = {
            DownloadState.INTERRUPTED: INTERRUPTED_RETENTION,
            DownloadState.FAILED: FAILED_RETENTION,
            DownloadState.CANCELLED: CANCELLED_RETENTION,
        }
        for item in self._repo.list(terminal_limit=10000):
            maximum = retention.get(item.state)
            if maximum is None or item.game_id is None or now - item.updated_at < maximum:
                continue
            try:
                self._cache.discard_staging(item.game_id)
            except RuntimeError:
                continue
            self._repo.update_progress(item.id, 0, item.bytes_total)
            cleaned += 1
        self._last_gc = time.monotonic()
        return cleaned

    def _request_active_stop(self, item_id: str, action: str) -> None:
        with self._control_lock:
            if self._active_item_id != item_id or self._active_token is None:
                raise ValueError("Download is no longer active.")
            self._requested_stop[item_id] = action
            self._active_token.cancel()

    def _require(self, item_id: str):  # noqa: ANN201
        item = self._repo.get(item_id)
        if item is None:
            raise ValueError("Download item not found.")
        return item

    def _worker(self) -> None:
        while not self._stopping.is_set():
            if time.monotonic() - self._last_gc >= GC_INTERVAL_SECONDS:
                try:
                    self.cleanup_stale_partials()
                except Exception:
                    log.exception("Stale partial cleanup failed")
            item = self._repo.next_queued()
            if item is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if item.game_id is None:
                self._repo.transition(
                    item.id, from_states=[DownloadState.QUEUED],
                    to_state=DownloadState.FAILED, error_code="game_missing",
                    error_message="The catalog game no longer exists.",
                )
                continue
            lease = self._cache.try_asset_locks(item.game_id)
            if lease is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            token = TransferCancellationToken()
            retained = self._cache.retained_staging_size(item.game_id)
            initial = DownloadState.VERIFYING if retained else DownloadState.RUNNING
            if not self._repo.transition(
                item.id, from_states=[DownloadState.QUEUED], to_state=initial,
                worker_instance_id=self.instance_id,
            ):
                lease.release()
                continue
            with self._control_lock:
                self._active_item_id = item.id
                self._active_token = token
            started = time.monotonic()
            previous_time = started
            previous_bytes = retained
            persisted_time = started
            persisted_bytes = retained
            latest_total = item.bytes_total

            def progress(done: int, total: int) -> None:
                nonlocal previous_time, previous_bytes, persisted_time, persisted_bytes, latest_total
                latest_total = total
                current = self._repo.get(item.id)
                if current is not None and current.state is DownloadState.VERIFYING:
                    self._repo.transition(
                        item.id, from_states=[DownloadState.VERIFYING],
                        to_state=DownloadState.RUNNING,
                        worker_instance_id=self.instance_id,
                    )
                now_mono = time.monotonic()
                if (
                    now_mono - persisted_time >= 1.0
                    or done - persisted_bytes >= 8 * 1024 * 1024
                    or (total > 0 and done >= total)
                ):
                    self._repo.update_progress(item.id, done, total)
                    persisted_time, persisted_bytes = now_mono, done
                elapsed = now_mono - previous_time
                if elapsed >= 0.25:
                    self._speed[item.id] = (
                        now_mono, done, max(0.0, (done - previous_bytes) / elapsed)
                    )
                    previous_time, previous_bytes = now_mono, done

            try:
                self._cache.cache_game(
                    item.game_id,
                    on_progress=progress,
                    cancellation=token,
                    owner_kind="manager",
                    owner_instance_id=self.instance_id,
                    download_item_id=item.id,
                    _asset_lock=lease,
                )
            except TransferCancelledError:
                current_progress = self._cache.retained_staging_size(item.game_id)
                current_item = self._repo.get(item.id)
                self._repo.update_progress(
                    item.id,
                    current_progress,
                    latest_total or (current_item.bytes_total if current_item is not None else 0),
                )
                with self._control_lock:
                    requested = self._requested_stop.pop(item.id, "interrupted")
                target = DownloadState(requested)
                current = self._repo.get(item.id)
                if current is not None:
                    self._repo.transition(
                        item.id, from_states=[current.state], to_state=target
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception("Download failed: %s", item.id)
                current = self._repo.get(item.id)
                if current is not None:
                    self._repo.transition(
                        item.id, from_states=[current.state], to_state=DownloadState.FAILED,
                        error_code=type(exc).__name__, error_message=str(exc),
                    )
            else:
                current = self._repo.get(item.id)
                if current is not None:
                    self._repo.transition(
                        item.id, from_states=[current.state], to_state=DownloadState.COMPLETE
                    )
            finally:
                lease.release()
                with self._control_lock:
                    self._active_item_id = None
                    self._active_token = None
                self._repo.prune_terminal(keep=HISTORY_LIMIT)
