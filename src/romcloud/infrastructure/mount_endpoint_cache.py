"""Cached resolved SMB endpoint — a boot-time UX optimization, not config.

Problem: a user configures a friendly hostname (e.g. ``omnivault``), but at
early Batocera boot, DNS/MagicDNS/Tailscale may not be ready yet. The mount
worker's bounded retry loop (:mod:`romcloud.infrastructure.mount_worker`)
already handles this *reliably*, but "reliably" can still mean minutes of
waiting for name resolution before the first successful attempt.

This module lets the worker remember the last concrete address (IP) that
successfully resolved for the configured hostname, and try it first on the
next boot — before waiting on hostname resolution at all. It is purely a
fast-path hint:

- It is derived, disposable *runtime state*, never user configuration — it
  lives under ``{romcloud_home}/run/``, next to the worker's own PID/status
  files, never in ``romcloud.toml``. Losing it (or deleting it) only costs
  a bit of boot-time UX, never functionality.
- It is keyed against the exact configured server string, so a stale entry
  from a previous configuration is simply ignored, never followed.
- It is refreshed on every successful mount, whether that mount used the
  cached endpoint or the configured hostname — so a genuinely stale entry
  self-heals as soon as hostname resolution next succeeds.
- If it doesn't work, the caller always falls back to the existing bounded
  hostname retry loop — it can never be the sole path to a working mount.

Persistence: this file lives under ``{romcloud_home}/run/`` — the exact
same directory tree as ``config/``, ``data/``, and ``venv/``, all of which
ROMCloud already treats as durable across a Batocera reboot (persisted on
the ``/userdata`` partition, not a tmpfs/overlay mount). Nothing in
install/update/setup touches this file; it is only ever cleared by
``romcloud mount remove`` alongside the worker's other runtime state.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from romcloud.infrastructure.logging import get_logger

log = get_logger("mount.endpoint_cache")


@dataclass(frozen=True)
class EndpointCacheEntry:
    server: str
    """The configured hostname/IP this endpoint was resolved for."""

    endpoint: str
    """The concrete IP address that last worked for *server*."""

    resolved_at: str


def endpoint_cache_path(romcloud_home: Path) -> Path:
    return romcloud_home / "run" / "mount-endpoint-cache.json"


def read_endpoint_cache(romcloud_home: Path) -> Optional[EndpointCacheEntry]:
    """Best-effort read — a missing or corrupt cache is never an error,
    just treated as "no hint available"."""
    path = endpoint_cache_path(romcloud_home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return EndpointCacheEntry(
            server=str(data["server"]),
            endpoint=str(data["endpoint"]),
            resolved_at=str(data.get("resolved_at", "")),
        )
    except (OSError, ValueError, KeyError):
        return None


def write_endpoint_cache(romcloud_home: Path, server: str, endpoint: str) -> None:
    """Best-effort write — caching a resolved endpoint is a nicety, never
    allowed to fail the mount it was derived from."""
    path = endpoint_cache_path(romcloud_home)
    payload = {
        "server": server,
        "endpoint": endpoint,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        log.warning("Could not write mount endpoint cache at %s", path)


def resolve_endpoint(
    host: str,
    port: int,
    *,
    resolver: Callable[[str, int], list] = socket.getaddrinfo,
) -> Optional[str]:
    """Best-effort resolve *host* to a concrete IP address string.

    Prefers an IPv4 result (the overwhelmingly common case for LAN/
    Tailscale/NAS setups) but falls back to the first result of any family.
    Returns None — never raises — on any resolution failure, so callers can
    always treat this as "no hint available" rather than a hard error.

    Ordering caveat: this returns whatever order the platform resolver's
    ``getaddrinfo()`` produces for *this one call*. For a hostname with
    multiple A/AAAA records (round-robin DNS, ``resolv.conf``'s ``rotate``
    option, or a dynamic resolver such as MagicDNS choosing between
    peers/relays), a later call is not guaranteed to return the same
    address as an earlier one — including whichever address a concurrent
    mount attempt's own, independent internal resolution actually used.
    The result is therefore always a *candidate*, not proof that it is the
    exact address a given successful mount connected to; callers that need
    that guarantee must independently verify the candidate (e.g. via
    :func:`romcloud.infrastructure.mount.check_reachable`) before trusting
    it.
    """
    try:
        results = resolver(host, port)
    except OSError:
        return None

    ipv4_address: Optional[str] = None
    for family, _type, _proto, _canonname, sockaddr in results:
        address = sockaddr[0]
        if family == socket.AF_INET:
            return address
        if ipv4_address is None:
            ipv4_address = address
    return ipv4_address
