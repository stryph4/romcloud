"""Tests for best-effort display transition diagnostics."""

from __future__ import annotations

from ports_gfx.display_diagnostics import DISPLAY_LOG_ENV, DisplayDiagnostics


def test_records_monotonic_event_and_filtered_display_environment(tmp_path, monkeypatch):
    log_path = tmp_path / "gui-display.log"
    monkeypatch.setenv(DISPLAY_LOG_ENV, str(log_path))
    monkeypatch.setenv("SDL_VIDEODRIVER", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

    diagnostics = DisplayDiagnostics("/userdata/system/romcloud/bin/romcloud")
    diagnostics.record("display_open_before", environment=diagnostics.environment())

    line = log_path.read_text(encoding="utf-8")
    assert 'event="display_open_before"' in line
    assert "monotonic=" in line
    assert "elapsed=" in line
    assert 'SDL_VIDEODRIVER":"wayland"' in line
    assert 'WAYLAND_DISPLAY":"wayland-1"' in line


def test_diagnostic_write_failure_does_not_escape(tmp_path, monkeypatch):
    unwritable_target = tmp_path / "directory"
    unwritable_target.mkdir()
    monkeypatch.setenv(DISPLAY_LOG_ENV, str(unwritable_target))

    diagnostics = DisplayDiagnostics("/opt/romcloud/bin/romcloud")
    diagnostics.record("must_not_raise", error="expected")
