"""Unit tests for `romcloud.services.smb_discovery`.

Uses a fake :class:`SMBTransport` — no real network, no real SMB server,
no subprocess calls. Covers the presentation-agnostic service layer:
authentication, share enumeration + administrative-share filtering, share
validation, and Batocera system-folder detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from romcloud.services.smb_discovery import (
    AuthResult,
    ListSharesResult,
    SMBCredentials,
    SMBDiscoveryService,
    SMBErrorKind,
    SMBServerTarget,
    ShareInfo,
    ShareValidationResult,
    is_administrative_share,
)
from romcloud.infrastructure.mount import ReachabilityResult


@dataclass
class FakeTransport:
    auth_result: AuthResult = field(default_factory=lambda: AuthResult(ok=True))
    shares_result: ListSharesResult = field(default_factory=lambda: ListSharesResult(ok=True, shares=()))
    directory_result: Optional[ShareValidationResult] = None
    authenticate_calls: list = field(default_factory=list)
    list_shares_calls: list = field(default_factory=list)
    list_directory_calls: list = field(default_factory=list)

    def authenticate(self, target, credentials) -> AuthResult:
        self.authenticate_calls.append((target, credentials))
        return self.auth_result

    def list_shares(self, target, credentials) -> ListSharesResult:
        self.list_shares_calls.append((target, credentials))
        return self.shares_result

    def list_share_directory(self, target, credentials, share, path=""):
        self.list_directory_calls.append((target, credentials, share, path))
        assert self.directory_result is not None
        return self.directory_result


def _target() -> SMBServerTarget:
    return SMBServerTarget(host="omnivault", port=445)


def _creds() -> SMBCredentials:
    return SMBCredentials(username="stryph", password="hunter2")


class TestValidateServer:
    def test_delegates_to_injected_reachability_checker(self):
        calls = []

        def fake_checker(host, port):
            calls.append((host, port))
            return ReachabilityResult(True, "ok", "")

        service = SMBDiscoveryService(FakeTransport(), reachability_checker=fake_checker)
        result = service.validate_server(_target())

        assert result.ok is True
        assert calls == [("omnivault", 445)]

    def test_dns_failure_reported(self):
        def fake_checker(host, port):
            return ReachabilityResult(False, "dns", "DNS resolution failed")

        service = SMBDiscoveryService(FakeTransport(), reachability_checker=fake_checker)
        result = service.validate_server(_target())

        assert result.ok is False
        assert result.stage == "dns"


class TestAuthenticate:
    def test_successful_authentication(self):
        transport = FakeTransport(auth_result=AuthResult(ok=True))
        service = SMBDiscoveryService(transport)

        result = service.authenticate(_target(), _creds())

        assert result.ok is True
        assert len(transport.authenticate_calls) == 1

    def test_authentication_failure(self):
        transport = FakeTransport(
            auth_result=AuthResult(ok=False, error_kind=SMBErrorKind.AUTH_FAILED, detail="bad creds")
        )
        service = SMBDiscoveryService(transport)

        result = service.authenticate(_target(), _creds())

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.AUTH_FAILED

    def test_dns_server_failure_surfaces_as_error_kind(self):
        transport = FakeTransport(
            auth_result=AuthResult(ok=False, error_kind=SMBErrorKind.SERVER_NOT_FOUND, detail="no DNS")
        )
        service = SMBDiscoveryService(transport)

        result = service.authenticate(_target(), _creds())

        assert result.error_kind is SMBErrorKind.SERVER_NOT_FOUND

    def test_connection_timeout_surfaces_as_error_kind(self):
        transport = FakeTransport(auth_result=AuthResult(ok=False, error_kind=SMBErrorKind.TIMEOUT))
        service = SMBDiscoveryService(transport)

        result = service.authenticate(_target(), _creds())

        assert result.error_kind is SMBErrorKind.TIMEOUT


class TestListShares:
    def test_share_enumeration_returns_shares(self):
        transport = FakeTransport(
            shares_result=ListSharesResult(ok=True, shares=(ShareInfo(name="Roms"), ShareInfo(name="Media")))
        )
        service = SMBDiscoveryService(transport)

        result = service.list_shares(_target(), _creds())

        assert result.ok is True
        names = {s.name for s in result.shares}
        assert names == {"Roms", "Media"}

    def test_administrative_shares_filtered_by_default(self):
        transport = FakeTransport(
            shares_result=ListSharesResult(
                ok=True,
                shares=(
                    ShareInfo(name="Roms"),
                    ShareInfo(name="IPC$", kind="ipc"),
                    ShareInfo(name="ADMIN$"),
                    ShareInfo(name="print$"),
                ),
            )
        )
        service = SMBDiscoveryService(transport)

        result = service.list_shares(_target(), _creds())

        assert result.ok is True
        assert [s.name for s in result.shares] == ["Roms"]

    def test_legitimate_shares_ending_in_dollar_are_preserved(self):
        """Only well-known admin share *names* are filtered — never a blanket
        'ends with $' rule, since legitimate shares can also end in $."""
        transport = FakeTransport(
            shares_result=ListSharesResult(ok=True, shares=(ShareInfo(name="Roms"), ShareInfo(name="Backup$")))
        )
        service = SMBDiscoveryService(transport)

        result = service.list_shares(_target(), _creds())

        assert result.ok is True
        assert {s.name for s in result.shares} == {"Roms", "Backup$"}

    def test_no_accessible_shares_after_filtering(self):
        transport = FakeTransport(
            shares_result=ListSharesResult(ok=True, shares=(ShareInfo(name="IPC$", kind="ipc"),))
        )
        service = SMBDiscoveryService(transport)

        result = service.list_shares(_target(), _creds())

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.NO_SHARES_FOUND

    def test_no_shares_found_from_transport_propagates(self):
        transport = FakeTransport(
            shares_result=ListSharesResult(ok=False, error_kind=SMBErrorKind.NO_SHARES_FOUND)
        )
        service = SMBDiscoveryService(transport)

        result = service.list_shares(_target(), _creds())

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.NO_SHARES_FOUND

    def test_include_administrative_flag_bypasses_filtering(self):
        transport = FakeTransport(
            shares_result=ListSharesResult(ok=True, shares=(ShareInfo(name="IPC$", kind="ipc"),))
        )
        service = SMBDiscoveryService(transport)

        result = service.list_shares(_target(), _creds(), include_administrative=True)

        assert result.ok is True
        assert [s.name for s in result.shares] == ["IPC$"]


class TestIsAdministrativeShare:
    def test_non_disk_kind_is_administrative(self):
        assert is_administrative_share(ShareInfo(name="Roms", kind="printer")) is True

    def test_known_admin_names_are_administrative(self):
        for name in ("IPC$", "ADMIN$", "C$", "print$"):
            assert is_administrative_share(ShareInfo(name=name)) is True

    def test_ordinary_share_is_not_administrative(self):
        assert is_administrative_share(ShareInfo(name="Roms")) is False


class TestValidateShare:
    def test_successful_validation(self):
        transport = FakeTransport(
            directory_result=ShareValidationResult(
                ok=True, share="Roms", top_level_entries=("dreamcast", "gamecube")
            )
        )
        service = SMBDiscoveryService(transport)

        result = service.validate_share(_target(), _creds(), "Roms")

        assert result.ok is True
        assert result.top_level_entries == ("dreamcast", "gamecube")

    def test_validation_failure_access_denied(self):
        transport = FakeTransport(
            directory_result=ShareValidationResult(
                ok=False, share="Secret", error_kind=SMBErrorKind.ACCESS_DENIED, detail="denied"
            )
        )
        service = SMBDiscoveryService(transport)

        result = service.validate_share(_target(), _creds(), "Secret")

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.ACCESS_DENIED

    def test_validation_failure_share_unavailable(self):
        transport = FakeTransport(
            directory_result=ShareValidationResult(
                ok=False, share="Gone", error_kind=SMBErrorKind.SHARE_UNAVAILABLE
            )
        )
        service = SMBDiscoveryService(transport)

        result = service.validate_share(_target(), _creds(), "Gone")

        assert result.ok is False
        assert result.error_kind is SMBErrorKind.SHARE_UNAVAILABLE

    def test_manual_share_uses_same_validation_pipeline(self):
        """Manual entry must go through the exact same validate_share call
        as a discovered share — there is no second, weaker path."""
        transport = FakeTransport(
            directory_result=ShareValidationResult(ok=True, share="HandTyped", top_level_entries=("psx",))
        )
        service = SMBDiscoveryService(transport)

        discovered = service.validate_share(_target(), _creds(), "HandTyped")

        assert discovered.ok is True
        assert transport.list_directory_calls[0][2] == "HandTyped"


class TestDetectSystems:
    def test_recognized_systems_detected(self):
        service = SMBDiscoveryService(FakeTransport())
        validation = ShareValidationResult(
            ok=True,
            share="Roms",
            top_level_entries=("dreamcast", "gamecube", "ps2", "psp", "psx", "saturn", "wii", "xbox", "xbox360"),
        )

        result = service.detect_systems(validation)

        assert result.count == 9
        assert set(result.detected_systems) == {
            "dreamcast",
            "gamecube",
            "ps2",
            "psp",
            "psx",
            "saturn",
            "wii",
            "xbox",
            "xbox360",
        }
        assert result.unrecognized_entries == ()

    def test_unrecognized_folders_ignored_safely(self):
        """Unrecognized entries must never be treated as an error — just
        reported separately, and never require every folder to be
        recognized."""
        service = SMBDiscoveryService(FakeTransport())
        validation = ShareValidationResult(
            ok=True,
            share="Roms",
            top_level_entries=("psx", "BIOS", "readme.txt", "downloads"),
        )

        result = service.detect_systems(validation)

        assert result.detected_systems == ("psx",)
        assert set(result.unrecognized_entries) == {"BIOS", "readme.txt", "downloads"}

    def test_case_insensitive_matching(self):
        service = SMBDiscoveryService(FakeTransport())
        validation = ShareValidationResult(ok=True, share="Roms", top_level_entries=("PSX", "GameCube"))

        result = service.detect_systems(validation)

        assert set(result.detected_systems) == {"PSX", "GameCube"}
