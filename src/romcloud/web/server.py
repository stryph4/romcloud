"""Dependency-free HTTP server for the ROMCloud browser manager."""

from __future__ import annotations

import hmac
import json
import mimetypes
import ssl
import threading
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from romcloud.core.exceptions import ROMCloudError
from romcloud.infrastructure.logging import get_logger
from romcloud.services.library_manager import LibraryManagerService

log = get_logger("web-manager")
_MAX_BODY = 128 * 1024


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
    ) -> None:
        self.manager = manager
        self.auth_token = token
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
        if not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."})
            return
        try:
            if self.path == "/api/actions":
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
        provided = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.auth_token}"
        return hmac.compare_digest(provided, expected)

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

    def _json(self, status: HTTPStatus, value: object) -> None:
        content = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
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
        log.debug("HTTP %s", format % args)


def serve_manager(
    manager: LibraryManagerService,
    host: str,
    port: int,
    token: str,
    *,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> None:
    server = ManagerHTTPServer((host, port), manager, token)
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            server.server_close()
            raise ValueError("Both a TLS certificate and key are required.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
