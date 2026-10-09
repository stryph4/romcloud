"""Small structured result model shared by Troubleshoot CLI and GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping


FINDING_STATUSES = frozenset({"healthy", "fixed", "warning", "error", "skipped"})
FINDING_SEVERITIES = frozenset({"info", "warning", "error"})
FIXABILITIES = frozenset({"none", "automatic", "conditional", "confirmation"})


@dataclass(frozen=True)
class FindingFix:
    attempted: bool = False
    succeeded: bool | None = None
    changed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class RestartRequirements:
    emulationstation: bool = False
    service: bool = False
    gui: bool = False


@dataclass(frozen=True)
class TroubleshootFinding:
    id: str
    component: str
    status: str
    severity: str
    message: str
    detail: str = ""
    fixability: str = "none"
    blocked_by: tuple[str, ...] = ()
    fix: FindingFix = field(default_factory=FindingFix)
    restart_required: RestartRequirements = field(default_factory=RestartRequirements)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in FINDING_STATUSES:
            raise ValueError(f"Unknown finding status: {self.status}")
        if self.severity not in FINDING_SEVERITIES:
            raise ValueError(f"Unknown finding severity: {self.severity}")
        if self.fixability not in FIXABILITIES:
            raise ValueError(f"Unknown finding fixability: {self.fixability}")

    @property
    def eligible_for_quick_repair(self) -> bool:
        return (
            self.status in {"warning", "error"}
            and self.fixability in {"automatic", "conditional"}
            and not self.blocked_by
        )

    def with_fix(
        self, *, succeeded: bool, changed: bool, reason: str = ""
    ) -> "TroubleshootFinding":
        return replace(
            self,
            status="fixed" if succeeded else "error",
            severity="info" if succeeded else "error",
            fix=FindingFix(True, succeeded, changed, reason),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TroubleshootReport:
    findings: tuple[TroubleshootFinding, ...]
    mode: str = "diagnostic"
    schema_version: int = 1
    cancelled: bool = False

    @property
    def summary(self) -> dict[str, int]:
        return {
            status: sum(item.status == status for item in self.findings)
            for status in ("healthy", "fixed", "warning", "error", "skipped")
        }

    @property
    def overall_status(self) -> str:
        if self.cancelled:
            return "partial"
        if any(item.status == "error" for item in self.findings):
            return "error"
        if any(item.status == "skipped" for item in self.findings):
            return "partial"
        if any(item.status == "warning" for item in self.findings):
            return "warning"
        if any(item.status == "fixed" for item in self.findings):
            return "fixed"
        return "healthy"

    @property
    def quick_repair_available(self) -> bool:
        return any(item.eligible_for_quick_repair for item in self.findings)

    @property
    def es_restart_required(self) -> bool:
        return self.mode == "quick_repair" and any(
            item.restart_required.emulationstation
            and item.fix.succeeded is True
            and item.fix.changed
            for item in self.findings
        )

    @property
    def service_restart_required(self) -> bool:
        return self.mode == "quick_repair" and any(
            item.restart_required.service
            and item.fix.succeeded is True
            and item.fix.changed
            for item in self.findings
        )

    @property
    def gui_relaunch_required(self) -> bool:
        return self.mode == "quick_repair" and any(
            item.restart_required.gui
            and item.fix.succeeded is True
            and item.fix.changed
            for item in self.findings
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": "troubleshoot",
            "mode": self.mode,
            "overall_status": self.overall_status,
            "summary": self.summary,
            "findings": [item.as_dict() for item in self.findings],
            "quick_repair_available": self.quick_repair_available,
            "es_restart_required": self.es_restart_required,
            "service_restart_required": self.service_restart_required,
            "gui_relaunch_required": self.gui_relaunch_required,
            "cancelled": self.cancelled,
        }
