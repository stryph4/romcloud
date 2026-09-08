# Central diagnostics

ROMCloud writes structured application diagnostics to
`<data.path>/diagnostics.db`. This is the primary support log; rotating text
logs remain as a fail-open fallback. The database is created automatically and
uses SQLite WAL, a short busy timeout, and one short transaction per event so
independent GUI, CLI, lifecycle, and worker processes can write concurrently.
A missing, locked, corrupt, or otherwise unwritable database never fails the
application operation that attempted to log.

## Schema and correlation

Schema version 1 is recorded in `diagnostic_schema_version`. The
`diagnostic_events` table stores an ordered integer ID, UTC timestamp,
monotonic clock value, level, subsystem, event code, message, operation and
parent-operation IDs, process/thread context, ROMCloud version/build,
redacted metadata JSON, and exception type/message. Indexes support
newest-first, operation, subsystem, level, and time-range queries.

An operation ID identifies a whole logical workflow. Nested synchronous work
inherits it. SaveSync's gameStop discovery, dirty classification,
reconciliation, transaction, remote journal, baseline/cursor advancement, and
result therefore share one ID. An explicitly nested independent operation may
record the outer ID as `parent_operation_id`. The worker-busy detached
drain-pending handoff passes the originating ID in its child environment.

## Retention and redaction

Retention constants live in `romcloud.infrastructure.diagnostics`. Events are
kept for at most 30 days and bounded to 100,000 rows / a 32 MiB database.
Every 256 writes, at most 1,000 oldest rows are pruned per criterion and free
pages are reclaimed incrementally. This keeps maintenance work short and
prevents unbounded userdata growth.

Metadata uses a closed allowlist. Unknown top-level fields and any field whose
name resembles a password, credential, private key, token, authorization
header, API key, session key, secret, or cookie are redacted centrally. Free
text also redacts credential URLs, secret assignments, bearer tokens, and
private-key material. Callers must still avoid logging private data; central
redaction is defense in depth. Save contents are never recorded. SaveSync may
record eligible paths, sizes, and content hashes for forensic comparison.

## Maintenance viewer and SaveSync audit

Maintenance contains **Diagnostics / Logs**. Its backend endpoint supports
newest-first pagination plus subsystem, level, operation ID, UTC range, and
text filters. Selecting an event in the terminal viewer opens that operation's
chronological chain. The graphical viewer shows a bounded newest-first page;
the same endpoint exposes all filters for richer clients.

SaveSync uses event codes including `operation.started`, `operation.stage`,
`group.classified`, `dirty_marker.created`, `reconciliation.decision`,
`physical_mutation.before`, `physical_mutation.after`, `journal.committed`,
`baseline.advanced`, `cursor.advanced`, `worker.busy`, `operation.result`,
`operation.completed`, and `operation.failed`. Destructive/replacement audit
records include the exact physical path, logical group, before/after hashes,
transaction/root identity, decision source, and reason, but never file bytes.

Diagnostic support-bundle export is intentionally deferred: safe snapshot and
configuration allowlisting need a dedicated UI flow. The database/viewer is
the supported source for this release.
