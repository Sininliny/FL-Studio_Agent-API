"""Validate raw FL captures and turn them into canonical Snapshot models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flslacker.contracts import canonical as canon
from flslacker.contracts import wire
from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.models import FL_TO_FIELD, Marker, Note, Selection, Snapshot, Target

RAW_KEYS = {
    "scope",
    "target_label",
    "captured_at",
    "ppq",
    "time_signature",
    "timeline_selection",
    "notes",
    "markers",
    "default_note",
    "field_errors",
    "extra_note_attributes",
    "state_hash",
    "content_hash",
    "host",
}
PRIMARY_FL_FIELDS = ("number", "time", "length", "velocity")


def _bad(message: str) -> FlsError:
    return FlsError(ErrorCode.INVALID_ARGUMENT, f"Rejected capture: {message}")


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def validate_raw(raw: Any) -> None:
    if not isinstance(raw, dict) or set(raw) != RAW_KEYS:
        raise _bad("unexpected snapshot fields")
    if raw["scope"] not in ("selected", "all_exposed"):
        raise _bad("bad scope")
    label = raw["target_label"]
    if not isinstance(label, str) or not 0 < len(label.strip()) <= 120:
        raise _bad("bad target label")
    ppq = raw["ppq"]
    if isinstance(ppq, bool) or not isinstance(ppq, int) or ppq <= 0:
        raise _bad("bad PPQ")
    notes, markers = raw["notes"], raw["markers"]
    if not isinstance(notes, list) or len(notes) > wire.MAX_SNAPSHOT_NOTES:
        raise FlsError(ErrorCode.LIMIT_EXCEEDED, "Capture exceeds the note limit.")
    if not isinstance(markers, list) or len(markers) > wire.MAX_SNAPSHOT_NOTES:
        raise FlsError(ErrorCode.LIMIT_EXCEEDED, "Capture exceeds the marker limit.")
    for record in notes:
        if not isinstance(record, dict) or not set(record) <= set(canon.NOTE_FIELD_NAMES):
            raise _bad("bad note record")
        if not all(_is_scalar(v) for v in record.values()):
            raise _bad("bad note value")
    for record in markers:
        if not isinstance(record, dict) or not set(record) <= set(canon.MARKER_FIELD_NAMES):
            raise _bad("bad marker record")
        if not all(_is_scalar(v) for v in record.values()):
            raise _bad("bad marker value")
    ts = raw["time_signature"]
    if ts is not None and not (isinstance(ts, list) and len(ts) == 2 and all(isinstance(v, int) for v in ts)):
        raise _bad("bad time signature")
    timeline = raw["timeline_selection"]
    if timeline is not None and not (
        isinstance(timeline, list) and len(timeline) <= 2 and all(isinstance(v, (int, float)) for v in timeline)
    ):
        raise _bad("bad timeline selection")
    default = raw["default_note"]
    if default is not None and not (
        isinstance(default, dict) and set(default) <= set(canon.NOTE_FIELD_NAMES) and all(_is_scalar(v) for v in default.values())
    ):
        raise _bad("bad default note")
    if not isinstance(raw["field_errors"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw["field_errors"].items()
    ):
        raise _bad("bad field errors")
    extras = raw["extra_note_attributes"]
    if not isinstance(extras, list) or not all(isinstance(v, str) for v in extras):
        raise _bad("bad extra attribute list")
    if not isinstance(raw["host"], dict) or len(canon.canonical_dumps(raw["host"])) > 4096:
        raise _bad("bad host info")
    try:
        wire.parse_utc_iso(raw["captured_at"])
    except wire.BridgeError:
        raise _bad("bad capture time")
    try:
        state = canon.state_hash(ppq, notes, markers)
        content = canon.content_hash(ppq, notes, markers)
    except canon.CanonicalError as exc:
        raise _bad(str(exc))
    if state != raw["state_hash"] or content != raw["content_hash"]:
        raise _bad("hash mismatch")


def note_from_record(ordinal: int, record: dict[str, Any], scope: str) -> Note:
    in_scope = scope == "all_exposed" or record.get("selected") is True
    return Note(
        note_id=f"n{ordinal}",
        ordinal=ordinal,
        pitch=record.get("number"),
        start_tick=record.get("time"),
        duration_tick=record.get("length"),
        velocity=record.get("velocity"),
        fl={k: v for k, v in record.items() if k not in PRIMARY_FL_FIELDS},
        in_scope=in_scope,
        fingerprint=canon.note_fingerprint(record),
    )


def build_snapshot(
    raw: dict[str, Any],
    *,
    snapshot_id: str,
    session_id: str,
    target: Target,
    purpose: str,
    job_id: str | None,
    received_at: datetime,
) -> Snapshot:
    scope = raw["scope"]
    notes = [note_from_record(i, r, scope) for i, r in enumerate(raw["notes"])]
    field_names = set(canon.NOTE_FIELD_NAMES)
    present = [r for r in raw["notes"]] or ([raw["default_note"]] if raw["default_note"] else [])
    supported = sorted(
        FL_TO_FIELD[name] for name in field_names if present and all(name in record for record in present)
    )
    unsupported = {
        FL_TO_FIELD.get(k, k): v for k, v in raw["field_errors"].items() if k in field_names
    }
    for name in field_names:
        if present and any(name not in record for record in present):
            unsupported.setdefault(FL_TO_FIELD[name], "not readable on every note")
    selected = [n.note_id for n, r in zip(notes, raw["notes"]) if r.get("selected") is True]
    return Snapshot(
        snapshot_id=snapshot_id,
        session_id=session_id,
        target=target,
        purpose=purpose,
        job_id=job_id,
        captured_at=datetime.fromtimestamp(wire.parse_utc_iso(raw["captured_at"]), timezone.utc),
        received_at=received_at,
        scope=scope,
        ppq=raw["ppq"],
        time_signature=raw["time_signature"],
        notes=notes,
        markers=[Marker(ordinal=i, **m) for i, m in enumerate(raw["markers"])],
        selection=Selection(
            selected_count=len(selected),
            exposed_count=len(notes),
            selected_note_ids=selected,
            timeline=raw["timeline_selection"],
        ),
        in_scope_count=sum(1 for n in notes if n.in_scope),
        supported_fields=supported,
        unsupported_fields=unsupported,
        extra_note_attributes=raw["extra_note_attributes"],
        default_note=raw["default_note"],
        state_hash=raw["state_hash"],
        content_hash=raw["content_hash"],
        host=raw["host"],
    )


def label_key(label: str) -> str:
    return " ".join(label.split()).casefold()
