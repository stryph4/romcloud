"""Short-lived browser handoffs and cookie sessions for the manager."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable


BOOTSTRAP_TTL_SECONDS = 60.0
SESSION_TTL_SECONDS = 12 * 60 * 60.0
MAX_BOOTSTRAPS = 32
MAX_SESSIONS = 64
MAX_LOCAL_EXITS = 32


def is_loopback(address: str) -> bool:
    return address == "::1" or address.startswith("127.")


@dataclass(frozen=True)
class BrowserSession:
    token: str
    local: bool
    launch_id: str | None


class BrowserAuthRegistry:
    """Bounded, process-local auth state; nothing here survives a restart."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._bootstraps: dict[str, tuple[float, bool, str | None]] = {}
        self._sessions: dict[str, tuple[float, bool, str | None]] = {}
        self._local_exits: set[str] = set()

    def issue(self, *, local: bool) -> tuple[str, str | None]:
        with self._lock:
            self._prune()
            while len(self._bootstraps) >= MAX_BOOTSTRAPS:
                self._bootstraps.pop(next(iter(self._bootstraps)))
            code = secrets.token_urlsafe(24)
            launch_id = secrets.token_urlsafe(18) if local else None
            self._bootstraps[code] = (
                self._clock() + BOOTSTRAP_TTL_SECONDS,
                local,
                launch_id,
            )
            return code, launch_id

    def exchange(self, code: str, *, peer: str) -> BrowserSession | None:
        with self._lock:
            self._prune()
            record = self._bootstraps.pop(code, None)
            if record is None:
                return None
            expires, local, launch_id = record
            if expires <= self._clock() or (local and not is_loopback(peer)):
                return None
            while len(self._sessions) >= MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            token = secrets.token_urlsafe(24)
            self._sessions[token] = (
                self._clock() + SESSION_TTL_SECONDS,
                local,
                launch_id,
            )
            return BrowserSession(token=token, local=local, launch_id=launch_id)

    def authenticate(self, token: str) -> BrowserSession | None:
        with self._lock:
            self._prune()
            record = self._sessions.get(token)
            if record is None:
                return None
            _, local, launch_id = record
            return BrowserSession(token=token, local=local, launch_id=launch_id)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def request_local_exit(self, session: BrowserSession, *, peer: str) -> bool:
        if not session.local or not session.launch_id or not is_loopback(peer):
            return False
        with self._lock:
            while len(self._local_exits) >= MAX_LOCAL_EXITS:
                self._local_exits.pop()
            self._local_exits.add(session.launch_id)
        return True

    def local_exit_requested(self, launch_id: str) -> bool:
        with self._lock:
            requested = launch_id in self._local_exits
            self._local_exits.discard(launch_id)
            return requested

    def _prune(self) -> None:
        now = self._clock()
        self._bootstraps = {
            key: value for key, value in self._bootstraps.items() if value[0] > now
        }
        self._sessions = {
            key: value for key, value in self._sessions.items() if value[0] > now
        }
