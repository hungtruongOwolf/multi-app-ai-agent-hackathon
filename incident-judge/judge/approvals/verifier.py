"""Rule P14: who may approve what. Every approval attempt — valid or not — becomes an Approval
record so the audit log shows rejected attempts (e.g. an intruder clicking Confirm)."""

from __future__ import annotations

from datetime import datetime

from judge.core.models import Approval, Incident, ServiceEntry, now


class ApprovalVerifier:
    def __init__(self, config, settings, catalog: dict[str, ServiceEntry] | None = None):
        self.config = config
        self.settings = settings
        self.catalog = catalog if catalog is not None else config.catalog

    def authorized_users(self, incident: Incident) -> set[str]:
        users = set(self.config.policy.oncall_allowlist)
        for name in incident.services:
            entry = self.catalog.get(name)
            if entry:
                users.update(entry.owners)
        return users

    def verify(self, *, incident: Incident, kind, subject_hash: str, user_id: str, is_bot: bool, verdict,
               via, requested_at: datetime | None = None, provided_hash: str | None = None,
               ts: datetime | None = None) -> Approval:
        """provided_hash: the hash the human referenced (8-char prefix from a text command, or the full hash
        carried by a button). Defaults to subject_hash."""
        ts = ts or now()
        provided = (provided_hash or subject_hash).lower()
        matches = bool(provided) and subject_hash.lower().startswith(provided) and len(provided) >= 8

        reasons: list[str] = []
        if is_bot:
            reasons.append("approver is a bot")
        if not user_id or user_id not in self.authorized_users(incident):
            reasons.append(f"user {user_id or '<none>'} not in on-call allowlist or service owners")
        if not matches:
            reasons.append(f"hash {provided[:8]} does not match current subject {subject_hash[:8]}")
        if verdict not in ("approve", "reject"):
            reasons.append(f"unknown verdict {verdict!r}")
        if requested_at is not None and ts < requested_at:
            reasons.append("approval predates the request")

        return Approval(
            incident_id=incident.id,
            kind=kind,
            # bind to the full current hash only when the reference matched; otherwise keep what was referenced
            subject_hash=subject_hash if matches else provided,
            user_id=user_id or "",
            verdict=verdict if verdict in ("approve", "reject") else "reject",
            via=via,
            valid=not reasons,
            reason="; ".join(reasons),
            ts=ts,
        )
