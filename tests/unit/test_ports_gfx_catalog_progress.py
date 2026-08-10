from __future__ import annotations

from ports_gfx.activity import ActivityEvent
from ports_gfx.catalog_progress import CatalogRefreshProgress


def event(stage, status="running", *, system="", current=None, total=None, metadata=None, detail=""):
    values = dict(metadata or {})
    if system:
        values["system"] = system
    return ActivityEvent(
        timestamp="12:00:00",
        operation="catalog_refresh",
        stage=stage,
        status=status,
        message=stage,
        detail=detail,
        current=current,
        total=total,
        metadata=values,
    )


def test_refresh_state_tracks_determinate_indeterminate_and_mixed_result():
    state = CatalogRefreshProgress()
    state.ingest(event("refresh_started"))
    state.ingest(event("systems_discovered", current=0, total=2, metadata={"systems": ["nes", "ps2"]}))
    state.ingest(event("system_started", system="nes"))
    assert state.systems["nes"].fraction is None
    state.ingest(event("system_progress", system="nes", current=5, total=10))
    assert state.systems["nes"].fraction == 0.5
    state.ingest(event("system_completed", "success", system="nes", current=10, total=10))
    state.ingest(event("system_failed", "error", system="ps2", detail="offline"))
    state.ingest(event("overall_progress", current=2, total=2, metadata={"succeeded": 1, "failed": 1}))
    state.ingest(event("refresh_completed", "error", current=2, total=2, metadata={"succeeded": 1, "failed": 1}))
    assert state.is_finished
    assert state.status == "error"
    assert state.systems["nes"].status == "success"
    assert state.systems["ps2"].status == "error"
    assert state.overall_fraction == 1.0


def test_unknown_total_never_fabricates_percentage():
    state = CatalogRefreshProgress()
    state.ingest(event("refresh_started"))
    state.ingest(event("system_started", system="ps2"))
    assert state.systems["ps2"].determinate is False
    assert state.systems["ps2"].fraction is None


def test_many_system_rows_scroll_within_bounds():
    state = CatalogRefreshProgress()
    state.ingest(event("systems_discovered", total=12, metadata={"systems": [f"s{i}" for i in range(12)]}))
    state.scroll(100, rows=4)
    assert state.scroll_offset == 8
    assert [row.system for row in state.visible_systems(4)] == ["s8", "s9", "s10", "s11"]
