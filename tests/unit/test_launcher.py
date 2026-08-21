"""Tests for integrations/batocera/launcher.py

Key invariants proved here:

1. A normal (non-.romcloud) argv reaches its native launcher byte-for-byte
   unchanged after the ROMCloud wrapper argv[0] is removed.

2. A .romcloud argv reaches its native launcher differing from the normal
   argv in *exactly one element*: the value immediately following "-rom".
   Every other argument — %CONTROLLERSCONFIG%, -system, -gameinfoxml,
   -systemname, their positions, and any unknown future args — is identical.

3. The argv helper functions are pure: no I/O, no side effects.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import call, patch

import pytest

from romcloud.integrations.batocera.launcher import (
    EmulatorLauncher,
    find_rom_path,
    is_romcloud_proxy,
    replace_rom_path,
)
from romcloud.core.exceptions import LaunchError

# ── representative Batocera 42 argv ───────────────────────────────────────────

_WRAPPER = "/userdata/system/romcloud/bin/romcloud-run"
_CTRLS = "/userdata/system/configs/emulationstation/.cec.cfg"
_GAMEINFOXML = "/tmp/es_gamelists/snes/gamelist.xml"
_SYSTEMNAME = "Super Nintendo Entertainment System"

# Argv exactly as EmulationStation would produce it (normal ROM, no ROMCloud)
_NORMAL_ARGV: list[str] = [
    _WRAPPER,
    "emulatorlauncher",
    _CTRLS,
    "-system", "snes",
    "-rom", "/userdata/roms/snes/Chrono Trigger.sfc",
    "-gameinfoxml", _GAMEINFOXML,
    "-systemname", _SYSTEMNAME,
]

# Same argv but with a .romcloud proxy as the -rom value
_ROMCLOUD_ARGV: list[str] = [
    _WRAPPER,
    "emulatorlauncher",
    _CTRLS,
    "-system", "snes",
    "-rom", "/userdata/roms/snes/Chrono Trigger.romcloud",
    "-gameinfoxml", _GAMEINFOXML,
    "-systemname", _SYSTEMNAME,
]

_CACHED_ROM = "/userdata/romcloud/cache/snes/abc123/Chrono Trigger.sfc"

# Expected argv[0] that reaches emulatorlauncher
_EL = "emulatorlauncher"


# ── find_rom_path ─────────────────────────────────────────────────────────────


class TestFindRomPath:
    def test_finds_rom_value(self):
        assert find_rom_path(_NORMAL_ARGV) == "/userdata/roms/snes/Chrono Trigger.sfc"

    def test_finds_romcloud_value(self):
        assert find_rom_path(_ROMCLOUD_ARGV) == "/userdata/roms/snes/Chrono Trigger.romcloud"

    def test_returns_none_when_absent(self):
        assert find_rom_path([_WRAPPER, _CTRLS, "-system", "snes"]) is None

    def test_returns_none_for_empty_argv(self):
        assert find_rom_path([]) is None

    def test_returns_none_when_rom_is_last_arg(self):
        # -rom at the end with no following value
        assert find_rom_path([_WRAPPER, "-rom"]) is None

    def test_first_occurrence_wins(self):
        # Two -rom args — first one wins
        argv = [_WRAPPER, "-rom", "first.sfc", "-rom", "second.sfc"]
        assert find_rom_path(argv) == "first.sfc"

    def test_does_not_match_rom_prefix(self):
        # -romfoo is not -rom
        assert find_rom_path([_WRAPPER, "-romfoo", "game.sfc"]) is None

    def test_real_batocera42_argv(self):
        assert find_rom_path(_NORMAL_ARGV) is not None
        assert find_rom_path(_ROMCLOUD_ARGV) is not None


# ── replace_rom_path ──────────────────────────────────────────────────────────


class TestReplaceRomPath:
    def test_replaces_rom_value(self):
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        assert find_rom_path(result) == _CACHED_ROM

    def test_length_unchanged(self):
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        assert len(result) == len(_ROMCLOUD_ARGV)

    def test_only_rom_value_differs_from_original(self):
        """Core invariant: replacing -rom changes exactly one element."""
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        differences = [
            (i, orig, new)
            for i, (orig, new) in enumerate(zip(_ROMCLOUD_ARGV, result))
            if orig != new
        ]
        assert len(differences) == 1
        idx, orig_val, new_val = differences[0]
        # The changed element must be the value after -rom
        assert _ROMCLOUD_ARGV[idx - 1] == "-rom"
        assert new_val == _CACHED_ROM

    def test_all_other_args_identical_to_normal_argv(self):
        """After replacing -rom, result matches _NORMAL_ARGV in every position."""
        # _ROMCLOUD_ARGV and _NORMAL_ARGV differ only in the -rom value
        result = replace_rom_path(_ROMCLOUD_ARGV, "/userdata/roms/snes/Chrono Trigger.sfc")
        assert result == _NORMAL_ARGV

    def test_controllersconfig_preserved(self):
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        assert result[2] == _CTRLS

    def test_system_arg_preserved(self):
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        assert "-system" in result
        idx = result.index("-system")
        assert result[idx + 1] == "snes"

    def test_gameinfoxml_preserved(self):
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        idx = result.index("-gameinfoxml")
        assert result[idx + 1] == _GAMEINFOXML

    def test_systemname_preserved(self):
        result = replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        idx = result.index("-systemname")
        assert result[idx + 1] == _SYSTEMNAME

    def test_noop_when_no_rom_arg(self):
        argv = [_WRAPPER, "-system", "snes"]
        result = replace_rom_path(argv, _CACHED_ROM)
        assert result == argv

    def test_does_not_mutate_input(self):
        original = list(_ROMCLOUD_ARGV)
        replace_rom_path(_ROMCLOUD_ARGV, _CACHED_ROM)
        assert _ROMCLOUD_ARGV == original

    def test_unknown_future_args_preserved(self):
        """Unknown args added by a future Batocera version must survive unchanged."""
        argv = list(_ROMCLOUD_ARGV) + ["-newoption", "newvalue"]
        result = replace_rom_path(argv, _CACHED_ROM)
        assert result[-2] == "-newoption"
        assert result[-1] == "newvalue"


# ── is_romcloud_proxy ─────────────────────────────────────────────────────────


class TestIsRomcloudProxy:
    def test_romcloud_extension(self):
        assert is_romcloud_proxy("/roms/snes/Game.romcloud") is True

    def test_uppercase_extension(self):
        assert is_romcloud_proxy("/roms/snes/Game.ROMCLOUD") is True

    def test_mixed_case(self):
        assert is_romcloud_proxy("/roms/snes/Game.RomCloud") is True

    def test_regular_rom(self):
        assert is_romcloud_proxy("/roms/snes/Game.sfc") is False

    def test_no_extension(self):
        assert is_romcloud_proxy("/roms/snes/BCES00000") is False

    def test_romcloud_in_directory_name(self):
        # Extension is .sfc, not .romcloud — must return False
        assert is_romcloud_proxy("/roms/romcloud/Game.sfc") is False

    def test_empty_string(self):
        assert is_romcloud_proxy("") is False


# ── EmulatorLauncher ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_launcher(tmp_path):
    """A real executable on PATH for shutil.which to find."""
    script = tmp_path / (
        "emulatorlauncher.cmd" if os.name == "nt" else "emulatorlauncher"
    )
    script.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    script.chmod(0o755)
    return str(script), str(tmp_path)


class TestEmulatorLauncherPassthrough:
    def test_passthrough_replaces_argv0_only(self, monkeypatch, fake_launcher):
        script_path, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        captured = {}

        def fake_execvp(file, args):
            captured["file"] = file
            captured["args"] = list(args)

        monkeypatch.setattr("os.execvp", fake_execvp)

        EmulatorLauncher().exec_passthrough(_NORMAL_ARGV)

        assert captured["args"][0] == _EL
        assert captured["args"] == _NORMAL_ARGV[1:]

    def test_passthrough_argv_identical_to_original_minus_wrapper_name(
        self, monkeypatch, fake_launcher
    ):
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        captured_args = []
        monkeypatch.setattr("os.execvp", lambda f, a: captured_args.extend(a))

        EmulatorLauncher().exec_passthrough(_NORMAL_ARGV)

        # Only ROMCloud's wrapper argv[0] is removed.
        assert captured_args == _NORMAL_ARGV[1:]

    def test_passthrough_not_found_raises_launch_error(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")
        with pytest.raises(LaunchError, match="native launcher executable not found"):
            EmulatorLauncher().exec_passthrough(_NORMAL_ARGV)


class TestEmulatorLauncherWithRom:
    def test_xbox_iso_with_spaces_is_one_extension_preserving_argument(
        self, monkeypatch, fake_launcher
    ):
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        xbox_argv = [
            _WRAPPER, "emulatorlauncher", "-system", "xbox", "-rom",
            "/userdata/roms/xbox/Aeon Flux.romcloud",
        ]
        cached_iso = "/userdata/romcloud/cache/xbox/Aeon Flux.iso"
        captured = {}
        monkeypatch.setattr("os.execvp", lambda f, a: captured.update(args=list(a)))

        EmulatorLauncher().exec_with_rom(xbox_argv, cached_iso)

        assert find_rom_path(captured["args"]) == cached_iso
        assert cached_iso in captured["args"]
        assert "Aeon" not in captured["args"]
        assert "Flux.iso" not in captured["args"]

    def test_exec_with_rom_passes_cached_path(self, monkeypatch, fake_launcher):
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        captured = {}
        monkeypatch.setattr("os.execvp", lambda f, a: captured.update(file=f, args=list(a)))

        EmulatorLauncher().exec_with_rom(_ROMCLOUD_ARGV, _CACHED_ROM)

        assert find_rom_path(captured["args"]) == _CACHED_ROM

    def test_exec_with_rom_argv0_is_emulatorlauncher(self, monkeypatch, fake_launcher):
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        captured = {}
        monkeypatch.setattr("os.execvp", lambda f, a: captured.update(args=list(a)))

        EmulatorLauncher().exec_with_rom(_ROMCLOUD_ARGV, _CACHED_ROM)

        assert captured["args"][0] == _EL

    def test_exec_with_rom_only_rom_value_differs_from_passthrough(
        self, monkeypatch, fake_launcher
    ):
        """Core property: exec_with_rom and exec_passthrough produce argv vectors
        that differ in exactly one element — the -rom value."""
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        passthrough_args: list[str] = []
        with_rom_args: list[str] = []

        def capture_passthrough(f, a):
            passthrough_args.extend(a)

        def capture_with_rom(f, a):
            with_rom_args.extend(a)

        monkeypatch.setattr("os.execvp", capture_passthrough)
        EmulatorLauncher().exec_passthrough(_NORMAL_ARGV)

        monkeypatch.setattr("os.execvp", capture_with_rom)
        EmulatorLauncher().exec_with_rom(_ROMCLOUD_ARGV, _CACHED_ROM)

        assert len(passthrough_args) == len(with_rom_args)

        differences = [
            (i, p, w)
            for i, (p, w) in enumerate(zip(passthrough_args, with_rom_args))
            if p != w
        ]
        assert len(differences) == 1, (
            f"Expected exactly 1 difference, found {len(differences)}: {differences}"
        )
        idx, orig, new = differences[0]
        assert with_rom_args[idx - 1] == "-rom"
        assert new == _CACHED_ROM
        assert orig == "/userdata/roms/snes/Chrono Trigger.sfc"

    def test_exec_with_rom_controllersconfig_preserved(self, monkeypatch, fake_launcher):
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        captured = {}
        monkeypatch.setattr("os.execvp", lambda f, a: captured.update(args=list(a)))

        EmulatorLauncher().exec_with_rom(_ROMCLOUD_ARGV, _CACHED_ROM)

        # %CONTROLLERSCONFIG% value must be unchanged (it's argv[1] in our fixture)
        assert captured["args"][1] == _CTRLS

    def test_exec_with_rom_not_found_raises_launch_error(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")
        with pytest.raises(LaunchError, match="native launcher executable not found"):
            EmulatorLauncher().exec_with_rom(_ROMCLOUD_ARGV, _CACHED_ROM)

    def test_exec_with_rom_unknown_future_args_preserved(self, monkeypatch, fake_launcher):
        """Future Batocera argv additions must not be dropped."""
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        future_argv = list(_ROMCLOUD_ARGV) + ["-newfeature", "somevalue"]
        captured = {}
        monkeypatch.setattr("os.execvp", lambda f, a: captured.update(args=list(a)))

        EmulatorLauncher().exec_with_rom(future_argv, _CACHED_ROM)

        assert "-newfeature" in captured["args"]
        assert "somevalue" in captured["args"]

    def test_custom_python_launcher_and_every_argument_are_preserved(self, monkeypatch):
        script = "/userdata/system/switch/configgen/switchlauncher.py"
        proxy = "/userdata/roms/switch/Metroid Dread.romcloud"
        cached = "/userdata/romcloud/cache/switch/Metroid Dread.xci"
        argv = [
            _WRAPPER,
            "python",
            script,
            _CTRLS,
            "-gameinfoxml",
            "/tmp/game info/switch.xml",
            "-system",
            "switch",
            "-rom",
            proxy,
            "-emulator",
            "yuzu",
            "-systemname",
            "Nintendo Switch",
            "--future-option",
            "value with spaces",
        ]
        captured = {}
        monkeypatch.setattr(
            "romcloud.integrations.batocera.launcher.shutil.which",
            lambda command: "/usr/bin/python" if command == "python" else None,
        )
        monkeypatch.setattr(
            "os.execvp", lambda file, args: captured.update(file=file, args=list(args))
        )

        EmulatorLauncher().exec_with_rom(argv, cached)

        expected = list(argv[1:])
        expected[expected.index(proxy)] = cached
        assert captured == {"file": "/usr/bin/python", "args": expected}


# ── _transfer_with_progress: graphical > curses > plain fallback order ───────


class _FakeConfig:
    def __init__(self, data_path="/userdata/system/romcloud/data"):
        self.data_path = data_path


class _FakeCache:
    def __init__(self, path="/cache/game.iso"):
        self.path = path
        self.cache_game_calls = 0

    def cache_game(self, game_id, on_progress=None):
        self.cache_game_calls += 1
        return self.path


class _FakeContainer:
    def __init__(self, cache):
        self.cache = cache


class TestTransferWithProgress:
    def test_uses_graphical_progress_when_available(self, monkeypatch):
        import romcloud.integrations.batocera.launcher as launcher_module
        import romcloud.ui.graphical_progress as gp

        monkeypatch.setattr(gp, "graphical_progress_binary", lambda config: Path("/bin/romcloud-launch-progress"))
        called = {}

        def fake_run(cache_service, game, *, launcher_bin):
            called["launcher_bin"] = launcher_bin
            return "/cache/graphical.iso"

        monkeypatch.setattr(gp, "run_graphical_progress_transfer", fake_run)

        cache = _FakeCache()
        container = _FakeContainer(cache)
        result = launcher_module._transfer_with_progress(container, _FakeConfig(), object())

        assert result == "/cache/graphical.iso"
        assert called["launcher_bin"] == Path("/bin/romcloud-launch-progress")
        assert cache.cache_game_calls == 0

    def test_falls_back_to_curses_when_graphical_unavailable_and_isatty(self, monkeypatch):
        import romcloud.integrations.batocera.launcher as launcher_module
        import romcloud.ui.graphical_progress as gp
        import romcloud.ui.progress as progress_module

        monkeypatch.setattr(gp, "graphical_progress_binary", lambda config: None)
        monkeypatch.setattr(launcher_module.sys.stdout, "isatty", lambda: True)
        called = {}

        def fake_curses(cache_service, game):
            called["curses"] = True
            return "/cache/curses.iso"

        monkeypatch.setattr(progress_module, "run_progress_transfer", fake_curses)

        cache = _FakeCache()
        container = _FakeContainer(cache)
        result = launcher_module._transfer_with_progress(container, _FakeConfig(), object())

        assert result == "/cache/curses.iso"
        assert called.get("curses") is True
        assert cache.cache_game_calls == 0

    def test_falls_back_to_plain_when_no_ui_available(self, monkeypatch):
        import romcloud.integrations.batocera.launcher as launcher_module
        import romcloud.ui.graphical_progress as gp

        monkeypatch.setattr(gp, "graphical_progress_binary", lambda config: None)
        monkeypatch.setattr(launcher_module.sys.stdout, "isatty", lambda: False)

        cache = _FakeCache(path="/cache/plain.iso")
        container = _FakeContainer(cache)
        game = type("_Game", (), {"id": "game-1"})()
        result = launcher_module._transfer_with_progress(container, _FakeConfig(), game)

        assert result == "/cache/plain.iso"
        assert cache.cache_game_calls == 1

    def test_graphical_unavailable_error_falls_back_instead_of_failing(self, monkeypatch):
        import romcloud.integrations.batocera.launcher as launcher_module
        import romcloud.ui.graphical_progress as gp

        monkeypatch.setattr(gp, "graphical_progress_binary", lambda config: Path("/bin/romcloud-launch-progress"))

        def fake_run(cache_service, game, *, launcher_bin):
            raise gp.GraphicalProgressUnavailable("broken install")

        monkeypatch.setattr(gp, "run_graphical_progress_transfer", fake_run)
        monkeypatch.setattr(launcher_module.sys.stdout, "isatty", lambda: False)

        cache = _FakeCache(path="/cache/fallback.iso")
        container = _FakeContainer(cache)
        game = type("_Game", (), {"id": "game-1"})()
        result = launcher_module._transfer_with_progress(container, _FakeConfig(), game)

        assert result == "/cache/fallback.iso"
        assert cache.cache_game_calls == 1


class TestCacheMissLaunchResolution:
    def test_xbox_nested_iso_reaches_launcher_as_file_not_recorded_container(
        self, tmp_path, monkeypatch
    ):
        import romcloud.bootstrap.container as container_module
        import romcloud.infrastructure.config as config_module
        import romcloud.integrations.batocera.launcher as launcher_module
        import romcloud.ui.graphical_progress as gp
        from romcloud.core.models.cache import CachePolicy
        from romcloud.core.models.game import Game, GameAsset
        from romcloud.infrastructure.config import (
            AppConfig,
            CacheConfig,
            SourceConfig,
        )
        from romcloud.infrastructure.database import Database
        from romcloud.infrastructure.repositories.cache import CacheRepository
        from romcloud.infrastructure.repositories.game import GameRepository
        from romcloud.services.cache import CacheService

        cache_root = tmp_path / "cache"
        data_root = tmp_path / "data"
        source_root = tmp_path / "source"
        cache_root.mkdir()
        source_root.mkdir()
        config = AppConfig(
            source=SourceConfig(provider="local", rom_root=str(source_root)),
            cache=CacheConfig(path=str(cache_root)),
            local_roms_path=str(tmp_path / "roms"),
            data_path=str(data_root),
        )
        database = Database(str(data_root / "catalog.db"))
        database.initialize()
        game_repo = GameRepository(database)
        cache_repo = CacheRepository(database)
        relative_path = "xbox/Airforce Delta Storm/Airforce Delta Storm.iso"
        game = Game.create(
            system="xbox",
            title="Airforce Delta Storm",
            source_provider="local",
            source_root=str(source_root),
            assets=[
                GameAsset(
                    filename="Airforce Delta Storm.iso",
                    relative_path=relative_path,
                    size_bytes=3,
                    is_primary=True,
                )
            ],
        )
        game_repo.save(game)

        class LegacyContainerTransfer:
            def transfer(self, transferred_game, on_progress=None):
                iso_path = cache_root / relative_path
                iso_path.parent.mkdir(parents=True)
                iso_path.write_bytes(b"iso")
                return str(iso_path.parent)

        cache = CacheService(
            cache_repo=cache_repo,
            game_repo=game_repo,
            transfer_service=LegacyContainerTransfer(),
            cache_root=str(cache_root),
            policy=CachePolicy.from_gb(1.0, 0.0),
        )

        class FakeCatalog:
            def resolve_proxy(self, proxy_path):
                return game

        class FakeProvider:
            def is_reachable(self, root):
                return True

        container = type(
            "FakeContainer",
            (),
            {"catalog": FakeCatalog(), "provider": FakeProvider(), "cache": cache},
        )()
        monkeypatch.setattr(config_module, "load_config", lambda: config)
        monkeypatch.setattr(container_module, "Container", lambda loaded: container)
        monkeypatch.setattr(gp, "graphical_progress_binary", lambda loaded: None)
        monkeypatch.setattr(launcher_module.sys.stdout, "isatty", lambda: False)

        result = launcher_module._resolve_and_cache(
            "/userdata/roms/xbox/Airforce Delta Storm.romcloud"
        )

        expected = cache_root / relative_path
        assert result == str(expected)
        assert expected.is_file()
        assert Path(cache.get_entry(game.id).cache_path).is_dir()


# ── stale persisted Game.source_root must never affect launch resolution ────


class TestStaleSourceRootResolution:
    """Real-hardware regression: a game catalogued before a source-path
    change (e.g. the legacy `/userdata/romcloud-source` -> current
    `/userdata/romcloud/source` runtime-layout migration) keeps its
    historical `source_root` in the DB forever unless `romcloud refresh`
    runs. Launch-time resolution must always use the *currently configured*
    source root, never that persisted value — no catalog refresh required."""

    def _build_config(self, tmp_path, *, current_source_name="source"):
        from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig

        current_source = tmp_path / current_source_name
        cache_root = tmp_path / "cache"
        local_roms = tmp_path / "roms"
        for root in (current_source / "snes", cache_root, local_roms / "snes"):
            root.mkdir(parents=True)
        config = AppConfig(
            source=SourceConfig("local", str(current_source)),
            cache=CacheConfig(str(cache_root)),
            local_roms_path=str(local_roms),
            data_path=str(tmp_path / "data"),
        )
        return config, current_source

    def _catalog_legacy_game(self, tmp_path, config, legacy_source_root: Path):
        from romcloud.core.models.game import Game, GameAsset
        from romcloud.infrastructure.database import Database
        from romcloud.infrastructure.repositories.game import GameRepository

        database = Database(str(Path(config.data_path) / "catalog.db"))
        database.initialize()
        game_repo = GameRepository(database)
        filename = "Chrono Trigger.sfc"
        # size_bytes left unknown: these tests are about source-root path
        # resolution, not size validation, and use content of varying length.
        asset = GameAsset(
            filename=filename, relative_path=f"snes/{filename}", size_bytes=None, is_primary=True
        )
        # Persisted with the legacy root, exactly as an already-catalogued
        # game would be after a source-path migration — never re-refreshed.
        game = Game.create("snes", "Chrono Trigger", "local", str(legacy_source_root), [asset])
        game_repo.save(game)

        proxy_path = Path(config.local_roms_path) / "snes" / "Chrono Trigger.romcloud"
        proxy_path.write_text(json.dumps({"game_id": game.id}), encoding="utf-8")
        return game, proxy_path

    def _run(self, tmp_path, monkeypatch, config, proxy_path):
        import romcloud.infrastructure.config as config_module
        import romcloud.integrations.batocera.launcher as launcher_module
        import romcloud.ui.graphical_progress as gp

        monkeypatch.setattr(config_module, "load_config", lambda: config)
        monkeypatch.setattr(gp, "graphical_progress_binary", lambda cfg: None)
        monkeypatch.setattr(launcher_module.sys.stdout, "isatty", lambda: False)
        return launcher_module._resolve_and_cache(str(proxy_path))

    def test_uncached_legacy_game_transfers_from_current_source_without_refresh(
        self, tmp_path, monkeypatch
    ):
        config, current_source = self._build_config(tmp_path)
        legacy_source = tmp_path / "romcloud-source"  # never mounted/created
        game, proxy_path = self._catalog_legacy_game(tmp_path, config, legacy_source)
        rom = current_source / "snes" / "Chrono Trigger.sfc"
        rom.write_bytes(b"data")

        result = self._run(tmp_path, monkeypatch, config, proxy_path)

        expected = Path(config.cache.path) / "snes" / "Chrono Trigger.sfc"
        assert result == str(expected)
        assert expected.read_bytes() == b"data"

    def test_custom_nonstandard_current_source_root_still_works(
        self, tmp_path, monkeypatch
    ):
        config, current_source = self._build_config(
            tmp_path, current_source_name="my-custom-nas-mount"
        )
        legacy_source = tmp_path / "romcloud-source"
        game, proxy_path = self._catalog_legacy_game(tmp_path, config, legacy_source)
        rom = current_source / "snes" / "Chrono Trigger.sfc"
        rom.write_bytes(b"custom-root-data")

        result = self._run(tmp_path, monkeypatch, config, proxy_path)

        expected = Path(config.cache.path) / "snes" / "Chrono Trigger.sfc"
        assert result == str(expected)
        assert expected.read_bytes() == b"custom-root-data"

    def test_stale_source_root_never_reports_false_unavailable_error(
        self, tmp_path, monkeypatch
    ):
        config, current_source = self._build_config(tmp_path)
        # A legacy root that not only differs but doesn't exist on disk at
        # all — the strongest possible stand-in for a stale historical path.
        legacy_source = tmp_path / "romcloud-source"
        game, proxy_path = self._catalog_legacy_game(tmp_path, config, legacy_source)
        rom = current_source / "snes" / "Chrono Trigger.sfc"
        rom.write_bytes(b"data")

        # Must not raise: the current source root is reachable even though
        # the persisted (legacy) source_root path never existed.
        result = self._run(tmp_path, monkeypatch, config, proxy_path)
        assert Path(result).read_bytes() == b"data"

    def test_unavailable_error_names_current_root_not_stale_historical_root(
        self, tmp_path, monkeypatch
    ):
        from romcloud.core.exceptions import GameNotCachedError
        from romcloud.infrastructure.config import AppConfig, SourceConfig

        config, _current_source = self._build_config(tmp_path)
        legacy_source = tmp_path / "romcloud-source"
        game, proxy_path = self._catalog_legacy_game(tmp_path, config, legacy_source)

        # The *current* configured root is genuinely unavailable here.
        unreachable_root = tmp_path / "does-not-exist-actually"
        unreachable_config = AppConfig(
            source=SourceConfig("local", str(unreachable_root)),
            cache=config.cache,
            local_roms_path=config.local_roms_path,
            data_path=config.data_path,
        )
        with pytest.raises(GameNotCachedError) as exc_info:
            self._run(tmp_path, monkeypatch, unreachable_config, proxy_path)
        assert str(unreachable_root) in str(exc_info.value)
        assert "romcloud-source" not in str(exc_info.value)

    def test_cached_game_launches_locally_regardless_of_stale_source_root(
        self, tmp_path, monkeypatch
    ):
        from romcloud.core.models.cache import CacheEntry, CacheStatus
        from romcloud.infrastructure.repositories.cache import CacheRepository
        from romcloud.infrastructure.database import Database

        config, _current_source = self._build_config(tmp_path)
        legacy_source = tmp_path / "romcloud-source"  # never created
        game, proxy_path = self._catalog_legacy_game(tmp_path, config, legacy_source)

        database = Database(str(Path(config.data_path) / "catalog.db"))
        cache_repo = CacheRepository(database)
        cached_path = Path(config.cache.path) / "snes" / "Chrono Trigger.sfc"
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(b"already-cached")
        entry = CacheEntry.create(game.id, str(cached_path))
        entry.status = CacheStatus.COMPLETE
        entry.size_bytes = len(b"already-cached")
        cache_repo.save(entry)

        result = self._run(tmp_path, monkeypatch, config, proxy_path)

        assert result == str(cached_path)

    def test_offline_mode_never_touches_source_for_valid_cached_game(
        self, tmp_path, monkeypatch
    ):
        from romcloud.core.capabilities import OperatingMode
        from romcloud.core.models.cache import CacheEntry, CacheStatus
        from romcloud.infrastructure.database import Database
        from romcloud.infrastructure.library_view import write_operating_mode
        from romcloud.infrastructure.repositories.cache import CacheRepository

        config, _current_source = self._build_config(tmp_path)
        legacy_source = tmp_path / "romcloud-source"
        game, proxy_path = self._catalog_legacy_game(tmp_path, config, legacy_source)
        write_operating_mode(config, OperatingMode.OFFLINE)

        database = Database(str(Path(config.data_path) / "catalog.db"))
        cache_repo = CacheRepository(database)
        cached_path = Path(config.cache.path) / "snes" / "Chrono Trigger.sfc"
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(b"offline-cached")
        entry = CacheEntry.create(game.id, str(cached_path))
        entry.status = CacheStatus.COMPLETE
        entry.size_bytes = len(b"offline-cached")
        cache_repo.save(entry)

        import romcloud.core.storage as storage_module

        def _fail_reachable(self, root):
            raise AssertionError("Offline Mode must never check source reachability")

        monkeypatch.setattr(storage_module.StorageProvider, "is_reachable", _fail_reachable)

        result = self._run(tmp_path, monkeypatch, config, proxy_path)

        assert result == str(cached_path)


# ── launch must never change operating mode or trigger presentation work ────


class TestLaunchModeIsolation:
    """Launching a game may resolve/cache/mark-launched, but must never
    persist a mode change or reconcile global library/ES presentation —
    that is exclusively the job of an explicit user mode-selection action
    (`set_operating_mode`), never a side effect of a launch."""

    def _build_cached_game(self, tmp_path):
        from romcloud.core.models.cache import CacheEntry, CacheStatus
        from romcloud.core.models.game import Game, GameAsset
        from romcloud.infrastructure.config import AppConfig, CacheConfig, SourceConfig
        from romcloud.infrastructure.database import Database
        from romcloud.infrastructure.repositories.cache import CacheRepository
        from romcloud.infrastructure.repositories.game import GameRepository
        from romcloud.infrastructure.repositories.proxy import ProxyRepository
        from romcloud.core.models.proxy import ProxyRecord
        from romcloud.lifecycle.manage import restore_owned_proxies

        source_root = tmp_path / "source"
        cache_root = tmp_path / "cache"
        local_roms = tmp_path / "roms"
        for root in (source_root / "snes", cache_root, local_roms / "snes"):
            root.mkdir(parents=True)
        config = AppConfig(
            source=SourceConfig("local", str(source_root)),
            cache=CacheConfig(str(cache_root)),
            local_roms_path=str(local_roms),
            data_path=str(tmp_path / "data"),
        )
        database = Database(str(Path(config.data_path) / "catalog.db"))
        database.initialize()
        game_repo = GameRepository(database)
        cache_repo = CacheRepository(database)
        filename = "Chrono Trigger.sfc"
        asset = GameAsset(
            filename=filename, relative_path=f"snes/{filename}", size_bytes=4, is_primary=True
        )
        game = Game.create("snes", "Chrono Trigger", "local", str(source_root), [asset])
        game_repo.save(game)
        cached_path = cache_root / "snes" / filename
        cached_path.parent.mkdir(parents=True, exist_ok=True)
        cached_path.write_bytes(b"data")
        entry = CacheEntry.create(game.id, str(cached_path))
        entry.status = CacheStatus.COMPLETE
        entry.size_bytes = 4
        cache_repo.save(entry)
        proxy_path = local_roms / "snes" / "Chrono Trigger.romcloud"
        ProxyRepository(database).save(
            ProxyRecord.create(game.id, str(proxy_path))
        )
        restore_owned_proxies(config, game_ids={game.id})
        return config, game, cached_path

    def test_cached_launch_never_changes_mode_or_reconciles_presentation(
        self, tmp_path, monkeypatch
    ):
        import romcloud.infrastructure.config as config_module
        import romcloud.integrations.batocera.launcher as launcher_module
        from romcloud.core.capabilities import OperatingMode
        from romcloud.infrastructure.library_view import operating_mode, state_path
        from romcloud.integrations.batocera import game_access, presentation
        from romcloud.integrations.batocera.game_access import set_operating_mode

        config, game, cached_path = self._build_cached_game(tmp_path)
        monkeypatch.setattr(game_access, "_refresh_emulationstation", lambda *a, **k: None)
        monkeypatch.setattr(game_access, "_reload_emulationstation", lambda: False)
        set_operating_mode(config, OperatingMode.CACHE)
        before_state = state_path(config).read_text()

        def _fail(*args, **kwargs):
            raise AssertionError("launch must never reconcile mode/presentation")

        monkeypatch.setattr(game_access, "set_operating_mode", _fail)
        monkeypatch.setattr(game_access, "reconcile_game_access", _fail)
        monkeypatch.setattr(presentation, "refresh_emulationstation", _fail)
        monkeypatch.setattr(presentation, "reload_emulationstation", _fail)
        monkeypatch.setattr(config_module, "load_config", lambda: config)

        proxy_path = str(Path(config.local_roms_path) / "snes" / "Chrono Trigger.romcloud")
        result = launcher_module._resolve_and_cache(proxy_path)

        assert result == str(cached_path)
        assert operating_mode(config) is OperatingMode.CACHE
        assert state_path(config).read_text() == before_state


# ── run_launcher_wrapper: cancellation must not crash the wrapper ────────────


class TestRunLauncherWrapperCancellation:
    def test_explicit_transfer_cancel_exits_successfully_without_launching(
        self, monkeypatch, capsys
    ):
        import romcloud.integrations.batocera.launcher as launcher_module
        from romcloud.core.exceptions import TransferCancelledError

        def fake_resolve_and_cache(proxy_path):
            raise TransferCancelledError("Transfer cancelled by user")

        launched = []
        monkeypatch.setattr(launcher_module, "_resolve_and_cache", fake_resolve_and_cache)
        monkeypatch.setattr(
            launcher_module.EmulatorLauncher,
            "exec_with_rom",
            lambda *args, **kwargs: launched.append((args, kwargs)),
        )

        with pytest.raises(SystemExit) as exc_info:
            launcher_module.run_launcher_wrapper(list(_ROMCLOUD_ARGV))

        assert exc_info.value.code == 0
        assert launched == []
        captured = capsys.readouterr()
        assert "cancelled" in captured.err.lower()
        assert "error preparing game" not in captured.err.lower()

    def test_cancelled_transfer_exits_cleanly_without_traceback(self, monkeypatch, capsys):
        import romcloud.integrations.batocera.launcher as launcher_module

        def fake_resolve_and_cache(proxy_path):
            raise KeyboardInterrupt("Transfer cancelled by user")

        monkeypatch.setattr(launcher_module, "_resolve_and_cache", fake_resolve_and_cache)

        with pytest.raises(SystemExit) as exc_info:
            launcher_module.run_launcher_wrapper(list(_ROMCLOUD_ARGV))

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "cancelled" in captured.err.lower()
        assert "Traceback" not in captured.err
