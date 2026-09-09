# Central diagnostics

ROMCloud writes structured application diagnostics to
`<data.path>/diagnostics.db`. This is the primary support log; rotating text
logs remain as a fail-open fallback. The database is created automatically and
uses SQLite WAL, a short busy timeout, and one short transaction per event so
independent GUI, CLI, lifecycle, and worker processes can write concurrently.
A missing, locked, corrupt, or otherwise unwritable database never fails the
application operation that attempted to log.

## Schema and correlation

Schema version 2 is recorded in `diagnostic_schema_version`. The
`diagnostic_events` table stores an ordered integer ID, UTC timestamp,
monotonic clock value, level, subsystem, event code, message, operation and
parent-operation IDs, process/thread context, ROMCloud version/build,
redacted metadata JSON, and exception type/message. Indexes support
newest-first, operation, subsystem, level, and time-range queries.

Version 2 adds `diagnostic_operations`, an incrementally maintained summary
table indexed by start time, subsystem, and status. Operation pages therefore
do not reconstruct each summary by repeatedly scanning its raw events. A v1
database is backfilled once during migration; raw event history remains intact.

An operation ID identifies a whole logical workflow. Nested synchronous work
inherits it. SaveSync's gameStop discovery, dirty classification,
reconciliation, transaction, remote journal, baseline/cursor advancement, and
result therefore share one ID. An explicitly nested independent operation may
record the outer ID as `parent_operation_id`. The worker-busy detached
drain-pending handoff passes the originating ID in its child environment.

## Retention and redaction

Retention constants live in `romcloud.infrastructure.diagnostics`. Events are
kept for at most 30 days and bounded to 100,000 rows / a 32 MiB database.
Every 256 writes, at most 1,000 oldest rows are pruned per criterion, orphaned
operation summaries are removed, and free pages are reclaimed incrementally.
This keeps maintenance work short and prevents unbounded userdata growth.

Metadata uses a closed allowlist. Unknown top-level fields and any field whose
name resembles a password, credential, private key, token, authorization
header, API key, session key, secret, or cookie are redacted centrally. Free
text also redacts credential URLs, secret assignments, bearer tokens, and
private-key material. Callers must still avoid logging private data; central
redaction is defense in depth. Save contents are never recorded. SaveSync may
record eligible paths, sizes, and content hashes for forensic comparison.

## Maintenance viewer and SaveSync audit

Maintenance contains **Diagnostics / Logs**. It opens a dedicated local
graphical browser with newest-first operation and raw-event pages, incremental
pagination, subsystem and level filters, operation-ID prefix filtering,
structured-metadata/message search, and 24-hour/7-day/30-day time filtering.
Selecting an operation opens its chronological event pages. Back restores the
prior result page, filters, and focus. SaveSync operations expose their mode,
provider, duration, journal generations, counts, changed logical groups,
classification/hashes/decision/reason, transaction and physical mutations,
and journal/baseline/cursor outcome. Advanced / Raw Events remains available.

The game browser and Diagnostics use the same browser focus navigator. It
handles D-pad and left-stick movement, bounded repeat, edge-triggered
activation/back, bumper paging, scroll-into-view, dialog/detail focus restore,
mouse/keyboard coexistence, and conventional Batocera pads that Chromium
reports without a `standard` mapping. The kiosk launch remains a blocking
foreground child of the Ports process, so EmulationStation does not resume
under the controller session.

SaveSync uses event codes including `operation.started`, `operation.stage`,
`session.created`, `session.stopped`, `local_observation.completed`,
`local_observation.failed`, `group.classified`, `dirty_marker.created`,
`dirty_marker.skipped`, `dirty_state.committed`, `reconciliation.decision`,
`physical_mutation.before`, `physical_mutation.after`, `journal.committed`,
`baseline.advanced`, `cursor.advanced`, `worker.busy`, `operation.result`,
`operation.completed`, and `operation.failed`. Destructive/replacement audit
records include the exact physical path, logical group, before/after hashes,
transaction/root identity, decision source, and reason, but never file bytes.

Batocera game-stop troubleshooting begins in
`<romcloud-home>/logs/auto-savesync-lifecycle.log`. Each stop receives an
operation ID shared with its structured SaveSync events. The lifecycle log
records the raw hook arguments, handoff, popup reporter availability, command
result, and hook return. The structured chain then records the session marker,
resolved layouts, each bounded scoped observation (up to 100 canonical and
physical paths, sizes, `mtime_ns` values, and content hashes), ownership groups, dirty state before
and after persistence, pending Quick Sync candidates, exclusions, and any
journal/cursor early-return reason. This distinguishes a missing hook from a
late save write or a rejected/empty candidate scope without logging save data.

Diagnostic support-bundle export is intentionally deferred: safe snapshot and
configuration allowlisting need a dedicated UI flow. The database/viewer is
the supported source for this release.
