"""Typed intents: the only thing an agent may hand to the gate."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

ACTIONS = {
    "read_record", "export_data", "send_email", "issue_refund",
    "pay_vendor", "change_permissions", "delete_record", "run_sql",
}
DATA_CLASSES = {"public", "internal", "confidential", "pii", "regulated"}


class IntentError(ValueError):
    """The agent proposed something that is not a well-formed intent."""


@dataclass(frozen=True)
class Intent:
    agent_id: str
    action: str
    resource: str
    data_class: str = "internal"
    amount: float = 0.0
    destination: str = ""
    justification: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.agent_id or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", self.agent_id):
            raise IntentError(f"bad agent_id {self.agent_id!r}")
        if self.action not in ACTIONS:
            raise IntentError(f"unknown action {self.action!r}; allowed: {sorted(ACTIONS)}")
        if self.data_class not in DATA_CLASSES:
            raise IntentError(f"unknown data_class {self.data_class!r}")
        if not isinstance(self.amount, (int, float)) or self.amount < 0:
            raise IntentError("amount must be a non-negative number")
        if not self.resource:
            raise IntentError("resource is required")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        known = {k: d[k] for k in ("agent_id", "action", "resource", "data_class",
                                   "amount", "destination", "justification") if k in d}
        if "amount" in known:
            try:
                known["amount"] = float(known["amount"])
            except (TypeError, ValueError) as e:
                raise IntentError("amount must be numeric") from e
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(**known, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


INTENT_LINE = re.compile(r"^\s*INTENT\s+(\{.*\})\s*$", re.MULTILINE)


def extract_intents(text: str, default_agent: str) -> tuple[list[Intent], list[str]]:
    """Pull `INTENT {json}` lines out of model output. Returns (intents, errors).

    Malformed proposals are never executed: they become errors the gate reports.
    """
    intents: list[Intent] = []
    errors: list[str] = []
    for m in INTENT_LINE.finditer(text):
        try:
            d = json.loads(m.group(1))
            d.setdefault("agent_id", default_agent)
            intents.append(Intent.from_dict(d))
        except (json.JSONDecodeError, IntentError, TypeError) as e:
            errors.append(f"rejected malformed intent: {e}")
    return intents, errors
