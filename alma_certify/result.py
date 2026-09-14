"""Result and metric types shared by every test."""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional


class Status:
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


class Severity:
    REQUIRED = "required"
    CONDITIONAL = "conditional"
    INFORMATIONAL = "informational"


class Direction:
    HIGHER = "higher_is_better"
    LOWER = "lower_is_better"
    INFO = "info"


@dataclasses.dataclass
class Metric:
    name: str
    value: float
    unit: str
    direction: str = Direction.INFO
    primary: bool = False
    # The device that produced the figure, when a benchmark ran on more than one of the same kind
    # (e.g. clpeak on both GPUs of a machine). Absent for a single-device benchmark, so the field
    # only appears where it distinguishes rows: the same metric name can then occur once per device.
    device: Optional[str] = None
    # The 0-based position of this card within its (identically named) group, set only by the
    # ``--all-gpus`` path so two physically identical cards - which share a device string and carry
    # no PCI id of their own - can still be told apart and submitted as individual results.
    device_ordinal: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "direction": self.direction,
        }
        if self.primary:
            d["primary"] = True
        if self.device is not None:
            d["device"] = self.device
        if self.device_ordinal is not None:
            d["device_ordinal"] = self.device_ordinal
        return d


@dataclasses.dataclass
class TestResult:
    id: str
    run_type: str
    category: str
    status: str
    severity: Optional[str] = None
    reason: Optional[str] = None
    started_at: Optional[str] = None
    duration_s: Optional[float] = None
    metrics: List[Metric] = dataclasses.field(default_factory=list)
    details: Dict[str, Any] = dataclasses.field(default_factory=dict)
    artifacts: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_type": self.run_type,
            "category": self.category,
            "severity": self.severity,
            "status": self.status,
            "reason": self.reason,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "metrics": [m.to_dict() for m in self.metrics],
            "details": self.details,
            "artifacts": self.artifacts,
        }


def passed(**kw: Any) -> TestResult:
    """Convenience used by tests: build a pass result with partial fields."""
    kw.setdefault("status", Status.PASS)
    return TestResult(**kw)
