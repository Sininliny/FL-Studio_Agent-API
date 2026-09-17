"""Likely pitch outliers: motif near-misses (strong evidence) and scale checks (weak).

Nothing here is certain. Expressive notes (slide, portamento, fine pitch), probable
keyswitches, percussive material and chromatic passing/neighbour tones are never
suggested for correction. An inferred key is only a hypothesis and never yields edits;
a user-specified key is a constraint and may.
"""

from __future__ import annotations

from collections import Counter

from flslacker.analysis.common import Event, Key, pitch_name
from flslacker.analysis.motifs import Motif


def _suggestion(note_id: str, pitch: int) -> dict:
    return {"op": "note.update", "note_id": note_id, "set": {"pitch": pitch}}


def motif_near_misses(seq: list[Event], motifs: list[Motif], key: Key | None) -> tuple[dict[str, list[dict]], int]:
    by_note: dict[str, list[dict]] = {}
    expressive_skipped = 0
    for motif in motifs:
        length = motif.length
        rhythm = tuple(ioi for _, ioi in motif.tokens)
        occupied = [(s, s + length) for s in motif.starts]
        for start in range(0, len(seq) - length + 1):
            if any(start < b and a < start + length for a, b in occupied):
                continue
            window = seq[start : start + length]
            if tuple(b.onset - a.onset for a, b in zip(window, window[1:])) != rhythm:
                continue
            if motif.transposing:
                offsets = [window[k].pitch - motif.profile[k] for k in range(length)]
                counts = Counter(offsets)
                shift = sorted(counts, key=lambda t: (-counts[t], t != offsets[0], t))[0]
                matches = counts[shift]
            else:
                shift = 0
                offsets = [window[k].pitch - motif.profile[k] for k in range(length)]
                matches = sum(1 for o in offsets if o == 0)
            if matches != length - 1:
                continue
            position = next(k for k in range(length) if offsets[k] != shift)
            event = window[position]
            expected = shift + motif.profile[position]
            delta = expected - event.pitch
            if not 0 <= expected <= 127 or delta == 0 or abs(delta) > 12:
                continue
            if event.expressive:
                expressive_skipped += 1
                continue
            support = len(motif.starts)
            confidence = 0.55 + 0.08 * min(support - 2, 3) + 0.03 * min(length - 4, 4)
            if position in (0, length - 1):
                confidence -= 0.15
            key_note = None
            if key is not None:
                pcs = key.pitch_classes()
                now_in, expected_in = event.pitch % 12 in pcs, expected % 12 in pcs
                if expected_in and not now_in:
                    confidence += 0.1
                    key_note = f"the expected pitch fits {key.name}; the current one does not"
                elif now_in and not expected_in:
                    confidence -= 0.2
                    key_note = f"the current pitch fits {key.name}; the expected one does not"
            reference = motif.starts[0]
            by_note.setdefault(event.note_id, []).append({
                "expected": expected,
                "confidence": round(max(0.05, min(confidence, 0.95)), 3),
                "event": event,
                "evidence": {
                    "motif_id": motif.motif_id,
                    "motif_occurrences": support,
                    "motif_length": length,
                    "position_in_motif": position,
                    "transposition": shift,
                    "window_note_ids": [e.note_id for e in window],
                    "reference_note_ids": [e.note_id for e in seq[reference : reference + length]],
                    "current_pitch": event.pitch,
                    "expected_pitch": expected,
                    "key_note": key_note,
                },
            })
    return by_note, expressive_skipped


