"""Unit tests for romcloud.infrastructure.mount_endpoint_cache.

Covers the boot-time UX optimization: caching a resolved SMB endpoint as
disposable runtime state (never config), keyed against the exact
configured server so a stale/foreign entry is never followed blindly.
"""

from __future__ import annotations

import socket
import threading
import time

from romcloud.infrastructure import mount_endpoint_cache as mec


class TestEndpointCacheRoundTrip:
    def test_missing_file_returns_none(self, tmp_path):
        assert mec.read_endpoint_cache(tmp_path) is None

    def test_round_trip(self, tmp_path):
        mec.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.10")
        entry = mec.read_endpoint_cache(tmp_path)
        assert entry is not None
        assert entry.server == "omnivault"
        assert entry.endpoint == "192.0.2.10"
        assert entry.resolved_at  # non-empty timestamp

    def test_corrupt_file_returns_none(self, tmp_path):
        path = mec.endpoint_cache_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json{{{", encoding="utf-8")
        assert mec.read_endpoint_cache(tmp_path) is None

    def test_missing_required_key_returns_none(self, tmp_path):
        path = mec.endpoint_cache_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"server": "omnivault"}', encoding="utf-8")  # no "endpoint"
        assert mec.read_endpoint_cache(tmp_path) is None

    def test_write_overwrites_previous_entry(self, tmp_path):
        mec.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.10")
        mec.write_endpoint_cache(tmp_path, "omnivault", "192.0.2.20")
        entry = mec.read_endpoint_cache(tmp_path)
        assert entry.endpoint == "192.0.2.20"

    def test_lives_under_run_directory_not_config(self, tmp_path):
        path = mec.endpoint_cache_path(tmp_path)
        assert path.parent.name == "run"
        assert path.name == "mount-endpoint-cache.json"


class TestResolveEndpoint:
    def test_prefers_ipv4_when_both_present(self):
        def fake_getaddrinfo(host, port):
            return [
                (socket.AF_INET6, None, None, "", ("2001:db8::1", port, 0, 0)),
                (socket.AF_INET, None, None, "", ("192.0.2.5", port)),
            ]

        result = mec.resolve_endpoint("omnivault", 445, resolver=fake_getaddrinfo)
        assert result == "192.0.2.5"

    def test_falls_back_to_first_result_when_no_ipv4(self):
        def fake_getaddrinfo(host, port):
            return [(socket.AF_INET6, None, None, "", ("2001:db8::1", port, 0, 0))]

        result = mec.resolve_endpoint("omnivault", 445, resolver=fake_getaddrinfo)
        assert result == "2001:db8::1"

    def test_returns_none_on_resolution_failure(self):
        def fake_getaddrinfo(host, port):
            raise OSError("Name or service not known")

        assert mec.resolve_endpoint("omnivault", 445, resolver=fake_getaddrinfo) is None

    def test_literal_ip_input_resolves_to_itself(self):
        def fake_getaddrinfo(host, port):
            return [(socket.AF_INET, None, None, "", (host, port))]

        assert mec.resolve_endpoint("192.0.2.5", 445, resolver=fake_getaddrinfo) == "192.0.2.5"

    def test_blocked_resolution_returns_none_at_deadline(self):
        release = threading.Event()

        def blocked_getaddrinfo(_host, _port):
            release.wait(5.0)
            return []

        started = time.monotonic()
        try:
            result = mec.resolve_endpoint(
                "omnivault", 445, resolver=blocked_getaddrinfo, timeout=0.02
            )
        finally:
            release.set()

        assert result is None
        assert time.monotonic() - started < 1.0
