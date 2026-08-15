from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from romcloud.web.auth import (
    MAX_PAIRING_ATTEMPTS,
    PAIRING_TTL_SECONDS,
    REMEMBER_90_DAYS_SECONDS,
    BrowserAuthRegistry,
    PairingRateLimited,
)
from romcloud.web.server import ControllerDiagnosticLog, ManagerHTTPServer


class _Manager:
    def status(self):
        return {"mode": "cache"}


def test_idle_manager_server_does_not_call_catalog_or_transfer_services() -> None:
    class IdleManager:
        def __getattr__(self, name):
            raise AssertionError(f"idle server unexpectedly called {name}")

    server = ManagerHTTPServer(("127.0.0.1", 0), IdleManager(), "secret")
    server.server_close()


def _request(url, *, body=None, headers=None):
    return urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers or {}, method="GET" if body is None else "POST")


def test_four_character_pairing_expiry_replay_and_rate_limit() -> None:
    now = [10.0]
    registry = BrowserAuthRegistry(clock=lambda: now[0])
    code = registry.issue_pairing()
    assert len(code) == 4
    assert registry.exchange_pairing(code, peer="192.168.1.50", trust="session")
    assert registry.exchange_pairing(code, peer="192.168.1.50", trust="session") is None
    expired = registry.issue_pairing()
    now[0] += PAIRING_TTL_SECONDS + 0.1
    assert registry.exchange_pairing(expired, peer="192.168.1.51", trust="session") is None
    for _ in range(MAX_PAIRING_ATTEMPTS - 1):
        assert registry.exchange_pairing("AAAA", peer="192.168.1.51", trust="session") is None
    with pytest.raises(PairingRateLimited):
        registry.exchange_pairing("BBBB", peer="192.168.1.51", trust="session")


def test_session_90_day_and_until_revoked_persistence(tmp_path) -> None:
    now = [100.0]
    path = tmp_path / "trusted.json"
    registry = BrowserAuthRegistry(clock=lambda: now[0], state_path=path)
    tokens = {}
    for trust in ("session", "90-days", "until-revoked"):
        tokens[trust] = registry.exchange_pairing(registry.issue_pairing(), peer="10.0.0.2", trust=trust).token
    assert registry.authenticate(tokens["session"])
    assert registry.authenticate(tokens["90-days"]).expires_at == 100.0 + REMEMBER_90_DAYS_SECONDS
    reloaded = BrowserAuthRegistry(clock=lambda: now[0], state_path=path)
    assert reloaded.authenticate(tokens["session"]) is None
    assert reloaded.authenticate(tokens["90-days"])
    assert reloaded.authenticate(tokens["until-revoked"])
    now[0] += REMEMBER_90_DAYS_SECONDS + 1
    assert reloaded.authenticate(tokens["90-days"]) is None
    assert reloaded.authenticate(tokens["until-revoked"])


def test_per_device_revoke_and_revoke_all_are_immediate() -> None:
    registry = BrowserAuthRegistry()
    first = registry.exchange_pairing(registry.issue_pairing(), peer="10.0.0.2", trust="until-revoked")
    second = registry.exchange_pairing(registry.issue_pairing(), peer="10.0.0.3", trust="until-revoked")
    assert registry.revoke_id(first.id)
    assert registry.authenticate(first.token) is None
    assert registry.authenticate(second.token)
    assert registry.revoke_all() == 1
    assert registry.authenticate(second.token) is None