def outlier_findings(by_note: dict[str, list[dict]]) -> list[dict]:
    findings = []
    for note_id in sorted(by_note, key=lambda n: by_note[n][0]["event"].ordinal):
        candidates = sorted(by_note[note_id], key=lambda c: (-c["confidence"], c["expected"], c["evidence"]["motif_id"]))
        best = candidates[0]
        others = [c for c in candidates[1:] if c["expected"] != best["expected"]]
        confidence = best["confidence"]
        if others and others[0]["confidence"] >= confidence - 0.05:
            confidence = round(max(0.05, confidence - 0.1), 3)
        event = best["event"]
        evidence = dict(best["evidence"])
        evidence["supporting_windows"] = sum(1 for c in candidates if c["expected"] == best["expected"])
        findings.append({
            "kind": "pitch_outlier",
            "severity": "suggestion",
            "confidence": confidence,
            "note_ids": [note_id],
            "message": (
                f"{note_id} ({pitch_name(event.pitch)}, pitch {event.pitch}) breaks motif "
                f"{evidence['motif_id']} ({evidence['motif_occurrences']} occurrences); "
                f"pitch {best['expected']} ({pitch_name(best['expected'])}) would match. This is a suggestion."
            ),
            "evidence": evidence,
            "suggestion": _suggestion(note_id, best["expected"]),
            "alternatives": [
                {"pitch": c["expected"], "confidence": c["confidence"], "motif_id": c["evidence"]["motif_id"]}
                for c in others[:3]
            ],
        })
    return findings


def scale_findings(melody: list[Event], harmony_only: list[Event], key: Key, key_source: str,
                   key_confidence: float, ppq: int, already: set[str]) -> list[dict]:
    pcs = key.pitch_classes()
    findings = []
    short = max(1, ppq // 2)
    for index, event in enumerate(melody):
        if event.pitch % 12 in pcs or event.note_id in already or event.expressive:
            continue
        prev = melody[index - 1] if index > 0 else None
        nxt = melody[index + 1] if index + 1 < len(melody) else None
        base = {"current_pitch": event.pitch, "key": key.name, "key_source": key_source}
        if prev and nxt and event.duration <= short:
            rising = prev.pitch < event.pitch < nxt.pitch
            falling = prev.pitch > event.pitch > nxt.pitch
            if (rising or falling) and abs(event.pitch - prev.pitch) <= 2 and abs(nxt.pitch - event.pitch) <= 2:
                findings.append({
                    "kind": "chromatic_passing_tone", "severity": "info", "confidence": 0.7,
                    "note_ids": [event.note_id],
                    "message": f"{event.note_id} is a chromatic passing tone between {prev.note_id} and {nxt.note_id}; not an error.",
                    "evidence": base,
                })
                continue
            if prev.pitch == nxt.pitch and abs(event.pitch - prev.pitch) <= 2:
                findings.append({
                    "kind": "chromatic_neighbor_tone", "severity": "info", "confidence": 0.7,
                    "note_ids": [event.note_id],
                    "message": f"{event.note_id} is a chromatic neighbour tone; not an error.",
                    "evidence": base,
                })
                continue
        if key_source != "user":
            findings.append({
                "kind": "outside_inferred_key", "severity": "info",
                "confidence": round(0.5 * key_confidence, 3),
                "note_ids": [event.note_id],
                "message": (f"{event.note_id} ({pitch_name(event.pitch)}) is outside the inferred key {key.name}. "
                            "The key is only a hypothesis, so no change is suggested."),
                "evidence": base,
            })
            continue
        reference = [n.pitch for n in (prev, nxt) if n is not None] or [event.pitch]
        target = sum(reference) / len(reference)
        options = [p for p in (event.pitch - 1, event.pitch + 1) if 0 <= p <= 127 and p % 12 in pcs]
        if not options:
            continue
        choice = sorted(options, key=lambda p: (abs(p - target), p))[0]
        findings.append({
            "kind": "outside_user_key", "severity": "suggestion", "confidence": 0.55,
            "note_ids": [event.note_id],
            "message": (f"{event.note_id} ({pitch_name(event.pitch)}) is outside the requested key {key.name}; "
                        f"{pitch_name(choice)} is the nearest scale tone toward its neighbours."),
            "evidence": {**base, "expected_pitch": choice},
            "suggestion": _suggestion(event.note_id, choice),
            "alternatives": [{"pitch": p} for p in options if p != choice],
        })
    for event in harmony_only:
        if event.pitch % 12 in pcs or event.expressive:
            continue
        findings.append({
            "kind": "chromatic_harmony_note", "severity": "info", "confidence": 0.4,
            "note_ids": [event.note_id],
            "message": f"{event.note_id} ({pitch_name(event.pitch)}) is a non-melody note outside {key.name}.",
            "evidence": {"current_pitch": event.pitch, "key": key.name, "key_source": key_source},
        })
    return findings
