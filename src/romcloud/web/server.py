"""Dependency-free HTTP server for the ROMCloud browser manager."""

from __future__ import annotations

import hmac
import json
import mimetypes
import ssl
import signal
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.logging import get_logger
from romcloud.services.library_manager import LibraryManagerService
from romcloud.web.auth import (
    REMEMBER_90_DAYS_SECONDS,
    BrowserAuthRegistry,
    BrowserSession,
    PairingRateLimited,
    is_loopback,
)

log = get_logger("web-manager")
_MAX_BODY = 128 * 1024
_CONTROLLER_LOG_MAX_BYTES = 256 * 1024
_CONTROLLER_EVENT_MAX_BYTES = 4096
_CONTROLLER_BATCH_MAX = 64


class ControllerDiagnosticLog:
    """Bounded JSON-lines diagnostics for the loopback Open Here session."""

    def __init__(self, path: str | Path, *, max_bytes: int = _CONTROLLER_LOG_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, events: object) -> int:
        if not isinstance(events, list):
            raise ValueError("Controller diagnostics must contain an events list.")
        if len(events) > _CONTROLLER_BATCH_MAX:
            raise ValueError("Controller diagnostics batch is too large.")
        lines: list[bytes] = []
        for raw in events:
            if not isinstance(raw, dict):
                raise ValueError("Controller diagnostic events must be objects.")
            event = str(raw.get("event", "")).strip()
            if not event or len(event) > 64:
                raise ValueError("Controller diagnostic event name is invalid.")
            detail = raw.get("detail", {})
            if not isinstance(detail, dict):
                raise ValueError("Controller diagnostic detail must be an object.")
            line = json.dumps(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "event": event,
                    "detail": detail,
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8") + b"\n"
            if len(line) > _CONTROLLER_EVENT_MAX_BYTES:
                raise ValueError("Controller diagnostic event is too large.")
            lines.append(line)
        if not lines:
            return 0
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for line in lines:
                try:
                    current_size = self.path.stat().st_size
                except FileNotFoundError:
                    current_size = 0
                if current_size and current_size + len(line) > self.max_bytes:
                    previous = self.path.with_suffix(self.path.suffix + ".1")
                    previous.unlink(missing_ok=True)
                    self.path.replace(previous)
                with self.path.open("ab") as handle:
                    handle.write(line)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return len(lines)


@dataclass
class TransferJob:
    id: str
    state: str = "queued"
    current_game: int = 0
    total_games: int = 0
    current_game_id: str | None = None
    bytes_done: int = 0
    bytes_total: int = 0
    completed_game_ids: list[str] = field(default_factory=list)
    error: str | None = None

    def serialize(self) -> dict[str, object]:
        return dict(vars(self))


class JobRegistry:
    def __init__(self, manager: LibraryManagerService, mutation_lock: threading.RLock) -> None:
        self._manager = manager
        self._jobs: dict[str, TransferJob] = {}
        self._lock = threading.Lock()
        self._transfer_lock = mutation_lock

    def get(self, job_id: str) -> TransferJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start_pinned(self) -> TransferJob:
        plan = self._manager.pinned_preflight()
        if not plan["allowed"]:
            raise ValueError(" ".join(str(reason) for reason in plan["reasons"]))
        job = TransferJob(
            id=uuid.uuid4().hex,
            total_games=int(plan["games_needing_data"]),
        )
        with self._lock:
            if any(item.state in {"queued", "running"} for item in self._jobs.values()):
                raise ValueError("A pinned download is already running.")
            self._jobs[job.id] = job
        threading.Thread(target=self._run_pinned, args=(job,), daemon=True).start()
        return job

    def _run_pinned(self, job: TransferJob) -> None:
        def on_game(index: int, total: int, game_id: str) -> None:
            with self._lock:
                job.current_game = index
                job.total_games = total
                job.current_game_id = game_id
                job.bytes_done = 0
                job.bytes_total = 0

        def on_progress(done: int, total: int) -> None:
            with self._lock:
                job.bytes_done = done
                job.bytes_total = total

        with self._transfer_lock:
            try:
                with self._lock:
                    job.state = "running"
                completed = self._manager.download_pinned(
                    on_game=on_game, on_progress=on_progress
                )
                with self._lock:
                    job.completed_game_ids = completed
                    job.state = "complete"
            except Exception as exc:  # noqa: BLE001 - job exposes a safe message
                log.exception("Pinned download job failed")
                with self._lock:
                    job.error = str(exc)
                    job.state = "failed"


class ManagerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        manager: LibraryManagerService,
        token: str,
        auth_registry: BrowserAuthRegistry | None = None,
        controller_log_path: str | Path | None = None,
    ) -> None:
        self.manager = manager
        self.auth_token = token
        self.browser_auth = auth_registry or BrowserAuthRegistry()
        self.controller_diagnostics = (
            ControllerDiagnosticLog(controller_log_path)
            if controller_log_path is not None
            else None
        )
        self.mutation_lock = threading.RLock()
        self.jobs = JobRegistry(manager, self.mutation_lock)
        super().__init__(address, ManagerRequestHandler)


