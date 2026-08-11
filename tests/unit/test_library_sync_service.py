from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from click.testing import CliRunner

from romcloud.bootstrap.container import Container
from romcloud.core.capabilities import OperatingMode
from romcloud.core.exceptions import CapabilityUnavailableError, LibrarySyncError
from romcloud.core.models.game import Game, GameAsset
from romcloud.infrastructure.config import (
    AppConfig,
    CacheConfig,
    LibrarySyncConfig,
    RemoteDataConfig,
    SourceConfig,
)
from romcloud.infrastructure.library_view import (
    write_offline_library_state,
    write_operating_mode,
)
from romcloud.services.library_sync import OWNERSHIP_TAG, library_id_for_game
from romcloud.core.models.librarysync import LibrarySyncReport


def _config(tmp_path: Path, *, enabled: bool = True, mode: str = "smart_cache") -> AppConfig:
    source = tmp_path / "source"
    local = tmp_path / "roms"
    data = tmp_path / "data"
    remote = tmp_path / "remote"
    cache = tmp_path / "cache"
    for root in (source, local, data, remote, cache):
        root.mkdir(exist_ok=True)
    (local / "ps2").mkdir(exist_ok=True)
    return AppConfig(
        source=SourceConfig("local", str(source)),
        cache=CacheConfig(str(cache), 10, 0),
        local_roms_path=str(local),
        data_path=str(data),
        remote_data=RemoteDataConfig("local", str(remote)),
        library_sync=LibrarySyncConfig(enabled),
        game_access_mode=mode,
    )


def _source_library(config: AppConfig) -> Path:
    system = Path(config.source.rom_root) / "ps2"
    (system / "images").mkdir(parents=True)
    (system / "Game.chd").write_bytes(b"game")
    (system / "images" / "Game.png").write_bytes(b"png-media")
    gamelist = system / "gamelist.xml"
    gamelist.write_text(
        """<?xml version="1.0"?>
<gameList>
  <game>
    <path>./Game.chd</path>
    <name>Scraped Game</name>
    <desc>Canonical description</desc>
    <rating>0.8</rating>
    <developer>Studio</developer>
    <image>./images/Game.png</image>
    <manual>../../outside.pdf</manual>
  </game>
</gameList>
""",
        encoding="utf-8",
    )
    return gamelist


def _local_root(config: AppConfig) -> Path:
    return Path(config.local_roms_path) / "ps2" / "gamelist.xml"


def _canonical(config: AppConfig) -> Path:
    assert config.remote_data is not None
    return Path(config.remote_data.root) / "library" / "library.json"


def _media_record(config: AppConfig) -> dict:
    record = next(iter(json.loads(_canonical(config).read_text())["records"].values()))
    return record["media"]["image"]


def _setup(tmp_path: Path, *, enabled: bool = True, mode: str = "smart_cache"):
    config = _config(tmp_path, enabled=enabled, mode=mode)
    source_xml = _source_library(config)
    local_xml = _local_root(config)
    local_xml.write_text(
        """<gameList>
  <game><path>./Local Game.iso</path><name>User-owned local game</name><favorite>true</favorite></game>
</gameList>
""",
        encoding="utf-8",
    )
    container = Container(config)
    result = container.catalog.refresh()
    assert result.errors == []
    assert result.added == 1  # gamelist.xml and images/ are metadata, not games
    return config, container, source_xml


def _managed(local_xml: Path) -> ET.Element:
    root = ET.parse(local_xml).getroot()
    return next(item for item in root.findall("game") if item.findtext(OWNERSHIP_TAG))


def test_opt_in_disabled_does_no_library_work(tmp_path: Path):
    config, container, _ = _setup(tmp_path, enabled=False)

    with pytest.raises(LibrarySyncError, match="disabled"):
        container.library_sync.sync()

    assert not _canonical(config).exists()
    assert _local_root(config).read_text().count("<game>") == 1


def test_source_import_preflight_is_lightweight_and_reports_media_types(
    tmp_path: Path, monkeypatch
):
    config, container, _ = _setup(tmp_path)
    monkeypatch.setattr(
        "romcloud.services.library_sync._hash_file",
        lambda path: (_ for _ in ()).throw(AssertionError("preflight must not hash media")),
    )

    preview = container.library_sync.preview_source_import().as_dict()

    assert preview["games_eligible"] == 1
    assert preview["systems"] == ["ps2"]
    assert preview["gamelist_files"] == 2
    assert preview["gamelist_bytes"] > 0
    assert preview["artwork_references"] == 1
    assert preview["video_references"] == 0
    assert preview["other_media_references"] == 1
    assert preview["estimated_bytes"] is None
    assert preview["duration_estimate"] is None
    assert "storage/network speed" in preview["duration_note"]


