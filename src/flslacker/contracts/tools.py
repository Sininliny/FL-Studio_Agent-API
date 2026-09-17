"""Agent tool registry. HTTP, MCP and Ollama schemas are all generated from this table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from flslacker.contracts import models as m


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: str  # read | propose | scoped_write | owning_client
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    accepted_job: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return m.ToolResult[self.output_model].model_json_schema()

    def annotations(self) -> dict[str, Any]:
        return {
            "title": self.name,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": False,
        }

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
            "outputSchema": self.output_schema(),
            "annotations": self.annotations(),
            "x-permission": self.permission,
        }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "fls_get_capabilities",
        "Discover what this FL Studio build and session can do. Call first. Capabilities with "
        "supported=false must be reported to the user as unavailable, not worked around.",
        m.NoArgs,
        m.CapabilitiesView,
        "read",
    ),
    ToolSpec(
        "fls_get_project_summary",
        "Partial project summary. Every field carries status, source and age; report 'unavailable' "
        "fields as unknown instead of guessing.",
        m.SessionArgs,
        m.ProjectSummary,
        "read",
    ),
    ToolSpec(
        "fls_capture_score",
        "Request a fresh capture of the notes FL exposes in the Piano Roll. scope must be chosen "
        "explicitly: 'selected' (an empty selection captures nothing) or 'all_exposed'. Returns a job "
        "in state awaiting_fl_action: tell the user the exact next_action and poll fls_get_job.",
        m.CaptureScoreArgs,
        m.Job,
        "read",
        idempotent=False,
        accepted_job=True,
    ),
    ToolSpec(
        "fls_get_snapshot",
        "Read an immutable captured snapshot. It is cached data; check freshness before relying on it. "
        "note_id values are only valid within this snapshot.",
        m.GetSnapshotArgs,
        m.Snapshot,
        "read",
    ),
    ToolSpec(
        "fls_analyze_score",
        "Deterministic analysis of a snapshot: motifs, ranked key/chord hypotheses and likely pitch "
        "outliers. Findings are suggestions with evidence, not certainty. key_hint is treated as a "
        "user constraint; an inferred key is only a hypothesis.",
        m.AnalyzeScoreArgs,
        m.Analysis,
        "read",
    ),
    ToolSpec(
        "fls_propose_patch",
        "Validate operations (note.update / note.insert / note.delete) against a snapshot and create "
        "an immutable plan with an exact diff and plan_hash. Propose the smallest patch that satisfies "
        "the request. Proposing never edits FL.",
        m.ProposePatchArgs,
        m.Plan,
        "propose",
        idempotent=False,
    ),
    ToolSpec(
        "fls_preview_plan",
        "Show a plan's exact diff, limits, warnings, staleness and whether an existing grant covers it.",
        m.PlanArgs,
        m.PlanPreview,
        "read",
    ),
    ToolSpec(
        "fls_apply_plan",
        "Request application of an immutable plan; may require an FL-side action. Needs an existing "
        "grant (PERMISSION_DENIED otherwise: ask the user to approve in the FL Slacker UI). Returns a "
        "job; the edit is only done when fls_get_job reports state 'applied' with a verified receipt.",
        m.ApplyPlanArgs,
        m.Job,
        "scoped_write",
        read_only=False,
        destructive=True,
        idempotent=True,
        accepted_job=True,
    ),
    ToolSpec(
        "fls_get_job",
        "Job status, the exact user action required next, errors, and the receipt once verified. "
        "Poll with bounded backoff.",
        m.JobArgs,
        m.JobView,
        "read",
    ),
    ToolSpec(
        "fls_cancel_job",
        "Cancel queued work you created. Succeeds only before FL acts; it never rolls back an edit.",
        m.JobArgs,
        m.JobView,
        "owning_client",
        read_only=False,
        idempotent=True,
    ),
    ToolSpec(
        "fls_restore_edit",
        "Build and request a guarded inverse plan for a verified receipt. Only works while the score "
        "still equals the recorded after-state; it is not a global Undo. Needs a covering grant.",
        m.RestoreEditArgs,
        m.Job,
        "scoped_write",
        read_only=False,
        destructive=True,
        idempotent=True,
        accepted_job=True,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
WRITE_TOOLS = frozenset(t.name for t in TOOLS if t.permission == "scoped_write")
