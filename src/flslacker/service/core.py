"""The single validation, authorization and execution service behind every client.

Local UI, MCP and Ollama all call these methods through ``Dispatcher``. Every write is
serialized by the database lock, journaled before the FL request is written, and only
reported as applied after a fresh capture verifies the predicted result.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from flslacker import __version__
from flslacker.adapters.mailbox import Inbound, MailboxAdapter
from flslacker.analysis import run_analysis
from flslacker.config import Settings
from flslacker.contracts import canonical as canon
from flslacker.contracts import wire
from flslacker.contracts.errors import ErrorCode, FlsError, not_found
from flslacker.contracts.models import (
    TERMINAL_STATES,
    Analysis,
    AnalysisParameters,
    CapabilitiesView,
    ErrorInfo,
    Freshness,
    Grant,
    GrantConstraints,
    GrantCoverage,
    GrantRequest,
    Job,
    JobEvent,
    JobView,
    Plan,
    PlanPreview,
    ProjectSummary,
    ProvenancedValue,
    Receipt,
    Recipe,
    RequiredAction,
    SessionInfo,
    Snapshot,
    Target,
    utcnow,
)
from flslacker.service import capabilities as capmod
from flslacker.service import plans as planmod
from flslacker.service import policy
from flslacker.service.recipes import load_recipes, operations_from_analysis
from flslacker.service.snapshots import build_snapshot, label_key, validate_raw
from flslacker.storage.db import Database, to_json

log = logging.getLogger("flslacker.service")

LOCKING_STATES = (
    "queued",
    "awaiting_fl_action",
    "validating",
    "applying",
    "awaiting_verification",
    "outcome_unknown",
    "partial_apply",
    "verification_failed",
)
RECONCILE_STATES = ("awaiting_verification", "outcome_unknown", "partial_apply", "verification_failed")
MAX_PENDING_CAPTURES = 8
APPLY_RESPONSE_KEYS = {
    "job_id",
    "plan_hash",
    "status",
    "error",
    "applied_operations",
    "before_state_hash",
    "after_state_hash",
    "after_content_hash",
    "after_note_count",
    "mismatches",
    "host",
}


@dataclass(frozen=True)
class Actor:
    name: str
    role: Literal["agent", "ui"]

    @property
    def is_ui(self) -> bool:
        return self.role == "ui"


LOCAL_UI = Actor("local-ui", "ui")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def capture_instruction(job: Job, short: str) -> RequiredAction:
    label = "Verify" if job.purpose == "verify" else "Request"
    target = f" of '{job.target_label}'" if job.target_label else ""
    return RequiredAction(
        actor="user",
        where="fl_piano_roll",
        script="Slacker Capture",
        request_short_id=short,
        instruction=(
            f"In FL Studio, open the Piano Roll{target} and run Tools > Scripting > Slacker > Slacker Capture. "
            f"Choose '{label} {short}', type the target label, tick the confirmation and press OK. "
            + ("Keep the same notes selected as before." if job.scope == "selected" else "")
        ).strip(),
    )


def apply_instruction(job: Job, short: str) -> RequiredAction:
    return RequiredAction(
        actor="user",
        where="fl_piano_roll",
        script="Slacker Apply",
        request_short_id=short,
        instruction=(
            f"In FL Studio, open the Piano Roll of '{job.target_label}' with the same selection as the capture, run "
            f"Tools > Scripting > Slacker > Slacker Apply, choose 'Job {job.job_id[:8]}', tick the target "
            "confirmation and press OK. Cancel leaves the job pending; 'Reject this job' cancels it."
        ),
    )


class Service:
    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        mailbox: MailboxAdapter | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.db = db or Database(settings.db_path)
        self.mailbox = mailbox or MailboxAdapter(settings.home)
        self.clock = clock or time.time
        self.records = capmod.load_records(settings.home)
        self.lock = self.db.lock
        self.session_id: str | None = None
        self.recipes: dict[str, Recipe] = load_recipes(settings.home)
        self.revision = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.installed_fl_build: str | None = None

    # ================================================================== basics
    def now(self) -> datetime:
        return datetime.fromtimestamp(int(self.clock()), timezone.utc)

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def _bump(self) -> None:
        self.revision += 1

    def _meta(self, key: str, value: str | None = None) -> str | None:
        with self.db.transaction() as conn:
            if value is not None:
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
                return value
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return None if row is None else row["value"]

    def _next_sequence(self) -> int:
        with self.db.transaction():
            value = int(self._meta("companion_sequence") or "0") + 1
            self._meta("companion_sequence", str(value))
            return value

    # ================================================================== sessions
    def start(self) -> SessionInfo:
        with self.lock:
            for row in self.db.all("SELECT session_id FROM sessions WHERE status='active'"):
                self._retire(row["session_id"], "companion restarted", "retired")
            self._reconcile_startup()
            return self._new_session()

    def _new_session(self) -> SessionInfo:
        row = self.db.one("SELECT MAX(adapter_epoch) AS epoch FROM sessions")
        epoch = (row["epoch"] or 0) + 1
        info = SessionInfo(
            session_id=self.new_id(),
            adapter_epoch=epoch,
            status="active",
            identity_strength="user_attested",
            created_at=self.now(),
        )
        secret = secrets.token_hex(32)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (info.session_id, epoch, secret, "active", _iso(info.created_at), info.model_dump_json()),
            )
        self.mailbox.open_session(info.session_id)
        self.mailbox.write_pairing(info.session_id, secret, epoch)
        self.session_id = info.session_id
        self.db.journal("session_started", {"session_id": info.session_id, "epoch": epoch})
        self._bump()
        return info

    def stop(self) -> None:
        self.stop_background()
        with self.lock:
            if self.session_id:
                self._retire(self.session_id, "companion stopped", "retired")
                self.session_id = None

    def invalidate_session(self, reason: str) -> SessionInfo:
        with self.lock:
            if self.session_id:
                self._retire(self.session_id, reason, "invalidated")
            return self._new_session()

    def _session(self, session_id: str) -> tuple[SessionInfo, str]:
        row = self.db.one("SELECT doc, secret FROM sessions WHERE session_id=?", (session_id,))
        if row is None:
            raise not_found("session", session_id)
        return SessionInfo.model_validate_json(row["doc"]), row["secret"]

    def _active_session(self, session_id: str) -> tuple[SessionInfo, str]:
        info, secret = self._session(session_id)
        if info.status != "active":
            raise FlsError(
                ErrorCode.EXPIRED,
                f"Session {session_id} has ended ({info.invalidated_reason}). Use the current session.",
                details={"current_session_id": self.session_id},
            )
        return info, secret

    def _save_session(self, info: SessionInfo) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET status=?, doc=? WHERE session_id=?",
                (info.status, info.model_dump_json(), info.session_id),
            )

    def _retire(self, session_id: str, reason: str, status: str) -> None:
        info, _ = self._session(session_id)
        info = info.model_copy(update={"status": status, "invalidated_reason": reason})
        self._save_session(info)
        for job in self._jobs("session_id=? AND state IN ('queued', 'awaiting_fl_action')", (session_id,)):
            self._expire_unclaimed(job, f"session ended: {reason}")
        self.mailbox.remove_pairing(session_id)
        self.db.journal("session_ended", {"session_id": session_id, "reason": reason, "status": status})
        self._bump()

    def _reconcile_startup(self) -> None:
        for job in self._jobs("state='queued'"):
            request_id = self._request_id(job.job_id)
            if request_id and self.mailbox.request_exists(job.session_id, request_id):
                self._transition(job, "awaiting_fl_action", "recovered after restart")
            else:
                self._fail(job, ErrorCode.ADAPTER_OFFLINE, "The request was never delivered to FL.", refund=True)

    def _poll_sessions(self) -> dict[str, str]:
        rows = self.db.all(
            "SELECT DISTINCT s.session_id, s.secret FROM sessions s "
            "LEFT JOIN jobs j ON j.session_id = s.session_id "
            "WHERE s.status='active' OR j.state IN (%s)" % ",".join("?" * len(LOCKING_STATES)),
            LOCKING_STATES,
        )
        return {row["session_id"]: row["secret"] for row in rows}

    # ================================================================== jobs
    def _jobs(self, where: str, params: tuple = ()) -> list[Job]:
        return [Job.model_validate(d) for d in self.db.docs(f"SELECT doc FROM jobs WHERE {where} ORDER BY updated_at", params)]

    def _job(self, job_id: str) -> Job:
        doc = self.db.doc("SELECT doc FROM jobs WHERE job_id=?", (job_id,))
        if doc is None:
            raise not_found("job", job_id)
        return Job.model_validate(doc)

    def _request_id(self, job_id: str) -> str | None:
        row = self.db.one("SELECT request_id FROM jobs WHERE job_id=?", (job_id,))
        return None if row is None else row["request_id"]

    def _job_by_request(self, request_id: str) -> Job | None:
        doc = self.db.doc("SELECT doc FROM jobs WHERE request_id=?", (request_id,))
        return None if doc is None else Job.model_validate(doc)

    def _save_job(self, job: Job, request_id: str | None = None, target_id: str | None = None) -> None:
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT request_id, target_id FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            if existing is not None:
                request_id = request_id or existing["request_id"]
                target_id = target_id or existing["target_id"]
            conn.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job.job_id, job.session_id, job.kind, job.state, request_id, target_id, _iso(job.updated_at),
                 job.model_dump_json()),
            )
        self._bump()

    def _transition(self, job: Job, state: str, note: str = "", **updates: Any) -> Job:
        now = self.now()
        history = list(job.history) + [JobEvent(state=state, at=now, note=note[:300])]
        job = job.model_copy(update={"state": state, "updated_at": now, "history": history, **updates})
        job = Job.model_validate(job.model_dump())
        self._save_job(job)
        self.db.journal("job_state", {"state": state, "note": note}, job_id=job.job_id)
        return job

    def _fail(self, job: Job, code: ErrorCode, message: str, *, state: str = "failed", refund: bool = False,
              details: dict | None = None, next_action: RequiredAction | None = None) -> Job:
        error = ErrorInfo(
            code=code.value,
            message=message,
            retryable=FlsError(code, message).retryable,
            details=details or {},
            required_action=next_action,
        )
        job = self._transition(job, state, message, error=error, next_action=next_action)
        if refund:
            self._refund(job)
        if job.kind == "capture" and job.purpose == "verify" and job.parent_job_id and state in TERMINAL_STATES:
            parent = self._job(job.parent_job_id)
            if parent.state in RECONCILE_STATES and parent.verify_job_id == job.job_id:
                self._transition(parent, parent.state, f"verification request ended ({state})",
                                 next_action=self._fresh_capture_action(parent))
        return job

    @staticmethod
    def _fresh_capture_action(job: Job) -> RequiredAction:
        choice = "New capture: selected notes only" if job.scope == "selected" else \
            "New capture: all notes visible to scripts"
        return RequiredAction(
            actor="user",
            where="fl_piano_roll",
            script="Slacker Capture",
            instruction=(
                f"In FL Studio, open the Piano Roll of '{job.target_label}' and run Tools > Scripting > Slacker > "
                f"Slacker Capture. Choose '{choice}', type the label '{job.target_label}', tick the confirmation "
                "and press OK. That capture verifies the edit."
            ),
        )

    def _expire_unclaimed(self, job: Job, reason: str) -> Job:
        request_id = self._request_id(job.job_id)
        if request_id and not self.mailbox.try_claim(job.session_id, request_id, "companion:expired"):
            return job  # FL claimed it first; its response decides the outcome
        if request_id:
            self.mailbox.withdraw(job.session_id, request_id)
        return self._fail(job, ErrorCode.EXPIRED, reason, state="expired", refund=True)

    # ================================================================== capabilities & summary
    def fl_build(self) -> tuple[str | None, str]:
        if self.session_id:
            info, _ = self._session(self.session_id)
            version = ((info.fl_host or {}).get("fl") or {}).get("version")
            if version:
                return version, "reported by FL this session"
        last = self._meta("last_fl_build")
        if last:
            return last, "last reported by FL"
        if self.installed_fl_build:
            return self.installed_fl_build, "installed executable (not yet reported by FL)"
        return None, "unknown"

    def capability_list(self) -> list:
        build, source = self.fl_build()
        info = None
        if self.session_id:
            info, _ = self._session(self.session_id)
        host = self._host_status(self.session_id) if self.session_id else None
        midi_connected = bool(host and (self.now() - host[0]).total_seconds() < 30)
        return capmod.compute(
            capmod.find_record(self.records, build),
            build,
            source,
            bool(info and info.last_fl_contact_at),
            self.settings.allow_unverified_host,
            midi_connected,
            self.settings.ollama_model,
        )

    def _require(self, *names: str) -> None:
        caps = {c.name: c for c in self.capability_list()}
        for name in names:
            cap = caps[name]
            if not cap.supported:
                raise FlsError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    f"Capability {name} is not available: {cap.reason}",
                    details={"capability": name},
                )

    def get_capabilities(self, actor: Actor) -> CapabilitiesView:
        with self.lock:
            build, source = self.fl_build()
            record = capmod.find_record(self.records, build)
            session = None
            if self.session_id:
                session, _ = self._session(self.session_id)
            caps = self.capability_list()
            if session:
                session = session.model_copy(update={"capabilities": caps})
            return CapabilitiesView(
                protocol=canon.PROTOCOL,
                companion_version=__version__,
                fl_build=build,
                compatibility_record=None if record is None else f"{record.source}:{record.record_id}",
                active_session=session,
                capabilities=caps,
                bridge={
                    "transport": "mailbox_file",
                    "connected": bool(session and session.last_fl_contact_at),
                    "last_fl_contact_at": None if not session or not session.last_fl_contact_at
                    else session.last_fl_contact_at.isoformat(),
                    "fl_build_source": source,
                    "note": "The companion never claims live connectivity from a cached snapshot.",
                },
            )

    def _host_status(self, session_id: str | None) -> tuple[datetime, dict] | None:
        if not session_id:
            return None
        row = self.db.one("SELECT received_at, doc FROM host_status WHERE session_id=?", (session_id,))
        if row is None:
            return None
        return datetime.fromisoformat(row["received_at"]), json.loads(row["doc"])

    def get_project_summary(self, actor: Actor, session_id: str) -> ProjectSummary:
        with self.lock:
            info, _ = self._session(session_id)
            now = self.now()
            fields: dict[str, ProvenancedValue] = {}
            row = self.db.one(
                "SELECT doc FROM snapshots WHERE session_id=? ORDER BY received_at DESC LIMIT 1", (session_id,)
            )
            if row is None:
                missing = ProvenancedValue(status="unavailable", reason="No capture in this session yet.")
                for name in ("target_label", "ppq", "time_signature", "exposed_note_count", "selected_note_count",
                             "timeline_selection"):
                    fields[name] = missing
            else:
                snap = Snapshot.model_validate_json(row["doc"])
                age = int((now - snap.captured_at).total_seconds())

                def cached(value: Any) -> ProvenancedValue:
                    return ProvenancedValue(status="available", value=value, source="piano_roll_capture",
                                            observed_at=snap.captured_at, age_seconds=age,
                                            reason="cached capture; may be out of date")

                fields["target_label"] = cached(snap.target.label)
                fields["ppq"] = cached(snap.ppq)
                fields["time_signature"] = cached(snap.time_signature)
                fields["exposed_note_count"] = cached(snap.selection.exposed_count)
                fields["selected_note_count"] = cached(snap.selection.selected_count)
                fields["timeline_selection"] = cached(snap.selection.timeline)
                fields["latest_snapshot_id"] = cached(snap.snapshot_id)
            fl = (info.fl_host or {}).get("fl") or {}
            fields["fl_build"] = (
                ProvenancedValue(status="available", value=fl.get("version"), source="piano_roll_script",
                                 observed_at=info.last_fl_contact_at)
                if fl.get("version") else ProvenancedValue(status="unavailable", reason="Not reported by FL yet.")
            )
            host = self._host_status(session_id)
            midi_fields = {
                "tempo_bpm": "tempo_bpm",
                "playing": "playing",
                "current_pattern": "current_pattern",
                "project_title": "title",
                "channel_names": ("names", "channels"),
                "pattern_names": ("names", "patterns"),
                "mixer_track_names": ("names", "mixer_tracks"),
            }
            for name, key in midi_fields.items():
                if host is None:
                    fields[name] = ProvenancedValue(
                        status="unavailable",
                        reason="MIDI metadata adapter (device_Slacker.py) is not connected in this session.",
                    )
                    continue
                received, doc = host
                value = doc.get(key[0], {}).get(key[1]) if isinstance(key, tuple) else doc.get(key)
                age = int((now - received).total_seconds())
                fields[name] = ProvenancedValue(
                    status="available" if age <= 10 else "stale",
                    value=value,
                    source="midi_adapter",
                    observed_at=received,
                    age_seconds=age,
                    reason=None if value is not None else "host call unavailable on this API version",
                )
            return ProjectSummary(session_id=session_id, fields=fields)

    # ================================================================== capture
    def capture_score(
        self,
        actor: Actor,
        session_id: str,
        scope: str,
        target_label: str | None = None,
        *,
        purpose: str = "capture",
        parent_job_id: str | None = None,
    ) -> Job:
        with self.lock:
            info, secret = self._active_session(session_id)
            self._require("notes.capture", "bridge.mailbox")
            pending = self._jobs("session_id=? AND kind='capture' AND state IN ('queued','awaiting_fl_action')",
                                 (session_id,))
            if len(pending) >= MAX_PENDING_CAPTURES:
                raise FlsError(ErrorCode.BUSY, "Too many pending capture requests; wait for FL or cancel some.")
            now = self.now()
            ttl = timedelta(minutes=self.settings.capture_ttl_minutes)
            job = Job(
                job_id=self.new_id(),
                kind="capture",
                purpose=purpose,
                state="queued",
                session_id=session_id,
                scope=scope,
                target_label=target_label,
                parent_job_id=parent_job_id,
                created_by=actor.name,
                created_at=now,
                updated_at=now,
                expires_at=now + ttl,
                history=[JobEvent(state="queued", at=now)],
            )
            self._save_job(job)
            body = {"job_id": job.job_id, "scope": scope, "purpose": purpose, "target_label": target_label}
            return self._dispatch_request(job, secret, "capture_request", body, ttl, capture_instruction)

    def _dispatch_request(self, job: Job, secret: str, kind: str, body: dict, ttl: timedelta,
                          instruction: Callable[[Job, str], RequiredAction]) -> Job:
        if self.db.in_transaction:
            raise RuntimeError("FL requests must not be written inside a database transaction")
        request_id = self.new_id()
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET request_id=? WHERE job_id=?", (request_id, job.job_id))
        try:
            self.mailbox.send(job.session_id, secret, kind, body, int(ttl.total_seconds()), self._next_sequence(),
                              request_id=request_id, now=self.clock())
        except Exception as exc:  # disk full, permissions, missing mailbox
            log.exception("mailbox write failed")
            self._fail(job, ErrorCode.ADAPTER_OFFLINE, f"Could not write the FL request: {type(exc).__name__}",
                       refund=True)
            raise FlsError(ErrorCode.ADAPTER_OFFLINE, "The FL mailbox is not writable; see `flslacker doctor`.")
        short = request_id[:8]
        return self._transition(job, "awaiting_fl_action", "request written to the FL mailbox",
                                request_short_id=short, next_action=instruction(job, short))

    # ================================================================== snapshots
    def _target_for(self, session_id: str, label: str) -> Target:
        key = label_key(label)
        doc = self.db.doc("SELECT doc FROM targets WHERE session_id=? AND label_key=?", (session_id, key))
        if doc:
            return Target.model_validate(doc)
        target = Target(target_id=self.new_id(), session_id=session_id, binding="user_attested",
                        label=" ".join(label.split()))
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO targets VALUES (?, ?, ?, ?)",
                         (target.target_id, session_id, key, target.model_dump_json()))
        return target

    def _snapshot_row(self, snapshot_id: str):
        row = self.db.one("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
        if row is None:
            raise not_found("snapshot", snapshot_id)
        return row

    def _context(self, snapshot_id: str) -> planmod.ScoreContext:
        row = self._snapshot_row(snapshot_id)
        raw = json.loads(row["raw"])
        return planmod.ScoreContext(Snapshot.model_validate_json(row["doc"]), raw["notes"], raw["markers"])

    def _latest_snapshot_row(self, target_id: str):
        return self.db.one(
            "SELECT snapshot_id, state_hash FROM snapshots WHERE target_id=? ORDER BY received_at DESC, rowid DESC LIMIT 1",
            (target_id,),
        )

    def get_snapshot(self, actor: Actor, snapshot_id: str, include_notes: bool = True) -> Snapshot:
        with self.lock:
            row = self._snapshot_row(snapshot_id)
            snap = Snapshot.model_validate_json(row["doc"])
            latest = self._latest_snapshot_row(row["target_id"])
            freshness = Freshness(
                age_seconds=int((self.now() - snap.captured_at).total_seconds()),
                is_latest_for_target=latest is not None and latest["snapshot_id"] == snapshot_id,
            )
            update: dict[str, Any] = {"freshness": freshness}
            if not include_notes:
                update["notes"] = []
            return snap.model_copy(update=update)

    def _on_snapshot(self, item: Inbound) -> None:
        body = item.body
        if set(body) != {"job_id", "purpose", "snapshot"}:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "unexpected snapshot body")
        raw = body["snapshot"]
        validate_raw(raw)
        info, _ = self._session(item.session_id)
        job = None
        if item.in_reply_to is not None:
            job = self._job_by_request(item.in_reply_to)
            if job is None or job.kind != "capture" or job.job_id != body["job_id"]:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "snapshot answers an unknown request")
            if job.state != "awaiting_fl_action":
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"snapshot for a job in state {job.state}")
            if raw["scope"] != job.scope:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "snapshot scope differs from the request")
            purpose = job.purpose if job.purpose in ("capture", "verify") else "capture"
        else:
            if body["purpose"] != "adhoc" or body["job_id"] is not None:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "unsolicited snapshot must be ad hoc")
            if info.status != "active":
                raise FlsError(ErrorCode.EXPIRED, "ad-hoc capture for an ended session")
            purpose = "adhoc"
        target = self._target_for(item.session_id, raw["target_label"])
        now = self.now()
        snapshot = build_snapshot(
            raw,
            snapshot_id=self.new_id(),
            session_id=item.session_id,
            target=target,
            purpose=purpose,
            job_id=None if job is None else job.job_id,
            received_at=now,
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot.snapshot_id, item.session_id, target.target_id, _iso(now), snapshot.state_hash,
                 snapshot.content_hash, canon.canonical_dumps(raw), snapshot.model_dump_json()),
            )
            self._record_contact(info, raw["host"])
            self.db.journal("snapshot_ingested", {"snapshot_id": snapshot.snapshot_id, "notes": len(snapshot.notes),
                                                  "target": target.label, "purpose": purpose},
                            job_id=None if job is None else job.job_id)
            if job is not None:
                self._transition(job, "completed", f"snapshot {snapshot.snapshot_id} received",
                                 snapshot_id=snapshot.snapshot_id, next_action=None, target_label=target.label)
        self._reconcile_with(snapshot, None if job is None else job.parent_job_id, item.sequence)
        self._advance_recipe(job, snapshot)

    def _record_contact(self, info: SessionInfo, host: dict) -> None:
        info, _ = self._session(info.session_id)
        info = info.model_copy(update={"last_fl_contact_at": self.now(), "fl_host": host})
        self._save_session(info)
        version = (host.get("fl") or {}).get("version") if isinstance(host.get("fl"), dict) else None
        if isinstance(version, str) and len(version) < 40:
            self._meta("last_fl_build", version)

    # ================================================================== analysis
    def analyze_score(self, actor: Actor, snapshot_id: str, analyses: list[str],
                      parameters: AnalysisParameters) -> Analysis:
        with self.lock:
            ctx = self._context(snapshot_id)
        result = run_analysis(ctx.snapshot, analyses, parameters)
        analysis = Analysis(
            analysis_id=self.new_id(),
            snapshot_id=snapshot_id,
            algorithm=result["algorithm"],
            version=result["version"],
            analyses=analyses,
            parameters=parameters,
            findings=result["findings"],
            summary=result["summary"],
            created_at=self.now(),
        )
        with self.lock:
            self.db.run("INSERT INTO analyses VALUES (?, ?, ?, ?)",
                        (analysis.analysis_id, snapshot_id, _iso(analysis.created_at), analysis.model_dump_json()))
            self._bump()
        return analysis

    # ================================================================== plans
    def _plan(self, plan_id: str) -> Plan:
        doc = self.db.doc("SELECT doc FROM plans WHERE plan_id=?", (plan_id,))
        if doc is None:
            raise not_found("plan", plan_id)
        plan = Plan.model_validate(doc)
        if not planmod.plan_intact(plan):
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "Stored plan failed its integrity check.")
        return plan

    def propose_patch(self, actor: Actor, snapshot_id: str, operations: list[Any], rationale: str, *,
                      kind: str = "edit", restores_receipt_id: str | None = None) -> Plan:
        with self.lock:
            ctx = self._context(snapshot_id)
            self._active_session(ctx.snapshot.session_id)
            if ctx.snapshot.purpose == "verify" and kind == "edit":
                pass  # verified captures are ordinary bases too
            plan = planmod.build_plan(
                ctx,
                operations,
                plan_id=self.new_id(),
                target_id=ctx.snapshot.target.target_id,
                rationale=rationale,
                created_by=actor.name,
                now=self.now(),
                ttl=timedelta(minutes=self.settings.plan_ttl_minutes),
                kind=kind,
                restores_receipt_id=restores_receipt_id,
            )
            self.db.run("INSERT INTO plans VALUES (?, ?, ?, ?, ?)",
                        (plan.plan_id, snapshot_id, plan.plan_hash, _iso(plan.created_at), plan.model_dump_json()))
            self.db.journal("plan_created", {"plan_id": plan.plan_id, "plan_hash": plan.plan_hash,
                                             "summary": planmod.summary_text(plan)}, actor=actor.name)
            self._bump()
            return plan

    def _grants(self, session_id: str | None = None, active_only: bool = True) -> list[Grant]:
        grants = [Grant.model_validate(d) for d in self.db.docs("SELECT doc FROM grants ORDER BY created_at")]
        if session_id:
            grants = [g for g in grants if g.session_id == session_id]
        if active_only:
            now = self.now()
            grants = [g for g in grants if not g.revoked and g.expires_at > now]
        return grants

    def preview_plan(self, actor: Actor, plan_id: str) -> PlanPreview:
        with self.lock:
            plan = self._plan(plan_id)
            latest = self._latest_snapshot_row(plan.target_id)
            coverage = policy.find(self._grants(plan.session_id), plan, self.now())
            warnings = list(plan.warnings)
            expired = self.now() >= plan.expires_at
            is_latest = latest is not None and latest["state_hash"] == plan.base_state_hash
            if expired:
                warnings.append("Plan expired; propose it again from a fresh capture.")
            if not is_latest:
                warnings.append("A newer capture of this target differs from the plan's base; the plan is stale.")
            return PlanPreview(
                plan=plan,
                text_diff=planmod.text_diff(plan),
                warnings=warnings,
                expired=expired,
                snapshot_is_latest=is_latest,
                grant_coverage=GrantCoverage(covered=coverage.covered,
                                             grant_id=coverage.grant.grant_id if coverage.grant else None,
                                             reason=coverage.reason),
            )

    # ================================================================== apply
    def apply_plan(self, actor: Actor, plan_id: str, expected_plan_hash: str, idempotency_key: str,
                   *, purpose: str = "apply") -> Job:
        with self.lock:
            fingerprint = canon.sha256_hex({"op": purpose, "plan_id": plan_id, "plan_hash": expected_plan_hash})
            row = self.db.one("SELECT * FROM idempotency WHERE key=?", (idempotency_key,))
            if row is not None:
                if row["actor"] == actor.name and row["fingerprint"] == fingerprint:
                    return self._job(row["job_id"])
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "idempotency_key was already used for a different request.",
                               http_status=409)
            plan = self._plan(plan_id)
            if plan.plan_hash != expected_plan_hash:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "expected_plan_hash does not match the plan.",
                               details={"plan_hash": plan.plan_hash}, http_status=409)
            now = self.now()
            if now >= plan.expires_at:
                raise FlsError(ErrorCode.EXPIRED, "The plan has expired; propose it again from a fresh capture.")
            info, secret = self._active_session(plan.session_id)
            needed = {capmod.OP_CAPABILITY[policy.OP_NAMES[op["op"]]] for op in plan.resolved_operations}
            self._require("bridge.mailbox", "notes.verify", *sorted(needed))
            latest = self._latest_snapshot_row(plan.target_id)
            if latest is None or latest["state_hash"] != plan.base_state_hash:
                raise FlsError(
                    ErrorCode.STALE_SNAPSHOT,
                    "A newer capture of this target differs from the plan's base snapshot.",
                    details={"latest_snapshot_id": None if latest is None else latest["snapshot_id"]},
                    required_action=RequiredAction(actor="agent", where="agent",
                                                   instruction="Capture again and propose a new plan.").model_dump(),
                )
            busy = self._jobs_for_label(plan.target_label, LOCKING_STATES)
            if busy:
                raise FlsError(ErrorCode.BUSY, f"Job {busy[0].job_id} holds the writer lock for this target "
                               f"({busy[0].state}); finish or reconcile it first.",
                               details={"job_id": busy[0].job_id, "state": busy[0].state})
            coverage = policy.find(self._grants(plan.session_id), plan, now)
            if not coverage.covered:
                self.db.run("INSERT OR REPLACE INTO approval_requests VALUES (?, ?, ?, ?)",
                            (plan.plan_id, actor.name, _iso(now), "pending"))
                self._bump()
                raise FlsError(
                    ErrorCode.PERMISSION_DENIED,
                    f"Writing needs approval: {coverage.reason}.",
                    retryable=True,
                    details={"plan_id": plan.plan_id},
                    required_action=RequiredAction(
                        actor="user", where="companion_ui",
                        instruction=f"Open the FL Slacker UI and approve plan {plan.plan_id[:8]}, then retry.",
                    ).model_dump(),
                )
            grant = coverage.grant
            ttl = timedelta(minutes=self.settings.apply_ttl_minutes)
            job = Job(
                job_id=self.new_id(),
                kind="apply",
                purpose=purpose,
                state="queued",
                session_id=plan.session_id,
                scope=plan.scope,
                target_label=plan.target_label,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                created_by=actor.name,
                created_at=now,
                updated_at=now,
                expires_at=min(now + ttl, plan.expires_at),
                history=[JobEvent(state="queued", at=now, note=f"authorized by {grant.kind} grant {grant.grant_id}")],
            )
            ctx = self._context(plan.snapshot_id)
            touched = sorted({op["ordinal"] for op in plan.resolved_operations if op["op"] != "insert"})
            before_image = {
                "plan_id": plan.plan_id,
                "snapshot_id": plan.snapshot_id,
                "base_state_hash": plan.base_state_hash,
                "grant_id": grant.grant_id,
                "affected": plan.limits.affected_notes,
                "notes": {str(i): ctx.raw_notes[i] for i in touched},
            }
            try:
                with self.db.transaction() as conn:
                    grant = grant.model_copy(update={"budget_used": grant.budget_used + plan.limits.affected_notes,
                                                     "uses": grant.uses + 1})
                    conn.execute("UPDATE grants SET doc=? WHERE grant_id=?", (grant.model_dump_json(), grant.grant_id))
                    self._save_job(job, target_id=plan.target_id)
                    conn.execute("INSERT INTO before_images VALUES (?, ?, ?)",
                                 (job.job_id, plan.plan_id, to_json(before_image)))
                    conn.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
                                 (idempotency_key, actor.name, fingerprint, job.job_id, _iso(now)))
                    conn.execute("UPDATE approval_requests SET status='used' WHERE plan_id=?", (plan.plan_id,))
                    conn.execute(
                        "INSERT INTO journal (at, event, job_id, actor, doc) VALUES (?, ?, ?, ?, ?)",
                        (_iso(now), "apply_prepared", job.job_id, actor.name,
                         to_json({"plan_hash": plan.plan_hash, "grant_id": grant.grant_id})),
                    )
            except Exception as exc:
                log.exception("journaling failed")
                raise FlsError(ErrorCode.ADAPTER_OFFLINE, f"Durable journaling failed ({type(exc).__name__}); no edit was requested.")
            body = {
                "job_id": job.job_id,
                "plan_id": plan.plan_id,
                "plan_hash": plan.plan_hash,
                "snapshot_id": plan.snapshot_id,
                "target_label": plan.target_label,
                "scope": plan.scope,
                "base_state_hash": plan.base_state_hash,
                "base_note_count": plan.base_note_count,
                "ppq": plan.ppq,
                "operations": plan.resolved_operations,
                "summary": planmod.summary_text(plan),
            }
            return self._dispatch_request(job, secret, "apply_request", body, ttl, apply_instruction)

    def _before_image(self, job_id: str) -> dict:
        row = self.db.one("SELECT doc FROM before_images WHERE job_id=?", (job_id,))
        return {} if row is None else json.loads(row["doc"])

    def _refund(self, job: Job) -> None:
        if job.kind != "apply":
            return
        image = self._before_image(job.job_id)
        if not image or image.get("refunded"):
            return
        doc = self.db.doc("SELECT doc FROM grants WHERE grant_id=?", (image["grant_id"],))
        with self.db.transaction() as conn:
            if doc is not None:
                grant = Grant.model_validate(doc)
                grant = grant.model_copy(update={"budget_used": max(0, grant.budget_used - image["affected"]),
                                                 "uses": max(0, grant.uses - 1)})
                conn.execute("UPDATE grants SET doc=? WHERE grant_id=?", (grant.model_dump_json(), grant.grant_id))
            image["refunded"] = True
            conn.execute("UPDATE before_images SET doc=? WHERE job_id=?", (to_json(image), job.job_id))

    def _on_apply_response(self, item: Inbound) -> None:
        body = item.body
        if set(body) != APPLY_RESPONSE_KEYS:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "unexpected apply response body")
        job = self._job_by_request(item.in_reply_to) if item.in_reply_to else None
        if job is None or job.kind != "apply" or body["job_id"] != job.job_id or body["plan_hash"] != job.plan_hash:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "apply response for an unknown job")
        if job.state not in ("awaiting_fl_action", "applying", "outcome_unknown"):
            raise FlsError(ErrorCode.INVALID_ARGUMENT, f"apply response for a job in state {job.state}")
        info, _ = self._session(item.session_id)
        if isinstance(body.get("host"), dict):
            self._record_contact(info, body["host"])
        status = body["status"]
        error = body["error"] if isinstance(body["error"], dict) else {}
        self.db.journal("apply_response", {k: body[k] for k in ("status", "error", "applied_operations",
                                                                "after_content_hash", "mismatches")},
                        job_id=job.job_id)
        image = self._before_image(job.job_id)
        if image:
            # Only captures sent after this response can say anything about the edit.
            image["response_sequence"] = item.sequence
            self.db.run("UPDATE before_images SET doc=? WHERE job_id=?", (to_json(image), job.job_id))
        if status == "applied_provisional":
            job = self._transition(job, "awaiting_verification",
                                   f"FL applied {body['applied_operations']} operations (in-script check passed)")
            self._request_verification(job)
        elif status == "verification_mismatch":
            job = self._fail(job, ErrorCode.VERIFICATION_FAILED,
                             "FL's in-script read-back differed from the prediction.",
                             state="verification_failed", details={"mismatches": body["mismatches"][:20]})
            self._request_verification(job)
        elif status == "partial_apply":
            job = self._fail(job, ErrorCode.PARTIAL_APPLY, str(error.get("message", "partial apply"))[:300],
                             state="partial_apply", details={"applied_operations": body["applied_operations"]})
            self._request_verification(job)
        elif status == "failed":
            code = error.get("code") if error.get("code") in ErrorCode.__members__ else "INVALID_ARGUMENT"
            self._fail(job, ErrorCode(code), "Nothing was changed: " + str(error.get("message", ""))[:300],
                       refund=True, details={"live_state_hash": body["before_state_hash"]},
                       next_action=RequiredAction(actor="agent", where="agent",
                                                  instruction="Capture again, then propose a new plan."))
        elif status == "rejected":
            self._fail(job, ErrorCode.USER_ACTION_REQUIRED, "The user rejected the job in FL. Nothing was changed.",
                       state="cancelled", refund=True)
        else:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "unknown apply status")

    def _request_verification(self, job: Job) -> None:
        """Ask FL for a fresh capture; any capture of the target also reconciles."""
        try:
            verify = self.capture_score(LOCAL_UI, job.session_id, job.scope or "selected", job.target_label,
                                        purpose="verify", parent_job_id=job.job_id)
        except FlsError as exc:
            note = RequiredAction(actor="user", where="fl_piano_roll", script="Slacker Capture",
                                  instruction=f"Run Slacker Capture on '{job.target_label}' to verify ({exc.message}).")
            self._transition(job, job.state, "verification request not written", next_action=note)
            return
        next_action = verify.next_action.model_copy(update={
            "instruction": verify.next_action.instruction
            + " Any fresh capture of this target also verifies the edit.",
        })
        self._transition(job, job.state, f"verification requested ({verify.job_id[:8]})",
                         verify_job_id=verify.job_id, next_action=next_action)

    def _jobs_for_label(self, label: str, states: tuple[str, ...]) -> list[Job]:
        key = label_key(label)
        jobs = self._jobs("kind='apply' AND state IN (%s)" % ",".join("?" * len(states)), states)
        return [j for j in jobs if label_key(j.target_label or "") == key]

    def _evidence_after(self, job: Job) -> int | None:
        """FL sequence (ns) a capture must exceed to be evidence about ``job``'s edit.

        Piano Roll scripts run one at a time, so a capture sent after FL's apply response (or,
        without a response, after FL's claim) was also read after the edit attempt. An older
        capture would show the pre-edit notes and could wrongly prove "not applied".
        """
        image = self._before_image(job.job_id)
        if image.get("response_sequence") is not None:
            return int(image["response_sequence"])
        request_id = self._request_id(job.job_id)
        claim = self.mailbox.claim_info(job.session_id, request_id) if request_id else None
        return None if claim is None else int(claim[1] * 1_000_000_000)

    def _reconcile_with(self, snapshot: Snapshot, prefer_job: str | None, sequence: int) -> None:
        jobs = self._jobs_for_label(snapshot.target.label, RECONCILE_STATES)
        if prefer_job:
            parent = self._job(prefer_job)
            if parent.state in RECONCILE_STATES and all(j.job_id != parent.job_id for j in jobs):
                jobs.append(parent)
        for job in jobs:
            threshold = self._evidence_after(job)
            if threshold is None or sequence <= threshold:
                self.db.journal("reconcile_skipped", {"snapshot_id": snapshot.snapshot_id,
                                                      "reason": "capture predates the edit attempt"},
                                job_id=job.job_id)
                continue
            self._verify_apply(job, snapshot)

    def _verify_apply(self, job: Job, snapshot: Snapshot) -> None:
        plan = self._plan(job.plan_id)
        if label_key(snapshot.target.label) != label_key(plan.target_label):
            self._fail(job, ErrorCode.TARGET_MISMATCH,
                       f"Verification capture named '{snapshot.target.label}', not '{plan.target_label}'.",
                       state=job.state, next_action=self._fresh_capture_action(job))
            return
        before = self._context(plan.snapshot_id)
        after = self._context(snapshot.snapshot_id)
        predicted = canon.predict_after(before.raw_notes, plan.resolved_operations)
        ok, mismatches = canon.match_contents(predicted, after.raw_notes)
        markers_ok = sorted(map(canon.canonical_dumps, before.raw_markers)) == sorted(
            map(canon.canonical_dumps, after.raw_markers))
        ppq_ok = before.snapshot.ppq == after.snapshot.ppq
        if ok and markers_ok and ppq_ok:
            self._finalize(job, plan, before, after)
            return
        unchanged, _ = canon.match_contents(before.raw_notes, after.raw_notes)
        if unchanged and job.state in ("outcome_unknown", "partial_apply", "verification_failed"):
            self._fail(job, ErrorCode.OUTCOME_UNKNOWN if job.state == "outcome_unknown" else ErrorCode.PARTIAL_APPLY,
                       "A fresh capture shows the score unchanged; the edit was not applied.", refund=True)
            return
        if unchanged and job.state == "awaiting_verification":
            details = {"reason": "unchanged", "snapshot_id": snapshot.snapshot_id}
            message = ("The fresh capture still shows the original notes. FL may not have committed the edit, "
                       "or the capture used a different Piano Roll.")
        else:
            details = {"mismatches": mismatches[:20], "markers_match": markers_ok, "ppq_match": ppq_ok,
                       "snapshot_id": snapshot.snapshot_id}
            message = "The fresh capture differs from the predicted result."
        self._fail(job, ErrorCode.VERIFICATION_FAILED, message, state="verification_failed", details=details,
                   next_action=RequiredAction(
                       actor="user", where="companion_ui",
                       instruction="Inspect the notes in FL (Edit > Undo may revert the script). Capture again to "
                                   "re-verify, or acknowledge the job in the UI to release the writer lock."))

    def _finalize(self, job: Job, plan: Plan, before: planmod.ScoreContext, after: planmod.ScoreContext) -> None:
        recovery = True
        note = "Restore builds a guarded inverse plan; it requires the score to still equal this after-state."
        try:
            planmod.inverse_operations(before, plan, after)
        except FlsError as exc:
            recovery = False
            note = f"Restore unavailable: {exc.message}"
        receipt = Receipt(
            receipt_id=self.new_id(),
            job_id=job.job_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            target_id=plan.target_id,
            target_label=plan.target_label,
            before_snapshot_id=plan.snapshot_id,
            before_state_hash=plan.base_state_hash,
            after_snapshot_id=after.snapshot.snapshot_id,
            after_state_hash=after.snapshot.state_hash,
            after_content_hash=after.snapshot.content_hash,
            applied_operation_count=len(plan.resolved_operations),
            verification="verified",
            recovery_available=recovery,
            recovery_note=note,
            changes=plan.diff,
            created_at=self.now(),
        )
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO receipts VALUES (?, ?, ?)",
                         (receipt.receipt_id, job.job_id, receipt.model_dump_json()))
            self._transition(job, "applied", f"verified by snapshot {after.snapshot.snapshot_id}",
                             receipt_id=receipt.receipt_id, next_action=None, error=None)
            self.db.journal("receipt", {"receipt_id": receipt.receipt_id, "plan_hash": plan.plan_hash},
                            job_id=job.job_id)

    # ================================================================== job tools
    def _receipt(self, receipt_id: str) -> Receipt:
        doc = self.db.doc("SELECT doc FROM receipts WHERE receipt_id=?", (receipt_id,))
        if doc is None:
            raise not_found("receipt", receipt_id)
        return Receipt.model_validate(doc)

    def get_job(self, actor: Actor, job_id: str) -> JobView:
        with self.lock:
            job = self._job(job_id)
            receipt = self._receipt(job.receipt_id) if job.receipt_id else None
            return JobView(job=job, receipt=receipt)

    def cancel_job(self, actor: Actor, job_id: str) -> JobView:
        with self.lock:
            job = self._job(job_id)
            if not actor.is_ui and job.created_by != actor.name:
                raise FlsError(ErrorCode.PERMISSION_DENIED, "Only the client that created a job (or the UI) may cancel it.")
            if job.state in TERMINAL_STATES:
                return JobView(job=job)
            if job.state == "queued":
                job = self._fail(job, ErrorCode.USER_ACTION_REQUIRED, f"Cancelled by {actor.name}.",
                                 state="cancelled", refund=True)
            elif job.state == "awaiting_fl_action":
                request_id = self._request_id(job.job_id)
                if self.mailbox.try_claim(job.session_id, request_id, "companion:cancelled"):
                    self.mailbox.withdraw(job.session_id, request_id)
                    job = self._fail(job, ErrorCode.USER_ACTION_REQUIRED, f"Cancelled by {actor.name} before FL acted.",
                                     state="cancelled", refund=True)
                else:
                    job = self._transition(job, "applying" if job.kind == "apply" else job.state,
                                           "cancel requested, but FL had already started; no rollback")
            elif job.state == "applying":
                job = self._transition(job, job.state, "cancel requested during application; no rollback is promised")
            else:
                raise FlsError(ErrorCode.INVALID_ARGUMENT,
                               f"Job is {job.state}; FL has already acted. Use fls_restore_edit after verification.")
            return JobView(job=job)

    def restore_edit(self, actor: Actor, receipt_id: str, idempotency_key: str) -> Job:
        with self.lock:
            row = self.db.one("SELECT * FROM idempotency WHERE key=?", (idempotency_key,))
            if row is not None:
                job = self._job(row["job_id"])
                if row["actor"] == actor.name and job.purpose == "restore":
                    if self._plan(job.plan_id).restores_receipt_id == receipt_id:
                        return job
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "idempotency_key was already used for a different request.",
                               http_status=409)
            plan = self.prepare_restore(actor, receipt_id)
            return self.apply_plan(actor, plan.plan_id, plan.plan_hash, idempotency_key, purpose="restore")

    def prepare_restore(self, actor: Actor, receipt_id: str) -> Plan:
        with self.lock:
            receipt = self._receipt(receipt_id)
            if not receipt.recovery_available:
                raise FlsError(ErrorCode.UNSUPPORTED_CAPABILITY, receipt.recovery_note)
            latest = self._latest_snapshot_row(receipt.target_id)
            if latest is None or latest["state_hash"] != receipt.after_state_hash:
                raise FlsError(
                    ErrorCode.STALE_SNAPSHOT,
                    "The latest capture of this target differs from the recorded after-state; restoring could "
                    "overwrite later edits. Capture again; restore is only possible if nothing changed.",
                )
            for doc in self.db.docs("SELECT doc FROM plans WHERE snapshot_id=? ORDER BY created_at DESC",
                                    (receipt.after_snapshot_id,)):
                existing = Plan.model_validate(doc)
                if existing.restores_receipt_id == receipt_id and self.now() < existing.expires_at:
                    return existing  # reuse, so an approval of this exact plan stays valid
            before = self._context(receipt.before_snapshot_id)
            after = self._context(receipt.after_snapshot_id)
            forward = self._plan(receipt.plan_id)
            operations = planmod.inverse_operations(before, forward, after)
            return self.propose_patch(
                actor, receipt.after_snapshot_id, operations,
                f"Restore of receipt {receipt.receipt_id} (plan {forward.plan_id}).",
                kind="restore", restores_receipt_id=receipt.receipt_id,
            )

    # ================================================================== UI-only
    def _ui_only(self, actor: Actor) -> None:
        if not actor.is_ui:
            raise FlsError(ErrorCode.PERMISSION_DENIED, "Only the local UI can manage grants.")

    def issue_grant(self, actor: Actor, request: GrantRequest, *, kind: str = "recipe",
                    plan_hash: str | None = None) -> Grant:
        self._ui_only(actor)
        with self.lock:
            self._active_session(request.session_id)
            now = self.now()
            grant = Grant(
                grant_id=self.new_id(),
                kind=kind,
                session_id=request.session_id,
                target_label=request.target_label,
                plan_hash=plan_hash,
                constraints=request.constraints,
                issued_by=actor.name,
                created_at=now,
                expires_at=now + timedelta(minutes=request.lifetime_minutes),
                max_uses=1 if kind == "plan" else request.max_uses,
            )
            self.db.run("INSERT INTO grants VALUES (?, ?, ?, ?)",
                        (grant.grant_id, grant.session_id, _iso(now), grant.model_dump_json()))
            self.db.journal("grant_issued", grant.model_dump(mode="json"), actor=actor.name)
            self._bump()
            return grant

    def approve_plan(self, actor: Actor, plan_id: str, lifetime_minutes: int = 10) -> Grant:
        self._ui_only(actor)
        with self.lock:
            plan = self._plan(plan_id)
            request = GrantRequest(session_id=plan.session_id, target_label=plan.target_label,
                                   constraints=policy.plan_constraints(plan), lifetime_minutes=lifetime_minutes)
            grant = self.issue_grant(actor, request, kind="plan", plan_hash=plan.plan_hash)
            self.db.run("UPDATE approval_requests SET status='approved' WHERE plan_id=?", (plan_id,))
            return grant

    def approve_and_apply(self, actor: Actor, plan_id: str) -> Job:
        with self.lock:
            plan = self._plan(plan_id)
            coverage = policy.find(self._grants(plan.session_id), plan, self.now())
            if not coverage.covered:
                self.approve_plan(actor, plan_id)
            return self.apply_plan(actor, plan_id, plan.plan_hash, self.new_id(),
                                   purpose="restore" if plan.kind == "restore" else "apply")

    def revoke_grant(self, actor: Actor, grant_id: str) -> Grant:
        self._ui_only(actor)
        with self.lock:
            doc = self.db.doc("SELECT doc FROM grants WHERE grant_id=?", (grant_id,))
            if doc is None:
                raise not_found("grant", grant_id)
            grant = Grant.model_validate(doc).model_copy(update={"revoked": True})
            self.db.run("UPDATE grants SET doc=? WHERE grant_id=?", (grant.model_dump_json(), grant_id))
            self.db.journal("grant_revoked", {"grant_id": grant_id}, actor=actor.name)
            self._bump()
            return grant

    def acknowledge_job(self, actor: Actor, job_id: str, note: str) -> Job:
        """Release the writer lock of an unresolved job after the user inspected FL."""
        self._ui_only(actor)
        with self.lock:
            job = self._job(job_id)
            if job.state not in ("outcome_unknown", "partial_apply", "verification_failed", "awaiting_verification"):
                raise FlsError(ErrorCode.INVALID_ARGUMENT, f"Job is {job.state}; nothing to acknowledge.")
            return self._fail(job, ErrorCode(job.error.code) if job.error else ErrorCode.OUTCOME_UNKNOWN,
                              f"Acknowledged by the user without verification: {note[:200]}")

    # ================================================================== recipes
    def run_recipe(self, actor: Actor, recipe_id: str, session_id: str, target_label: str | None = None) -> dict:
        self._ui_only(actor)
        recipe = self.recipes.get(recipe_id)
        if recipe is None:
            raise not_found("recipe", recipe_id)
        with self.lock:
            grant = self.issue_grant(actor, GrantRequest(
                session_id=session_id, target_label=target_label, constraints=recipe.constraints,
                lifetime_minutes=recipe.lifetime_minutes))
            job = self.capture_score(actor, session_id, recipe.scope, target_label)
            run = {"run_id": self.new_id(), "recipe_id": recipe_id, "grant_id": grant.grant_id,
                   "capture_job_id": job.job_id, "state": "awaiting_capture", "messages": [],
                   "plan_id": None, "apply_job_id": None, "analysis_id": None}
            self.db.run("INSERT INTO recipe_runs VALUES (?, ?, ?, ?, ?, ?)",
                        (run["run_id"], recipe_id, job.job_id, run["state"], _iso(self.now()), to_json(run)))
            return run

    def _save_run(self, run: dict) -> None:
        self.db.run("UPDATE recipe_runs SET state=?, doc=? WHERE run_id=?", (run["state"], to_json(run), run["run_id"]))
        self._bump()

    def _advance_recipe(self, job: Job | None, snapshot: Snapshot) -> None:
        if job is None:
            return
        row = self.db.one("SELECT doc FROM recipe_runs WHERE capture_job_id=?", (job.job_id,))
        if row is None:
            return
        run = json.loads(row["doc"])
        recipe = self.recipes[run["recipe_id"]]
        try:
            params = AnalysisParameters.model_validate(recipe.parameters)
            analysis = self.analyze_score(LOCAL_UI, snapshot.snapshot_id, recipe.analyses, params)
            run["analysis_id"] = analysis.analysis_id
            operations, skipped = operations_from_analysis(analysis, recipe)
            run["messages"] += skipped
            if not operations:
                run["state"] = "nothing_to_do"
                run["messages"].append("No suggestion met the recipe's confidence and limits.")
                self._save_run(run)
                return
            plan = self.propose_patch(Actor(f"recipe:{recipe.recipe_id}", "agent"), snapshot.snapshot_id,
                                      operations, f"Recipe '{recipe.name}': {len(operations)} suggested pitch changes.")
            run["plan_id"] = plan.plan_id
            apply_job = self.apply_plan(LOCAL_UI, plan.plan_id, plan.plan_hash, self.new_id())
            run["apply_job_id"] = apply_job.job_id
            run["state"] = "awaiting_apply"
        except FlsError as exc:
            run["state"] = "stopped"
            run["messages"].append(f"{exc.code.value}: {exc.message}")
        self._save_run(run)

    # ================================================================== background
    def pump(self) -> int:
        """Ingest mailbox messages and sweep timeouts. Returns the number of messages handled."""
        handled = 0
        with self.lock:
            accepted, rejected = self.mailbox.poll(self._poll_sessions(), now=self.clock())
            for item in rejected:
                self.db.journal("message_rejected", {"file": item.path.name, "reason": item.reason})
                self.mailbox.reject(item)
            for item in accepted:
                handled += 1
                seen = self.db.one("SELECT 1 FROM messages WHERE session_id=? AND request_id=?",
                                   (item.session_id, item.request_id))
                # The MIDI script and the Piano Roll scripts are separate sequence streams.
                stream = "kind = 'midi_status'" if item.kind == "midi_status" else "kind != 'midi_status'"
                last = self.db.one(f"SELECT MAX(sequence) AS s FROM messages WHERE session_id=? AND sender='fl' "
                                   f"AND {stream}", (item.session_id,))
                if seen is not None:
                    self._reject(item, "replayed message")
                    continue
                if last is not None and last["s"] is not None and item.sequence <= last["s"]:
                    self._reject(item, "sequence not increasing")
                    continue
                self.db.run("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                            (item.session_id, item.request_id, "fl", item.sequence, item.kind, _iso(self.now())))
                try:
                    if item.kind == "snapshot":
                        self._on_snapshot(item)
                    elif item.kind == "apply_response":
                        self._on_apply_response(item)
                    elif item.kind == "capture_error":
                        self._on_capture_error(item)
                    elif item.kind == "midi_status":
                        self._on_midi_status(item)
                except FlsError as exc:
                    self._reject(item, f"{exc.code.value}: {exc.message}")
                    continue
                except Exception as exc:  # never let one bad file stop the bridge
                    log.exception("failed to process %s", item.path.name)
                    self._reject(item, f"internal error: {type(exc).__name__}")
                    continue
                self.mailbox.archive(item)
            self._sweep()
        return handled

    def _reject(self, item: Inbound, reason: str) -> None:
        self.db.journal("message_rejected", {"file": item.path.name, "kind": item.kind, "reason": reason})
        self.mailbox.reject(item, reason)
        self._bump()

    def _on_capture_error(self, item: Inbound) -> None:
        body = item.body
        job = self._job_by_request(item.in_reply_to) if item.in_reply_to else None
        if set(body) != {"job_id", "code", "message"} or job is None or job.job_id != body["job_id"]:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "capture error for an unknown job")
        code = body["code"] if body["code"] in ErrorCode.__members__ else "INVALID_ARGUMENT"
        self._fail(job, ErrorCode(code), str(body["message"])[:300])

    def _on_midi_status(self, item: Inbound) -> None:
        body = item.body
        if len(canon.canonical_dumps(body)) > 64 * 1024:
            raise FlsError(ErrorCode.LIMIT_EXCEEDED, "status too large")
        event = body.get("event")
        info, _ = self._session(item.session_id)
        if info.status != "active":
            return
        self.db.run("INSERT OR REPLACE INTO host_status VALUES (?, ?, ?)",
                    (item.session_id, _iso(self.now()), to_json(body)))
        self._bump()
        if event == "project_load":
            self.invalidate_session("FL loaded a project")

    def _sweep(self) -> None:
        now = self.now()
        timeout = timedelta(minutes=self.settings.outcome_timeout_minutes)
        for job in self._jobs("state IN ('awaiting_fl_action', 'applying')"):
            request_id = self._request_id(job.job_id)
            claim = self.mailbox.claim_info(job.session_id, request_id) if request_id else None
            claimed_at = None if claim is None else datetime.fromtimestamp(claim[1], timezone.utc)
            if job.state == "awaiting_fl_action":
                if claim is not None and claim[0].startswith("companion:"):
                    state = "cancelled" if claim[0] == "companion:cancelled" else "expired"
                    self.mailbox.withdraw(job.session_id, request_id)
                    self._fail(job, ErrorCode.EXPIRED if state == "expired" else ErrorCode.USER_ACTION_REQUIRED,
                               f"Request was {state} by the companion.", state=state, refund=True)
                    continue
                if claim is not None and claim[0].startswith("fl:"):
                    if job.kind == "apply" and claim[0] == "fl:applying":
                        job = self._transition(job, "applying", "FL claimed the job")
                    elif now - claimed_at > timeout:
                        self._fail(job, ErrorCode.OUTCOME_UNKNOWN, "FL claimed the capture but never reported.")
                        continue
                elif claim is None and now >= job.expires_at:
                    self._expire_unclaimed(job, "Nobody ran the FL script before the request expired.")
                    continue
            if job.state == "applying" and claimed_at is not None and now - claimed_at > timeout:
                job = self._fail(
                    job, ErrorCode.OUTCOME_UNKNOWN,
                    "FL started applying but no result arrived. Do not retry; capture to reconcile.",
                    state="outcome_unknown",
                )
                self._request_verification(job)

    def run_background(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.pump()
                except Exception:
                    log.exception("pump failed")
                self._stop.wait(self.settings.poll_interval_seconds)

        self._thread = threading.Thread(target=loop, name="flslacker-pump", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None

    # ================================================================== UI state
    def ui_state(self) -> dict[str, Any]:
        with self.lock:
            caps = self.get_capabilities(LOCAL_UI)
            snapshots = []
            for row in self.db.all(
                "SELECT doc, target_id FROM snapshots WHERE session_id=? ORDER BY received_at DESC, rowid DESC LIMIT 20",
                (self.session_id or "",),
            ):
                snap = Snapshot.model_validate_json(row["doc"])
                latest = self._latest_snapshot_row(row["target_id"])
                snapshots.append({
                    "snapshot_id": snap.snapshot_id, "target": snap.target.label, "scope": snap.scope,
                    "purpose": snap.purpose, "captured_at": snap.captured_at.isoformat(),
                    "age_seconds": int((self.now() - snap.captured_at).total_seconds()),
                    "notes": len(snap.notes), "in_scope": snap.in_scope_count,
                    "is_latest": latest["snapshot_id"] == snap.snapshot_id, "state_hash": snap.state_hash,
                })
            jobs = [j.model_dump(mode="json") for j in self._jobs("1=1")][-40:]
            jobs.reverse()
            plans = []
            for doc in self.db.docs("SELECT doc FROM plans ORDER BY created_at DESC LIMIT 20"):
                plan = Plan.model_validate(doc)
                approval = self.db.one("SELECT status, requested_by FROM approval_requests WHERE plan_id=?",
                                       (plan.plan_id,))
                plans.append({
                    "plan_id": plan.plan_id, "kind": plan.kind, "target": plan.target_label,
                    "summary": planmod.summary_text(plan), "created_by": plan.created_by,
                    "created_at": plan.created_at.isoformat(), "expires_at": plan.expires_at.isoformat(),
                    "expired": self.now() >= plan.expires_at, "snapshot_id": plan.snapshot_id,
                    "approval": None if approval is None else dict(approval),
                })
            receipts = [Receipt.model_validate(d).model_dump(mode="json")
                        for d in self.db.docs("SELECT doc FROM receipts ORDER BY rowid DESC LIMIT 20")]
            grants = [g.model_dump(mode="json") for g in self._grants(active_only=False)[-20:]]
            runs = self.db.docs("SELECT doc FROM recipe_runs ORDER BY created_at DESC LIMIT 10")
            for run in runs:
                if run.get("apply_job_id"):
                    run["apply_state"] = self._job(run["apply_job_id"]).state
            host = self._host_status(self.session_id)
            return {
                "revision": self.revision,
                "now": self.now().isoformat(),
                "capabilities": caps.model_dump(mode="json"),
                "snapshots": snapshots,
                "jobs": jobs,
                "plans": plans,
                "receipts": receipts,
                "grants": grants,
                "recipes": [r.model_dump(mode="json") for r in self.recipes.values()],
                "recipe_runs": runs,
                "host_status": None if host is None else {"received_at": host[0].isoformat(), "status": host[1]},
                "settings": {"local_only": self.settings.local_only, "ollama_model": self.settings.ollama_model,
                             "allow_unverified_host": self.settings.allow_unverified_host},
            }
