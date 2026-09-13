"""TriageContext: everything the judge may look at. Built by gather.py from measured sources."""

from __future__ import annotations

from dataclasses import dataclass, field

from judge.core.models import Incident, ServiceEntry, Signal


@dataclass
class TriageContext:
    incident: Incident
    signals: list[Signal]
    services: list[ServiceEntry | None]
    metrics: dict[str, dict[str, float | None]]  # service -> metric -> value
    open_incidents: list[Incident] = field(default_factory=list)
    runbook_id: str | None = None
    runbook_via: str | None = None
    runbook_match_ok: bool | None = None
    runbook_markdown: str | None = None
    runbook_action: dict | None = None
    time_scale: float = 1.0
    catalog: dict[str, ServiceEntry] = field(default_factory=dict)
    other_metrics: dict[str, dict[str, float | None]] = field(default_factory=dict)  # services of other incidents

    def summary(self) -> str:
        """Compact, redacted, internal-only description (used for index lookup and Linear)."""
        inc = self.incident
        lines = [f"environment={inc.environment.value} services={','.join(inc.services)}"]
        for s in self.signals[-5:]:
            lines.append(
                f"- [{s.source}] {s.error_type} at {s.culprit} count={s.count} users={s.user_count}"
                + (f" burn={s.burn_rate:.1f}" if s.burn_rate else "")
            )
        for svc, m in self.metrics.items():
            vals = " ".join(f"{k}={v:.3f}" for k, v in m.items() if v is not None)
            lines.append(f"- metrics {svc}: {vals}")
        return "\n".join(lines)
