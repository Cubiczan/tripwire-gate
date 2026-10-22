"""Deterministic policy evaluation. No model is consulted here, ever.

Rules are data (loaded from Zetaris, or the SQLite fallback). Each rule matches on
action and optional conditions and yields allow / review / deny. Precedence is
fixed: deny > review > allow, and an intent no rule allows is denied (default deny).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .intents import Intent

PRECEDENCE = {"deny": 3, "review": 2, "allow": 1}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    action: str            # an action name or "*"
    effect: str            # allow | review | deny
    reason: str
    data_classes: tuple[str, ...] = ()   # match only these classes (empty = any)
    min_amount: float | None = None      # match when amount >= min_amount
    max_amount: float | None = None      # match when amount <= max_amount
    dest_not_suffix: str | None = None   # match when destination lacks this suffix
    agents: tuple[str, ...] = ()         # match only these agents (empty = any)

    def matches(self, i: Intent) -> bool:
        if self.action not in ("*", i.action):
            return False
        if self.data_classes and i.data_class not in self.data_classes:
            return False
        if self.min_amount is not None and i.amount < self.min_amount:
            return False
        if self.max_amount is not None and i.amount > self.max_amount:
            return False
        if self.dest_not_suffix is not None and (not i.destination or i.destination.endswith(self.dest_not_suffix)):
            return False
        if self.agents and i.agent_id not in self.agents:
            return False
        return True

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Rule":
        def tup(v: Any) -> tuple[str, ...]:
            if not v:
                return ()
            return tuple(s.strip() for s in str(v).split(",") if s.strip())

        def num(v: Any) -> float | None:
            return None if v in (None, "", "null") else float(v)

        return cls(
            rule_id=str(r["rule_id"]), action=str(r["action"]), effect=str(r["effect"]).lower(),
            reason=str(r["reason"]), data_classes=tup(r.get("data_classes")),
            min_amount=num(r.get("min_amount")), max_amount=num(r.get("max_amount")),
            dest_not_suffix=(r.get("dest_not_suffix") or None), agents=tup(r.get("agents")),
        )


@dataclass
class Decision:
    verdict: str
    intent: Intent
    matched: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    policy_version: str = ""

    def explain(self) -> str:
        why = "; ".join(f"{rid}: {r}" for rid, r in zip(self.matched, self.reasons)) or "no rule allows this action (default deny)"
        i = self.intent
        return (f"{self.verdict.upper()} {i.agent_id} -> {i.action}({i.resource}"
                f"{', $%.2f' % i.amount if i.amount else ''}"
                f"{', to ' + i.destination if i.destination else ''}) because {why}")

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "intent": self.intent.to_dict(), "matched": self.matched,
                "reasons": self.reasons, "policy_version": self.policy_version, "explanation": self.explain()}


def policy_version(rules: list[Rule]) -> str:
    blob = json.dumps([r.__dict__ for r in sorted(rules, key=lambda r: r.rule_id)], sort_keys=True, default=list)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def evaluate(intent: Intent, rules: list[Rule]) -> Decision:
    version = policy_version(rules)
    hits = [r for r in rules if r.matches(intent)]
    if not hits:
        return Decision("deny", intent, [], [], version)
    top = max(PRECEDENCE[r.effect] for r in hits)
    winners = sorted((r for r in hits if PRECEDENCE[r.effect] == top), key=lambda r: r.rule_id)
    verdict = next(k for k, v in PRECEDENCE.items() if v == top)
    return Decision(verdict, intent, [r.rule_id for r in winners], [r.reason for r in winners], version)


# Seed policy: written into Zetaris (or the fallback) by `tripwire init`.
SEED_RULES: list[dict[str, Any]] = [
    {"rule_id": "R01-read-ok", "action": "read_record", "effect": "allow", "reason": "reads of non-regulated data are routine",
     "data_classes": "public,internal,confidential"},
    {"rule_id": "R02-read-regulated", "action": "read_record", "effect": "review", "reason": "regulated data needs a named human approver",
     "data_classes": "regulated,pii"},
    {"rule_id": "R03-export-internal", "action": "export_data", "effect": "allow", "reason": "public/internal exports are allowed",
     "data_classes": "public,internal"},
    {"rule_id": "R04-export-pii", "action": "export_data", "effect": "deny", "reason": "PII/regulated data may never leave via export",
     "data_classes": "pii,regulated,confidential"},
    {"rule_id": "R05-email-internal", "action": "send_email", "effect": "allow", "reason": "email inside the company domain"},
    {"rule_id": "R06-email-external", "action": "send_email", "effect": "review", "reason": "external recipients need review",
     "dest_not_suffix": "@acme.example"},
    {"rule_id": "R07-refund-small", "action": "issue_refund", "effect": "allow", "reason": "refunds up to $200 are auto-approved",
     "max_amount": 200},
    {"rule_id": "R08-refund-large", "action": "issue_refund", "effect": "review", "reason": "refunds over $200 need finance review",
     "min_amount": 200.01},
    {"rule_id": "R09-pay-cap", "action": "pay_vendor", "effect": "review", "reason": "every vendor payment is reviewed"},
    {"rule_id": "R10-pay-hard-cap", "action": "pay_vendor", "effect": "deny", "reason": "payments of $10,000+ are blocked for agents",
     "min_amount": 10000},
    {"rule_id": "R11-perm", "action": "change_permissions", "effect": "deny", "reason": "agents may not change access rights"},
    {"rule_id": "R12-delete", "action": "delete_record", "effect": "review", "reason": "deletes are irreversible; human must confirm"},
    {"rule_id": "R13-sql", "action": "run_sql", "effect": "allow", "reason": "read-only SQL through the gate is allowed",
     "data_classes": "public,internal"},
]


def seed_rules() -> list[Rule]:
    return [Rule.from_row(r) for r in SEED_RULES]
