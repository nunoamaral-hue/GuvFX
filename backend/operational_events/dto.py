"""WP5.1 — immutable DTOs (frozen dataclasses; the house style — cf. execution.broker_gate.GateDecision,
execution.runtime_pause.ResumeResult). No raw ORM instance ever crosses the service boundary; callers
receive these value objects (or their ``as_dict()``)."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OperationalEventDTO:
    """An immutable, non-secret view of one OperationalEvent row."""
    id: int
    timestamp: Optional[str]
    account_id: Optional[int]
    runtime_uuid: str
    category: str
    event_type: str
    severity: str
    status: str
    reason_code: str
    summary: str
    source: str
    correlation_id: str
    state_version: Optional[int]
    actor: str
    customer_visible: bool
    resolved: bool
    resolved_at: Optional[str]
    metadata: dict

    @classmethod
    def from_model(cls, ev) -> "OperationalEventDTO":
        return cls(
            id=ev.id,
            timestamp=ev.created_at.isoformat() if ev.created_at else None,
            account_id=ev.account_id,
            runtime_uuid=ev.runtime_uuid or "",
            category=ev.category,
            event_type=ev.event_type,
            severity=ev.severity,
            status=ev.status or "",
            reason_code=ev.reason_code or "",
            summary=ev.summary or "",
            source=ev.source or "",
            correlation_id=ev.correlation_id or "",
            state_version=ev.state_version,
            actor=ev.actor or "",
            customer_visible=bool(ev.customer_visible),
            resolved=bool(ev.resolved),
            resolved_at=ev.resolved_at.isoformat() if ev.resolved_at else None,
            # deepcopy so the DTO owns its metadata outright — mutating it can never reach back into the
            # source ORM instance's (possibly still-referenced) nested dicts.
            metadata=copy.deepcopy(ev.metadata or {}),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "account_id": self.account_id,
            "runtime_uuid": self.runtime_uuid,
            "category": self.category,
            "event_type": self.event_type,
            "severity": self.severity,
            "status": self.status,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "source": self.source,
            "correlation_id": self.correlation_id,
            "state_version": self.state_version,
            "actor": self.actor,
            "customer_visible": self.customer_visible,
            "resolved": self.resolved,
            "resolved_at": self.resolved_at,
            # deepcopy (not dict()) so a caller mutating a NESTED key of the returned dict cannot alter
            # the frozen DTO's internal state — a shallow copy would share nested dicts/lists by reference.
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True)
class OperationalSummaryDTO:
    """An immutable, deterministic per-account operational summary. State fields are read LIVE from the
    authoritative sources; latest_*/event_counts aggregate the OperationalEvent timeline."""
    account_id: int
    generated_at: str
    validation_state: dict
    health_state: dict
    runtime_pause: dict
    credential_status: dict
    disconnect_state: dict
    latest_validation: Optional[dict]
    latest_error: Optional[dict]
    latest_warning: Optional[dict]
    event_counts: dict
    last_update: Optional[str]

    def as_dict(self) -> dict:
        # deepcopy each nested dict — several carry a further nested payload (runtime_pause.record,
        # latest_*.metadata, event_counts.by_severity/by_category), which a shallow dict() would alias.
        return {
            "account_id": self.account_id,
            "generated_at": self.generated_at,
            "validation_state": copy.deepcopy(self.validation_state),
            "health_state": copy.deepcopy(self.health_state),
            "runtime_pause": copy.deepcopy(self.runtime_pause),
            "credential_status": copy.deepcopy(self.credential_status),
            "disconnect_state": copy.deepcopy(self.disconnect_state),
            "latest_validation": copy.deepcopy(self.latest_validation) if self.latest_validation else None,
            "latest_error": copy.deepcopy(self.latest_error) if self.latest_error else None,
            "latest_warning": copy.deepcopy(self.latest_warning) if self.latest_warning else None,
            "event_counts": copy.deepcopy(self.event_counts),
            "last_update": self.last_update,
        }
