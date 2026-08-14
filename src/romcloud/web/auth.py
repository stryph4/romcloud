"""Lightweight LAN pairing and browser-session authentication."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PAIRING_TTL_SECONDS = 120.0
SESSION_TTL_SECONDS = 12 * 60 * 60.0
REMEMBER_90_DAYS_SECONDS = 90 * 24 * 60 * 60
PAIRING_ATTEMPT_WINDOW_SECONDS = 60.0
MAX_PAIRING_ATTEMPTS = 5
MAX_PAIRINGS = 8
MAX_SESSIONS = 128
MAX_LOCAL_EXITS = 32
PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
TRUST_MODES = frozenset({"session", "90-days", "until-revoked"})


def is_loopback(address: str) -> bool:
    return address == "::1" or address.startswith("127.")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BrowserSession:
    token: str
    id: str
    local: bool = False
    trust: str = "session"
    label: str = ""
    expires_at: float | None = None


class PairingRateLimited(RuntimeError):
    """Raised after too many failed code attempts from one peer."""


class BrowserAuthRegistry:
    """Bounded sessions with optional persistence for remembered devices."""

    def __init__(self, *, clock: Callable[[], float] = time.time, state_path: str | Path | None = None) -> None:
        self._clock = clock
        self._state_path = Path(state_path) if state_path else None
        self._lock = threading.Lock()
        self._pairings: dict[str, float] = {}
        self._sessions: dict[str, dict[str, object]] = {}
        self._attempts: dict[str, list[float]] = {}
        self._local_launches: list[str] = []
        self._local_exits: set[str] = set()
        self._load()

    def issue_pairing(self) -> str:
        with self._lock:
            self._prune()
            while len(self._pairings) >= MAX_PAIRINGS:
                self._pairings.pop(next(iter(self._pairings)))
            for _ in range(32):
                code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(4))
                if code not in self._pairings:
                    self._pairings[code] = self._clock() + PAIRING_TTL_SECONDS
                    return code
            raise RuntimeError("Could not allocate a pairing code.")

    def exchange_pairing(self, code: str, *, peer: str, trust: str, label: str = "") -> BrowserSession | None:
        trust = trust.strip().lower()
        if trust not in TRUST_MODES:
            raise ValueError("Invalid trust duration.")
        normalized = code.strip().upper().replace("-", "").replace(" ", "")
        with self._lock:
            self._prune()
            attempts = self._attempts.setdefault(peer, [])
            if len(attempts) >= MAX_PAIRING_ATTEMPTS:
                raise PairingRateLimited("Too many pairing attempts. Try again in a minute.")
            expires = self._pairings.pop(normalized, None)
            if expires is None or expires <= self._clock():
                attempts.append(self._clock())
                return None
            self._attempts.pop(peer, None)
            return self._create_session(trust=trust, label=label or f"Browser at {peer}")

    def authenticate(self, token: str) -> BrowserSession | None:
        with self._lock:
            self._prune()
            record = self._sessions.get(_token_digest(token))
            return None if record is None else self._session(token, record)

    def list_sessions(self, *, current_token: str = "") -> list[dict[str, object]]:
        with self._lock:
            self._prune()
            current = _token_digest(current_token) if current_token else ""
            return [{"id": str(record["id"]), "label": str(record["label"]), "trust": str(record["trust"]), "created_at": float(record["created_at"]), "expires_at": record.get("expires_at"), "current": digest == current} for digest, record in self._sessions.items()]

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_token_digest(token), None)
            self._save()

    def revoke_id(self, session_id: str) -> bool:
        with self._lock:
            match = next((digest for digest, value in self._sessions.items() if value["id"] == session_id), None)
            if match is None:
                return False
            self._sessions.pop(match, None)
            self._save()
            return True

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            self._save()
            return count

    def begin_local_launch(self) -> str:
        with self._lock:
            launch_id = secrets.token_urlsafe(18)
            while len(self._local_launches) >= MAX_LOCAL_EXITS:
                self._local_launches.pop(0)
            self._local_launches.append(launch_id)
            return launch_id

    def request_local_exit(self, *, peer: str) -> bool:
        if not is_loopback(peer):
            return False
        with self._lock:
            if not self._local_launches:
                return False
            self._local_exits.add(self._local_launches[-1])
            return True

    def local_exit_requested(self, launch_id: str) -> bool:
        with self._lock:
            requested = launch_id in self._local_exits
            self._local_exits.discard(launch_id)
            if requested and launch_id in self._local_launches:
                self._local_launches.remove(launch_id)
            return requested

    def _create_session(self, *, trust: str, label: str) -> BrowserSession:
        while len(self._sessions) >= MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires_at = now + SESSION_TTL_SECONDS if trust == "session" else now + REMEMBER_90_DAYS_SECONDS if trust == "90-days" else None
        record: dict[str, object] = {"id": secrets.token_urlsafe(12), "label": label[:160], "trust": trust, "created_at": now, "expires_at": expires_at}
        self._sessions[_token_digest(token)] = record
        self._save()
        return self._session(token, record)

    @staticmethod
    def _session(token: str, record: dict[str, object]) -> BrowserSession:
        expires = record.get("expires_at")
        return BrowserSession(token=token, id=str(record["id"]), trust=str(record["trust"]), label=str(record["label"]), expires_at=float(expires) if expires is not None else None)

    def _prune(self) -> None:
        now = self._clock()
        self._pairings = {key: value for key, value in self._pairings.items() if value > now}
        self._attempts = {peer: [stamp for stamp in stamps if stamp + PAIRING_ATTEMPT_WINDOW_SECONDS > now] for peer, stamps in self._attempts.items()}
        before = len(self._sessions)
        self._sessions = {key: value for key, value in self._sessions.items() if value.get("expires_at") is None or float(value["expires_at"]) > now}
        if len(self._sessions) != before:
            self._save()

    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            sessions = value.get("sessions", {})
            if isinstance(sessions, dict):
                self._sessions = {str(key): record for key, record in sessions.items() if isinstance(record, dict) and record.get("trust") != "session"}
            self._prune()
        except (OSError, ValueError, TypeError):
            self._sessions = {}

    def _save(self) -> None:
        if self._state_path is None:
            return
        persistent = {key: value for key, value in self._sessions.items() if value.get("trust") != "session"}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(f".{self._state_path.name}.tmp")
        temporary.write_text(json.dumps({"version": 1, "sessions": persistent}, indent=2) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(self._state_path)
