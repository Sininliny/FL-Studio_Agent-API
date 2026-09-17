"""Canonicalization vectors and signed-envelope rules (flslacker/1)."""

from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path

import pytest

from flslacker.contracts import canonical as c
from flslacker.contracts import wire

VECTORS = json.loads((Path(__file__).parent / "vectors" / "canonical_v1.json").read_text(encoding="utf-8"))
SECRET = "11" * 32


def test_value_vectors():
    for item in VECTORS["values"]:
        assert c.canonical_dumps(item["input"]) == item["canonical"]
        assert c.sha256_hex(item["input"]) == item["sha256"]


def test_note_vectors():
    notes = VECTORS["notes"]
    a, b = notes["records"]
    assert [c.note_fingerprint(a), c.note_fingerprint(b)] == notes["fingerprints"]
    assert c.state_hash(96, [a, b], notes["markers"]) == notes["state_hash"]
    assert c.state_hash(96, [b, a], notes["markers"]) == notes["state_hash_reordered"]
    assert notes["state_hash"] != notes["state_hash_reordered"]
    assert c.content_hash(96, [a, b], notes["markers"]) == notes["content_hash"]
    assert notes["content_hash"] == notes["content_hash_reordered_unselected"]
    tag = VECTORS["hmac"]
    assert c.hmac_hex(tag["secret"], tag["value"]) == tag["tag"]


def test_rejects_non_finite_and_bad_keys():
    for bad in (math.nan, math.inf, {1: "x"}, {"a": {1, 2}}):
        with pytest.raises(c.CanonicalError):
            c.canonical_dumps(bad)


def test_duplicates_are_distinct_by_ordinal_not_fingerprint():
    record = {"number": 60, "time": 0, "length": 96}
    assert c.note_fingerprint(record) == c.note_fingerprint(dict(record))
    ops = [{"op": "delete", "ordinal": 1, "fingerprint": c.note_fingerprint(record)}]
    assert c.predict_after([record, dict(record)], ops) == [record]
    assert c.validate_operations([record, dict(record)], ops + [dict(ops[0])])  # same note twice


def test_validate_operations_catches_problems():
    record = {"number": 60, "time": 0, "length": 96}
    fp = c.note_fingerprint(record)
    problems = c.validate_operations([record], [
        {"op": "update", "ordinal": 0, "fingerprint": fp, "set": {"number": 128}},
    ])
    assert "out of range" in problems[0]
    assert c.validate_operations([record], [{"op": "update", "ordinal": 0, "fingerprint": "x" * 64, "set": {"number": 61}}])
    assert c.validate_operations([record], [{"op": "update", "ordinal": 3, "fingerprint": fp, "set": {"number": 61}}])
    assert c.validate_operations([record], [{"op": "update", "ordinal": 0, "fingerprint": fp, "set": {"selected": True}}])
    assert c.validate_operations([record], [{"op": "insert", "note": {"number": 61}}])
    assert c.validate_operations([record], [{"op": "rm", "ordinal": 0}])
    assert c.validate_operations([record], [{"op": "update", "ordinal": 0, "fingerprint": fp, "set": {"velocity": True}}])


def test_match_contents_tolerates_quantization_only_for_floats():
    a = {"number": 60, "time": 0, "velocity": 0.7}
    ok, _ = c.match_contents([a], [dict(a, velocity=0.703125)])
    assert ok
    ok, mismatches = c.match_contents([a], [dict(a, velocity=0.75)])
    assert not ok and mismatches
    ok, _ = c.match_contents([a], [dict(a, number=61)])
    assert not ok
    ok, _ = c.match_contents([a, a], [a])
    assert not ok
    ok, _ = c.match_contents([dict(a, selected=True)], [dict(a, selected=False)])
    assert ok  # selection is UI state


def _envelope(**overrides):
    session = str(uuid.uuid4())
    env = wire.make_envelope(SECRET, "capture_request", session, 1, {"scope": "selected"}, 60)
    env.update(overrides)
    return session, env


def test_envelope_checks():
    session, env = _envelope()
    assert wire.check_envelope(SECRET, env, session, ("capture_request",)) is None
    assert wire.check_envelope("22" * 32, env, session, ("capture_request",)) == "bad authentication tag"
    assert wire.check_envelope(SECRET, env, str(uuid.uuid4()), ("capture_request",)) == "wrong session"
    assert wire.check_envelope(SECRET, env, session, ("apply_request",)) == "unexpected message kind"
    tampered = dict(env, body={"scope": "all_exposed"})
    assert wire.check_envelope(SECRET, tampered, session, ("capture_request",)) == "bad authentication tag"
    extra = dict(env, extra=1)
    assert wire.check_envelope(SECRET, extra, session, ("capture_request",)) == "unexpected or missing envelope fields"
    assert wire.check_envelope(SECRET, env, session, ("capture_request",), now=time.time() + 120) == "expired"
    future = wire.make_envelope(SECRET, "capture_request", session, 1, {}, 60, now=time.time() + 3600)
    assert wire.check_envelope(SECRET, future, session, ("capture_request",)) == "created in the future"
    forged = dict(env, sender="fl")
    assert wire.check_envelope(SECRET, forged, session, ("capture_request",)) == "wrong sender"


def test_atomic_write_and_claims(tmp_path):
    folder = tmp_path / "requests"
    folder.mkdir()
    with pytest.raises(wire.BridgeError):
        wire.atomic_write_text(str(folder), "../escape.json", "{}")
    with pytest.raises(wire.BridgeError):
        wire.atomic_write_text(str(folder), "big.json", "x" * (wire.MAX_MESSAGE_BYTES + 1))
    wire.atomic_write_text(str(folder), "ok.json", '{"a": 1}')
    assert wire.load_json_file(str(folder / "ok.json")) == {"a": 1}
    assert not any(p.name.startswith(".tmp") for p in folder.iterdir())
    (tmp_path / "claims").mkdir()
    request = str(uuid.uuid4())
    assert wire.try_claim(str(tmp_path), request, "fl:applying")
    assert not wire.try_claim(str(tmp_path), request, "companion:cancelled")
    assert wire.read_claim(str(tmp_path), request) == "fl:applying"
    with pytest.raises(wire.BridgeError):
        wire.try_claim(str(tmp_path), "../../x", "a")
    (folder / "nan.json").write_text('{"a": NaN}', encoding="utf-8")
    with pytest.raises(wire.BridgeError):
        wire.load_json_file(str(folder / "nan.json"))
