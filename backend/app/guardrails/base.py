from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal

Action = Literal["allow", "warn", "human", "block"]

@dataclass
class GuardrailVerdict:
    layer: str
    action: Action
    reasons: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)
