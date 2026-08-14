from __future__ import annotations

from romcloud.integrations.batocera import startup_activation


def test_restart_required_clears_only_after_activation(tmp_path):
    path = tmp_path / "state" / startup_activation.STATE_FILENAME
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a", encoding="ascii")

    startup_activation.mark_restart_required(path, boot_id_path=boot_id_path)
    pending = startup_activation.activation_status(path)
    assert pending["startup_restart_required"] is True
    assert pending["startup_integration_activated"] is False

    assert startup_activation.mark_activated(path, boot_id_path=boot_id_path) is False
    assert path.exists()

    boot_id_path.write_text("boot-b", encoding="ascii")
    assert startup_activation.mark_activated(path, boot_id_path=boot_id_path) is True
    assert startup_activation.activation_status(path) == {
        "startup_restart_required": False
    }


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
    assert startup_activation.activation_status(path)[
        "startup_restart_required"
    ] is True
