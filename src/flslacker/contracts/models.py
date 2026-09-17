"""Canonical request/result models. JSON Schemas and OpenAPI are generated from these."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Generic, Literal, TypeVar, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
HASH_PATTERN = r"^[a-f0-9]{64}$"

Uuid = Annotated[str, Field(pattern=UUID_PATTERN, json_schema_extra={"format": "uuid"})]
Sha256 = Annotated[str, Field(pattern=HASH_PATTERN)]
NoteId = Annotated[str, Field(pattern=r"^n[0-9]{1,5}$", description="Snapshot-local note id")]
ClientId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")]
Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr, None]
Number = Union[StrictInt, StrictFloat]
Scope = Literal["selected", "all_exposed"]
OpKind = Literal["note.update", "note.insert", "note.delete"]

# Agent-facing field name -> FL-native Note attribute.
FIELD_TO_FL: dict[str, str] = {
    "pitch": "number",
    "start_tick": "time",
    "duration_tick": "length",
    "velocity": "velocity",
    "pan": "pan",
    "release": "release",
    "color": "color",
    "fcut": "fcut",
    "fres": "fres",
    "pitchofs": "pitchofs",
    "slide": "slide",
    "porta": "porta",
    "muted": "muted",
    "repeats": "repeats",
    "group": "group",
    "selected": "selected",
}
FL_TO_FIELD = {v: k for k, v in FIELD_TO_FL.items()}
UPDATE_FIELDS = tuple(k for k in FIELD_TO_FL if k not in ("group", "selected"))
FLOAT_FIELDS = ("velocity", "pan", "release", "fcut", "fres")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ capabilities & sessions

CapabilityStatus = Literal["supported", "unavailable", "not_connected", "requires_user_action"]


class Capability(Strict):
    name: str
    supported: bool
    status: CapabilityStatus
    execution: str
    scope: str
    identity: str
    verified_build: str | None = None
    limitations: list[str] = Field(default_factory=list)
    reason: str | None = None


class SessionInfo(Strict):
    session_id: Uuid
    adapter_epoch: int
    status: Literal["active", "retired", "invalidated"]
    identity_strength: Literal["verified", "user_attested", "none"]
    created_at: AwareDatetime
    last_fl_contact_at: AwareDatetime | None = None
    fl_host: dict[str, Any] | None = None
    invalidated_reason: str | None = None
    capabilities: list[Capability] = Field(default_factory=list)


class Target(Strict):
    target_id: Uuid
    session_id: Uuid
    binding: Literal["verified", "user_attested"]
    label: str
    observed: dict[str, Scalar] = Field(default_factory=dict)


# ------------------------------------------------------------------ snapshots


class Note(Strict):
    note_id: NoteId
    ordinal: int
    pitch: Number | None
    start_tick: Number | None
    duration_tick: Number | None
    velocity: Number | None
    fl: dict[str, Scalar] = Field(description="All other readable FL properties in native units")
    in_scope: bool
    fingerprint: Sha256


class Marker(Strict):
    ordinal: int
    time: Scalar = None
    name: Scalar = None
    mode: Scalar = None
    tsnum: Scalar = None
    tsden: Scalar = None


class Selection(Strict):
    selected_count: int
    exposed_count: int
    selected_note_ids: list[NoteId]
    timeline: list[Number] | None = None


class Freshness(Strict):
    cached: Literal[True] = True
    age_seconds: int
    is_latest_for_target: bool
    note: str = "Cached capture; the live score may have changed since."


class Snapshot(Strict):
    snapshot_id: Uuid
    session_id: Uuid
    target: Target
    purpose: Literal["capture", "verify", "adhoc"]
    job_id: Uuid | None = None
    captured_at: AwareDatetime
    received_at: AwareDatetime
    coverage: Literal["exposed_score"] = "exposed_score"
    scope: Scope
    ppq: int
    time_signature: list[int] | None = None
    notes: list[Note]
    markers: list[Marker]
    selection: Selection
    in_scope_count: int
    supported_fields: list[str]
    unsupported_fields: dict[str, str] = Field(default_factory=dict)
    extra_note_attributes: list[str] = Field(default_factory=list)
    default_note: dict[str, Scalar] | None = None
    state_hash: Sha256
    content_hash: Sha256
    host: dict[str, Any] = Field(default_factory=dict)
    freshness: Freshness | None = None


# ------------------------------------------------------------------ operations


class NoteSet(Strict):
    """Allowlisted fields an update may change. Omitted fields are untouched."""

    pitch: StrictInt | None = Field(None, ge=0, le=127)
    start_tick: StrictInt | None = Field(None, ge=0)
    duration_tick: StrictInt | None = Field(None, ge=1)
    velocity: Number | None = Field(None, ge=0, le=1)
    pan: Number | None = Field(None, ge=0, le=1)
    release: Number | None = Field(None, ge=0, le=1)
    color: StrictInt | None = Field(None, ge=0, le=15)
    fcut: Number | None = Field(None, ge=0, le=1)
    fres: Number | None = Field(None, ge=0, le=1)
    pitchofs: StrictInt | None = Field(None, ge=-120, le=120)
    slide: StrictBool | None = None
    porta: StrictBool | None = None
    muted: StrictBool | None = None
    repeats: StrictInt | None = Field(None, ge=0, le=14)

    @model_validator(mode="after")
    def _not_empty(self) -> "NoteSet":
        if not self.changes():
            raise ValueError("set must contain at least one field")
        return self

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class NoteTemplate(Strict):
    """A new note. Omitted optional fields are filled from the captured default note."""

    pitch: StrictInt = Field(ge=0, le=127)
    start_tick: StrictInt = Field(ge=0)
    duration_tick: StrictInt = Field(ge=1)
    velocity: Number | None = Field(None, ge=0, le=1)
    pan: Number | None = Field(None, ge=0, le=1)
    release: Number | None = Field(None, ge=0, le=1)
    color: StrictInt | None = Field(None, ge=0, le=15)
    fcut: Number | None = Field(None, ge=0, le=1)
    fres: Number | None = Field(None, ge=0, le=1)
    pitchofs: StrictInt | None = Field(None, ge=-120, le=120)
    slide: StrictBool | None = None
    porta: StrictBool | None = None
    muted: StrictBool | None = None
    repeats: StrictInt | None = Field(None, ge=0, le=14)
    group: StrictInt | None = Field(None, ge=0)
    selected: StrictBool | None = None


class UpdateOp(Strict):
    op: Literal["note.update"]
    note_id: NoteId
    set: NoteSet


class InsertOp(Strict):
    op: Literal["note.insert"]
    client_id: ClientId
    note: NoteTemplate


class DeleteOp(Strict):
    op: Literal["note.delete"]
    note_id: NoteId


Operation = Annotated[Union[UpdateOp, InsertOp, DeleteOp], Field(discriminator="op")]


# ------------------------------------------------------------------ plans


class DiffRow(Strict):
    kind: Literal["update", "insert", "delete"]
    note_id: NoteId | None = None
    client_id: ClientId | None = None
    before: dict[str, Scalar] | None = None
    after: dict[str, Scalar] | None = None
    changed_fields: list[str] = Field(default_factory=list)
    summary: str


class PlanLimits(Strict):
    operation_count: int
    max_operations: int
    update_count: int
    insert_count: int
    delete_count: int
    affected_notes: int
    fields: list[str]
    max_pitch_delta: int
    max_time_delta_ticks: int
    max_velocity_delta: float


class Plan(Strict):
    plan_id: Uuid
    kind: Literal["edit", "restore"] = "edit"
    session_id: Uuid
    target_id: Uuid
    target_label: str
    snapshot_id: Uuid
    scope: Scope
    ppq: int
    base_state_hash: Sha256
    base_note_count: int
    operations: list[Operation]
    resolved_operations: list[dict[str, Any]]
    diff: list[DiffRow]
    limits: PlanLimits
    predicted_content_hash: Sha256
    rationale: str
    warnings: list[str] = Field(default_factory=list)
    restores_receipt_id: Uuid | None = None
    created_by: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    plan_hash: Sha256


class GrantCoverage(Strict):
    covered: bool
    grant_id: Uuid | None = None
    reason: str


class PlanPreview(Strict):
    plan: Plan
    text_diff: str
    warnings: list[str]
    expired: bool
    snapshot_is_latest: bool
    grant_coverage: GrantCoverage


# ------------------------------------------------------------------ jobs, receipts


JobState = Literal[
    "queued",
    "awaiting_fl_action",
    "validating",
    "applying",
    "awaiting_verification",
    "applied",
    "completed",
    "cancelled",
    "expired",
    "failed",
    "outcome_unknown",
    "partial_apply",
    "verification_failed",
]
TERMINAL_STATES = frozenset({"applied", "completed", "cancelled", "expired", "failed"})


class RequiredAction(Strict):
    actor: Literal["user", "agent"]
    where: Literal["fl_piano_roll", "companion_ui", "agent"]
    instruction: str
    script: str | None = None
    request_short_id: str | None = None


class ErrorInfo(Strict):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)
    required_action: RequiredAction | None = None


class JobEvent(Strict):
    state: str
    at: AwareDatetime
    note: str = ""


class Job(Strict):
    job_id: Uuid
    kind: Literal["capture", "apply"]
    purpose: Literal["capture", "verify", "apply", "restore"]
    state: JobState
    session_id: Uuid
    scope: Scope | None = None
    target_label: str | None = None
    plan_id: Uuid | None = None
    plan_hash: Sha256 | None = None
    parent_job_id: Uuid | None = None
    verify_job_id: Uuid | None = None
    request_short_id: str | None = None
    snapshot_id: Uuid | None = None
    receipt_id: Uuid | None = None
    next_action: RequiredAction | None = None
    error: ErrorInfo | None = None
    created_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime
    history: list[JobEvent] = Field(default_factory=list)


class Receipt(Strict):
    receipt_id: Uuid
    job_id: Uuid
    plan_id: Uuid
    plan_hash: Sha256
    target_id: Uuid
    target_label: str
    before_snapshot_id: Uuid
    before_state_hash: Sha256
    after_snapshot_id: Uuid
    after_state_hash: Sha256
    after_content_hash: Sha256
    applied_operation_count: int
    verification: Literal["verified"]
    recovery_available: bool
    recovery_note: str
    changes: list[DiffRow]
    created_at: AwareDatetime


class JobView(Strict):
    job: Job
    receipt: Receipt | None = None


# ------------------------------------------------------------------ grants & recipes


class GrantConstraints(Strict):
    scope: Scope = "selected"
    operation_kinds: list[OpKind] = Field(default_factory=lambda: ["note.update"], min_length=1)
    fields: list[str] = Field(default_factory=lambda: ["pitch"])
    max_notes: int = Field(16, ge=1, le=1000, description="Change budget: total notes this grant may touch")
    max_pitch_delta: int | None = Field(2, ge=0, le=127)
    max_time_delta_ticks: int | None = Field(None, ge=0)
    max_velocity_delta: float | None = Field(None, ge=0, le=1)

    @model_validator(mode="after")
    def _fields_known(self) -> "GrantConstraints":
        unknown = set(self.fields) - set(FIELD_TO_FL)
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return self


class Grant(Strict):
    grant_id: Uuid
    kind: Literal["plan", "recipe"]
    session_id: Uuid
    target_label: str | None = Field(None, description="None: any user-attested target in the session")
    plan_hash: Sha256 | None = None
    constraints: GrantConstraints
    issued_by: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    budget_used: int = 0
    uses: int = 0
    max_uses: int | None = None
    revoked: bool = False


class GrantRequest(Strict):
    session_id: Uuid
    target_label: str | None = None
    constraints: GrantConstraints = Field(default_factory=GrantConstraints)
    lifetime_minutes: int = Field(10, ge=1, le=24 * 60)
    max_uses: int | None = Field(None, ge=1)


class Recipe(Strict):
    recipe_id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    name: str
    description: str
    scope: Scope = "selected"
    analyses: list[str] = Field(default_factory=lambda: ["motifs", "key", "pitch_outliers"])
    parameters: dict[str, Any] = Field(default_factory=dict)
    min_confidence: float = Field(0.6, ge=0, le=1)
    constraints: GrantConstraints = Field(default_factory=GrantConstraints)
    lifetime_minutes: int = Field(10, ge=1, le=240)


# ------------------------------------------------------------------ analysis


class AnalysisParameters(Strict):
    key_hint: str | None = Field(None, description="User-specified key such as 'C major'; treated as a constraint")
    transpose_invariant: bool = True
    min_motif_notes: int = Field(4, ge=3, le=32)
    max_motif_notes: int = Field(12, ge=3, le=64)
    min_occurrences: int = Field(2, ge=2, le=100)
    onset_tolerance_ticks: int | None = Field(None, ge=0, description="Default: PPQ/8")
    allow_overlap: bool = False
    voice: Literal["skyline", "all"] = "skyline"
    exclude_note_ids: list[NoteId] = Field(default_factory=list)
    pitch_range: list[int] | None = Field(None, min_length=2, max_length=2)
    chord_window_beats: int = Field(4, ge=1, le=16)
    max_findings: int = Field(50, ge=1, le=500)
    in_scope_only: bool = True


AnalysisKind = Literal["motifs", "key", "chords", "pitch_outliers"]


class Finding(Strict):
    finding_id: str
    kind: str
    severity: Literal["info", "suggestion"]
    message: str
    confidence: float
    note_ids: list[NoteId] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggestion: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)


class Analysis(Strict):
    analysis_id: Uuid
    snapshot_id: Uuid
    algorithm: str
    version: str
    analyses: list[AnalysisKind]
    parameters: AnalysisParameters
    findings: list[Finding]
    summary: dict[str, Any]
    created_at: AwareDatetime


# ------------------------------------------------------------------ project summary


class ProvenancedValue(Strict):
    status: Literal["available", "unavailable", "stale"]
    value: Any = None
    source: str | None = None
    observed_at: AwareDatetime | None = None
    age_seconds: int | None = None
    reason: str | None = None


class ProjectSummary(Strict):
    session_id: Uuid
    partial: Literal[True] = True
    fields: dict[str, ProvenancedValue]


class CapabilitiesView(Strict):
    protocol: str
    companion_version: str
    fl_build: str | None
    compatibility_record: str | None
    active_session: SessionInfo | None
    capabilities: list[Capability]
    bridge: dict[str, Any]


# ------------------------------------------------------------------ tool inputs


class NoArgs(Strict):
    pass


class SessionArgs(Strict):
    session_id: Uuid


class CaptureScoreArgs(Strict):
    session_id: Uuid
    scope: Scope
    target_label: str | None = Field(None, max_length=120, description="Optional hint shown in FL")


class GetSnapshotArgs(Strict):
    snapshot_id: Uuid
    include_notes: bool = True


class AnalyzeScoreArgs(Strict):
    snapshot_id: Uuid
    analyses: list[AnalysisKind] = Field(min_length=1)
    parameters: AnalysisParameters = Field(default_factory=AnalysisParameters)


class ProposePatchArgs(Strict):
    snapshot_id: Uuid
    operations: list[Operation] = Field(min_length=1, max_length=1000)
    rationale: str = Field(min_length=1, max_length=2000)


class PlanArgs(Strict):
    plan_id: Uuid


class ApplyPlanArgs(Strict):
    plan_id: Uuid
    expected_plan_hash: Sha256
    idempotency_key: Uuid


class JobArgs(Strict):
    job_id: Uuid


class RestoreEditArgs(Strict):
    receipt_id: Uuid
    idempotency_key: Uuid


# ------------------------------------------------------------------ envelope

T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    request_id: Uuid
    data: T | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ToolResult[T]":
        if (self.data is None) == (self.error is None):
            raise ValueError("exactly one of data and error must be set")
        if self.ok != (self.error is None):
            raise ValueError("ok must match error")
        return self
