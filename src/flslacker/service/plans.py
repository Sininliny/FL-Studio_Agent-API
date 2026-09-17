"""Resolve agent operations into immutable, hash-locked plans; build inverse plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from flslacker.contracts import canonical as canon
from flslacker.contracts import wire
from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.models import (
    FIELD_TO_FL,
    FL_TO_FIELD,
    FLOAT_FIELDS,
    DeleteOp,
    DiffRow,
    InsertOp,
    NoteSet,
    NoteTemplate,
    Plan,
    PlanLimits,
    Snapshot,
    UpdateOp,
)

# Documented defaults (installed Piano Roll reference), used only when FL did not report
# getDefaultNoteProperties(); plans say so in their warnings.
DOCUMENTED_NOTE_DEFAULTS: dict[str, Any] = {
    "group": 0,
    "pan": 0.5,
    "velocity": 0.8,
    "release": 0.5,
    "color": 0,
    "fcut": 0.5,
    "fres": 0.5,
    "pitchofs": 0,
    "slide": False,
    "porta": False,
    "muted": False,
    "selected": False,
    "repeats": 0,
}
CONTEXT_FIELDS = ("pitch", "start_tick", "duration_tick")


@dataclass
class ScoreContext:
    snapshot: Snapshot
    raw_notes: list[dict[str, Any]]
    raw_markers: list[dict[str, Any]]


def _agent_view(record: dict[str, Any], fields: tuple[str, ...] | None = None) -> dict[str, Any]:
    view = {FL_TO_FIELD[k]: v for k, v in record.items() if k in FL_TO_FIELD}
    if fields is not None:
        view = {k: v for k, v in view.items() if k in fields}
    return view


def _coerce(field: str, value: Any) -> Any:
    if field in FLOAT_FIELDS and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _note_label(record: dict[str, Any]) -> str:
    return f"pitch {record.get('number')} @ {record.get('time')}"


def _unsupported(field: str) -> FlsError:
    return FlsError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        f"Field '{field}' was not readable in this capture, so it cannot be written safely.",
        details={"field": field},
    )


def resolve(ctx: ScoreContext, operations: list[Any]) -> tuple[list[dict], list[DiffRow], PlanLimits, list[str]]:
    snap = ctx.snapshot
    by_id = {n.note_id: n for n in snap.notes}
    resolved: list[dict[str, Any]] = []
    diff: list[DiffRow] = []
    warnings: list[str] = []
    touched: set[str] = set()
    clients: set[str] = set()
    fields: set[str] = set()
    pitch_delta = time_delta = 0
    velocity_delta = 0.0
    counts = {"update": 0, "insert": 0, "delete": 0}
    float_writes: set[str] = set()

    if len(operations) > wire.MAX_PATCH_OPERATIONS:
        raise FlsError(ErrorCode.LIMIT_EXCEEDED, f"A plan may contain at most {wire.MAX_PATCH_OPERATIONS} operations.")

    for index, op in enumerate(operations):
        where = f"operations[{index}]"
        if isinstance(op, (UpdateOp, DeleteOp)):
            note = by_id.get(op.note_id)
            if note is None:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"{where}: unknown note_id {op.note_id} in this snapshot.")
            if not note.in_scope:
                raise FlsError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{where}: {op.note_id} is outside the captured scope '{snap.scope}'.",
                )
            if op.note_id in touched:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"{where}: {op.note_id} is targeted by more than one operation.")
            touched.add(op.note_id)
            record = ctx.raw_notes[note.ordinal]

        if isinstance(op, UpdateOp):
            fl_set: dict[str, Any] = {}
            before: dict[str, Any] = {}
            after: dict[str, Any] = {}
            for field, value in op.set.changes().items():
                if field in snap.unsupported_fields:
                    raise _unsupported(field)
                fl_name = FIELD_TO_FL[field]
                value = _coerce(field, value)
                old = record.get(fl_name)
                if _same(old, value):
                    continue
                fl_set[fl_name] = value
                before[field] = old
                after[field] = value
                if field in FLOAT_FIELDS:
                    float_writes.add(field)
                if field == "pitch" and isinstance(old, (int, float)):
                    pitch_delta = max(pitch_delta, int(abs(value - old)))
                    if abs(value - old) > 12:
                        warnings.append(f"{op.note_id}: pitch moves {int(abs(value - old))} semitones.")
                    if record.get("slide") or record.get("porta") or record.get("pitchofs"):
                        warnings.append(f"{op.note_id}: expressive note (slide/portamento/fine pitch); confirm intent.")
                if field == "start_tick" and isinstance(old, (int, float)):
                    time_delta = max(time_delta, int(abs(value - old)))
                if field == "duration_tick" and isinstance(old, (int, float)):
                    time_delta = max(time_delta, int(abs(value - old)))
                if field == "velocity" and isinstance(old, (int, float)):
                    velocity_delta = max(velocity_delta, float(abs(value - old)))
            if not fl_set:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"{where}: the update changes nothing.")
            fields.update(after)
            counts["update"] += 1
            context = _agent_view(record, CONTEXT_FIELDS)
            resolved.append({"op": "update", "ordinal": note.ordinal, "fingerprint": note.fingerprint, "set": fl_set})
            diff.append(
                DiffRow(
                    kind="update",
                    note_id=op.note_id,
                    before={**context, **before},
                    after={**context, **after},
                    changed_fields=sorted(after),
                    summary=f"{op.note_id} ({_note_label(record)}): "
                    + ", ".join(f"{k} {before[k]} -> {after[k]}" for k in sorted(after)),
                )
            )
        elif isinstance(op, DeleteOp):
            counts["delete"] += 1
            resolved.append({"op": "delete", "ordinal": note.ordinal, "fingerprint": note.fingerprint})
            diff.append(
                DiffRow(
                    kind="delete",
                    note_id=op.note_id,
                    before=_agent_view(record),
                    after=None,
                    changed_fields=[],
                    summary=f"delete {op.note_id} ({_note_label(record)})",
                )
            )
            missing = set(canon.NOTE_FIELD_NAMES) - set(record)
            if missing:
                warnings.append(f"{op.note_id}: some properties were unreadable; Restore cannot re-create it.")
        elif isinstance(op, InsertOp):
            if op.client_id in clients:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"{where}: duplicate client_id {op.client_id}.")
            clients.add(op.client_id)
            unreadable = [f for f in snap.unsupported_fields if f in FIELD_TO_FL]
            if unreadable:
                raise _unsupported(unreadable[0])
            defaults = snap.default_note
            if not defaults or set(defaults) != set(canon.NOTE_FIELD_NAMES):
                defaults = dict(DOCUMENTED_NOTE_DEFAULTS)
                warnings.append("FL did not report default note properties; documented defaults were used for inserts.")
            note_record = {k: defaults[k] for k in canon.NOTE_FIELD_NAMES if k in defaults}
            note_record["selected"] = snap.scope == "selected"
            for field, value in op.note.model_dump().items():
                if value is not None:
                    note_record[FIELD_TO_FL[field]] = _coerce(field, value)
            for name in ("pan", "velocity", "release", "fcut", "fres"):
                note_record[name] = float(note_record[name])
            problems = [p for p in (canon.check_note_value(k, v) for k, v in note_record.items()) if p]
            if problems or set(note_record) != set(canon.NOTE_FIELD_NAMES):
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"{where}: invalid note template: {'; '.join(problems)[:300]}")
            counts["insert"] += 1
            float_writes.update(FLOAT_FIELDS)
            resolved.append({"op": "insert", "note": note_record})
            diff.append(
                DiffRow(
                    kind="insert",
                    client_id=op.client_id,
                    before=None,
                    after=_agent_view(note_record),
                    changed_fields=sorted(_agent_view(note_record)),
                    summary=f"insert {op.client_id} ({_note_label(note_record)})",
                )
            )
        else:  # pragma: no cover - discriminated union
            raise FlsError(ErrorCode.INVALID_ARGUMENT, f"{where}: unknown operation")

    problems = canon.validate_operations(ctx.raw_notes, resolved)
    if problems:
        raise FlsError(ErrorCode.INVALID_ARGUMENT, "Plan failed validation: " + "; ".join(problems[:5]))
    if float_writes:
        warnings.append(
            "FL may store " + ", ".join(sorted(float_writes)) + " quantized; verification tolerates "
            f"±{canon.FLOAT_TOLERANCE}."
        )
    if "start_tick" in fields or counts["insert"] or counts["delete"]:
        warnings.append("Note order may change in FL; verification compares content, not order.")
    limits = PlanLimits(
        operation_count=len(resolved),
        max_operations=wire.MAX_PATCH_OPERATIONS,
        update_count=counts["update"],
        insert_count=counts["insert"],
        delete_count=counts["delete"],
        affected_notes=len(resolved),
        fields=sorted(fields),
        max_pitch_delta=pitch_delta,
        max_time_delta_ticks=time_delta,
        max_velocity_delta=round(velocity_delta, 6),
    )
    return resolved, diff, limits, list(dict.fromkeys(warnings))


def compute_plan_hash(plan: Plan) -> str:
    return canon.sha256_hex(plan.model_dump(mode="json", exclude={"plan_hash"}))


def plan_intact(plan: Plan) -> bool:
    return compute_plan_hash(plan) == plan.plan_hash


def build_plan(
    ctx: ScoreContext,
    operations: list[Any],
    *,
    plan_id: str,
    target_id: str,
    rationale: str,
    created_by: str,
    now: datetime,
    ttl: timedelta,
    kind: str = "edit",
    restores_receipt_id: str | None = None,
) -> Plan:
    snap = ctx.snapshot
    resolved, diff, limits, warnings = resolve(ctx, operations)
    predicted = canon.predict_after(ctx.raw_notes, resolved)
    plan = Plan(
        plan_id=plan_id,
        kind=kind,
        session_id=snap.session_id,
        target_id=target_id,
        target_label=snap.target.label,
        snapshot_id=snap.snapshot_id,
        scope=snap.scope,
        ppq=snap.ppq,
        base_state_hash=snap.state_hash,
        base_note_count=len(ctx.raw_notes),
        operations=operations,
        resolved_operations=resolved,
        diff=diff,
        limits=limits,
        predicted_content_hash=canon.content_hash(snap.ppq, predicted, ctx.raw_markers),
        rationale=rationale,
        warnings=warnings,
        restores_receipt_id=restores_receipt_id,
        created_by=created_by,
        created_at=now,
        expires_at=now + ttl,
        plan_hash="0" * 64,
    )
    return plan.model_copy(update={"plan_hash": compute_plan_hash(plan)})


def summary_text(plan: Plan) -> str:
    parts = []
    for key, label in (("update_count", "update"), ("insert_count", "insert"), ("delete_count", "delete")):
        count = getattr(plan.limits, key)
        if count:
            parts.append(f"{count} {label}{'s' if count != 1 else ''}")
    return ", ".join(parts) or "no changes"


def text_diff(plan: Plan) -> str:
    lines = [
        f"Plan {plan.plan_id} ({plan.kind}) on '{plan.target_label}' [{plan.scope}]",
        f"Base snapshot {plan.snapshot_id} state {plan.base_state_hash[:12]}; {summary_text(plan)}",
    ]
    for row in plan.diff:
        marker = {"update": "~", "insert": "+", "delete": "-"}[row.kind]
        lines.append(f"  {marker} {row.summary}")
    return "\n".join(lines)


# ------------------------------------------------------------------ verification & inverse


def pair_records(expected: list[dict], observed: list[dict], tolerance: float = canon.FLOAT_TOLERANCE) -> list[int | None]:
    """Map each expected record to a distinct observed index (order-independent, float tolerant)."""

    def key(record: dict) -> tuple[str, dict]:
        return canon._split_record(record)

    buckets: dict[str, list[tuple[int, dict]]] = {}
    for index, record in enumerate(observed):
        exact, floats = key(record)
        buckets.setdefault(exact, []).append((index, floats))
    mapping: list[int | None] = []
    for record in expected:
        exact, floats = key(record)
        found = None
        candidates = buckets.get(exact, [])
        for position, (index, other) in enumerate(candidates):
            if set(other) == set(floats) and all(abs(other[k] - floats[k]) <= tolerance for k in floats):
                found = index
                del candidates[position]
                break
        mapping.append(found)
    return mapping


def predicted_with_origin(raw_notes: list[dict], resolved: list[dict]) -> tuple[list[dict], list[tuple[str, int]]]:
    """Like ``canonical.predict_after`` but also returns where each resulting record came from."""
    rows = [(("kept", i), dict(r)) for i, r in enumerate(raw_notes)]
    for op in resolved:
        if op["op"] == "update":
            rows[op["ordinal"]][1].update(op["set"])
    deleted = {op["ordinal"] for op in resolved if op["op"] == "delete"}
    rows = [row for row in rows if row[0][1] not in deleted]
    insert_index = 0
    for op in resolved:
        if op["op"] == "insert":
            rows.append((("insert", insert_index), dict(op["note"])))
            insert_index += 1
    return [r for _, r in rows], [o for o, _ in rows]


def inverse_operations(before: ScoreContext, plan: Plan, after: ScoreContext) -> list[Any]:
    """Operations that turn the verified after-state back into the before-state."""
    predicted, origins = predicted_with_origin(before.raw_notes, plan.resolved_operations)
    mapping = pair_records(predicted, after.raw_notes)
    if any(m is None for m in mapping) or len(predicted) != len(after.raw_notes):
        raise FlsError(ErrorCode.VERIFICATION_FAILED, "The after-state does not match the plan; restore is unavailable.")
    position = {origin: mapping[i] for i, origin in enumerate(origins)}
    after_notes = after.snapshot.notes
    operations: list[Any] = []
    insert_index = 0
    try:
        for op in plan.resolved_operations:
            if op["op"] == "update":
                target = position[("kept", op["ordinal"])]
                old = before.raw_notes[op["ordinal"]]
                current = after.raw_notes[target]
                # A float FL quantized back to its old value needs no restore.
                values = {FL_TO_FIELD[k]: old[k] for k in op["set"] if not _same(old.get(k), current.get(k))}
                if values:
                    operations.append(
                        UpdateOp(op="note.update", note_id=after_notes[target].note_id, set=NoteSet(**values)))
            elif op["op"] == "insert":
                target = position[("insert", insert_index)]
                insert_index += 1
                operations.append(DeleteOp(op="note.delete", note_id=after_notes[target].note_id))
        for op in plan.resolved_operations:
            if op["op"] == "delete":
                old = before.raw_notes[op["ordinal"]]
                if set(old) != set(canon.NOTE_FIELD_NAMES):
                    raise FlsError(ErrorCode.UNSUPPORTED_CAPABILITY, "A deleted note had unreadable properties.")
                template = NoteTemplate(**{FL_TO_FIELD[k]: v for k, v in old.items()})
                operations.append(InsertOp(op="note.insert", client_id=f"restore-{op['ordinal']}", note=template))
    except ValidationError as exc:
        raise FlsError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "The recorded before-values cannot be written back by the baseline writer.",
            details={"errors": exc.errors(include_url=False)[:3]},
        )
    if not operations:
        raise FlsError(ErrorCode.UNSUPPORTED_CAPABILITY, "The verified after-state already equals the before-state.")
    for op in operations:
        if isinstance(op, (UpdateOp, DeleteOp)):
            note = after_notes[int(op.note_id[1:])]
            if not note.in_scope:
                raise FlsError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    f"{op.note_id} is outside the verified capture's scope; recapture with scope 'all_exposed'.",
                )
    return operations
