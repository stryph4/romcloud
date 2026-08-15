from click.testing import CliRunner
from types import SimpleNamespace

from romcloud.cli.commands.manager import manager_cmd


def test_manager_help_exposes_secure_and_diagnostic_server_options() -> None:
    result = CliRunner().invoke(manager_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--host" in result.output and "--port" in result.output
    assert "--tls-cert" in result.output and "--tls-key" in result.output
    assert "--http" in result.output
    assert "Gamepad API" in result.output


def test_foreground_manager_records_discoverable_runtime_state(tmp_path, monkeypatch) -> None:
    import romcloud.cli.commands.manager as manager_module
    from romcloud.web.lifecycle import read_manager_state

    captured = {}
    container = SimpleNamespace(
        config=SimpleNamespace(data_path=str(tmp_path / "data")),
        library_manager=object(),
    )
    monkeypatch.setattr(manager_module, "get_container", lambda ctx: container)

    def serve(*args, **kwargs):
        captured["state_before_bind"] = read_manager_state(container.config.data_path)
        kwargs["on_ready"]()
        captured["state"] = read_manager_state(container.config.data_path)

    monkeypatch.setattr("romcloud.web.server.serve_manager", serve)
    result = CliRunner().invoke(
        manager_cmd,
        ["--http", "--host", "127.0.0.1", "--token", "shown-token"],
    )

    assert result.exit_code == 0, result.output
    assert captured["state_before_bind"] == {}
    assert captured["state"]["running"] is True
    assert captured["state"]["token"] == "shown-token"
    assert captured["state"]["url"] == "http://127.0.0.1:8765/"
    assert read_manager_state(container.config.data_path) == {}


def test_bind_failure_never_publishes_ready_state(tmp_path, monkeypatch) -> None:
    import romcloud.cli.commands.manager as manager_module
    from romcloud.web.lifecycle import read_manager_state

    container = SimpleNamespace(
        config=SimpleNamespace(data_path=str(tmp_path / "data")),
        library_manager=object(),
    )
    monkeypatch.setattr(manager_module, "get_container", lambda ctx: container)
    monkeypatch.setattr(
        "romcloud.web.server.serve_manager",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("bind failed")),
    )

    result = CliRunner().invoke(manager_cmd, ["--http", "--host", "127.0.0.1"])

    assert result.exit_code != 0
    assert read_manager_state(container.config.data_path) == {}
