"""Pure GUI state for structured catalog-refresh progress events."""

from __future__ import annotations

from dataclasses import dataclass, field

from ports_gfx.activity import ActivityEvent


@dataclass
class SystemRefreshState:
    system: str
    status: str = "queued"
    current: int | None = None
    total: int | None = None
    message: str = "Waiting"
    detail: str = ""

    @property
    def determinate(self) -> bool:
        return self.total is not None and self.total > 0 and self.current is not None

    @property
    def fraction(self) -> float | None:
        if not self.determinate:
            return None
        assert self.current is not None and self.total is not None
        return max(0.0, min(1.0, self.current / self.total))


@dataclass
class CatalogRefreshProgress:
    systems: dict[str, SystemRefreshState] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    status: str = "idle"
    current: int = 0
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    message: str = ""
    scroll_offset: int = 0

    @property
    def active_system(self) -> str | None:
        return next(
            (name for name in self.order if self.systems[name].status == "running"),
            None,
        )

    @property
    def overall_fraction(self) -> float | None:
        if self.total <= 0:
            return None
        return max(0.0, min(1.0, self.current / self.total))

    @property
    def is_finished(self) -> bool:
        return self.status in ("success", "error")

    def visible_systems(self, rows: int) -> list[SystemRefreshState]:
        if rows <= 0:
            return []
        max_offset = max(0, len(self.order) - rows)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))
        return [
            self.systems[name]
            for name in self.order[self.scroll_offset : self.scroll_offset + rows]
        ]

    def scroll(self, delta: int, rows: int) -> None:
        maximum = max(0, len(self.order) - max(0, rows))
        self.scroll_offset = max(0, min(self.scroll_offset + delta, maximum))

    def ingest(self, event: ActivityEvent) -> bool:
        if event.operation != "catalog_refresh":
            return False

        stage = event.stage
        metadata = event.metadata
        system = str(metadata.get("system", ""))
        self.message = event.message

        if stage == "refresh_started":
            self.status = "running"
            self.current = self.total = self.succeeded = self.failed = 0
            self.systems.clear()
            self.order.clear()
            self.scroll_offset = 0
            return True

        if stage == "systems_discovered":
            self.status = "running"
            self.current = event.current or 0
            self.total = event.total or 0
            for name in metadata.get("systems", []):
                self._system(str(name))
            return True

        if stage == "system_queued" and system:
            row = self._system(system)
            row.status = "queued"
            row.message = "Waiting"
            return True

        if stage in {"system_started", "system_progress"} and system:
            row = self._system(system)
            row.status = "running"
            row.current = event.current
            row.total = event.total
            row.message = event.message
            row.detail = event.detail
            return True

        if stage == "system_completed" and system:
            row = self._system(system)
            row.status = "success"
            row.current = event.current
            row.total = event.total
            row.message = "Done"
            return True

        if stage == "system_failed" and system:
            row = self._system(system)
            row.status = "error"
            row.message = "Failed"
            row.detail = event.detail
            return True

        if stage == "overall_progress":
            self.current = event.current or 0
            self.total = event.total or self.total
            self.succeeded = int(metadata.get("succeeded", self.succeeded))
            self.failed = int(metadata.get("failed", self.failed))
            return True

        if stage == "refresh_completed":
            self.status = "error" if event.status == "error" else "success"
            self.current = event.current or self.current
            self.total = event.total if event.total is not None else self.total
            self.succeeded = int(metadata.get("succeeded", self.succeeded))
            self.failed = int(metadata.get("failed", self.failed))
            return True

        return True

    def _system(self, name: str) -> SystemRefreshState:
        if name not in self.systems:
            self.systems[name] = SystemRefreshState(system=name)
            self.order.append(name)
        return self.systems[name]
