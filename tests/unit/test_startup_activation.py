from __future__ import annotations

import json

from romcloud.integrations.batocera import startup_activation


def test_restart_required_clears_only_after_activation(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")

    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    pending = startup_activation.activation_status(path, boot_id_path=boot_id_path)
    assert pending["startup_restart_required"] is True
    assert pending["startup_integration_activated"] is False

    assert startup_activation.mark_activated(path, boot_id_path=boot_id_path) is False
    assert path.exists()

    boot_id_path.write_text("boot-b", encoding="ascii")
    assert startup_activation.mark_activated(path, boot_id_path=boot_id_path) is True
    assert startup_activation.activation_status(path, boot_id_path=boot_id_path) == {
        "startup_restart_required": False
    }


def test_failed_later_boot_is_distinct_and_does_not_rearm_marker(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")
    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    original = json.loads(path.read_text(encoding="utf-8"))

    boot_id_path.write_text("boot-b", encoding="ascii")
    startup_activation.record_startup_attempt(path, boot_id_path=boot_id_path)
    startup_activation.record_startup_failure(
        path, "manager did not bind", boot_id_path=boot_id_path
    )
    failed = json.loads(path.read_text(encoding="utf-8"))
    status = startup_activation.activation_status(path, boot_id_path=boot_id_path)

    assert failed["changed_at"] == original["changed_at"]
    assert failed["changed_boot_id"] == "boot-a"
    assert failed["last_attempt_boot_id"] == "boot-b"
    assert status["startup_restart_required"] is False
    assert status["startup_manager_startup_failed"] is True
    assert "manager did not bind" in status["startup_manager_failure_message"]


def test_repeated_mark_in_originating_boot_does_not_rewrite_marker(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")

    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    first = path.read_bytes()
    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)

    assert path.read_bytes() == first

    boot_id_path.write_text("boot-b", encoding="ascii")
    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    assert path.read_bytes() == first


def test_successful_retry_on_later_boot_clears_prior_failure(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")
    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    boot_id_path.write_text("boot-b", encoding="ascii")
    startup_activation.record_startup_failure(
        path, "first attempt failed", boot_id_path=boot_id_path
    )

    assert startup_activation.mark_activated(path, boot_id_path=boot_id_path)
    assert not path.exists()


def test_ordinary_boot_failure_is_persisted_without_restart_marker(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-z", encoding="ascii")

    startup_activation.record_startup_attempt(path, boot_id_path=boot_id_path)
    startup_activation.record_startup_failure(
        path, "TLS bind failed", boot_id_path=boot_id_path
    )
    status = startup_activation.activation_status(path, boot_id_path=boot_id_path)

    assert status["startup_restart_required"] is False
    assert status["startup_manager_startup_failed"] is True
    assert "TLS bind failed" in status["startup_manager_failure_message"]

    assert startup_activation.mark_activated(path, boot_id_path=boot_id_path) is False
    assert not path.exists()


def test_restart_request_uses_batocera_command_and_keeps_state_pending(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")
    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    calls = []

    result = startup_activation.request_reboot(
        popen=lambda args, **kwargs: calls.append((args, kwargs))
    )

    assert calls[0][0] == [startup_activation.REBOOT_COMMAND, "--reboot"]
    assert result == {
        "restart_requested": True,
        "startup_restart_required": True,
    }
    assert startup_activation.activation_status(path, boot_id_path=boot_id_path)[
        "startup_restart_required"
    ] is True


def test_later_boot_without_service_attempt_is_not_another_restart_prompt(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")
    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)

    boot_id_path.write_text("boot-b", encoding="ascii")
    status = startup_activation.activation_status(path, boot_id_path=boot_id_path)

    assert status["startup_restart_required"] is False
    assert status["startup_manager_startup_failed"] is True
    assert "did not record" in status["startup_manager_failure_message"]
