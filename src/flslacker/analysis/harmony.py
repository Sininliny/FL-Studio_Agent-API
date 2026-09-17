"""Ranked key and chord hypotheses (never certainty)."""

from __future__ import annotations

import math

from flslacker.analysis.common import PITCH_CLASSES, Event, Key

# Krumhansl-Kessler key profiles.
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

CHORD_TEMPLATES = (  # (suffix, intervals) - simpler chords first for tie-breaking
    ("", (0, 4, 7)),
    ("m", (0, 3, 7)),
    ("dim", (0, 3, 6)),
    ("aug", (0, 4, 8)),
    ("sus4", (0, 5, 7)),
    ("sus2", (0, 2, 7)),
    ("7", (0, 4, 7, 10)),
    ("maj7", (0, 4, 7, 11)),
    ("m7", (0, 3, 7, 10)),
    ("m7b5", (0, 3, 6, 10)),
)


def histogram(items: list[Event]) -> list[float]:
    weights = [0.0] * 12
    for event in items:
        weights[event.pitch % 12] += max(event.duration, 1)
    return weights


def _pearson(a: list[float], b: tuple[float, ...]) -> float:
    mean_a = sum(a) / 12
    mean_b = sum(b) / 12
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    den = math.sqrt(sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b))
    return 0.0 if den == 0 else num / den


def key_hypotheses(items: list[Event], limit: int = 5) -> list[dict]:
    weights = histogram(items)
    if sum(weights) == 0:
        return []
    scores = []
    for tonic in range(12):
        rotated = weights[tonic:] + weights[:tonic]
        scores.append((round(_pearson(rotated, MAJOR_PROFILE), 6), 0, tonic, "major"))
        scores.append((round(_pearson(rotated, MINOR_PROFILE), 6), 1, tonic, "minor"))
    scores.sort(key=lambda s: (-s[0], s[1], s[2]))
    return [
        {"key": Key(tonic, mode).name, "tonic_pc": tonic, "mode": mode, "score": score}
        for score, _, tonic, mode in scores[:limit]
    ]


def key_confidence(hypotheses: list[dict], note_count: int) -> float:
    if len(hypotheses) < 2:
        return 0.0
    best, second = hypotheses[0]["score"], hypotheses[1]["score"]
    value = 0.4 + 2.0 * (best - second) + (0.1 if best > 0.7 else 0.0)
    if note_count < 12:
        value -= 0.2
    return round(max(0.05, min(value, 0.9)), 3)


def chord_windows(items: list[Event], ppq: int, window_quarters: int, limit: int = 64) -> list[dict]:
    if not items:
        return []
    span = ppq * window_quarters
    end = max(e.start + max(e.duration, 1) for e in items)
    windows = []
    start = (min(e.start for e in items) // span) * span
    while start < end and len(windows) < limit:
        stop = start + span
        weights = [0.0] * 12
        for event in items:
            overlap = min(stop, event.start + max(event.duration, 1)) - max(start, event.start)
            if overlap > 0:
                weights[event.pitch % 12] += overlap
        total = sum(weights)
        if total > 0:
            ranked = []
            for order, (suffix, intervals) in enumerate(CHORD_TEMPLATES):
                for root in range(12):
                    members = {(root + i) % 12 for i in intervals}
                    inside = sum(weights[pc] for pc in members)
                    outside = total - inside
                    covered = sum(1 for pc in members if weights[pc] > 0) / len(members)
                    score = (inside - 0.5 * outside) / total * covered
                    ranked.append((round(score, 6), order, root, PITCH_CLASSES[root] + suffix))
            ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
            windows.append({
                "start_tick": start,
                "end_tick": stop,
                "hypotheses": [{"chord": name, "root_pc": root, "score": score}
                               for score, _, root, name in ranked[:3] if score > 0],
            })
        start = stop
    return windows
