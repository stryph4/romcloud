"""Unit tests for the `romcloud update` CLI wiring.

The underlying network/subprocess logic is exercised in test_update.py; here
we only verify the CLI's behavior — argument handling, output formatting,
and exit codes — by monkeypatching the (already unit-tested)
check_for_update/perform_update functions.
"""

from __future__ import annotations

from click.testing import CliRunner

import romcloud.cli.commands.update as update_cmd_module
from romcloud.cli.commands.update import update_cmd
from romcloud.core.exceptions import UpdateDownloadError, UpdateInstallError
from romcloud.infrastructure.update import BuildInfo, CheckResult, CommitInfo, UpdateResult


def _run(args):
    return CliRunner().invoke(update_cmd, args)


class TestUpdateCheckMode:
    def test_reports_update_available(self, monkeypatch):
        current = BuildInfo(
            version="0.1.0", commit="a" * 40, commit_short="a" * 12, build_date="x", source="s"
        )
        latest = CommitInfo(sha="b" * 40, date="2026-08-08T00:00:00Z", message="msg")
        monkeypatch.setattr(
            update_cmd_module,
            "check_for_update",
            lambda home, repo, branch: CheckResult(current=current, latest_commit=latest, update_available=True),
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
            lambda home, repo, branch: CheckResult(current=current, latest_commit=latest, update_available=False),
        )
        result = _run(["--check"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output.lower()

    def test_no_prior_version_shown_as_unknown(self, monkeypatch):
        latest = CommitInfo(sha="b" * 40, date="x", message="m")
        monkeypatch.setattr(
            update_cmd_module,
            "check_for_update",
            lambda home, repo, branch: CheckResult(current=None, latest_commit=latest, update_available=True),
        )
        result = _run(["--check"])
        assert result.exit_code == 0, result.output
        assert "unknown" in result.output.lower()

    def test_check_failure_exits_nonzero_with_clear_message(self, monkeypatch):
        def _raise(home, repo, branch):
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
            lambda home, repo, branch: CheckResult(current=None, latest_commit=latest, update_available=True),
        )
        _run(["--check"])
        assert called == []


class TestUpdatePerform:
    def test_prints_installed_version_on_success(self, monkeypatch):
        new = BuildInfo(version="2.0.0", commit="c" * 40, commit_short="c" * 12, build_date="x", source="s")
        monkeypatch.setattr(
            update_cmd_module,
            "perform_update",
            lambda home, venv_python, repo, branch: UpdateResult(previous=None, new=new),
        )
        result = _run([])
        assert result.exit_code == 0, result.output
        assert "2.0.0" in result.output
        assert "c" * 12 in result.output

    def test_failure_exits_nonzero_with_clear_message(self, monkeypatch):
        def _raise(home, venv_python, repo, branch):
            raise UpdateInstallError("pip explosion")

        monkeypatch.setattr(update_cmd_module, "perform_update", _raise)
        result = _run([])
        assert result.exit_code != 0
        assert "pip explosion" in result.output

    def test_default_repo_and_branch_used_when_not_overridden(self, monkeypatch):
        captured = {}

        def fake_perform_update(home, venv_python, repo, branch):
            captured["repo"] = repo
            captured["branch"] = branch
            return UpdateResult(
                previous=None,
                new=BuildInfo(version="1", commit="d" * 40, commit_short="d" * 12, build_date="x", source="s"),
            )

        monkeypatch.setattr(update_cmd_module, "perform_update", fake_perform_update)
        result = _run([])
        assert result.exit_code == 0, result.output
        assert captured["repo"] == update_cmd_module.DEFAULT_REPO
        assert captured["branch"] == update_cmd_module.DEFAULT_BRANCH
