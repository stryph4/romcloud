from __future__ import annotations

import logging
import multiprocessing
import sqlite3
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from romcloud.infrastructure import diagnostics
from romcloud.infrastructure.diagnostics import (
    DiagnosticQuery,
    DiagnosticStore,
    operation,
)
from romcloud.infrastructure.logging import configure_logging, get_logger
from romcloud.infrastructure import save_transaction
from romcloud.core.models.savesync import SaveArtifact


def _multiprocess_write(path: str, prefix: str, count: int) -> None:
    store = DiagnosticStore(path)
    assert store.initialize()
    for index in range(count):
        assert store.write(
            level="INFO", subsystem="worker", event_code="worker.event",
            message=f"{prefix}-{index}", metadata={"count": index},
        )


def test_generic_log_is_persisted_and_message_is_redacted(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.db"
    configure_logging(console=False, diagnostic_db=str(path))
    get_logger("generic").warning(
        "request password=hunter2 Authorization: Bearer abc.def"
    )

    events = DiagnosticStore(path).query(DiagnosticQuery(subsystem="generic"))
    assert len(events) == 1
    assert events[0]["level"] == "WARNING"
    assert "hunter2" not in events[0]["message"]
    assert "abc.def" not in events[0]["message"]


def test_operation_id_propagates_and_chain_is_chronological(tmp_path: Path) -> None:
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    with operation("gameStop Quick", subsystem="savesync", source="Auto gameStop") as op_id:
        diagnostics.event(
            "savesync", "group.classified", "examined",
            metadata={"group_id": "snes/metroid", "classification": "unchanged"},
        )
        diagnostics.event(
            "savesync", "operation.result", "no-op",
            metadata={"status": "unchanged", "unchanged": 1},
        )

    chain = store.operation_chain(op_id)
    assert [item["event_code"] for item in chain] == [
        "operation.started", "group.classified", "operation.result",
        "operation.completed", "operation.timing_summary",
    ]
    assert {item["operation_id"] for item in chain} == {op_id}
    summary = store.operation_summaries()[0]
    assert summary["operation_id"] == op_id
    assert summary["status"] == "unchanged"
    assert summary["unchanged"] == 1


def test_stage_timings_form_an_inspectable_operation_timeline(tmp_path: Path) -> None:
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    with operation("Auto Quick Sync", subsystem="savesync", source="Auto gameStop") as op_id:
        for stage in ("stability", "discovery", "scan-remote", "stability"):
            with diagnostics.stage_timer(stage) as timing:
                timing["observations"] = 2
                if stage == "scan-remote":
                    time.sleep(0.02)

    timeline = diagnostics.operation_timeline(op_id)
    assert {entry["stage"] for entry in timeline} == {
        "stability", "discovery", "scan-remote",
    }
    repeated = next(entry for entry in timeline if entry["stage"] == "stability")
    assert repeated["count"] == 2
    assert all("duration_ms" in entry for entry in timeline)
    assert timeline[0]["stage"] == "scan-remote"
    assert timeline[0]["percent"] > 50
    assert round(sum(entry["percent"] for entry in timeline)) == 100


def test_stage_timer_and_counters_share_one_operation_scoped_summary(
    tmp_path: Path,
) -> None:
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    with operation("targeted gameStart", subsystem="savesync") as op_id:
        with diagnostics.stage_timer("head-read"):
            diagnostics.increment_operation_counter("head_reads")
        snapshot = diagnostics.current_timing_snapshot()
        assert snapshot["stages"]["head-read"]["count"] == 1
        assert snapshot["counters"]["head_reads"] == 1
        assert snapshot["counters"]["diagnostic_write_attempts"] >= 2
        assert snapshot["counters"]["diagnostic_writes"] >= 2
        assert snapshot["stages"]["diagnostics-write"]["count"] >= 2
        assert 0 <= snapshot["covered_ms"] <= snapshot["total_ms"]
        assert snapshot["unattributed_ms"] >= 0

    summary = next(
        event
        for event in store.operation_chain(op_id)
        if event["event_code"] == "operation.timing_summary"
    )
    assert summary["metadata"]["total_ms"] >= 0
    assert summary["metadata"]["covered_ms"] >= 0
    assert summary["metadata"]["unattributed_ms"] >= 0
    assert summary["metadata"]["stages"]["head-read"]["count"] == 1
    assert summary["metadata"]["counters"]["head_reads"] == 1
    assert summary["metadata"]["counters"]["diagnostic_write_attempts"] >= 3
    assert summary["metadata"]["counters"]["diagnostic_writes"] >= 3


def test_operation_coverage_merges_nested_intervals_without_double_counting() -> None:
    millisecond = 1_000_000
    timing = diagnostics.OperationTiming(
        started_ns=0,
        stages={},
        counters={},
        intervals=[
            (0, 10 * millisecond),
            (2 * millisecond, 8 * millisecond),
            (15 * millisecond, 20 * millisecond),
        ],
        total_ms=20.0,
    )

    snapshot = timing.snapshot()

    assert snapshot["covered_ms"] == 15.0
    assert snapshot["unattributed_ms"] == 5.0


def test_operation_can_emit_one_concise_timing_log(caplog) -> None:
    logger = logging.getLogger("romcloud.timing-test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        with operation(
            "gameStart",
            subsystem="savesync",
            timing_logger=logger,
            timing_label="targeted-gameStart",
        ):
            with diagnostics.stage_timer("head-read"):
                diagnostics.increment_operation_counter("head_reads")
    summaries = [
        record.message
        for record in caplog.records
        if record.message.startswith("SaveSync timing summary:")
    ]
    assert len(summaries) == 1
    assert "operation=targeted-gameStart" in summaries[0]
    assert '"head_reads":1' in summaries[0]


def test_stage_timer_is_fail_open_without_a_configured_store(monkeypatch) -> None:
    """Timing instrumentation must never be able to break the timed work."""
    monkeypatch.setattr(diagnostics, "_active_store", None)
    with diagnostics.stage_timer("stability") as timing:
        timing["observations"] = 1
    assert diagnostics.operation_timeline("does-not-exist") == []


def test_stage_timer_records_the_stage_even_when_the_stage_raises(
    tmp_path: Path,
) -> None:
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    with operation("Auto Quick Sync", subsystem="savesync") as op_id:
        try:
            with diagnostics.stage_timer("transaction-apply"):
                raise RuntimeError("staging failed")
        except RuntimeError:
            pass
    assert [entry["stage"] for entry in diagnostics.operation_timeline(op_id)] == [
        "transaction-apply"
    ]


def test_metadata_allowlist_and_secret_fields_are_redacted(tmp_path: Path) -> None:
    store = DiagnosticStore(tmp_path / "diagnostics.db")
    assert store.initialize()
    assert store.write(
        level="INFO", subsystem="provider", event_code="provider.test", message="safe",
        metadata={
            "provider_type": "sftp", "password": "do-not-store",
            "access_token": "token-value", "unexpected": "private-value",
            "path": "sftp://user:password@example.test/saves",
        },
    )
    event = store.query()[0]
    encoded = str(event)
    assert "do-not-store" not in encoded
    assert "token-value" not in encoded
    assert "private-value" not in encoded
    assert event["metadata"]["provider_type"] == "sftp"
    assert "password@example" not in event["metadata"]["path"]


def test_busy_database_never_breaks_caller(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.db"
    store = DiagnosticStore(path)
    assert store.initialize()
    blocker = sqlite3.connect(path, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        assert not store.write(
            level="INFO", subsystem="busy", event_code="busy", message="ignored"
        )
    finally:
        blocker.rollback()
        blocker.close()


def test_retention_prunes_age_and_count_incrementally(tmp_path: Path, monkeypatch) -> None:
    store = DiagnosticStore(tmp_path / "diagnostics.db")
    assert store.initialize()
    for index in range(6):
        assert store.write(
            level="INFO", subsystem="retention", event_code="row",
            message=str(index), metadata={"count": index},
        )
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE diagnostic_events SET timestamp_utc = ? WHERE id <= 2", (old,))
    monkeypatch.setattr(diagnostics, "RETENTION_MAX_EVENTS", 3)
    monkeypatch.setattr(diagnostics, "RETENTION_PRUNE_BATCH", 20)
    assert store.prune() >= 3
    assert len(store.query(DiagnosticQuery(page_size=20))) <= 3


def test_filters_and_pagination(tmp_path: Path) -> None:
    store = DiagnosticStore(tmp_path / "diagnostics.db")
    assert store.initialize()
    for index in range(5):
        store.write(
            level="ERROR" if index % 2 else "INFO",
            subsystem="savesync" if index < 4 else "cache",
            event_code="test", message=f"needle {index}",
            operation_id="a" if index < 3 else "b",
            metadata={"count": index},
        )
    first = store.query(DiagnosticQuery(subsystem="savesync", level="INFO", page_size=1))
    second = store.query(DiagnosticQuery(subsystem="savesync", level="INFO", page=2, page_size=1))
    assert len(first) == len(second) == 1
    assert first[0]["id"] > second[0]["id"]
    assert len(store.query(DiagnosticQuery(operation_id="a", text="needle"))) == 3


def test_multiple_processes_write_same_wal_database(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.db"
    assert DiagnosticStore(path).initialize()
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(target=_multiprocess_write, args=(str(path), str(index), 12))
        for index in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    assert len(DiagnosticStore(path).query(DiagnosticQuery(page_size=100))) == 36


def test_corrupt_database_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "diagnostics.db"
    path.write_bytes(b"not sqlite")
    store = DiagnosticStore(path)
    assert not store.initialize()
    assert not store.write(level="INFO", subsystem="x", event_code="x", message="x")


def test_physical_mutation_audit_is_scoped_to_its_logical_group(tmp_path: Path) -> None:
    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    root = tmp_path / "saves"
    source = tmp_path / "source"
    target = root / "snes" / "Metroid.srm"
    unrelated = root / "psx" / "Crash.srm"
    target.parent.mkdir(parents=True)
    unrelated.parent.mkdir(parents=True)
    source.mkdir()
    target.write_bytes(b"old")
    unrelated.write_bytes(b"must-remain")
    (source / "Metroid.srm").write_bytes(b"new")

    def artifact(relative: str, value: bytes) -> SaveArtifact:
        return SaveArtifact(relative, len(value), hashlib.sha256(value).hexdigest())

    current = {"snes/Metroid.srm": artifact("snes/Metroid.srm", b"old")}
    desired = {"snes/Metroid.srm": artifact("snes/Metroid.srm", b"new")}
    with operation("audit", subsystem="savesync") as op_id:
        transaction = save_transaction.prepare_transaction(
            tmp_path / "transaction.json",
            (
                save_transaction.SelectedView(
                    root, current, desired,
                    lambda _relative, _artifact: source / "Metroid.srm",
                    lambda _relative: "retroarch-root-snes/metroid",
                ),
            ),
            operation_id=op_id,
        )
        save_transaction.apply_transaction(
            transaction,
            verify_current=lambda *_args: None,
            verify_desired=lambda *_args: None,
        )
        transaction.finalize()

    mutations = [
        item for item in store.operation_chain(op_id)
        if item["event_code"].startswith("physical_mutation.")
    ]
    assert mutations
    assert {item["metadata"]["group_id"] for item in mutations} == {
        "retroarch-root-snes/metroid"
    }
    assert all("Crash.srm" not in item["metadata"]["physical_path"] for item in mutations)
    assert unrelated.read_bytes() == b"must-remain"
    assert target.read_bytes() == b"new"


def test_savesync_upload_noop_download_and_conflict_chains(tmp_path: Path) -> None:
    from tests.unit.test_save_sync_service import _FakeProvider
    from romcloud.services.saves import SaveSyncService

    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    local_root = tmp_path / "local"
    remote_root = tmp_path / "remote"
    local_root.mkdir()
    service = SaveSyncService(
        provider=_FakeProvider(), connectivity_root=str(tmp_path / "remote-data"),
        local_root=str(local_root), remote_root=str(remote_root),
        state_path=tmp_path / "state" / "savesync-state.json",
    )
    local = local_root / "psx" / "Game.srm"
    remote = remote_root / "psx" / "Game.srm"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"initial")

    def operation_tree(operation_id: str) -> list[dict]:
        child_ids = [
            summary["operation_id"]
            for summary in store.operation_summaries(page_size=200)
            if summary.get("parent_operation_id") == operation_id
        ]
        return store.operation_chain(operation_id) + [
            event
            for child_id in child_ids
            for event in store.operation_chain(child_id)
        ]

    report = service.full_sync()
    assert report.uploaded == 1
    full = store.query(DiagnosticQuery(text="Full Sync started", page_size=1))[0]
    full_codes = {item["event_code"] for item in operation_tree(full["operation_id"])}
    assert {
        "operation.started", "reconciliation.decision", "physical_mutation.before",
        "physical_mutation.after", "journal.committed", "baseline.advanced",
        "cursor.advanced", "operation.result", "operation.completed",
    }.issubset(full_codes)

    unchanged = service.quick_sync()
    assert unchanged.status == "unchanged"
    quick = store.query(DiagnosticQuery(text="Quick Sync started", page_size=1))[0]
    quick_chain = store.operation_chain(quick["operation_id"])
    assert any(
        item["event_code"] == "operation.result"
        and item["metadata"].get("status") == "unchanged"
        for item in quick_chain
    )

    remote.write_bytes(b"remote-wins")
    service.commit_download(service.preview_download())
    download = store.query(DiagnosticQuery(text="Download All started", page_size=1))[0]
    download_chain = operation_tree(download["operation_id"])
    assert any(
        item["event_code"] == "physical_mutation.after"
        and item["metadata"].get("group_id") == "retroarch-root-psx/game"
        for item in download_chain
    )

    local.write_bytes(b"local-diverged")
    remote.write_bytes(b"remote-diverged")
    conflict_report = service.reconcile()
    assert conflict_report.conflicts == 1
    reconcile = store.query(DiagnosticQuery(text="Reconcile started", page_size=1))[0]
    assert any(
        item["event_code"] == "reconciliation.decision"
        and item["metadata"].get("decision") == "conflict"
        for item in store.operation_chain(reconcile["operation_id"])
    )


def test_graphical_maintenance_exposes_operation_summary() -> None:
    from ports_gfx.app import DIAGNOSTICS_ACTION, MENU_CATEGORIES, _OPERATIONS, format_result
    from ports_gfx.client import BackendResult

    assert any(
        item.action == DIAGNOSTICS_ACTION and item.label == "Diagnostics / Logs"
        for item in MENU_CATEGORIES["Maintenance"]
    )
    assert DIAGNOSTICS_ACTION not in _OPERATIONS
    rendered = format_result(
        "diagnostics",
        BackendResult(
            ok=True,
            data={
                "page": 1,
                "operations": [
                    {
                        "timestamp_utc": "2026-01-01T14:01:22+00:00",
                        "name": "Quick Sync", "status": "reconciled",
                        "generation": 288, "uploaded": 1,
                        "downloaded": 0, "conflicts": 0,
                    }
                ],
                "events": [
                    {
                        "timestamp_utc": "2026-01-01T14:01:22+00:00",
                        "level": "INFO", "subsystem": "savesync",
                        "message": "completed",
                    }
                ],
            },
        ),
    )
    assert "Quick Sync" in rendered
    assert "gen=288" in rendered
    assert "Raw events" in rendered


def test_browser_controller_diagnostics_bridge_to_central_store(tmp_path: Path) -> None:
    from romcloud.web.server import ControllerDiagnosticLog

    store = diagnostics.configure_diagnostics(tmp_path / "diagnostics.db")
    assert store is not None
    log = ControllerDiagnosticLog(tmp_path / "controller.jsonl")
    assert log.append([{"event": "button", "detail": {"button": 1}}]) == 1
    events = store.query(DiagnosticQuery(subsystem="web-controller"))
    assert events[0]["event_code"] == "controller.button"


def test_maintenance_endpoint_filters_and_pages(tmp_path: Path) -> None:
    import json
    from click.testing import CliRunner
    from romcloud.cli.main import cli
    from romcloud.infrastructure.config import (
        AppConfig, CacheConfig, SourceConfig, write_config,
    )

    config_path = tmp_path / "config" / "romcloud.toml"
    data_path = tmp_path / "data"
    write_config(
        AppConfig(
            source=SourceConfig("local", str(tmp_path / "roms")),
            cache=CacheConfig(str(tmp_path / "cache")),
            local_roms_path=str(tmp_path / "local-roms"),
            data_path=str(data_path),
        ),
        str(config_path),
    )
    store = DiagnosticStore(data_path / "diagnostics.db")
    assert store.initialize()
    store.write(
        level="ERROR", subsystem="savesync", event_code="needle",
        message="find this event", operation_id="abc",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config", str(config_path), "uidata", "diagnostics",
            "--subsystem", "savesync", "--level", "ERROR",
            "--operation-id", "abc", "--search", "find this",
            "--page", "1", "--page-size", "1",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["events"][0]["event_code"] == "needle"
    assert payload["operation_view"] is True


def test_operation_pages_use_v2_summary_table_not_event_chain_reconstruction(
    tmp_path: Path, monkeypatch
) -> None:
    store = DiagnosticStore(tmp_path / "diagnostics.db")
    assert store.initialize()
    store.write(
        level="INFO", subsystem="savesync", event_code="operation.started",
        message="Quick Sync started", operation_id="indexed-op",
        metadata={"operation_name": "Auto Quick Sync", "group_id": "game/save"},
    )
    monkeypatch.setattr(
        store, "operation_chain",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("N+1 chain query")),
    )
    rows = store.operation_summaries(text="game/save")
    assert rows[0]["operation_id"] == "indexed-op"
    assert rows[0]["event_count"] == 1
    assert store.query(DiagnosticQuery(text="indexed-op"))[0]["operation_id"] == "indexed-op"


def test_v1_schema_migrates_and_backfills_operation_summaries(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "diagnostics.db"
    store = DiagnosticStore(path)
    assert store.initialize()
    store.write(
        level="INFO", subsystem="savesync", event_code="operation.started",
        message="Legacy operation", operation_id="legacy-op",
        metadata={"operation_name": "Legacy Quick Sync"},
    )
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE diagnostic_schema_version SET version = 1")
        connection.execute("DELETE FROM diagnostic_operations")
        connection.commit()

    migrated = DiagnosticStore(path)
    assert migrated.initialize()
    assert migrated.operation_summaries()[0]["operation_id"] == "legacy-op"
