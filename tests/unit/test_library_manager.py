from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from romcloud.core.capabilities import CapabilityPolicy, OperatingMode
from romcloud.core.models.cache import CachePolicy
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.repositories.library_browser import LibraryBrowserRepository
from romcloud.services.library_manager import LibraryManagerService
from romcloud.web.server import ManagerHTTPServer


def _manager(db, game_repo, cache_repo, cache_service, mode=OperatingMode.CACHE):
    return LibraryManagerService(
        LibraryBrowserRepository(db),
        game_repo,
        cache_repo,
        cache_service,
        policy_loader=lambda: CapabilityPolicy("smart_cache", mode),
        source_reachable=lambda: True,
    )


def _game(game_repo, root: Path, system: str, title: str, suffix: str = ".rom") -> Game:
    directory = root / system
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{title}{suffix}"
    path.write_bytes(title.encode() or b"x")
    game = Game.create(
        system,
        title,
        "local",
        str(root),
        [GameAsset(path.name, f"{system}/{path.name}", path.stat().st_size, True)],
    )
    game_repo.save(game)
    return game


def test_system_first_paging_search_filters_and_bulk_pin(
    db, game_repo, cache_repo, cache_service, tmp_path
):
    root = tmp_path / "source"
    games = [_game(game_repo, root, "nes", f"Game {index:03}") for index in range(125)]
    _game(game_repo, root, "snes", "Other")
    manager = _manager(db, game_repo, cache_repo, cache_service)

    page = manager.browse(system="nes", scope="full", page=2, page_size=40)
    assert page["total"] == 125
    assert page["pages"] == 4
    assert len(page["games"]) == 40
    assert all(item["system"] == "nes" for item in page["games"])
    assert manager.browse(system="nes", scope="full", search="Game 12")["total"] == 5

    selected = [games[0].id, games[1].id, games[2].id]
    manager.action("pin", selected)
    pinned = manager.browse(system="nes", scope="device", state="pinned")
    assert {item["id"] for item in pinned["games"]} == set(selected)
    assert all(item["state"] == "incomplete" and item["pinned"] for item in pinned["games"])
    assert all(item["has_local_copy"] is False for item in pinned["games"])
    manager.action("unpin", selected)
    assert manager.browse(system="nes", scope="device")["total"] == 0


def test_browse_reports_cached_pinned_and_incomplete_without_descriptor_reads(
    db, game_repo, cache_repo, cache_service, tmp_path, monkeypatch
):
    root = tmp_path / "source"
    cached = _game(game_repo, root, "nes", "Cached")
    pinned = _game(game_repo, root, "nes", "Pinned")
    playlist = _game(game_repo, root, "psx", "Multi", ".m3u")
    cache_service.cache_game(cached.id)
    cache_service.cache_game(pinned.id)
    cache_service.pin(pinned.id)
    cache_service.pin(playlist.id)
    monkeypatch.setattr(
        cache_service._dependencies,
        "resolve",
        lambda game: (_ for _ in ()).throw(AssertionError("browse resolved descriptor")),
    )
    manager = _manager(db, game_repo, cache_repo, cache_service)

    nes = manager.browse(system="nes", scope="full")
    states = {item["title"]: item["state"] for item in nes["games"]}
    assert states == {"Cached": "cached", "Pinned": "pinned"}
    psx = manager.browse(system="psx", scope="device")
    assert psx["games"][0]["state"] == "incomplete"
    assert psx["games"][0]["pinned"] is True
    assert psx["games"][0]["offline_ready"] is False


def test_offline_forces_local_scope_and_hides_remote_only_systems(
    db, game_repo, cache_repo, cache_service, tmp_path
):
    root = tmp_path / "source"
    local = _game(game_repo, root, "nes", "Local")
    _game(game_repo, root, "snes", "Remote")
    cache_service.cache_game(local.id)
    manager = _manager(db, game_repo, cache_repo, cache_service, OperatingMode.OFFLINE)

    page = manager.browse(system="nes", scope="full")
    assert page["scope"] == "device" and page["offline_limited"] is True
    assert [item["title"] for item in page["games"]] == ["Local"]
    assert manager.systems()["systems"] == [{"system": "nes", "total": 1, "local": 1}]
    with pytest.raises(Exception, match="Offline"):
        manager.pinned_preflight()


def test_pinned_preflight_resolves_m3u_xbla_and_deduplicates_shared_members(
    cache_service, cache_repo, game_repo, tmp_path
):
    root = tmp_path / "source"
    psx = root / "psx"
    psx.mkdir(parents=True)
    shared = psx / "Shared.chd"
    shared.write_bytes(b"shared-physical-disc")
    playlists = []
    for title in ("Collection A", "Collection B"):
        marker = psx / f"{title}.m3u"
        marker.write_text("Shared.chd\n")
        game = Game.create(
            "psx", title, "local", str(root),
            [GameAsset(marker.name, f"psx/{marker.name}", marker.stat().st_size, True)],
        )
        game_repo.save(game)
        cache_service.pin(game.id)
        playlists.append((game, marker))

    xbla = root / "xbox360" / "xbla"
    xbla.mkdir(parents=True)
    xmarker = xbla / "Zuma.xbox360"
    xmarker.write_text("Zuma\n")
    payload = xbla / "Zuma" / "584109C4" / "000D0000"
    payload.mkdir(parents=True)
    payload_file = payload / "package"
    payload_file.write_bytes(b"real-xbla-payload")
    xbox = Game.create(
        "xbox360", "Zuma", "local", str(root),
        [GameAsset(xmarker.name, "xbox360/xbla/Zuma.xbox360", xmarker.stat().st_size, True)],
    )
    game_repo.save(xbox)
    cache_service.pin(xbox.id)

    expected = (
        sum(marker.stat().st_size for _, marker in playlists)
        + shared.stat().st_size
        + xmarker.stat().st_size
        + payload_file.stat().st_size
    )
    plan = cache_service.preflight_pinned(free_bytes=10**9)
    assert plan.games_needing_data == 3
    assert plan.additional_bytes == expected
    assert plan.allowed

    cache_service.cache_game(playlists[0][0].id)
    repaired = cache_service.preflight_pinned(free_bytes=10**9)
    assert repaired.games_needing_data == 2
    assert repaired.additional_bytes == playlists[1][1].stat().st_size + xmarker.stat().st_size + payload_file.stat().st_size
    assert cache_repo.owner_count("psx/Shared.chd") == 1


