"""Shared helpers for deterministic score analysis.

Conventions (documented in docs/API.md):
* Times are integer ticks relative to the captured score; ``PPQ`` comes from the capture.
* Onsets are quantized to the tolerance grid by rounding half up: ``(t + tol // 2) // tol``.
* "Quarter notes" are ``ticks / PPQ``; they are not assumed to be time-signature beats.
* Pitch names use FL's display convention (60 = C5) and are presentation only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from flslacker.contracts.models import AnalysisParameters, Note, Snapshot

PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
FLAT_NAMES = {"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10, "Cb": 11, "Fb": 4, "E#": 5, "B#": 0}
MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)
MINOR_SCALE = (0, 2, 3, 5, 7, 8, 10)
# Minor keys also accept the raised 6th and 7th (melodic/harmonic minor).
MINOR_EXTENDED = (0, 2, 3, 5, 7, 8, 9, 10, 11)
KEYSWITCH_MAX_PITCH = 23


@dataclass(frozen=True)
class Event:
    note_id: str
    ordinal: int
    pitch: int
    start: int
    duration: int
    onset: int  # quantized grid index
    expressive: bool


@dataclass(frozen=True)
class Key:
    tonic: int
    mode: str  # "major" | "minor"

    @property
    def name(self) -> str:
        return f"{PITCH_CLASSES[self.tonic]} {self.mode}"

    def pitch_classes(self, extended: bool = True) -> frozenset[int]:
        steps = MAJOR_SCALE if self.mode == "major" else (MINOR_EXTENDED if extended else MINOR_SCALE)
        return frozenset((self.tonic + s) % 12 for s in steps)


def pitch_name(number: int) -> str:
    return f"{PITCH_CLASSES[number % 12]}{number // 12}"


def parse_key(text: str) -> Key:
    """Parse 'C major', 'A minor', 'F# min', 'Bbm', 'Am'."""
    match = re.fullmatch(r"\s*([A-Ga-g])([#b]?)\s*(major|maj|minor|min|m|M)?\s*", text or "")
    if not match:
        raise ValueError(f"unrecognized key: {text!r}")
    letter, accidental, mode = match.groups()
    name = letter.upper() + accidental
    tonic = FLAT_NAMES[name] if name in FLAT_NAMES else PITCH_CLASSES.index(name)
    if mode in ("minor", "min", "m"):
        kind = "minor"
    elif mode in ("major", "maj", "M"):
        kind = "major"
    else:
        kind = "minor" if letter.islower() else "major"
    return Key(tonic, kind)


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def events(snapshot: Snapshot, params: AnalysisParameters) -> tuple[list[Event], dict[str, int], int]:
    """Analyzable notes, skip counts and the onset tolerance in ticks."""
    tolerance = params.onset_tolerance_ticks or max(1, snapshot.ppq // 8)
    excluded = set(params.exclude_note_ids)
    skipped = {"out_of_scope": 0, "excluded": 0, "muted": 0, "unreadable": 0, "out_of_range": 0}
    result: list[Event] = []
    for note in snapshot.notes:
        if params.in_scope_only and not note.in_scope:
            skipped["out_of_scope"] += 1
            continue
        if note.note_id in excluded:
            skipped["excluded"] += 1
            continue
        if note.fl.get("muted") is True:
            skipped["muted"] += 1
            continue
        if not (_numeric(note.pitch) and _numeric(note.start_tick) and _numeric(note.duration_tick)):
            skipped["unreadable"] += 1
            continue
        pitch = int(note.pitch)
        if params.pitch_range and not params.pitch_range[0] <= pitch <= params.pitch_range[1]:
            skipped["out_of_range"] += 1
            continue
        start = int(note.start_tick)
        result.append(
            Event(
                note_id=note.note_id,
                ordinal=note.ordinal,
                pitch=pitch,
                start=start,
                duration=max(0, int(note.duration_tick)),
                onset=(start + tolerance // 2) // tolerance,
                expressive=_expressive(note),
            )
        )
    result.sort(key=lambda e: (e.onset, e.pitch, e.ordinal))
    return result, skipped, tolerance


def _expressive(note: Note) -> bool:
    fl = note.fl
    return bool(fl.get("slide")) or bool(fl.get("porta")) or (fl.get("pitchofs") not in (0, None))


def melody(items: list[Event], voice: str) -> list[Event]:
    """Skyline: the highest note per quantized onset (ties by lowest ordinal)."""
    if voice == "all":
        return list(items)
    best: dict[int, Event] = {}
    for event in items:
        current = best.get(event.onset)
        if current is None or (event.pitch, -event.ordinal) > (current.pitch, -current.ordinal):
            best[event.onset] = event
    return [best[k] for k in sorted(best)]


def looks_percussive(items: list[Event], ppq: int) -> bool:
    if len(items) < 8:
        return False
    durations = sorted(e.duration for e in items)
    median = durations[len(durations) // 2]
    distinct = len({e.pitch for e in items})
    return median <= ppq // 8 and distinct <= 4
