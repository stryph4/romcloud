"""Unit tests for the `romcloud update` CLI wiring.

The underlying network/subprocess logic is exercised in test_update.py; here
we only verify the CLI's behavior — argument handling, output formatting,
and exit codes — by monkeypatching the (already unit-tested)
check_for_update/perform_update functions.
"""

from __future__ import annotations

from click.testing import CliRunner
from types import SimpleNamespace

import romcloud.cli.commands.update as update_cmd_module
from romcloud.cli.commands.update import update_cmd
from romcloud.core.exceptions import UpdateDownloadError, UpdateInstallError
from romcloud.lifecycle.update import BuildInfo, CheckResult, CommitInfo, UpdateResult


def _run(args, *, obj=None):
    return CliRunner().invoke(update_cmd, args, obj=obj)


class TestUpdateCheckMode:
    def test_reports_update_available(self, monkeypatch):
        current = BuildInfo(
            version="0.1.0", commit="a" * 40, commit_short="a" * 12, build_date="x", source="s"
        )
        latest = CommitInfo(sha="b" * 40, date="2026-08-08T00:00:00Z", message="msg")
        monkeypatch.setattr(
            update_cmd_module,
            "check_for_update",
            lambda home, repo, channel: CheckResult(current=current, latest_commit=latest, update_available=True),
        )
        result = _run(["--check"])
        assert result.exit_code == 0, result.output
        assert "0.1.0" in result.output
        assert "b" * 12 in result.output
        assert "update is available" in result.output.lower()

    def test_reports_up_to_date(self, monkeypatch):
        current = BuildInfo(
            version="0.1.0", commit="a" * 40, commit_short="a" * 12, build_date="x", source="s"
        )
        latest = CommitInfo(sha="a" * 40, date="2026-08-08T00:00:00Z", message="msg")
        monkeypatch.setattr(
            update_cmd_module,
            "check_for_update",
            lambda home, repo, channel: CheckResult(current=current, latest_commit=latest, update_available=False),
        )
        result = _run(["--check"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output.lower()

    def test_no_prior_version_shown_as_unknown(self, monkeypatch):
        latest = CommitInfo(sha="b" * 40, date="x", message="m")
        monkeypatch.setattr(
            update_cmd_module,
            "check_for_update",
            lambda home, repo, channel: CheckResult(current=None, latest_commit=latest, update_available=True),
        )
        result = _run(["--check"])
        assert result.exit_code == 0, result.output
        assert "unknown" in result.output.lower()

    def test_check_failure_exits_nonzero_with_clear_message(self, monkeypatch):
        def _raise(home, repo, channel):
            raise UpdateDownloadError("network unreachable")

        monkeypatch.setattr(update_cmd_module, "check_for_update", _raise)
        result = _run(["--check"])
        assert result.exit_code != 0
        assert "network unreachable" in result.output

    def test_check_mode_never_calls_perform_update(self, monkeypatch):
        called = []
        monkeypatch.setattr(update_cmd_module, "perform_update", lambda *a, **k: called.append(1))
        latest = CommitInfo(sha="a" * 40, date="x", message="m")
        monkeypatch.setattr(
            update_cmd_module,
            "check_for_update",
            lambda home, repo, channel: CheckResult(current=None, latest_commit=latest, update_available=True),
        )
        _run(["--check"])
        assert called == []


class TestUpdatePerform:
    def test_prints_installed_version_on_success(self, monkeypatch):
        new = BuildInfo(version="2.0.0", commit="c" * 40, commit_short="c" * 12, build_date="x", source="s")
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda home, venv_python, repo, channel: UpdateResult(previous=None, new=new),
        )
        result = _run([])
        assert result.exit_code == 0, result.output
        assert "2.0.0" in result.output
        assert "c" * 12 in result.output

    def test_failure_exits_nonzero_with_clear_message(self, monkeypatch):
        def _raise(home, venv_python, repo, channel):
            raise UpdateInstallError("pip explosion")

        monkeypatch.setattr(update_cmd_module, "perform_update", _raise)
        result = _run([])
        assert result.exit_code != 0
        assert "pip explosion" in result.output

    def test_success_surfaces_optional_provider_warning(self, monkeypatch):
        new = BuildInfo(
            version="2.0.0",
            commit="c" * 40,
            commit_short="c" * 12,
            build_date="x",
            source="s",
        )
        warning = "Google Drive is unavailable; other features are unaffected."
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda *args, **kwargs: UpdateResult(
                previous=None,
                new=new,
                reconcile_log=f"warning: {warning}",
                warnings=(warning,),
            ),
        )

        result = _run([])

        assert result.exit_code == 0
        assert warning in result.output

    def test_default_repo_and_stable_channel_used(self, monkeypatch):
        captured = {}

        def fake_perform_update(home, venv_python, repo, channel):
            captured["repo"] = repo
            captured["channel"] = channel.value
            return UpdateResult(
                previous=None,
                new=BuildInfo(version="1", commit="d" * 40, commit_short="d" * 12, build_date="x", source="s"),
            )

        monkeypatch.setattr(update_cmd_module, "perform_update", fake_perform_update)
        result = _run([])
        assert result.exit_code == 0, result.output
        assert captured["repo"] == update_cmd_module.DEFAULT_REPO
        assert captured["channel"] == "stable"

    def test_successful_channel_switch_persists_after_update(self, monkeypatch):
        calls = []
        new = BuildInfo(
            version="2", commit="e" * 40, commit_short="e" * 12,
            build_date="x", source="s", channel="develop",
        )
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda *args, **kwargs: calls.append(("update", kwargs["channel"].value))
            or UpdateResult(previous=None, new=new),
        )
        monkeypatch.setattr(
            update_cmd_module,
            "write_update_channel",
            lambda channel, path: calls.append(("persist", channel.value)),
        )

        result = _run(["--channel", "develop"])

        assert result.exit_code == 0, result.output
        assert calls == [("update", "develop"), ("persist", "develop")]

    def test_failed_channel_switch_does_not_persist(self, monkeypatch):
        persisted = []
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda *args, **kwargs: (_ for _ in ()).throw(UpdateInstallError("failed")),
        )
        monkeypatch.setattr(
            update_cmd_module,
            "write_update_channel",
            lambda *args: persisted.append(args),
        )

        result = _run(["--channel", "develop"])

        assert result.exit_code != 0
        assert persisted == []

    def test_invalid_channel_is_rejected_before_update(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            update_cmd_module, "perform_update", lambda *args, **kwargs: called.append(1)
        )

        result = _run(["--channel", "feature/foo"])

        assert result.exit_code != 0
        assert called == []

    def test_normal_update_respects_persisted_develop_channel(self, monkeypatch, tmp_path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            'update_channel = "develop"\n[source]\nprovider = "none"\n',
            encoding="utf-8",
        )
        captured = []
        monkeypatch.setattr(
            update_cmd_module,
            "load_config",
            lambda *args, **kwargs: SimpleNamespace(update_channel="develop"),
        )
        monkeypatch.setattr(
            update_cmd_module,
            "capability_policy",
            lambda config: SimpleNamespace(require=lambda *args: None),
        )
        new = BuildInfo("2", None, None, "x", "s", channel="develop")
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda *args, **kwargs: captured.append(kwargs["channel"].value)
            or UpdateResult(previous=None, new=new),
        )
        monkeypatch.setattr(
            update_cmd_module,
            "write_update_channel",
            lambda *args: (_ for _ in ()).throw(AssertionError("must not rewrite")),
        )

        result = _run([], obj={"config_path": str(config_path)})

        assert result.exit_code == 0, result.output
        assert captured == ["develop"]

    def test_develop_to_stable_switch_persists_after_success(self, monkeypatch, tmp_path):
        config_path = tmp_path / "romcloud.toml"
        config_path.write_text(
            'update_channel = "develop"\n[source]\nprovider = "none"\n',
            encoding="utf-8",
        )
        calls = []
        monkeypatch.setattr(
            update_cmd_module,
            "load_config",
            lambda *args, **kwargs: SimpleNamespace(update_channel="develop"),
        )
        monkeypatch.setattr(
            update_cmd_module,
            "capability_policy",
            lambda config: SimpleNamespace(require=lambda *args: None),
        )
        new = BuildInfo("2", None, None, "x", "s", channel="stable")
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda *args, **kwargs: calls.append(("update", kwargs["channel"].value))
            or UpdateResult(previous=None, new=new),
        )
        monkeypatch.setattr(
            update_cmd_module,
            "write_update_channel",
            lambda channel, path: calls.append(("persist", channel.value)),
        )

        result = _run(
            ["--channel", "stable"], obj={"config_path": str(config_path)}
        )

        assert result.exit_code == 0, result.output
        assert calls == [("update", "stable"), ("persist", "stable")]
