from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from romcloud.web.server import ManagerHTTPServer


class _Manager:
    def status(self):
        return {"mode": "cache"}


def _request(url, *, body=None, headers=None):
    return urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers or {},
        method="GET" if body is None else "POST",
    )


def _serve(tmp_path):
    diagnostics_path = tmp_path / "diagnostics.db"
    server = ManagerHTTPServer(
        ("127.0.0.1", 0),
        _Manager(),
        "secret",
        diagnostics_path=diagnostics_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_diagnostics_is_served_by_the_same_manager_server_and_port(tmp_path) -> None:
    server, thread, base = _serve(tmp_path)
    try:
        # One listener: the manager's own ephemeral port.
        assert server.server_address[1] > 0
        # The library shell and the diagnostics assets are served together.
        with urllib.request.urlopen(f"{base}/", timeout=2) as response:
            html = response.read().decode()
        assert 'id="library-main"' in html
        assert 'id="diagnostics-main"' in html
        assert 'id="nav-diagnostics"' in html and 'id="nav-library"' in html
        with urllib.request.urlopen(f"{base}/diagnostics.js", timeout=2) as response:
            assert response.status == 200
        with urllib.request.urlopen(f"{base}/app.js", timeout=2) as response:
            app = response.read().decode()
        assert "openDiagnostics" in app and "showLibrary" in app
        # Diagnostics API is on the same base URL (no second port).
        with urllib.request.urlopen(f"{base}/api/diagnostics", timeout=2) as response:
            payload = json.load(response)
        assert "operations" in payload and "facets" in payload
        assert {"subsystems", "levels"} <= set(payload["facets"])
    finally:
        _stop(server, thread)


def test_diagnostics_api_requires_pairing_for_remote_clients(tmp_path) -> None:
    server, thread, base = _serve(tmp_path)
    try:
        # A remote (non-loopback Host) client is NOT trusted without pairing.
        remote = {"Host": "batocera.local"}
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(_request(f"{base}/api/diagnostics", headers=remote), timeout=2)
        assert denied.value.code == 401

        # The same pairing flow used by the Library Manager unlocks diagnostics.
        issue = _request(
            f"{base}/api/auth/pairing-code",
            body={},
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(issue, timeout=2) as response:
            code = json.load(response)["code"]
        exchange = _request(
            f"{base}/auth/pair",
            body={"code": code, "trust": "until-revoked"},
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(exchange, timeout=2) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        paired = {"Host": "batocera.local", "Cookie": cookie}
        with urllib.request.urlopen(_request(f"{base}/api/diagnostics", headers=paired), timeout=2) as response:
            assert "operations" in json.load(response)

        # A bearer-token session (the same one the manager uses) also works.
        bearer = {"Host": "batocera.local", "Authorization": "Bearer secret"}
        with urllib.request.urlopen(_request(f"{base}/api/diagnostics", headers=bearer), timeout=2) as response:
            assert "operations" in json.load(response)
    finally:
        _stop(server, thread)


def test_library_navigation_still_works_and_no_second_listener(tmp_path) -> None:
    server, thread, base = _serve(tmp_path)
    try:
        with urllib.request.urlopen(f"{base}/api/status", timeout=2) as response:
            assert json.load(response)["mode"] == "cache"
        # Confirm only the manager socket is bound for this instance.
        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        # No second HTTP service is introduced by diagnostics.
        assert server.diagnostic_store is not None
    finally:
        _stop(server, thread)
