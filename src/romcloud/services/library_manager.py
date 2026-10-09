"""Application service behind the browser Library/Cache Manager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable

from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.models.cache import CacheStatus
from romcloud.core.models.game import Game
from romcloud.infrastructure.repositories.cache import CacheRepository
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.library_browser import LibraryBrowserRepository
from romcloud.services.cache import CacheService
from romcloud.core.models.download import DownloadOrigin


class LibraryManagerService:
    """Narrow browser facade; cache/dependency decisions remain in CacheService."""

    def __init__(
        self,
        browser_repo: LibraryBrowserRepository,
        game_repo: GameRepository,
        cache_repo: CacheRepository,
        cache: CacheService,
        policy_loader: Callable[[], CapabilityPolicy],
        source_reachable: Callable[[], bool],
        downloads=None,  # noqa: ANN001
    ) -> None:
        self._browser_repo = browser_repo
        self._game_repo = game_repo
        self._cache_repo = cache_repo
        self._cache = cache
        self._policy_loader = policy_loader
        self._source_reachable = source_reachable
        self._downloads = downloads

    def status(self) -> dict[str, object]:
        policy = self._policy_loader()
        reachable = False if policy.offline else bool(self._source_reachable())
        return {
            "mode": policy.effective_mode.value,
            "offline": policy.offline,
            "source_reachable": reachable,
            "full_library_available": not policy.offline and reachable,
            "can_download": policy.allows(Capability.GAME_DOWNLOAD) and reachable,
        }

    def systems(self) -> dict[str, object]:
        offline = self._policy_loader().offline
        return {"offline": offline, "systems": self._browser_repo.systems(device_only=offline)}

    def browse(self, **options: object) -> dict[str, object]:
        policy = self._policy_loader()
        requested_scope = str(options.get("scope", "full"))
        scope = "device" if policy.offline else requested_scope
        page = self._browser_repo.browse(
            system=_optional_text(options.get("system")),
            scope=scope if scope in {"full", "device"} else "full",
            search=str(options.get("search", "")),
            state=str(options.get("state", "all")),
            sort=str(options.get("sort", "title")),
            page=int(options.get("page", 1)),
            page_size=int(options.get("page_size", 50)),
        )
        cached_ids = [row.game_id for row in page.rows if row.entry is not None]
        memberships = self._cache_repo.list_members_for(cached_ids)
        games: list[dict[str, object]] = []
        for row in page.rows:
            entry = row.entry
            valid = False
            if entry is not None and entry.status is CacheStatus.COMPLETE:
                game = Game(
                    id=row.game_id,
                    system=row.system,
                    title=row.title,
                    source_provider="",
                    source_root="",
                    assets=[],
                    added_at=datetime.now(timezone.utc),
                )
                valid = self._cache.is_valid_cached_entry(
                    entry,
                    game,
                    members=memberships.get(row.game_id, []),
                    membership_resolved=row.membership_resolved,
                )
            state = _state(entry, valid)
            games.append(
                {
                    "id": row.game_id,
                    "system": row.system,
                    "title": row.title,
                    "filename": row.filename,
                    "source_size_bytes": row.source_size_bytes,
                    "local_size_bytes": entry.size_bytes if entry else 0,
                    "state": state,
                    "cache_status": entry.status.value if entry else None,
                    "pinned": bool(entry and entry.is_pinned),
                    # A membership snapshot is ownership metadata, not proof
                    # of playable bytes. Only the canonical validity check may
                    # advertise a local copy to the browser.
                    "has_local_copy": valid,
                    "offline_ready": valid,
                }
            )
        return {
            "games": games,
            "total": page.total,
            "page": page.page,
            "page_size": page.page_size,
            "pages": (page.total + page.page_size - 1) // page.page_size,
            "scope": scope,
            "offline_limited": policy.offline,
        }

    def action(self, action: str, game_ids: Iterable[str]) -> dict[str, object]:
        ids = tuple(dict.fromkeys(str(value) for value in game_ids if value))
        if not ids or len(ids) > 500:
            raise ValueError("Select between 1 and 500 games.")
        if action in {"cache", "download_selected"}:
            self._require_download_available("Downloading a game")
            if self._downloads is None:
                if action == "download_selected":
                    raise RuntimeError("Download Manager is unavailable.")
                for game_id in ids:
                    self._cache.cache_game(game_id)
                completed = list(ids)
            else:
                origin = (
                    DownloadOrigin.SELECTED
                    if action == "download_selected" else DownloadOrigin.MANUAL
                )
                result = self._downloads.enqueue(ids, origin=origin)
                return {"action": action, "queued": result["items"], "count": result["count"]}
        elif action == "pin":
            for game_id in ids:
                self._cache.pin(game_id)
            completed = list(ids)
        elif action == "unpin":
            for game_id in ids:
                self._cache.unpin(game_id)
            completed = list(ids)
        elif action == "remove":
            for game_id in ids:
                self._cache.remove(game_id)
            completed = list(ids)
        else:
            raise ValueError(f"Unsupported action: {action}")
        return {"action": action, "completed": completed, "count": len(completed)}

    def enqueue_downloads(
        self,
        game_ids: Iterable[str],
        *,
        origin: DownloadOrigin = DownloadOrigin.MANUAL,
    ) -> dict[str, object]:
        """Queue user-requested downloads behind the shared policy boundary."""
        ids = tuple(dict.fromkeys(str(value) for value in game_ids if value))
        if not ids or len(ids) > 500:
            raise ValueError("Select between 1 and 500 games.")
        self._require_download_available("Downloading a game")
        if self._downloads is None:
            raise RuntimeError("Download Manager is unavailable.")
        return self._downloads.enqueue(ids, origin=origin)

    def _require_download_available(self, operation: str) -> None:
        self._policy_loader().require(Capability.GAME_DOWNLOAD, operation)
        if not self._source_reachable():
            raise RuntimeError("The ROM source is unavailable; downloads cannot start.")

    def pinned_preflight(self) -> dict[str, object]:
        self._policy_loader().require(Capability.GAME_DOWNLOAD, "Download Pinned preflight")
        if not self._source_reachable():
            raise RuntimeError("The ROM source is unavailable; pinned downloads cannot start.")
        return self._cache.preflight_pinned().as_dict()

    def download_pinned(self, **callbacks: object) -> list[str]:
        self._policy_loader().require(Capability.GAME_DOWNLOAD, "Download Pinned")
        if not self._source_reachable():
            raise RuntimeError("The ROM source is unavailable; pinned downloads cannot start.")
        return self._cache.download_pinned(**callbacks)  # type: ignore[arg-type]

    def enqueue_pinned(self) -> dict[str, object]:
        self._policy_loader().require(Capability.GAME_DOWNLOAD, "Download Pinned")
        if not self._source_reachable():
            raise RuntimeError("The ROM source is unavailable; pinned downloads cannot start.")
        plan = self._cache.preflight_pinned()
        if not plan.allowed:
            raise ValueError(" ".join(plan.reasons))
        if not plan.game_ids:
            return {"items": [], "created": 0, "count": 0, "batch_id": None}
        if self._downloads is None:
            raise RuntimeError("Download Manager is unavailable.")
        import uuid

        batch_id = uuid.uuid4().hex
        result = self._downloads.enqueue(
            plan.game_ids, origin=DownloadOrigin.PINNED, batch_id=batch_id
        )
        return {**result, "batch_id": batch_id}

    @property
    def downloads(self):  # noqa: ANN201
        if self._downloads is None:
            raise RuntimeError("Download Manager is unavailable.")
        return self._downloads


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _state(entry, valid: bool) -> str:  # type: ignore[no-untyped-def]
    if entry is None:
        return "remote_only"
    if entry.status is CacheStatus.TRANSFERRING:
        return "transferring"
    if valid:
        return "pinned" if entry.is_pinned else "cached"
    return "incomplete"
