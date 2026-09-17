"""Deterministic score analysis. Same snapshot + parameters => same findings."""

from __future__ import annotations

from typing import Any

from flslacker.analysis import harmony, motifs, outliers
from flslacker.analysis.common import KEYSWITCH_MAX_PITCH, events, looks_percussive, melody, parse_key, pitch_name
from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.models import AnalysisParameters, Finding, Snapshot

ALGORITHM = "flslacker-analysis"
VERSION = "1.0"
INFERRED_KEY_MIN_CONFIDENCE = 0.5


def run_analysis(snapshot: Snapshot, analyses: list[str], params: AnalysisParameters) -> dict[str, Any]:
    if params.max_motif_notes < params.min_motif_notes:
        raise FlsError(ErrorCode.INVALID_ARGUMENT, "max_motif_notes must be >= min_motif_notes")
    items, skipped, tolerance = events(snapshot, params)
    seq = melody(items, params.voice)
    percussive = looks_percussive(items, snapshot.ppq)
    tonal = [e for e in items if e.pitch > KEYSWITCH_MAX_PITCH and not e.expressive]
    findings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "ppq": snapshot.ppq,
        "analyzed_notes": len(items),
        "melody_notes": len(seq),
        "voice": params.voice,
        "onset_tolerance_ticks": tolerance,
        "rounding": "onsets round half up to the tolerance grid",
        "skipped": skipped,
        "possible_keyswitches": sum(1 for e in items if e.pitch <= KEYSWITCH_MAX_PITCH),
        "expressive_notes": sum(1 for e in items if e.expressive),
        "percussive_material": percussive,
    }

    key = None
    key_source = "none"
    key_conf = 0.0
    hypotheses = harmony.key_hypotheses(tonal)
    if params.key_hint:
        try:
            key = parse_key(params.key_hint)
        except ValueError as exc:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, str(exc))
        key_source, key_conf = "user", 1.0
    elif hypotheses and not percussive:
        key_conf = harmony.key_confidence(hypotheses, len(tonal))
        if key_conf >= INFERRED_KEY_MIN_CONFIDENCE:
            key = parse_key(hypotheses[0]["key"])
            key_source = "inferred"
    summary["key"] = {"key": key.name if key else None, "source": key_source, "confidence": key_conf}

    if "key" in analyses:
        if hypotheses:
            top = hypotheses[0]["key"]
            findings.append({
                "kind": "key_hypotheses",
                "severity": "info",
                "confidence": harmony.key_confidence(hypotheses, len(tonal)),
                "message": (f"Most likely key: {top} (ranked hypotheses; not certain)."
                            + (f" The user-specified key {key.name} is used as the constraint." if key_source == "user" else "")),
                "evidence": {"method": "Krumhansl-Kessler correlation, duration weighted", "notes": len(tonal)},
                "alternatives": hypotheses,
            })
        else:
            findings.append({"kind": "key_hypotheses", "severity": "info", "confidence": 0.0,
                             "message": "No tonal material to estimate a key.", "evidence": {}})

    found: list[motifs.Motif] = []
    if "motifs" in analyses or "pitch_outliers" in analyses:
        found = motifs.find_motifs(
            seq,
            min_notes=params.min_motif_notes,
            max_notes=params.max_motif_notes,
            min_occurrences=params.min_occurrences,
            transpose_invariant=params.transpose_invariant,
            allow_overlap=params.allow_overlap,
        )
        summary["motif_count"] = len(found)

    if "motifs" in analyses:
        for motif in found:
            occurrences = []
            for start in motif.starts:
                window = seq[start : start + motif.length]
                occurrences.append({
                    "start_tick": window[0].start,
                    "end_tick": max(e.start + e.duration for e in window),
                    "note_ids": [e.note_id for e in window],
                    "transposition": window[0].pitch - seq[motif.starts[0]].pitch if motif.transposing else 0,
                    "first_pitch": pitch_name(window[0].pitch),
                })
            findings.append({
                "kind": "motif",
                "severity": "info",
                "confidence": motifs.motif_confidence(motif, params.min_motif_notes),
                "message": f"Motif {motif.motif_id}: {motif.length} notes, {len(motif.starts)} occurrences.",
                "note_ids": sorted({n for o in occurrences for n in o["note_ids"]}, key=lambda n: int(n[1:]))[:200],
                "evidence": {
                    "motif_id": motif.motif_id,
                    "length_notes": motif.length,
                    "intervals": [step for step, _ in motif.tokens] if motif.transposing else None,
                    "pitches": None if motif.transposing else list(motif.profile),
                    "inter_onset_ticks": [ioi * tolerance for _, ioi in motif.tokens],
                    "transposition_invariant": motif.transposing,
                    "occurrences": occurrences,
                },
            })

    if "chords" in analyses:
        windows = harmony.chord_windows(tonal, snapshot.ppq, params.chord_window_beats)
        findings.append({
            "kind": "chord_hypotheses",
            "severity": "info",
            "confidence": 0.5 if windows else 0.0,
            "message": f"Ranked chord hypotheses for {len(windows)} windows of {params.chord_window_beats} quarter notes.",
            "evidence": {"windows": windows, "window_ticks": snapshot.ppq * params.chord_window_beats},
        })

    if "pitch_outliers" in analyses:
        if percussive:
            findings.append({
                "kind": "percussive_material",
                "severity": "info",
                "confidence": 0.6,
                "message": "The material looks percussive; no pitch corrections are suggested.",
                "evidence": {"distinct_pitches": len({e.pitch for e in items})},
            })
        else:
            by_note, expressive_skipped = outliers.motif_near_misses(seq, found, key)
            outlier = outliers.outlier_findings(by_note)
            findings.extend(outlier)
            summary["expressive_skipped"] = expressive_skipped
            if key is not None:
                melody_ids = {e.note_id for e in seq}
                harmony_only = [e for e in tonal if e.note_id not in melody_ids]
                findings.extend(outliers.scale_findings(
                    [e for e in seq if e.pitch > KEYSWITCH_MAX_PITCH], harmony_only, key, key_source, key_conf,
                    snapshot.ppq, {f["note_ids"][0] for f in outlier},
                ))

    counts: dict[str, int] = {}
    kept: list[Finding] = []
    for item in findings:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        if counts[item["kind"]] > params.max_findings:
            continue
        kept.append(Finding(finding_id=f"{item['kind']}-{counts[item['kind']]}", **item))
    summary["finding_counts"] = counts
    summary["truncated"] = any(c > params.max_findings for c in counts.values())
    return {"algorithm": ALGORITHM, "version": VERSION, "findings": kept, "summary": summary}
