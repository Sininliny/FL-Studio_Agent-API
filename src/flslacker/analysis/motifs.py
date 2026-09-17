"""Deterministic motif detection from rhythm (inter-onset) and pitch-interval sequences.

A motif of L notes is L-1 tokens ``(interval, ioi)`` where ``interval`` is the pitch step to
the next note (or the absolute pitch pair without transposition invariance) and ``ioi`` the
quantized inter-onset interval. Repeats are found longest-first; a shorter pattern is dropped
when every occurrence lies inside an accepted longer one. Without ``allow_overlap``,
occurrences are chosen greedily from the left and may not share notes.
"""

from __future__ import annotations

from dataclasses import dataclass

from flslacker.analysis.common import Event


@dataclass
class Motif:
    motif_id: str
    length: int
    tokens: tuple
    starts: list[int]  # indices into the melody sequence
    profile: tuple[int, ...]  # pitch offsets from the first note (or absolute pitches)
    transposing: bool


def tokens_for(seq: list[Event], transpose_invariant: bool) -> list[tuple]:
    out = []
    for a, b in zip(seq, seq[1:]):
        step = b.pitch - a.pitch if transpose_invariant else (a.pitch, b.pitch)
        out.append((step, b.onset - a.onset))
    return out


def _profile(seq: list[Event], start: int, length: int, transposing: bool) -> tuple[int, ...]:
    first = seq[start].pitch
    return tuple((seq[start + k].pitch - first) if transposing else seq[start + k].pitch for k in range(length))


def _select(starts: list[int], length: int, allow_overlap: bool) -> list[int]:
    if allow_overlap:
        return list(starts)
    chosen: list[int] = []
    for start in starts:
        if not chosen or start >= chosen[-1] + length:
            chosen.append(start)
    return chosen


def find_motifs(
    seq: list[Event],
    *,
    min_notes: int,
    max_notes: int,
    min_occurrences: int,
    transpose_invariant: bool,
    allow_overlap: bool,
) -> list[Motif]:
    tokens = tokens_for(seq, transpose_invariant)
    accepted: list[Motif] = []
    covered: list[tuple[int, int]] = []  # (start, end) note ranges of accepted occurrences
    upper = min(max_notes, len(seq))
    for length in range(upper, min_notes - 1, -1):
        windows: dict[tuple, list[int]] = {}
        for start in range(0, len(seq) - length + 1):
            key = tuple(tokens[start : start + length - 1])
            if any(ioi <= 0 for _, ioi in key):
                continue
            windows.setdefault(key, []).append(start)
        for key in sorted(windows, key=lambda k: (windows[k][0], k)):
            starts = _select(windows[key], length, allow_overlap)
            if len(starts) < min_occurrences:
                continue
            steps = [step for step, _ in key]
            if transpose_invariant and all(step == 0 for step in steps):
                continue  # repeated single pitch: not a useful motif
            if all(any(s >= a and s + length <= b for a, b in covered) for s in starts):
                continue
            motif = Motif(
                motif_id=f"M{len(accepted) + 1}",
                length=length,
                tokens=key,
                starts=starts,
                profile=_profile(seq, starts[0], length, transpose_invariant),
                transposing=transpose_invariant,
            )
            accepted.append(motif)
            covered.extend((s, s + length) for s in starts)
    return accepted


def motif_confidence(motif: Motif, min_notes: int) -> float:
    value = 0.5 + 0.1 * min(len(motif.starts) - 2, 3) + 0.05 * min(motif.length - min_notes, 4)
    return round(min(value, 0.95), 3)
