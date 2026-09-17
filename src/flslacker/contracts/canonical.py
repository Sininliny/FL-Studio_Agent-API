"""Canonical JSON, hashing and FL note records for protocol ``flslacker/1``.

STDLIB ONLY. This module is inlined verbatim into the FL-side Piano Roll scripts
(see ``flslacker.fl_build``), so it must run on FL's embedded interpreter without
companion dependencies. Lines ending in ``# fl-inline: skip`` are dropped when inlined.

Canonicalization (version 1), also published in docs/SPEC.md:

* JSON text with keys sorted, separators ``,`` and ``:``, ASCII-only escapes,
  UTF-8 bytes, no insignificant whitespace.
* Integers are written in decimal. ``bool`` is never treated as an integer.
* Floats are written with Python's shortest round-trip ``repr``; ``-0.0`` becomes
  ``0.0``; NaN and infinities are rejected.
* Lists keep their order. Dictionary keys must be strings.
* A hash is the lowercase hex SHA-256 of the canonical bytes.
"""

import hashlib
import hmac
import json
import math

PROTOCOL = "flslacker/1"
CANON_VERSION = 1

# FL-native Note attributes, in canonical order, with the Python type FL is documented to return.
NOTE_FIELDS = (
    ("number", "int"),
    ("time", "int"),
    ("length", "int"),
    ("group", "int"),
    ("pan", "float"),
    ("velocity", "float"),
    ("release", "float"),
    ("color", "int"),
    ("fcut", "float"),
    ("fres", "float"),
    ("pitchofs", "int"),
    ("slide", "bool"),
    ("porta", "bool"),
    ("muted", "bool"),
    ("selected", "bool"),
    ("repeats", "int"),
)
NOTE_FIELD_NAMES = tuple(name for name, _ in NOTE_FIELDS)
NOTE_FIELD_TYPES = dict(NOTE_FIELDS)

MARKER_FIELDS = (
    ("time", "int"),
    ("name", "str"),
    ("mode", "int"),
    ("tsnum", "int"),
    ("tsden", "int"),
)
MARKER_FIELD_NAMES = tuple(name for name, _ in MARKER_FIELDS)

# Fields the baseline writer may set, with inclusive bounds (None = boolean).
WRITABLE_NOTE_FIELDS = {
    "number": (0, 127),
    "time": (0, 2**31 - 1),
    "length": (1, 2**31 - 1),
    "group": (0, 2**31 - 1),
    "pan": (0.0, 1.0),
    "velocity": (0.0, 1.0),
    "release": (0.0, 1.0),
    "color": (0, 15),
    "fcut": (0.0, 1.0),
    "fres": (0.0, 1.0),
    "pitchofs": (-120, 120),
    "slide": None,
    "porta": None,
    "muted": None,
    "selected": None,
    "repeats": (0, 14),
}
# Fields an update may change. `group` and `selected` are only written on insert.
UPDATABLE_NOTE_FIELDS = tuple(
    name for name in NOTE_FIELD_NAMES if name not in ("group", "selected")
)

# Tolerance for comparing float properties after a write; FL stores several of them quantized.
FLOAT_TOLERANCE = 0.01

# Fields ignored when comparing musical content (selection is UI state).
CONTENT_IGNORED_FIELDS = ("selected",)


class CanonicalError(ValueError):
    pass