def test_remote_pairing_cookie_and_local_open_here_bypass() -> None:
    server = ManagerHTTPServer(("127.0.0.1", 0), _Manager(), "permanent-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # Loopback + loopback Host is the credential-free Open Here boundary.
        with urllib.request.urlopen(f"{base}/api/status", timeout=2) as response:
            assert json.load(response)["mode"] == "cache"
        issue = _request(f"{base}/api/auth/pairing-code", body={}, headers={"Authorization": "Bearer permanent-secret", "Content-Type": "application/json"})
        with urllib.request.urlopen(issue, timeout=2) as response:
            code = json.load(response)["code"]
        exchange = _request(f"{base}/auth/pair", body={"code": code, "trust": "90-days"}, headers={"Content-Type": "application/json", "User-Agent": "Test Browser"})
        with urllib.request.urlopen(exchange, timeout=2) as response:
            cookie_header = response.headers["Set-Cookie"]
        assert "permanent-secret" not in cookie_header
        assert f"Max-Age={REMEMBER_90_DAYS_SECONDS}" in cookie_header
        with pytest.raises(urllib.error.HTTPError) as replay:
            urllib.request.urlopen(exchange, timeout=2)
        assert replay.value.code == 401
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_remote_host_header_does_not_receive_loopback_bypass() -> None:
    server = ManagerHTTPServer(("127.0.0.1", 0), _Manager(), "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request = _request(f"http://127.0.0.1:{server.server_address[1]}/api/status", headers={"Host": "batocera.local"})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=2)
        assert denied.value.code == 401
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_http_device_revoke_invalidates_the_next_remote_request() -> None:
    server = ManagerHTTPServer(("127.0.0.1", 0), _Manager(), "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        issue = _request(f"{base}/api/auth/pairing-code", body={}, headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
        with urllib.request.urlopen(issue, timeout=2) as response:
            code = json.load(response)["code"]
        exchange = _request(f"{base}/auth/pair", body={"code": code, "trust": "until-revoked"}, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(exchange, timeout=2) as response:
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        remote_headers = {"Host": "batocera.local", "Cookie": cookie}
        with urllib.request.urlopen(_request(f"{base}/api/trusted-devices", headers=remote_headers), timeout=2) as response:
            device_id = json.load(response)["devices"][0]["id"]
        revoke = _request(f"{base}/api/trusted-devices/revoke", body={"id": device_id}, headers={**remote_headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(revoke, timeout=2) as response:
            assert json.load(response)["revoked"] is True
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(_request(f"{base}/api/status", headers=remote_headers), timeout=2)
        assert denied.value.code == 401
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_local_exit_does_not_stop_manager() -> None:
    registry = BrowserAuthRegistry()
    launch_id = registry.begin_local_launch()
    assert registry.request_local_exit(peer="127.0.0.1")
    assert registry.local_exit_requested(launch_id)
    assert not registry.local_exit_requested(launch_id)


def test_controller_diagnostics_are_persistent_bounded_and_loopback_only(tmp_path) -> None:
    log_path = tmp_path / "browser-controller.log"
    server = ManagerHTTPServer(
        ("127.0.0.1", 0),
        _Manager(),
        "secret",
        controller_log_path=log_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        event = _request(
            f"{base}/api/controller-diagnostics",
            body={
                "events": [
                    {
                        "event": "gamepad-snapshot",
                        "detail": {
                            "id": "Steam Deck Controller",
                            "mapping": "",
                            "buttons": 20,
                            "axes": 4,
                        },
                    }
                ]
            },
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(event, timeout=2) as response:
            assert json.load(response)["recorded"] == 1
        payload = json.loads(log_path.read_text().strip())
        assert payload["event"] == "gamepad-snapshot"
        assert payload["detail"]["mapping"] == ""
        assert log_path.stat().st_mode & 0o777 == 0o600

        denied = _request(
            f"{base}/api/controller-diagnostics",
            body={"events": [{"event": "remote", "detail": {}}]},
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
                "Host": "batocera.local",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(denied, timeout=2)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    bounded = ControllerDiagnosticLog(log_path, max_bytes=100)
    bounded.append([{"event": "one", "detail": {"value": "x" * 40}}])
    bounded.append([{"event": "two", "detail": {"value": "y" * 40}}])
    assert log_path.with_suffix(".log.1").is_file()