def test_preflight_hard_blocks_cache_limit_and_free_reserve(
    cache_service, game_repo, tmp_path
):
    root = tmp_path / "source"
    game = _game(game_repo, root, "nes", "Large")
    cache_service.pin(game.id)
    needed = (root / "nes" / "Large.rom").stat().st_size

    cache_service._policy = CachePolicy(max_size_bytes=needed - 1, min_free_bytes=0)
    quota = cache_service.preflight_pinned(free_bytes=10**9)
    assert not quota.allowed and "cache-size limit" in quota.reasons[0]

    cache_service._policy = CachePolicy(max_size_bytes=10**9, min_free_bytes=100)
    reserve = cache_service.preflight_pinned(free_bytes=needed + 99)
    assert not reserve.allowed and "minimum reserve" in reserve.reasons[0]


def test_already_cached_pinned_game_requires_zero_additional_bytes(
    cache_service, game_repo, tmp_path
):
    game = _game(game_repo, tmp_path / "source", "nes", "Ready")
    cache_service.cache_game(game.id)
    cache_service.pin(game.id)
    plan = cache_service.preflight_pinned(free_bytes=10**9)
    assert plan.games_needing_data == 0
    assert plan.additional_bytes == 0


def test_large_catalog_page_query_is_bounded_and_fast(db, cache_repo, game_repo, cache_service):
    now = datetime.now(timezone.utc).isoformat()
    count = 28_000
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO games (id, system, title, source_provider, source_root, added_at, is_eligible) VALUES (?, 'nes', ?, 'local', '/source', ?, 1)",
            [(f"g-{index}", f"Synthetic {index:05}", now) for index in range(count)],
        )
        conn.executemany(
            "INSERT INTO game_assets (id, game_id, relative_path, filename, size_bytes, is_primary) VALUES (?, ?, ?, ?, 10, 1)",
            [(f"a-{index}", f"g-{index}", f"nes/{index}.nes", f"{index}.nes") for index in range(count)],
        )
    manager = _manager(db, game_repo, cache_repo, cache_service)

    started = time.perf_counter()
    page = manager.browse(system="nes", scope="full", search="279", page=1, page_size=50)
    elapsed = time.perf_counter() - started

    assert 0 < len(page["games"]) <= 50
    assert elapsed < 2.0


def test_http_api_requires_token_and_serves_paginated_json(
    db, game_repo, cache_repo, cache_service, tmp_path
):
    _game(game_repo, tmp_path / "source", "nes", "Browser")
    server = ManagerHTTPServer(("127.0.0.1", 0), _manager(db, game_repo, cache_repo, cache_service), "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/", timeout=2) as response:
            assert b"ROMCloud Library" in response.read()
        with urllib.request.urlopen(f"{base}/controller.js", timeout=2) as response:
            assert b"navigator.getGamepads" in response.read()
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{base}/api/status", headers={"Host": "batocera.local"}
                ),
                timeout=2,
            )
        assert denied.value.code == 401
        request = urllib.request.Request(
            f"{base}/api/games?system=nes&scope=full&page_size=10",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        assert payload["total"] == 1
        assert payload["games"][0]["title"] == "Browser"

        action = urllib.request.Request(
            f"{base}/api/actions",
            data=json.dumps(
                {"action": "pin", "game_ids": [payload["games"][0]["id"]]}
            ).encode(),
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(action, timeout=2) as response:
            assert json.load(response)["count"] == 1
        preflight = urllib.request.Request(
            f"{base}/api/download-pinned/preflight",
            data=b"{}",
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(preflight, timeout=2) as response:
            assert json.load(response)["games_needing_data"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_browser_loads_authenticated_manager_app(
    db, game_repo, cache_repo, cache_service, tmp_path
):
    candidates = [
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    browser = next((str(path) for path in candidates if path and Path(path).is_file()), None)
    if browser is None:
        pytest.skip("Chromium is not installed on this development host")
    _game(game_repo, tmp_path / "source", "nes", "Rendered Browser Game")
    server = ManagerHTTPServer(
        ("127.0.0.1", 0),
        _manager(db, game_repo, cache_repo, cache_service),
        "browser-secret",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        result = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-proxy-server",
                f"--user-data-dir={tmp_path / 'chrome-profile'}",
                "--virtual-time-budget=2500",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Rendered Browser Game" in result.stdout
        assert 'id="shell" class="shell hidden"' not in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