def _normalize(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError("non-finite float")
        if value == 0.0:
            return 0.0
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("non-string key")
            out[key] = _normalize(item)
        return out
    raise CanonicalError("unsupported type: " + type(value).__name__)


def canonical_dumps(value):
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_bytes(value):
    return canonical_dumps(value).encode("utf-8")


def sha256_hex(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def hmac_hex(secret_hex, value):
    return hmac.new(bytes.fromhex(secret_hex), canonical_bytes(value), hashlib.sha256).hexdigest()


def hmac_matches(secret_hex, value, tag):
    if not isinstance(tag, str):
        return False
    return hmac.compare_digest(hmac_hex(secret_hex, value), tag)


# ---------------------------------------------------------------- records


def _coerce_read(kind, value):
    """Keep what FL returned, except for lossless int/bool normalisation."""
    if kind == "bool" and isinstance(value, int) and value in (0, 1):
        return bool(value)
    if kind == "int" and isinstance(value, bool):
        return int(value)
    if kind == "int" and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_record(obj, fields):
    """Read ``fields`` from an FL object. Returns (record, errors)."""
    record = {}
    errors = {}
    for name, kind in fields:
        try:
            value = _coerce_read(kind, getattr(obj, name))
            _normalize(value)
        except Exception as exc:  # FL raises for unsupported attributes
            errors[name] = type(exc).__name__ + ": " + str(exc)[:200]
            continue
        record[name] = value
    return record, errors


def read_note(note):
    return read_record(note, NOTE_FIELDS)


def read_marker(marker):
    return read_record(marker, MARKER_FIELDS)


def extra_attributes(obj, known):
    """Public, non-callable scalar attributes not in ``known`` (for compatibility reports)."""
    extras = {}
    for name in dir(obj):
        if name.startswith("_") or name in known:
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        if isinstance(value, (bool, int, float, str)) or value is None:
            try:
                extras[name] = _normalize(value)
            except CanonicalError:
                extras[name] = repr(value)[:80]
    return extras


def note_fingerprint(record):
    return sha256_hex({"v": CANON_VERSION, "kind": "note", "record": record})


def state_hash(ppq, notes, markers):
    """Order-sensitive hash of the exposed score, used to detect any intervening change."""
    return sha256_hex(
        {"v": CANON_VERSION, "kind": "state", "ppq": ppq, "notes": list(notes), "markers": list(markers)}
    )


def _content_view(record):
    return {k: v for k, v in record.items() if k not in CONTENT_IGNORED_FIELDS}


def content_hash(ppq, notes, markers):
    """Order-independent hash of musical content (selection excluded)."""
    note_keys = sorted(canonical_dumps(_content_view(r)) for r in notes)
    marker_keys = sorted(canonical_dumps(m) for m in markers)
    return sha256_hex(
        {"v": CANON_VERSION, "kind": "content", "ppq": ppq, "notes": note_keys, "markers": marker_keys}
    )


# ---------------------------------------------------------------- validation


def check_note_value(field, value):
    """Return an error string, or None when ``value`` may be written to ``field``."""
    if field not in WRITABLE_NOTE_FIELDS:
        return "field not writable: " + str(field)
    bounds = WRITABLE_NOTE_FIELDS[field]
    kind = NOTE_FIELD_TYPES[field]
    if bounds is None:
        if not isinstance(value, bool):
            return field + " must be a boolean"
        return None
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return field + " must be an integer"
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return field + " must be a number"
        if not math.isfinite(float(value)):
            return field + " must be finite"
    if value < bounds[0] or value > bounds[1]:
        return "%s out of range [%s, %s]" % (field, bounds[0], bounds[1])
    return None


def validate_operations(records, operations):
    """Validate FL-native operations against live ``records``. Returns a list of errors."""
    errors = []
    touched = set()
    for index, op in enumerate(operations):
        kind = op.get("op")
        where = "operation %d" % index
        if kind in ("update", "delete"):
            ordinal = op.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 0 <= ordinal < len(records):
                errors.append(where + ": ordinal out of range")
                continue
            if ordinal in touched:
                errors.append(where + ": note targeted twice")
            touched.add(ordinal)
            if note_fingerprint(records[ordinal]) != op.get("fingerprint"):
                errors.append(where + ": note fingerprint does not match live note")
            if kind == "update":
                values = op.get("set")
                if not isinstance(values, dict) or not values:
                    errors.append(where + ": empty update")
                    continue
                for field, value in values.items():
                    if field not in UPDATABLE_NOTE_FIELDS:
                        errors.append(where + ": field not updatable: " + str(field))
                        continue
                    problem = check_note_value(field, value)
                    if problem:
                        errors.append(where + ": " + problem)
        elif kind == "insert":
            note = op.get("note")
            if not isinstance(note, dict) or set(note) != set(NOTE_FIELD_NAMES):
                errors.append(where + ": insert needs a complete note template")
                continue
            for field, value in note.items():
                problem = check_note_value(field, value)
                if problem:
                    errors.append(where + ": " + problem)
        else:
            errors.append(where + ": unknown op " + repr(kind)[:40])
    return errors


def predict_after(records, operations):
    """Apply FL-native operations to a copy of ``records`` (updates, deletes, then inserts)."""
    result = [dict(r) for r in records]
    for op in operations:
        if op["op"] == "update":
            result[op["ordinal"]].update(op["set"])
    for op in sorted((o for o in operations if o["op"] == "delete"), key=lambda o: o["ordinal"], reverse=True):
        del result[op["ordinal"]]
    for op in operations:
        if op["op"] == "insert":
            result.append(dict(op["note"]))
    return result


def _split_record(record):
    exact = {}
    floats = {}
    for key, value in _content_view(record).items():
        if isinstance(value, float):
            floats[key] = value
        else:
            exact[key] = value
    return canonical_dumps(exact), floats


def match_contents(expected, observed, tolerance=FLOAT_TOLERANCE, limit=20):
    """Compare two note multisets, ignoring order and selection, with float tolerance.

    Returns (ok, mismatches) where mismatches is a short list of human-readable strings.
    Float fields are compared with ``tolerance``; all other fields must match exactly, so
    callers must write float fields as floats.
    """
    mismatches = []

    def bucket(records):
        out = {}
        for record in records:
            exact_key, floats = _split_record(record)
            out.setdefault(exact_key, []).append(floats)
        return out

    want = bucket(expected)
    have = bucket(observed)
    for key in sorted(set(want) | set(have)):
        wanted = sorted(want.get(key, []), key=lambda f: sorted(f.items()))
        found = sorted(have.get(key, []), key=lambda f: sorted(f.items()))
        remaining = list(found)
        for floats in wanted:
            hit = None
            for position, candidate in enumerate(remaining):
                if set(candidate) == set(floats) and all(
                    abs(candidate[name] - floats[name]) <= tolerance for name in floats
                ):
                    hit = position
                    break
            if hit is None:
                if len(mismatches) < limit:
                    mismatches.append("missing note " + key + " " + canonical_dumps(floats))
            else:
                del remaining[hit]
        for floats in remaining:
            if len(mismatches) < limit:
                mismatches.append("unexpected note " + key + " " + canonical_dumps(floats))
        if remaining and len(mismatches) >= limit:
            break
    ok = not mismatches and len(expected) == len(observed)
    if not ok and not mismatches:
        mismatches.append("note count differs: expected %d, observed %d" % (len(expected), len(observed)))
    return ok, mismatches