def test_source_import_emits_real_entry_file_byte_and_render_progress(tmp_path: Path):
    _config_value, container, _ = _setup(tmp_path)
    events = []

    report = container.library_sync.sync(progress=events.append)

    assert report.rendered == 1
    metadata = [event for event in events if event.stage == "metadata"]
    assert metadata[-1].current is not None and metadata[-1].current >= 1
    media = [event for event in events if event.stage == "media"]
    assert media[-1].current == media[-1].total == 1
    assert media[-1].metadata["media_examined"] == 1
    assert media[-1].metadata["media_copied"] == 1
    assert media[-1].metadata["bytes_transferred"] > 0
    rendered = [event for event in events if event.stage == "render"]
    assert rendered[-1].current == rendered[-1].total == 1
    assert events[-1].stage == "complete"
    assert events[-1].status == "success"


def test_offline_mode_blocks_remote_sync_without_changing_canonical(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    before = _canonical(config).read_bytes()
    write_offline_library_state(config, True)
    offline = Container(config)

    with pytest.raises(CapabilityUnavailableError, match="Offline Mode"):
        offline.library_sync.sync()

    assert _canonical(config).read_bytes() == before

    write_offline_library_state(config, False)
    restored = Container(config).library_sync.sync()
    assert restored.direction == "sync"
    assert _canonical(config).is_file()


def test_cli_refreshes_es_only_after_successful_local_merge(monkeypatch):
    import romcloud.cli.commands.library_sync as command

    calls: list[str] = []
    service = type(
        "Service", (), {"sync": lambda self: LibrarySyncReport(direction="sync")}
    )()
    container = type(
        "Container", (), {
            "library_sync": service,
            "config": object(),
            "game_repo": type("Repo", (), {"list_systems": lambda self: ["ps2"]})(),
        }
    )()
    monkeypatch.setattr(command, "get_container", lambda ctx: container)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.presentation.refresh_emulationstation",
        lambda config, systems: calls.append("refreshed"),
    )

    success = CliRunner().invoke(command.library_sync_group, ["sync"], obj={})
    assert success.exit_code == 0
    assert calls == ["refreshed"]

    service.sync = lambda: (_ for _ in ()).throw(LibrarySyncError("merge failed"))
    failed = CliRunner().invoke(command.library_sync_group, ["sync"], obj={})
    assert failed.exit_code == 1
    assert calls == ["refreshed"]


def test_import_sync_and_smart_cache_render_are_safe_and_idempotent(tmp_path: Path):
    config, container, source_xml = _setup(tmp_path)
    source_before = source_xml.read_bytes()

    first = container.library_sync.sync()

    assert any("unsafe media path" in failure for failure in first.failures)
    assert source_xml.read_bytes() == source_before
    payload = json.loads(_canonical(config).read_text())
    assert payload["schema_version"] == 1
    assert len(payload["records"]) == 1
    record = next(iter(payload["records"].values()))
    assert record["metadata"]["name"] == "Scraped Game"
    assert record["metadata"]["desc"] == "Canonical description"
    assert record["metadata"]["rating"] == "0.8"
    assert record["metadata"]["developer"] == "Studio"
    assert "manual" not in record["media"]
    assert record["media"]["image"]["sha256"]
    assert (Path(config.remote_data.root) / "library" / record["media"]["image"]["blob"]).is_file()

    local_text = _local_root(config).read_text()
    assert "User-owned local game" in local_text
    managed = _managed(_local_root(config))
    assert managed.findtext("path") == "./Game.romcloud"
    assert managed.findtext("name") == "Scraped Game"
    assert managed.findtext("image").startswith("./.romcloud-media/")
    assert managed.findtext(OWNERSHIP_TAG)
    assert (_local_root(config).with_name("gamelist.xml.romcloud.bak")).is_file()

    canonical_before = _canonical(config).read_bytes()
    second = container.library_sync.sync()
    assert second.media_transferred == 0
    assert second.conflicts == []
    assert _canonical(config).read_bytes() == canonical_before
    assert source_xml.read_bytes() == source_before


