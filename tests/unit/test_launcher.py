"""Tests for integrations/batocera/launcher.py

Key invariants proved here:

1. A normal (non-.romcloud) argv reaches emulatorlauncher byte-for-byte
   unchanged — only argv[0] (the wrapper script name) is replaced with
   "emulatorlauncher".

2. A .romcloud argv reaches emulatorlauncher differing from the normal
   argv in *exactly one element*: the value immediately following "-rom".
   Every other argument — %CONTROLLERSCONFIG%, -system, -gameinfoxml,
   -systemname, their positions, and any unknown future args — is identical.

3. The argv helper functions are pure: no I/O, no side effects.
"""

from __future__ import annotations

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
    _CTRLS,
    "-system", "snes",
    "-rom", "/userdata/roms/snes/Chrono Trigger.sfc",
    "-gameinfoxml", _GAMEINFOXML,
    "-systemname", _SYSTEMNAME,
]

# Same argv but with a .romcloud proxy as the -rom value
_ROMCLOUD_ARGV: list[str] = [
    _WRAPPER,
    _CTRLS,
    "-system", "snes",
    "-rom", "/userdata/roms/snes/Chrono Trigger.romcloud",
    "-gameinfoxml", _GAMEINFOXML,
    "-systemname", _SYSTEMNAME,
]

_CACHED_ROM = "/userdata/romcloud-cache/snes/abc123/Chrono Trigger.sfc"

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
        assert result[1] == _CTRLS

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
    script = tmp_path / "emulatorlauncher"
    script.write_text("#!/bin/sh\n")
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
        assert captured["args"][1:] == _NORMAL_ARGV[1:]

    def test_passthrough_argv_identical_to_original_minus_wrapper_name(
        self, monkeypatch, fake_launcher
    ):
        _, bin_dir = fake_launcher
        monkeypatch.setenv("PATH", bin_dir)
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")

        captured_args = []
        monkeypatch.setattr("os.execvp", lambda f, a: captured_args.extend(a))

        EmulatorLauncher().exec_passthrough(_NORMAL_ARGV)

        # Every element except argv[0] must be the same as _NORMAL_ARGV[1:]
        assert captured_args[1:] == _NORMAL_ARGV[1:]

    def test_passthrough_not_found_raises_launch_error(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setenv("ROMCLOUD_EMULATORLAUNCHER", "emulatorlauncher")
        with pytest.raises(LaunchError, match="emulatorlauncher not found"):
            EmulatorLauncher().exec_passthrough(_NORMAL_ARGV)


class TestEmulatorLauncherWithRom:
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
        with pytest.raises(LaunchError, match="emulatorlauncher not found"):
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
