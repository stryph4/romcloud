from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from romcloud.web.auth import BOOTSTRAP_TTL_SECONDS, BrowserAuthRegistry
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


def test_bootstrap_is_single_use_and_expires() -> None:
    now = [10.0]
    registry = BrowserAuthRegistry(clock=lambda: now[0])
    code, _ = registry.issue(local=False)
    assert registry.exchange(code, peer="192.168.1.50") is not None
    assert registry.exchange(code, peer="192.168.1.50") is None

    expired, _ = registry.issue(local=False)
    now[0] += BOOTSTRAP_TTL_SECONDS + 0.1
    assert registry.exchange(expired, peer="192.168.1.50") is None


def test_local_bootstrap_cannot_be_exchanged_remotely() -> None:
    registry = BrowserAuthRegistry()
    code, _ = registry.issue(local=True)
    assert registry.exchange(code, peer="192.168.1.50") is None

    loopback_code, _ = registry.issue(local=True)
    session = registry.exchange(loopback_code, peer="127.0.0.1")
    assert session is not None and session.local


def test_remote_pairing_exchanges_for_cookie_and_replay_is_rejected() -> None:
    server = ManagerHTTPServer(("127.0.0.1", 0), _Manager(), "permanent-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        issue = _request(
            f"{base}/api/auth/bootstrap",
            body={"kind": "remote"},
            headers={"Authorization": "Bearer permanent-secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(issue, timeout=2) as response:
            code = json.load(response)["code"]
        exchange = _request(
            f"{base}/auth/exchange",
            body={"code": code},
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(exchange, timeout=2) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        assert "permanent-secret" not in cookie
        with urllib.request.urlopen(
            _request(f"{base}/api/status", headers={"Cookie": cookie}), timeout=2
        ) as response:
            assert json.load(response)["mode"] == "cache"
        with pytest.raises(urllib.error.HTTPError) as replay:
            urllib.request.urlopen(exchange, timeout=2)
        assert replay.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_exit_closes_session_launcher_without_stopping_daemon() -> None:
    server = ManagerHTTPServer(("127.0.0.1", 0), _Manager(), "permanent-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        code, launch_id = server.browser_auth.issue(local=True)
        exchange = _request(
            f"{base}/auth/exchange",
            body={"code": code},
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(exchange, timeout=2) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        exit_request = _request(
            f"{base}/api/local-exit",
            body={},
            headers={"Cookie": cookie, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(exit_request, timeout=2) as response:
            assert json.load(response)["closing"] is True
        assert server.browser_auth.local_exit_requested(str(launch_id))
        # The same persistent server remains available after the kiosk exits.
        with urllib.request.urlopen(
            _request(
                f"{base}/api/status",
                headers={"Authorization": "Bearer permanent-secret"},
            ),
            timeout=2,
        ) as response:
            assert json.load(response)["mode"] == "cache"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
