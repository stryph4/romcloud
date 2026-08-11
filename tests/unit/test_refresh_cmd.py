"""Refresh command integration with Batocera's persistent ES overlay."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

import romcloud.cli.commands.refresh as refresh_module
from romcloud.cli.commands.refresh import refresh_cmd
from romcloud.integrations.batocera import es_config


class _RefreshResult:
    errors: list = []

    def __str__(self) -> str:
        return "Catalog refreshed"


def test_successful_refresh_registers_only_cataloged_systems(monkeypatch) -> None:
    calls = []
    container = SimpleNamespace(
        config=SimpleNamespace(source=SimpleNamespace(rom_root="/source"), game_access_mode="smart_cache"),
        catalog=SimpleNamespace(refresh=lambda: _RefreshResult()),
        game_repo=SimpleNamespace(list_systems=lambda: ["ps2", "snes"]),
    )
    monkeypatch.setattr(refresh_module, "get_container", lambda ctx: container)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access.reconcile_game_access",
        lambda config: SimpleNamespace(created=0, removed=0),
    )
    monkeypatch.setattr(
        es_config,
        "refresh",
        lambda systems: calls.append(list(systems))
        or SimpleNamespace(included_systems=["ps2", "snes"], missing_systems=[]),
    )

    result = CliRunner().invoke(refresh_cmd, [], obj={})

    assert result.exit_code == 0, result.output
    assert calls == [["ps2", "snes"]]
    assert "Updated EmulationStation registration for 2 system(s)" in result.output
    assert "restart EmulationStation" in result.output


def test_es_registration_failure_makes_incomplete_refresh_nonzero(monkeypatch) -> None:
    container = SimpleNamespace(
        config=SimpleNamespace(source=SimpleNamespace(rom_root="/source"), game_access_mode="smart_cache"),
        catalog=SimpleNamespace(refresh=lambda: _RefreshResult()),
        game_repo=SimpleNamespace(list_systems=lambda: ["snes"]),
    )
    monkeypatch.setattr(refresh_module, "get_container", lambda ctx: container)
    monkeypatch.setattr(
        "romcloud.integrations.batocera.game_access.reconcile_game_access",
        lambda config: SimpleNamespace(created=0, removed=0),
    )
    monkeypatch.setattr(
        es_config,
        "refresh",
        lambda systems: (_ for _ in ()).throw(es_config.ESConfigError("stock file missing")),
    )

    result = CliRunner().invoke(refresh_cmd, [], obj={})

    assert result.exit_code == 1
    assert "could not update EmulationStation integration" in result.output