class ManagerRequestHandler(BaseHTTPRequestHandler):
    server: ManagerHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."})
            return
        try:
            if parsed.path == "/api/status":
                self._json(HTTPStatus.OK, self.server.manager.status())
            elif parsed.path == "/api/trusted-devices":
                session = self._cookie_session()
                self._json(
                    HTTPStatus.OK,
                    {"devices": self.server.browser_auth.list_sessions(
                        current_token=session.token if session else ""
                    )},
                )
            elif parsed.path == "/api/systems":
                self._json(HTTPStatus.OK, self.server.manager.systems())
            elif parsed.path == "/api/games":
                query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                self._json(HTTPStatus.OK, self.server.manager.browse(**query))
            elif parsed.path.startswith("/api/jobs/"):
                job = self.server.jobs.get(parsed.path.rsplit("/", 1)[-1])
                self._json(
                    HTTPStatus.OK if job else HTTPStatus.NOT_FOUND,
                    job.serialize() if job else {"error": "Job not found."},
                )
            elif parsed.path.startswith("/api/local-session-status/"):
                launch_id = parsed.path.rsplit("/", 1)[-1]
                self._json(
                    HTTPStatus.OK,
                    {"exit_requested": self.server.browser_auth.local_exit_requested(launch_id)},
                )
            elif parsed.path == "/" or parsed.path == "/index.html":
                self._static("index.html")
            elif parsed.path in {"/app.js", "/app.css", "/controller.js"}:
                self._static(parsed.path[1:])
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, ROMCloudError, RuntimeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            log.exception("Browser manager GET failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/auth/pair":
            self._exchange_pairing()
            return
        if not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."})
            return
        try:
            if parsed.path == "/api/auth/pairing-code":
                self._json(
                    HTTPStatus.CREATED,
                    {"code": self.server.browser_auth.issue_pairing(), "expires_in": 120},
                )
            elif parsed.path == "/api/auth/local-launch":
                if not self._bearer_authenticated():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Local launch registration is unavailable."})
                else:
                    self._json(HTTPStatus.CREATED, {"launch_id": self.server.browser_auth.begin_local_launch()})
            elif parsed.path == "/api/auth/logout":
                session = self._cookie_session()
                if session:
                    self.server.browser_auth.revoke(session.token)
                self._json(HTTPStatus.OK, {"signed_out": True}, clear_cookie=True)
            elif parsed.path == "/api/local-exit":
                if not self._trusted_local_request() or not self.server.browser_auth.request_local_exit(peer=self.client_address[0]):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Local exit is unavailable."})
                else:
                    self._json(HTTPStatus.OK, {"closing": True})
            elif parsed.path == "/api/controller-diagnostics":
                if not self._trusted_local_request():
                    self._json(
                        HTTPStatus.FORBIDDEN,
                        {"error": "Controller diagnostics are loopback-only."},
                    )
                elif self.server.controller_diagnostics is None:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "Controller diagnostics are unavailable."},
                    )
                else:
                    count = self.server.controller_diagnostics.append(
                        self._body().get("events")
                    )
                    self._json(HTTPStatus.OK, {"recorded": count})
            elif parsed.path == "/api/trusted-devices/revoke":
                revoked = self.server.browser_auth.revoke_id(str(self._body().get("id", "")))
                self._json(HTTPStatus.OK if revoked else HTTPStatus.NOT_FOUND, {"revoked": revoked})
            elif parsed.path == "/api/trusted-devices/revoke-all":
                count = self.server.browser_auth.revoke_all()
                self._json(HTTPStatus.OK, {"revoked": count}, clear_cookie=True)
            elif parsed.path == "/api/actions":
                body = self._body()
                with self.server.mutation_lock:
                    result = self.server.manager.action(
                        str(body.get("action", "")), body.get("game_ids", [])
                    )
                self._json(HTTPStatus.OK, result)
            elif self.path == "/api/download-pinned/preflight":
                with self.server.mutation_lock:
                    result = self.server.manager.pinned_preflight()
                self._json(HTTPStatus.OK, result)
            elif self.path == "/api/download-pinned":
                with self.server.mutation_lock:
                    job = self.server.jobs.start_pinned()
                self._json(HTTPStatus.ACCEPTED, job.serialize())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, ROMCloudError, RuntimeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            log.exception("Browser manager POST failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})

    def _authenticated(self) -> bool:
        return self._trusted_local_request() or self._bearer_authenticated() or self._cookie_session() is not None

    def _bearer_authenticated(self) -> bool:
        provided = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.auth_token}"
        return hmac.compare_digest(provided, expected)

    def _trusted_local_request(self) -> bool:
        if not is_loopback(self.client_address[0]):
            return False
        host = (urlparse("//" + self.headers.get("Host", "")).hostname or "").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _cookie_session(self) -> BrowserSession | None:
        cookie = self.headers.get("Cookie", "")
        for item in cookie.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "romcloud_session":
                return self.server.browser_auth.authenticate(value)
        return None

    def _exchange_pairing(self) -> None:
        try:
            body = self._body()
            session = self.server.browser_auth.exchange_pairing(
                str(body.get("code", "")),
                peer=self.client_address[0],
                trust=str(body.get("trust", "session")),
                label=self.headers.get("User-Agent", "") or f"Browser at {self.client_address[0]}",
            )
            if session is None:
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "That pairing code is invalid, expired, or already used."},
                )
                return
            self._json(
                HTTPStatus.OK,
                {"authenticated": True},
                session_cookie=session.token,
                cookie_max_age=(
                    None if session.trust == "session"
                    else REMEMBER_90_DAYS_SECONDS if session.trust == "90-days"
                    else 10 * 365 * 24 * 60 * 60
                ),
            )
        except PairingRateLimited as exc:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Requests must use application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0 or length > _MAX_BODY:
            raise ValueError("Request body is too large.")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON body.") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def _static(self, name: str) -> None:
        content = files("romcloud.web.static").joinpath(name).read_bytes()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _json(
        self,
        status: HTTPStatus,
        value: object,
        *,
        session_cookie: str | None = None,
        cookie_max_age: int | None = None,
        clear_cookie: bool = False,
    ) -> None:
        content = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        if session_cookie is not None:
            max_age = "" if cookie_max_age is None else f"; Max-Age={cookie_max_age}"
            self.send_header(
                "Set-Cookie",
                f"romcloud_session={session_cookie}; Path=/; HttpOnly; Secure; SameSite=Strict{max_age}",
            )
        elif clear_cookie:
            self.send_header(
                "Set-Cookie",
                "romcloud_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict",
            )
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args: object) -> None:
        # Never put pairing query strings or credentials in logs.
        log.debug("HTTP %s %s", self.command, urlparse(self.path).path)


def serve_manager(
    manager: LibraryManagerService,
    host: str,
    port: int,
    token: str,
    *,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    auth_state_path: str | None = None,
    controller_log_path: str | None = None,
    on_ready: Callable[[], None] | None = None,
) -> None:
    registry = BrowserAuthRegistry(state_path=auth_state_path) if auth_state_path else None
    server = ManagerHTTPServer(
        (host, port),
        manager,
        token,
        auth_registry=registry,
        controller_log_path=controller_log_path,
    )
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            server.server_close()
            raise ValueError("Both a TLS certificate and key are required.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    if on_ready is not None:
        try:
            on_ready()
        except Exception:
            server.server_close()
            raise
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)

        def stop_on_term(signum, frame):  # noqa: ANN001, ARG001
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop_on_term)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        server.server_close()
