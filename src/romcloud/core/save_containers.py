"""Provider-neutral logical-container models and three-way planning.

The models in this module deliberately contain no filesystem or emulator
knowledge.  Format adapters turn a physical save container into a logical
snapshot; :func:`plan_container_reconcile` then applies the same domain-level
three-way rule to every supported format.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol


class OpaqueReason(str, Enum):
    """Stable, non-content-bearing reason codes for conservative fallback."""

    UNSUPPORTED_FORMAT = "unsupported-format"
    UNSUPPORTED_PS1_CARD_SIZE = "unsupported-ps1-card-size"
    MALFORMED_CHAIN = "malformed-chain"
    CHECKSUM_FAILURE = "checksum-failure"
    DUPLICATE_IDENTITY = "duplicate-identity"
    AMBIGUOUS_NAMESPACE = "ambiguous-namespace"
    PS1_BROKEN_SECTOR_MAPPING = "ps1-broken-sector-mapping"
    INSUFFICIENT_VERIFIED_FREE_CAPACITY = "insufficient-verified-free-capacity"
    UNSUPPORTED_PS2_FILE_CARD = "unsupported-ps2-file-card"
    AMBIGUOUS_PS2_FOLDER_GROUPING = "ambiguous-ps2-folder-grouping"
    SYMLINK_OR_PATH_SUBSTITUTION = "symlink-or-path-substitution"
    ADAPTER_VERSION_MISMATCH = "adapter-version-mismatch"
    SOURCE_CHANGED_DURING_STAGING = "source-changed-during-staging"
    VALIDATION_FAILURE = "validation-failure"
    LOGICAL_BASELINE_REQUIRED = "logical-baseline-required"


class OpaqueContainerError(ValueError):
    """An adapter cannot prove that a container is safe to merge."""

    def __init__(self, reason: OpaqueReason, detail: str = "") -> None:
        super().__init__(reason.value if not detail else f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, order=True)
class EntryIdentity:
    """Stable adapter-defined logical identity, independent of allocation."""

    value: str


@dataclass(frozen=True)
class ContainerEntry:
    identity: EntryIdentity
    merge_domain_id: str
    canonical_hash: str
    size_bytes: int
    locator: str = ""


@dataclass(frozen=True)
class ContainerSnapshot:
    container_id: str
    adapter_id: str
    schema_version: int
    format_variant: str
    entries: tuple[ContainerEntry, ...]

    def entry_map(self) -> dict[EntryIdentity, ContainerEntry]:
        return {entry.identity: entry for entry in self.entries}

    def domain_map(self) -> dict[str, tuple[ContainerEntry, ...]]:
        domains: dict[str, list[ContainerEntry]] = {}
        for entry in self.entries:
            domains.setdefault(entry.merge_domain_id, []).append(entry)
        return {
            domain: tuple(sorted(values, key=lambda item: item.identity.value))
            for domain, values in domains.items()
        }


@dataclass(frozen=True)
class LogicalEntryState:
    identity: str
    canonical_hash: str
    size_bytes: int


@dataclass(frozen=True)
class LogicalDomainState:
    merge_domain_id: str
    entries: tuple[LogicalEntryState, ...] = ()


@dataclass(frozen=True)
class ContainerBaseline:
    """Last logical state known to have converged on both physical sides.

    A domain in ``tombstones`` is explicitly known absent.  This distinguishes
    a propagated deletion from a domain that an older adapter never observed.
    """

    container_id: str
    adapter_id: str
    schema_version: int
    format_variant: str
    domains: tuple[LogicalDomainState, ...] = ()
    tombstones: tuple[str, ...] = ()


class DomainAction(str, Enum):
    CONVERGED = "converged"
    USE_LOCAL = "use-local"
    USE_REMOTE = "use-remote"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class DomainDecision:
    merge_domain_id: str
    action: DomainAction
    baseline: Optional[LogicalDomainState]
    local: Optional[LogicalDomainState]
    remote: Optional[LogicalDomainState]


@dataclass(frozen=True)
class ContainerReconcilePlan:
    container_id: str
    decisions: tuple[DomainDecision, ...]
    desired_local: tuple[LogicalDomainState, ...]
    desired_remote: tuple[LogicalDomainState, ...]
    next_baseline: ContainerBaseline

    @property
    def conflicts(self) -> tuple[DomainDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.action is DomainAction.CONFLICT
        )

    @property
    def local_changes(self) -> tuple[DomainDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.action is DomainAction.USE_REMOTE
        )

    @property
    def remote_changes(self) -> tuple[DomainDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.action is DomainAction.USE_LOCAL
        )


@dataclass(frozen=True)
class ProbeResult:
    supported: bool
    format_variant: str = ""
    opaque_reason: Optional[OpaqueReason] = None


@dataclass(frozen=True)
class ExtractedEntry:
    identity: EntryIdentity
    canonical_hash: str
    payload: Any


@dataclass(frozen=True)
class EntryReplacement:
    entry: ContainerEntry
    extracted: ExtractedEntry


@dataclass(frozen=True)
class RebuildPlan:
    expected: ContainerSnapshot
    replacements: tuple[EntryReplacement, ...] = ()
    removals: tuple[EntryIdentity, ...] = ()


@dataclass(frozen=True)
class CandidateReceipt:
    candidate_path: Path
    physical_hash: str
    expected: ContainerSnapshot


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    snapshot: Optional[ContainerSnapshot] = None
    opaque_reason: Optional[OpaqueReason] = None
    detail: str = ""


class SaveContainerAdapter(Protocol):
    adapter_id: str
    schema_version: int

    def probe(self, source: Path) -> ProbeResult: ...

    def snapshot(self, source: Path, *, container_id: str) -> ContainerSnapshot: ...

    def enumerate_entries(
        self, snapshot: ContainerSnapshot
    ) -> tuple[ContainerEntry, ...]: ...

    def identify_entry(self, entry: ContainerEntry) -> EntryIdentity: ...

    def hash_entry(self, entry: ContainerEntry) -> str: ...

    def extract_entry(
        self,
        source: Path,
        snapshot: ContainerSnapshot,
        identity: EntryIdentity,
    ) -> ExtractedEntry: ...

    def replace_entry(
        self, candidate: Path, replacement: EntryReplacement
    ) -> None: ...

    def remove_entry(self, candidate: Path, identity: EntryIdentity) -> None: ...

    def rebuild(
        self, destination: Path, plan: RebuildPlan, candidate: Path
    ) -> CandidateReceipt: ...

    def validate(
        self, candidate: Path, expected: ContainerSnapshot
    ) -> ValidationReport: ...


def snapshot_to_domains(snapshot: ContainerSnapshot) -> tuple[LogicalDomainState, ...]:
    return tuple(
        LogicalDomainState(
            merge_domain_id=domain_id,
            entries=tuple(
                LogicalEntryState(
                    identity=entry.identity.value,
                    canonical_hash=entry.canonical_hash,
                    size_bytes=entry.size_bytes,
                )
                for entry in entries
            ),
        )
        for domain_id, entries in sorted(snapshot.domain_map().items())
    )


def baseline_from_snapshot(
    snapshot: ContainerSnapshot,
    *,
    tombstones: tuple[str, ...] = (),
) -> ContainerBaseline:
    return ContainerBaseline(
        container_id=snapshot.container_id,
        adapter_id=snapshot.adapter_id,
        schema_version=snapshot.schema_version,
        format_variant=snapshot.format_variant,
        domains=snapshot_to_domains(snapshot),
        tombstones=tuple(sorted(set(tombstones))),
    )


def _domain_map(
    domains: tuple[LogicalDomainState, ...],
) -> dict[str, LogicalDomainState]:
    return {domain.merge_domain_id: domain for domain in domains}


def _assert_compatible(
    local: ContainerSnapshot,
    remote: ContainerSnapshot,
    baseline: Optional[ContainerBaseline],
) -> None:
    identities = {
        (local.container_id, local.adapter_id, local.schema_version, local.format_variant),
        (remote.container_id, remote.adapter_id, remote.schema_version, remote.format_variant),
    }
    if len(identities) != 1:
        raise OpaqueContainerError(OpaqueReason.ADAPTER_VERSION_MISMATCH)
    if baseline is not None:
        identity = next(iter(identities))
        baseline_identity = (
            baseline.container_id,
            baseline.adapter_id,
            baseline.schema_version,
            baseline.format_variant,
        )
        if identity != baseline_identity:
            raise OpaqueContainerError(OpaqueReason.ADAPTER_VERSION_MISMATCH)


def plan_container_reconcile(
    local: ContainerSnapshot,
    remote: ContainerSnapshot,
    baseline: Optional[ContainerBaseline],
) -> ContainerReconcilePlan:
    """Plan a logical per-domain three-way merge without touching storage."""

    _assert_compatible(local, remote, baseline)
    local_map = _domain_map(snapshot_to_domains(local))
    remote_map = _domain_map(snapshot_to_domains(remote))

    if baseline is None:
        if local_map != remote_map:
            raise OpaqueContainerError(OpaqueReason.LOGICAL_BASELINE_REQUIRED)
        established = baseline_from_snapshot(local)
        return ContainerReconcilePlan(
            container_id=local.container_id,
            decisions=tuple(
                DomainDecision(domain, DomainAction.CONVERGED, None, value, value)
                for domain, value in sorted(local_map.items())
            ),
            desired_local=tuple(local_map[key] for key in sorted(local_map)),
            desired_remote=tuple(remote_map[key] for key in sorted(remote_map)),
            next_baseline=established,
        )

    baseline_map = _domain_map(baseline.domains)
    known_domains = set(baseline_map).union(baseline.tombstones)
    all_domains = sorted(known_domains.union(local_map).union(remote_map))
    decisions: list[DomainDecision] = []
    desired_local = dict(local_map)
    desired_remote = dict(remote_map)
    next_domains = dict(baseline_map)
    next_tombstones = set(baseline.tombstones)

    for domain_id in all_domains:
        base = baseline_map.get(domain_id)
        local_domain = local_map.get(domain_id)
        remote_domain = remote_map.get(domain_id)
        if local_domain == remote_domain:
            action = DomainAction.CONVERGED
            selected = local_domain
        elif local_domain == base:
            action = DomainAction.USE_REMOTE
            selected = remote_domain
            if selected is None:
                desired_local.pop(domain_id, None)
            else:
                desired_local[domain_id] = selected
        elif remote_domain == base:
            action = DomainAction.USE_LOCAL
            selected = local_domain
            if selected is None:
                desired_remote.pop(domain_id, None)
            else:
                desired_remote[domain_id] = selected
        else:
            action = DomainAction.CONFLICT
            selected = None

        decisions.append(
            DomainDecision(domain_id, action, base, local_domain, remote_domain)
        )
        if action is not DomainAction.CONFLICT:
            if selected is None:
                next_domains.pop(domain_id, None)
                next_tombstones.add(domain_id)
            else:
                next_domains[domain_id] = selected
                next_tombstones.discard(domain_id)

    next_baseline = ContainerBaseline(
        container_id=baseline.container_id,
        adapter_id=baseline.adapter_id,
        schema_version=baseline.schema_version,
        format_variant=baseline.format_variant,
        domains=tuple(next_domains[key] for key in sorted(next_domains)),
        tombstones=tuple(sorted(next_tombstones)),
    )
    return ContainerReconcilePlan(
        container_id=local.container_id,
        decisions=tuple(decisions),
        desired_local=tuple(desired_local[key] for key in sorted(desired_local)),
        desired_remote=tuple(desired_remote[key] for key in sorted(desired_remote)),
        next_baseline=next_baseline,
    )


def target_snapshot(
    plan: ContainerReconcilePlan,
    local: ContainerSnapshot,
    remote: ContainerSnapshot,
    *,
    side: str,
) -> ContainerSnapshot:
    """Rehydrate one planned logical target from observed adapter entries."""

    if side not in {"local", "remote"}:
        raise ValueError("container target side must be local or remote")
    desired = plan.desired_local if side == "local" else plan.desired_remote
    observed = tuple((*local.entries, *remote.entries))
    entries: list[ContainerEntry] = []
    for domain in desired:
        for logical in domain.entries:
            matches = tuple(
                entry
                for entry in observed
                if entry.identity.value == logical.identity
                and entry.merge_domain_id == domain.merge_domain_id
                and entry.canonical_hash == logical.canonical_hash
                and entry.size_bytes == logical.size_bytes
            )
            if not matches:
                raise OpaqueContainerError(OpaqueReason.VALIDATION_FAILURE)
            entries.append(matches[0])
    return ContainerSnapshot(
        container_id=local.container_id,
        adapter_id=local.adapter_id,
        schema_version=local.schema_version,
        format_variant=local.format_variant,
        entries=tuple(sorted(entries, key=lambda entry: entry.identity.value)),
    )
