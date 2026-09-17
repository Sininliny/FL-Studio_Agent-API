"""Saved 'one-click slacker' recipes: an analysis + conservative transform + bounded grant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flslacker.config import write_private_json
from flslacker.contracts.models import Analysis, GrantConstraints, NoteSet, Recipe, UpdateOp

BUILTIN_RECIPES = (
    Recipe(
        recipe_id="slacker-fix-outliers",
        name="Fix likely pitch outliers",
        description=(
            "Capture the selected notes, detect motifs and likely pitch outliers, and propose "
            "conservative pitch changes: pitch updates only, at most 16 notes, at most two "
            "semitones per note, no inserts or deletes, ten-minute grant."
        ),
        scope="selected",
        analyses=["motifs", "key", "pitch_outliers"],
        parameters={},
        min_confidence=0.6,
        constraints=GrantConstraints(
            scope="selected",
            operation_kinds=["note.update"],
            fields=["pitch"],
            max_notes=16,
            max_pitch_delta=2,
        ),
        lifetime_minutes=10,
    ),
)


def recipes_path(home: Path) -> Path:
    return Path(home) / "recipes.json"


def load_recipes(home: Path) -> dict[str, Recipe]:
    recipes = {r.recipe_id: r for r in BUILTIN_RECIPES}
    path = recipes_path(home)
    if path.exists():
        for item in json.loads(path.read_text(encoding="utf-8")).get("recipes", []):
            recipe = Recipe.model_validate(item)
            recipes[recipe.recipe_id] = recipe
    return recipes


def save_recipe(home: Path, recipe: Recipe) -> None:
    if any(recipe.recipe_id == r.recipe_id for r in BUILTIN_RECIPES):
        raise ValueError("built-in recipes cannot be replaced")
    path = recipes_path(home)
    existing = json.loads(path.read_text(encoding="utf-8")).get("recipes", []) if path.exists() else []
    existing = [r for r in existing if r.get("recipe_id") != recipe.recipe_id]
    existing.append(recipe.model_dump(mode="json"))
    write_private_json(path, {"recipes": existing})


def operations_from_analysis(analysis: Analysis, recipe: Recipe) -> tuple[list[UpdateOp], list[str]]:
    """Turn pitch suggestions into update operations that fit the recipe's constraints."""
    c = recipe.constraints
    ops: list[UpdateOp] = []
    skipped: list[str] = []
    used: set[str] = set()
    findings = sorted(
        (f for f in analysis.findings if f.suggestion and f.severity == "suggestion"),
        key=lambda f: (-f.confidence, f.finding_id),
    )
    for finding in findings:
        suggestion: dict[str, Any] = finding.suggestion or {}
        note_id = suggestion.get("note_id")
        new_pitch = suggestion.get("set", {}).get("pitch")
        old_pitch = finding.evidence.get("current_pitch")
        if suggestion.get("op") != "note.update" or note_id is None or new_pitch is None:
            continue
        if note_id in used:
            continue
        if finding.confidence < recipe.min_confidence:
            skipped.append(f"{note_id}: confidence {finding.confidence:.2f} below {recipe.min_confidence}")
            continue
        if set(suggestion["set"]) - set(c.fields):
            skipped.append(f"{note_id}: changes fields outside the recipe")
            continue
        if c.max_pitch_delta is not None and old_pitch is not None and abs(new_pitch - old_pitch) > c.max_pitch_delta:
            skipped.append(f"{note_id}: {abs(new_pitch - old_pitch)} semitones exceeds {c.max_pitch_delta}")
            continue
        if len(ops) >= c.max_notes:
            skipped.append(f"{note_id}: recipe note budget ({c.max_notes}) reached")
            continue
        used.add(note_id)
        ops.append(UpdateOp(op="note.update", note_id=note_id, set=NoteSet(pitch=new_pitch)))
    return ops, skipped
