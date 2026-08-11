from __future__ import annotations

from types import SimpleNamespace

from romcloud.core.capabilities import OperatingMode
from romcloud.integrations.batocera import presentation


def test_target_mode_controls_es_override_before_state_commit(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        presentation.es_config,
        "remove",
        lambda: calls.append(("remove", ())) or True,
    )
    monkeypatch.setattr(
        presentation.es_config,
        "install",
        lambda systems: calls.append(("install", tuple(systems)))
        or SimpleNamespace(included_systems=list(systems), missing_systems=[]),
    )
    config = SimpleNamespace()

    presentation.refresh_emulationstation(
        config, ["snes"], mode=OperatingMode.CONNECTED
    )
    presentation.refresh_emulationstation(
        config, ["snes"], mode=OperatingMode.CACHE
    )

    assert calls == [("remove", ()), ("install", ("snes",))]


def test_reload_uses_batocera_supported_restart_command(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        presentation.shutil,
        "which",
        lambda name: "/usr/bin/batocera-es-swissknife",
    )
    monkeypatch.setattr(
        presentation.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )

    assert presentation.reload_emulationstation() is True
    assert calls == [
        (
            ["/usr/bin/batocera-es-swissknife", "--restart"],
            {"check": True, "capture_output": True, "text": True, "timeout": 30},
        )
    ]


def test_reload_is_a_noop_outside_batocera(monkeypatch) -> None:
    monkeypatch.setattr(presentation.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        presentation.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess")),
    )
    assert presentation.reload_emulationstation() is False
