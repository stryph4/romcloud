"""Unit tests for `romcloud.cli.smb_setup_wizard`.

Exercises the interactive flow end-to-end via `click.testing.CliRunner`,
against a fake discovery service (duck-typing
`SMBDiscoveryService`) — no real network, no real SMB server. Confirms the
CLI layer only prompts/renders and delegates every actual decision to the
injected service, per the "CLI must not embed SMB logic" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import click
from click.testing import CliRunner

from romcloud.core.services.smb_discovery import (
    AuthResult,
    ListSharesResult,
    ShareInfo,
    ShareValidationResult,
    SystemDetectionResult,
)
from romcloud.infrastructure.mount import ReachabilityResult
from romcloud.cli.smb_setup_wizard import run_smb_setup_wizard


@dataclass
class FakeDiscovery:
    """Fake `SMBDiscoveryService` — every method returns a pre-programmed,
    canned result regardless of arguments (tests set up exactly the
    sequence they need)."""

    reachable: ReachabilityResult = field(default_factory=lambda: ReachabilityResult(True, "ok", ""))
    auth: AuthResult = field(default_factory=lambda: AuthResult(ok=True))
    shares: ListSharesResult = field(default_factory=lambda: ListSharesResult(ok=True, shares=(ShareInfo("Roms"),)))
    validations: dict = field(default_factory=dict)  # share name -> ShareValidationResult
    validate_calls: list = field(default_factory=list)

    def validate_server(self, target):
        return self.reachable

    def authenticate(self, target, credentials):
        return self.auth

    def list_shares(self, target, credentials):
        return self.shares

    def validate_share(self, target, credentials, share):
        self.validate_calls.append(share)
        return self.validations[share]

    def detect_systems(self, validation):
        detected = tuple(
            e for e in validation.top_level_entries if e.lower() in {"psx", "dreamcast", "gamecube"}
        )
        unrecognized = tuple(e for e in validation.top_level_entries if e not in detected)
        return SystemDetectionResult(detected_systems=detected, unrecognized_entries=unrecognized)


def _make_command(discovery):
    @click.command()
    def cmd():
        result = run_smb_setup_wizard(discovery)
        if result is None:
            click.echo("CANCELLED")
        else:
            click.echo(
                "RESULT:"
                f"{result.server}:{result.share}:{result.username}:{result.password}:"
                f"{','.join(result.detected_systems)}"
            )

    return cmd


class TestHappyPath:
    def test_full_successful_flow(self):
        discovery = FakeDiscovery(
            shares=ListSharesResult(ok=True, shares=(ShareInfo("Roms"), ShareInfo("Media"))),
            validations={
                "Roms": ShareValidationResult(ok=True, share="Roms", top_level_entries=("psx", "dreamcast"))
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input="omnivault\nstryph\nhunter2\nRoms\ny\n",
        )

        assert result.exit_code == 0, result.output
        assert "RESULT:omnivault:Roms:stryph:hunter2:" in result.output
        assert "psx" in result.output and "dreamcast" in result.output


class TestServerUnreachable:
    def test_dns_failure_cancels_immediately(self):
        discovery = FakeDiscovery(reachable=ReachabilityResult(False, "dns", "DNS resolution failed"))
        runner = CliRunner()
        result = runner.invoke(_make_command(discovery), input="badhost\n")

        assert result.exit_code == 0, result.output
        assert "CANCELLED" in result.output


class TestAuthenticationFailure:
    def test_auth_failure_cancels(self):
        from romcloud.core.services.smb_discovery import SMBErrorKind

        discovery = FakeDiscovery(auth=AuthResult(ok=False, error_kind=SMBErrorKind.AUTH_FAILED, detail="bad creds"))
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input="omnivault\nstryph\nwrongpass\n",
        )

        assert result.exit_code == 0, result.output
        assert "CANCELLED" in result.output


class TestManualShareFallback:
    def test_falls_back_to_manual_entry_when_enumeration_unavailable(self):
        from romcloud.core.services.smb_discovery import SMBErrorKind

        discovery = FakeDiscovery(
            shares=ListSharesResult(ok=False, error_kind=SMBErrorKind.TOOL_UNAVAILABLE, detail="no smbclient"),
            validations={"Roms": ShareValidationResult(ok=True, share="Roms", top_level_entries=("psx",))},
        )
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input="omnivault\nstryph\nhunter2\nRoms\ny\n",
        )

        assert result.exit_code == 0, result.output
        assert "Falling back to manual share entry" in result.output
        assert "RESULT:omnivault:Roms:" in result.output

    def test_manual_share_still_goes_through_validation(self):
        """Even in the manual fallback path, the share must be validated —
        there is no second, weaker path that skips validation."""
        from romcloud.core.services.smb_discovery import SMBErrorKind

        discovery = FakeDiscovery(
            shares=ListSharesResult(ok=False, error_kind=SMBErrorKind.TOOL_UNAVAILABLE),
            validations={
                "TypedShare": ShareValidationResult(
                    ok=False, share="TypedShare", error_kind=SMBErrorKind.ACCESS_DENIED, detail="denied"
                )
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input="omnivault\nstryph\nhunter2\nTypedShare\nn\n",
        )

        assert result.exit_code == 0, result.output
        assert discovery.validate_calls == ["TypedShare"]
        assert "CANCELLED" in result.output


class TestManualEntryFromMenu:
    def test_selecting_manual_entry_option_from_share_menu(self):
        from romcloud.cli.smb_setup_wizard import _MANUAL_ENTRY_LABEL

        discovery = FakeDiscovery(
            shares=ListSharesResult(ok=True, shares=(ShareInfo("Roms"),)),
            validations={"CustomShare": ShareValidationResult(ok=True, share="CustomShare", top_level_entries=())},
        )
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input=f"omnivault\nstryph\nhunter2\n{_MANUAL_ENTRY_LABEL}\nCustomShare\ny\n",
        )

        assert result.exit_code == 0, result.output
        assert "RESULT:omnivault:CustomShare:" in result.output


class TestShareValidationFailureThenRetryDeclined:
    def test_declining_retry_after_validation_failure_cancels(self):
        from romcloud.core.services.smb_discovery import SMBErrorKind

        discovery = FakeDiscovery(
            shares=ListSharesResult(ok=True, shares=(ShareInfo("Roms"),)),
            validations={
                "Roms": ShareValidationResult(
                    ok=False, share="Roms", error_kind=SMBErrorKind.SHARE_UNAVAILABLE, detail="gone"
                )
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input="omnivault\nstryph\nhunter2\nRoms\nn\n",
        )

        assert result.exit_code == 0, result.output
        assert "CANCELLED" in result.output


class TestDecliningUseThisLibrary:
    def test_declining_confirmation_then_declining_retry_cancels(self):
        discovery = FakeDiscovery(
            shares=ListSharesResult(ok=True, shares=(ShareInfo("Roms"),)),
            validations={"Roms": ShareValidationResult(ok=True, share="Roms", top_level_entries=("psx",))},
        )
        runner = CliRunner()
        result = runner.invoke(
            _make_command(discovery),
            input="omnivault\nstryph\nhunter2\nRoms\nn\nn\n",
        )

        assert result.exit_code == 0, result.output
        assert "CANCELLED" in result.output
