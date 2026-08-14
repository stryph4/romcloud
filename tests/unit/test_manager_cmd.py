from click.testing import CliRunner

from romcloud.cli.commands.manager import manager_cmd


def test_manager_help_exposes_secure_and_diagnostic_server_options() -> None:
    result = CliRunner().invoke(manager_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--host" in result.output and "--port" in result.output
    assert "--tls-cert" in result.output and "--tls-key" in result.output
    assert "--http" in result.output
    assert "Gamepad API" in result.output
