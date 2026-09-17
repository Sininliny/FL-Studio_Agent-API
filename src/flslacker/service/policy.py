"""Permission grants: read and propose are open; scoped writes need a UI-issued grant.

Agents can never create, widen or approve grants. A grant binds session, target label,
scope, operation kinds, fields, per-note limits, expiry and a change budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flslacker.contracts.models import FL_TO_FIELD, Grant, GrantConstraints, Plan
from flslacker.service.snapshots import label_key

OP_NAMES = {"update": "note.update", "insert": "note.insert", "delete": "note.delete"}
SCOPE_RANK = {"selected": 0, "all_exposed": 1}


@dataclass
class Coverage:
    covered: bool
    grant: Grant | None
    reason: str


def plan_constraints(plan: Plan) -> GrantConstraints:
    """The narrowest constraints that still cover ``plan`` (used for exact-plan approval)."""
    kinds = sorted({OP_NAMES[op["op"]] for op in plan.resolved_operations})
    return GrantConstraints(
        scope=plan.scope,
        operation_kinds=kinds,
        fields=sorted(set(plan.limits.fields)),
        max_notes=max(1, plan.limits.affected_notes),
        max_pitch_delta=plan.limits.max_pitch_delta,
        max_time_delta_ticks=plan.limits.max_time_delta_ticks,
        max_velocity_delta=plan.limits.max_velocity_delta,
    )


def check(grant: Grant, plan: Plan, now: datetime) -> str | None:
    """Return None if ``grant`` covers ``plan`` right now, else the reason it does not."""
    c = grant.constraints
    if grant.revoked:
        return "grant revoked"
    if now >= grant.expires_at:
        return "grant expired"
    if grant.session_id != plan.session_id:
        return "grant belongs to another session"
    if grant.target_label is not None and label_key(grant.target_label) != label_key(plan.target_label):
        return "grant is for another target"
    if SCOPE_RANK[c.scope] < SCOPE_RANK[plan.scope]:
        return f"grant scope '{c.scope}' does not cover '{plan.scope}'"
    if grant.max_uses is not None and grant.uses >= grant.max_uses:
        return "grant has no uses left"
    if grant.kind == "plan" and grant.plan_hash != plan.plan_hash:
        return "grant approves a different plan"
    kinds = {OP_NAMES[op["op"]] for op in plan.resolved_operations}
    if not kinds <= set(c.operation_kinds):
        return f"operation kinds {sorted(kinds - set(c.operation_kinds))} not granted"
    fields = {FL_TO_FIELD[f] for op in plan.resolved_operations if op["op"] == "update" for f in op["set"]}
    if not fields <= set(c.fields):
        return f"fields {sorted(fields - set(c.fields))} not granted"
    if grant.budget_used + plan.limits.affected_notes > c.max_notes:
        return f"change budget exceeded ({grant.budget_used}+{plan.limits.affected_notes} > {c.max_notes} notes)"
    if c.max_pitch_delta is not None and plan.limits.max_pitch_delta > c.max_pitch_delta:
        return f"pitch change {plan.limits.max_pitch_delta} exceeds {c.max_pitch_delta} semitones"
    if c.max_time_delta_ticks is not None and plan.limits.max_time_delta_ticks > c.max_time_delta_ticks:
        return f"time change exceeds {c.max_time_delta_ticks} ticks"
    if c.max_velocity_delta is not None and plan.limits.max_velocity_delta > c.max_velocity_delta + 1e-9:
        return f"velocity change exceeds {c.max_velocity_delta}"
    return None


def find(grants: list[Grant], plan: Plan, now: datetime) -> Coverage:
    reasons = []
    # Prefer exact-plan grants, then the grant expiring first.
    for grant in sorted(grants, key=lambda g: (g.kind != "plan", g.expires_at)):
        reason = check(grant, plan, now)
        if reason is None:
            return Coverage(True, grant, f"covered by {grant.kind} grant {grant.grant_id}")
        reasons.append(reason)
    if not grants:
        return Coverage(False, None, "no active write grant; approve the plan in the FL Slacker UI")
    return Coverage(False, None, "no grant covers this plan: " + "; ".join(dict.fromkeys(reasons)))
