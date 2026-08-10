from __future__ import annotations

from ports_gfx.activity import ActivityLog, parse_progress_line
from romcloud.core.progress import ProgressEvent


def test_progress_event_round_trip_is_user_facing_and_redacted():
    line = ProgressEvent(
        "mount",
        "authenticate",
        "success",
        "Authentication successful",
        detail="credential super-secret accepted",
    ).wire_line("super-secret")

    event = parse_progress_line(line)

    assert event is not None
    assert "Authentication successful" in event.display_line
    assert "super-secret" not in event.detail
    assert "***" in event.detail


def test_activity_log_keeps_user_messages_separate_from_details():
    activity = ActivityLog()
    activity.ingest(
        ProgressEvent("browse", "directory", "error", "Could not open folder", detail="access denied").wire_line()
    )

    assert activity.user_lines()[-1].endswith("Could not open folder")
    assert activity.detail_lines() == ["directory: access denied"]