def test_second_unchanged_import_uses_fingerprints_without_full_file_hashes(
    tmp_path: Path, monkeypatch
):
    config, container, _ = _setup(tmp_path)
    image = Path(config.source.rom_root) / "ps2" / "images" / "Game.png"
    image.write_bytes(b"large-media" * 100_000)
    assert image.stat().st_size > 3 * 64 * 1024
    first = container.library_sync.sync()
    descriptor = _media_record(config)
    state = json.loads(
        (Path(config.data_path) / "library-sync-state.json").read_text()
    )

    assert descriptor["source_fingerprint"]["sample_sha256"]
    assert descriptor["blob_fingerprint"]["sample_sha256"]
    assert state["media_validation"]
    assert first.media_bytes_transferred == image.stat().st_size * 2

    monkeypatch.setattr(
        "romcloud.services.library_sync._hash_file",
        lambda path: (_ for _ in ()).throw(
            AssertionError(f"unchanged media was fully hashed: {path}")
        ),
    )
    second = container.library_sync.sync()

    assert second.media_transferred == 0
    assert second.media_bytes_transferred == 0
    assert second.media_hashed == 0
    assert second.media_examined == 2  # source/blob plus local rendered copy
    assert second.media_skipped == 2


def test_changed_source_media_is_rehashed_copied_and_fully_verified(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    image = Path(config.source.rom_root) / "ps2" / "images" / "Game.png"
    container.library_sync.sync()
    before = _media_record(config)
    old_blob = Path(config.remote_data.root) / "library" / before["blob"]
    replacement = b"new-media"
    assert len(replacement) == image.stat().st_size

    image.write_bytes(replacement)
    report = container.library_sync.sync()
    after = _media_record(config)
    new_blob = Path(config.remote_data.root) / "library" / after["blob"]

    assert after["sha256"] != before["sha256"]
    assert new_blob.read_bytes() == replacement
    assert old_blob.is_file()  # content-addressed history is never deleted
    assert report.media_hashed >= 3  # source plus both verified copies
    assert report.media_transferred == 2
    assert report.media_bytes_transferred == len(replacement) * 2


def test_changed_media_copy_verification_failure_preserves_canonical(
    tmp_path: Path, monkeypatch
):
    import romcloud.services.library_sync as library_sync_module

    config, container, _ = _setup(tmp_path)
    image = Path(config.source.rom_root) / "ps2" / "images" / "Game.png"
    container.library_sync.sync()
    canonical_before = _canonical(config).read_bytes()
    image.write_bytes(b"new-media")

    def corrupt_copy(source: Path, destination: Path):
        destination.write_bytes(b"x" * source.stat().st_size)

    monkeypatch.setattr(library_sync_module.shutil, "copyfile", corrupt_copy)

    with pytest.raises(LibrarySyncError, match="verification failed"):
        container.library_sync.sync()

    assert _canonical(config).read_bytes() == canonical_before
    assert not list(
        (Path(config.remote_data.root) / "library" / "media").rglob("*.partial")
    )


def test_missing_remote_blob_is_rebuilt_without_rehashing_unchanged_source(
    tmp_path: Path, monkeypatch
):
    import romcloud.services.library_sync as library_sync_module

    config, container, _ = _setup(tmp_path)
    image = Path(config.source.rom_root) / "ps2" / "images" / "Game.png"
    expected = image.read_bytes()
    container.library_sync.sync()
    descriptor = _media_record(config)
    blob = Path(config.remote_data.root) / "library" / descriptor["blob"]
    blob.unlink()
    real_hash = library_sync_module._hash_file
    hashed: list[Path] = []

    def recording_hash(path: Path):
        hashed.append(path)
        return real_hash(path)

    monkeypatch.setattr(library_sync_module, "_hash_file", recording_hash)
    report = container.library_sync.sync()

    assert blob.read_bytes() == expected
    assert image not in hashed
    assert report.media_transferred == 1
    assert report.media_bytes_transferred == len(expected)


def test_source_fingerprint_mismatch_falls_back_to_full_sha256(
    tmp_path: Path, monkeypatch
):
    import romcloud.services.library_sync as library_sync_module

    config, container, _ = _setup(tmp_path)
    image = Path(config.source.rom_root) / "ps2" / "images" / "Game.png"
    container.library_sync.sync()
    payload = json.loads(_canonical(config).read_text())
    descriptor = next(iter(payload["records"].values()))["media"]["image"]
    descriptor["source_fingerprint"]["mtime_ns"] = -1
    _canonical(config).write_text(json.dumps(payload), encoding="utf-8")
    real_hash = library_sync_module._hash_file
    hashed: list[Path] = []

    def recording_hash(path: Path):
        hashed.append(path)
        return real_hash(path)

    monkeypatch.setattr(library_sync_module, "_hash_file", recording_hash)
    report = container.library_sync.sync()

    assert image in hashed
    assert report.media_hashed == 1
    assert report.media_transferred == 0


def test_corrupt_same_size_remote_blob_is_detected_and_rebuilt(
    tmp_path: Path, monkeypatch
):
    import romcloud.services.library_sync as library_sync_module

    config, container, _ = _setup(tmp_path)
    image = Path(config.source.rom_root) / "ps2" / "images" / "Game.png"
    expected = image.read_bytes()
    container.library_sync.sync()
    descriptor = _media_record(config)
    blob = Path(config.remote_data.root) / "library" / descriptor["blob"]
    blob.write_bytes(b"x" * len(expected))
    real_hash = library_sync_module._hash_file
    hashed: list[Path] = []

    def recording_hash(path: Path):
        hashed.append(path)
        return real_hash(path)

    monkeypatch.setattr(library_sync_module, "_hash_file", recording_hash)
    report = container.library_sync.sync()

    assert image not in hashed
    assert blob in hashed
    assert blob.read_bytes() == expected
    assert report.media_transferred == 1
    assert report.media_hashed >= 2  # corrupt blob plus verified temporary


def test_local_gamelist_generation_uses_atomic_writer(tmp_path: Path, monkeypatch):
    config, container, _ = _setup(tmp_path)
    from romcloud.infrastructure.atomic_file import atomic_write_text as real_atomic

    written: list[Path] = []

    def recording_atomic(path: Path, content: str, **kwargs):
        written.append(path)
        return real_atomic(path, content, **kwargs)

    monkeypatch.setattr("romcloud.services.library_sync.atomic_write_text", recording_atomic)

    container.library_sync.sync()

    assert _local_root(config) in written
    assert not list(_local_root(config).parent.glob(".gamelist.xml.*"))


def test_additive_local_scrape_blank_and_conflict_policy(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    tree = ET.parse(_local_root(config))
    managed = next(item for item in tree.getroot().findall("game") if item.findtext(OWNERSHIP_TAG))
    managed.find("desc").text = ""  # blank must not delete canonical
    ET.SubElement(managed, "publisher").text = "New Publisher"
    managed.find("name").text = "Different Local Name"
    tree.write(_local_root(config), encoding="unicode")

    report = container.library_sync.sync()
    record = next(iter(json.loads(_canonical(config).read_text())["records"].values()))

    assert record["metadata"]["desc"] == "Canonical description"
    assert record["metadata"]["publisher"] == "New Publisher"
    assert record["metadata"]["name"] == "Scraped Game"
    assert any("name" in conflict for conflict in report.conflicts)
    rendered = _managed(_local_root(config))
    assert rendered.findtext("name") == "Scraped Game"
    assert rendered.findtext("publisher") == "New Publisher"


def test_local_media_is_added_incrementally_and_never_destructively_removed(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    videos = Path(config.local_roms_path) / "ps2" / "videos"
    videos.mkdir()
    (videos / "local.mp4").write_bytes(b"local-video")
    tree = ET.parse(_local_root(config))
    managed = next(item for item in tree.getroot().findall("game") if item.findtext(OWNERSHIP_TAG))
    ET.SubElement(managed, "video").text = "./videos/local.mp4"
    tree.write(_local_root(config), encoding="unicode")

    added = container.library_sync.sync()
    record = next(iter(json.loads(_canonical(config).read_text())["records"].values()))
    assert added.media_transferred >= 1
    assert record["media"]["video"]["sha256"]

    # A later device-local omission cannot remove the canonical media.
    tree = ET.parse(_local_root(config))
    managed = next(item for item in tree.getroot().findall("game") if item.findtext(OWNERSHIP_TAG))
    managed.remove(managed.find("video"))
    tree.write(_local_root(config), encoding="unicode")
    container.library_sync.sync()
    record = next(iter(json.loads(_canonical(config).read_text())["records"].values()))
    assert "video" in record["media"]


def test_second_device_pull_uses_same_identity_and_does_not_write_remote(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    remote_before = _canonical(config).read_bytes()

    device_b_root = tmp_path / "device-b"
    (device_b_root / "roms" / "ps2").mkdir(parents=True)
    (device_b_root / "data").mkdir()
    (device_b_root / "cache").mkdir()
    device_b = replace(
        config,
        local_roms_path=str(device_b_root / "roms"),
        data_path=str(device_b_root / "data"),
        cache=replace(config.cache, path=str(device_b_root / "cache")),
    )
    second = Container(device_b)
    assert second.catalog.refresh().added == 1

    report = second.library_sync.pull()

    assert report.rendered == 1
    assert _managed(Path(device_b.local_roms_path) / "ps2" / "gamelist.xml").findtext("name") == "Scraped Game"
    assert _canonical(config).read_bytes() == remote_before


def test_direct_paths_and_mode_switch_do_not_change_canonical(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    canonical_before = _canonical(config).read_bytes()

    direct = replace(config, game_access_mode="direct_nas")
    write_operating_mode(direct, OperatingMode.CONNECTED)
    Container(direct).library_sync.sync()
    direct_entry = _managed(_local_root(config))
    assert direct_entry.findtext("path") == "./ROMCloud/Game.chd"
    assert direct_entry.findtext("image") == "./ROMCloud/images/Game.png"
    assert _canonical(config).read_bytes() == canonical_before

    write_operating_mode(config, OperatingMode.CACHE)
    Container(config).library_sync.sync()
    assert _managed(_local_root(config)).findtext("path") == "./Game.romcloud"
    assert _canonical(config).read_bytes() == canonical_before


def test_direct_device_references_source_media_without_local_duplication(tmp_path: Path):
    config, container, _ = _setup(tmp_path, mode="direct_nas")

    container.library_sync.sync()

    managed = _managed(_local_root(config))
    assert managed.findtext("path") == "./ROMCloud/Game.chd"
    assert managed.findtext("image") == "./ROMCloud/images/Game.png"
    assert not (Path(config.local_roms_path) / "ps2" / ".romcloud-media").exists()


def test_offline_mode_remains_cached_only_and_does_not_recreate_proxy(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    game = container.game_repo.list_all()[0]
    proxy = container.proxy_repo.get(game.id)
    assert proxy is not None
    Path(proxy.proxy_path).unlink()
    write_offline_library_state(config, True)

    container.library_sync.sync()

    assert not Path(proxy.proxy_path).exists()
    root = ET.parse(_local_root(config)).getroot()
    assert not any(item.findtext(OWNERSHIP_TAG) for item in root.findall("game"))
    assert any(item.findtext("name") == "User-owned local game" for item in root.findall("game"))


def test_remove_local_touches_only_owned_entries_and_never_media_trees(tmp_path: Path):
    config, container, _ = _setup(tmp_path)
    container.library_sync.sync()
    media_files = list((Path(config.local_roms_path) / "ps2" / ".romcloud-media").rglob("*"))

    assert container.library_sync.remove_local_metadata() == 1

    root = ET.parse(_local_root(config)).getroot()
    assert [item.findtext("name") for item in root.findall("game")] == ["User-owned local game"]
    assert all(path.exists() for path in media_files)
    assert _canonical(config).is_file()


def test_malformed_local_xml_and_unsafe_media_fail_safely(tmp_path: Path):
    config, container, source_xml = _setup(tmp_path)
    _local_root(config).write_text("<broken", encoding="utf-8")
    before = _local_root(config).read_bytes()

    report = container.library_sync.sync()

    assert _local_root(config).read_bytes() == before
    assert source_xml.is_file()
    assert any("malformed" in failure.lower() for failure in report.failures)
    record = next(iter(json.loads(_canonical(config).read_text())["records"].values()))
    assert "manual" not in record["media"]


def test_library_identity_ignores_device_id_root_and_launch_path():
    first = Game.create("ps2", "One", "local", "/mnt/a", [GameAsset("Game.chd", "ps2/Game.chd", is_primary=True)])
    second = Game.create("ps2", "Renamed", "local", "/different/mount", [GameAsset("Game.chd", "ps2/Game.chd", is_primary=True)])

    assert first.id != second.id
    assert library_id_for_game(first) == library_id_for_game(second)
