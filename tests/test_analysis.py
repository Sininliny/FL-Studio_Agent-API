"""Deterministic analysis: motifs, hypotheses, and what must never be flagged as an error."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from flslacker.analysis import run_analysis
from flslacker.analysis.common import parse_key
from flslacker.contracts import canonical as c
from flslacker.contracts.errors import FlsError
from flslacker.contracts.models import AnalysisParameters, Target
from flslacker.service.snapshots import build_snapshot

NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)


def snapshot(notes, ppq=96, scope="all_exposed"):
    records = []
    for n in notes:
        record = {"number": n[0], "time": n[1], "length": n[2], "group": 0, "pan": 0.5, "velocity": 0.78,
                  "release": 0.5, "color": 0, "fcut": 0.5, "fres": 0.5, "pitchofs": 0, "slide": False,
                  "porta": False, "muted": False, "selected": True, "repeats": 0}
        if len(n) > 3:
            record.update(n[3])
        records.append(record)
    raw = {
        "scope": scope, "target_label": "t", "captured_at": "2026-09-17T00:00:00Z", "ppq": ppq,
        "time_signature": [4, 4], "timeline_selection": None, "notes": records, "markers": [],
        "default_note": None, "field_errors": {}, "extra_note_attributes": [],
        "state_hash": c.state_hash(ppq, records, []), "content_hash": c.content_hash(ppq, records, []), "host": {},
    }
    target = Target(target_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()), binding="user_attested", label="t")
    return build_snapshot(raw, snapshot_id=str(uuid.uuid4()), session_id=target.session_id, target=target,
                          purpose="adhoc", job_id=None, received_at=NOW)


def motif(start_pitch, start_time, steps=(2, 2, 3), wrong=None):
    pitches = [start_pitch]
    for step in steps:
        pitches.append(pitches[-1] + step)
    if wrong:
        pitches[wrong[0]] += wrong[1]
    return [(p, start_time + i * 48, 48) for i, p in enumerate(pitches)]


def kinds(result, kind):
    return [f for f in result["findings"] if f.kind == kind]


def test_motif_and_outlier_detection_is_deterministic():
    notes = motif(60, 0) + motif(60, 384) + motif(65, 768, wrong=(2, 1))
    snap = snapshot(notes)
    first = run_analysis(snap, ["motifs", "key", "pitch_outliers"], AnalysisParameters())
    second = run_analysis(snap, ["motifs", "key", "pitch_outliers"], AnalysisParameters())
    assert [f.model_dump() for f in first["findings"]] == [f.model_dump() for f in second["findings"]]
    motifs = kinds(first, "motif")
    assert len(motifs) == 1 and len(motifs[0].evidence["occurrences"]) == 2
    outliers = kinds(first, "pitch_outlier")
    assert [o.note_ids for o in outliers] == [["n10"]]
    assert outliers[0].suggestion["set"] == {"pitch": 69}
    assert outliers[0].evidence["current_pitch"] == 70
    assert outliers[0].confidence >= 0.6


def test_transposition_invariance_can_be_disabled():
    notes = motif(60, 0) + motif(65, 384)
    snap = snapshot(notes)
    assert len(kinds(run_analysis(snap, ["motifs"], AnalysisParameters()), "motif")) == 1
    strict = run_analysis(snap, ["motifs"], AnalysisParameters(transpose_invariant=False))
    assert kinds(strict, "motif") == []


def test_expressive_notes_are_not_suggested():
    notes = motif(60, 0) + motif(60, 384) + motif(65, 768)
    notes[10] = (70, notes[10][1], 48, {"slide": True})
    result = run_analysis(snapshot(notes), ["pitch_outliers"], AnalysisParameters())
    assert kinds(result, "pitch_outlier") == []
    assert result["summary"]["expressive_skipped"] >= 1


def test_chromatic_passing_tone_is_not_an_error():
    notes = [(60, 0, 96), (62, 96, 96), (63, 192, 48), (64, 240, 96), (65, 336, 96), (67, 432, 96)]
    result = run_analysis(snapshot(notes), ["pitch_outliers"], AnalysisParameters(key_hint="C major"))
    assert [f.note_ids for f in kinds(result, "chromatic_passing_tone")] == [["n2"]]
    assert not [f for f in result["findings"] if f.suggestion]


def test_user_key_is_a_constraint_but_inferred_key_is_only_a_hypothesis():
    notes = [(60, 0, 192), (64, 192, 192), (67, 384, 192), (66, 576, 192), (72, 768, 192), (60, 960, 192)]
    constrained = run_analysis(snapshot(notes), ["pitch_outliers"], AnalysisParameters(key_hint="C major"))
    suggestion = kinds(constrained, "outside_user_key")
    assert [f.note_ids for f in suggestion] == [["n3"]]
    assert suggestion[0].suggestion["set"]["pitch"] in (65, 67)
    inferred = run_analysis(snapshot(notes), ["pitch_outliers", "key"], AnalysisParameters())
    assert not [f for f in inferred["findings"] if f.suggestion]


def test_percussive_material_and_keyswitches():
    drums = [(36 if i % 2 == 0 else 38, i * 96, 6) for i in range(16)]
    result = run_analysis(snapshot(drums), ["pitch_outliers"], AnalysisParameters())
    assert kinds(result, "percussive_material")
    assert not [f for f in result["findings"] if f.suggestion]
    keyswitch = [(12, 0, 960)] + [(60 + (i % 3) * 2, i * 96, 96) for i in range(8)]
    summary = run_analysis(snapshot(keyswitch), ["key"], AnalysisParameters())["summary"]
    assert summary["possible_keyswitches"] == 1


def test_key_and_chord_hypotheses_are_ranked():
    notes = [(57, 0, 384), (60, 0, 384), (64, 0, 384), (53, 384, 384), (57, 384, 384), (60, 384, 384),
             (52, 768, 384), (56, 768, 384), (59, 768, 384), (57, 1152, 384), (60, 1152, 384), (64, 1152, 384)]
    result = run_analysis(snapshot(notes), ["key", "chords"], AnalysisParameters())
    key = kinds(result, "key_hypotheses")[0]
    assert key.alternatives[0]["key"] in ("A minor", "C major")
    assert len(key.alternatives) == 5
    windows = kinds(result, "chord_hypotheses")[0].evidence["windows"]
    assert windows[0]["hypotheses"][0]["chord"] == "Am"
    assert windows[1]["hypotheses"][0]["chord"] == "F"
    assert windows[2]["hypotheses"][0]["chord"] == "E"


def test_scope_muting_exclusion_and_bad_parameters():
    notes = motif(60, 0) + [(61, 1000, 48, {"muted": True}), (62, 1100, 48, {"selected": False})]
    snap = snapshot(notes, scope="selected")
    summary = run_analysis(snap, ["key"], AnalysisParameters(exclude_note_ids=["n0"]))["summary"]
    assert summary["skipped"] == {"out_of_scope": 1, "excluded": 1, "muted": 1, "unreadable": 0, "out_of_range": 0}
    with pytest.raises(FlsError):
        run_analysis(snap, ["key"], AnalysisParameters(key_hint="H major"))
    with pytest.raises(FlsError):
        run_analysis(snap, ["motifs"], AnalysisParameters(min_motif_notes=6, max_motif_notes=4))


def test_parse_key():
    assert parse_key("C major").name == "C major"
    assert parse_key("Am").name == "A minor"
    assert parse_key("f# min").name == "F# minor"
    assert parse_key("Bb").name == "A# major"
    assert parse_key("c").mode == "minor"
