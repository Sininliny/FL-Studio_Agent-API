"""`flslacker m0`: guided host-feasibility checklist that records real FL evidence.

Start the companion with ``flslacker serve --allow-unverified-host`` and use a NEW,
disposable FL project. The wizard asks you to run the Slacker scripts, checks what
arrived, and writes ``compatibility.local.json`` so capabilities can report the exact
tested build. Nothing is claimed that was not observed.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from flslacker import __version__, hostinfo
from flslacker.config import Credentials, Settings, write_private_json
from flslacker.service.capabilities import os_family

LABEL = "M0 test"
TERMINAL = {"applied", "completed", "cancelled", "expired", "failed", "verification_failed", "partial_apply",
            "outcome_unknown"}


class Operator(Protocol):
    def instruct(self, step: str, text: str) -> None: ...
    def ask_int(self, step: str, text: str) -> int: ...
    def ask_yes(self, step: str, text: str) -> bool: ...
    def say(self, text: str) -> None: ...


class ConsoleOperator:
    def say(self, text: str) -> None:
        print(text, flush=True)

    def instruct(self, step: str, text: str) -> None:
        print(f"\n[{step}] {text}", flush=True)
        input("    Press Enter when done (Ctrl+C aborts)... ")

    def ask_int(self, step: str, text: str) -> int:
        while True:
            answer = input(f"[{step}] {text} ").strip()
            if answer.isdigit():
                return int(answer)
            print("    Please type a whole number.")

    def ask_yes(self, step: str, text: str) -> bool:
        return input(f"[{step}] {text} [y/n] ").strip().lower().startswith("y")


class Api:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        creds = Credentials.load(settings)
        if creds is None:
            raise SystemExit("No credentials: start `flslacker serve --allow-unverified-host` first.")
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{creds.port}", timeout=30, trust_env=False,
                                   headers={"Authorization": f"Bearer {creds.ui_token}"}, transport=transport)

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.client.post(f"/v1/tools/{name}", json=arguments).json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        response = self.client.post(path, json=body or {})
        return response.status_code, response.json()

    def get(self, path: str) -> Any:
        return self.client.get(path).json()


@dataclass
class Evidence:
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def mark(self, capability: str, ok: bool, note: str) -> None:
        self.capabilities[capability] = {"status": "verified" if ok else "failed", "note": note}

    def step(self, name: str, ok: bool, **details: Any) -> None:
        self.steps.append({"step": name, "ok": ok, **details})


class Wizard:
    def __init__(self, settings: Settings, operator: Operator, api: Api | None = None,
                 poll: float = 1.0, timeout: float = 900.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self.op = operator
        self.api = api or Api(settings)
        self.poll = poll
        self.timeout = timeout
        self.sleep = sleep
        self.ev = Evidence()
        self.session_id: str | None = None

    # ------------------------------------------------------------ helpers
    def job(self, job_id: str) -> dict[str, Any]:
        return self.api.tool("fls_get_job", {"job_id": job_id})["data"]

    def wait(self, job_id: str, states: set[str]) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            view = self.job(job_id)
            if view["job"]["state"] in states:
                return view
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id[:8]} stayed {view['job']['state']}")
            self.sleep(self.poll)

    def capture(self, step: str, scope: str = "selected", text: str = "") -> dict[str, Any] | None:
        result = self.api.tool("fls_capture_score", {"session_id": self.session_id, "scope": scope,
                                                     "target_label": LABEL})
        if not result["ok"]:
            raise RuntimeError(result["error"]["message"])
        job = result["data"]
        self.op.instruct(step, (text + " " if text else "") + job["next_action"]["instruction"]
                         + f" Use the target label '{LABEL}'.")
        view = self.wait(job["job_id"], TERMINAL)
        if view["job"]["state"] != "completed":
            return None
        return self.api.tool("fls_get_snapshot", {"snapshot_id": view["job"]["snapshot_id"]})["data"]

    def apply(self, step: str, snapshot_id: str, operations: list[dict[str, Any]], text: str = "") -> dict[str, Any]:
        plan = self.api.tool("fls_propose_patch", {"snapshot_id": snapshot_id, "operations": operations,
                                                    "rationale": f"M0 {step}"})
        if not plan["ok"]:
            raise RuntimeError(plan["error"]["message"])
        status, job = self.api.post(f"/v1/ui/plans/{plan['data']['plan_id']}/apply")
        if status >= 400:
            raise RuntimeError(job["error"]["message"])
        self.op.instruct(step, (text + " " if text else "") + job["next_action"]["instruction"])
        view = self.wait(job["job_id"], TERMINAL | {"awaiting_verification"})
        if view["job"]["state"] in ("awaiting_verification", "verification_failed", "partial_apply"):
            verify_job = view["job"]["verify_job_id"]
            self.op.instruct(step + "-verify", self.job(verify_job)["job"]["next_action"]["instruction"]
                             + f" Use the target label '{LABEL}'.")
            self.wait(verify_job, TERMINAL)
            view = self.job(job["job_id"])
        return view

    # ------------------------------------------------------------ steps
    def run(self) -> Path:
        caps = self.api.tool("fls_get_capabilities", {})["data"]
        by_name = {c["name"]: c for c in caps["capabilities"]}
        if not by_name["notes.patch.update"]["supported"]:
            raise SystemExit("Restart the companion with `flslacker serve --allow-unverified-host` for M0.")
        self.session_id = caps["active_session"]["session_id"]
        self.op.say("FL Slacker M0 checklist. Use a NEW, disposable FL project; nothing here touches other projects.")
        try:
            self._steps()
        except (KeyboardInterrupt, TimeoutError, RuntimeError) as exc:
            self.op.say(f"Stopped early: {type(exc).__name__}: {exc}. Recording what was observed so far.")
            self.ev.step("aborted", False, reason=str(exc)[:200])
        return self.write_record()

    def _steps(self) -> None:
        started = time.time()
        self.op.instruct(
            "prepare",
            "Save your work, then in FL use File > New. Open a pattern's Piano Roll and draw exactly 6 notes "
            "at different positions. Give one note a non-default velocity and pan (double-click the note to "
            "edit its properties). Then select exactly 3 notes.",
        )
        total = self.op.ask_int("prepare", "How many notes are in the Piano Roll?")
        selected = self.op.ask_int("prepare", "How many of them are selected?")
        self.ev.facts["prepared"] = {"total": total, "selected": selected}

        # Probe: what does FL expose?
        self.op.instruct("probe", "Run Piano Roll > Tools > Scripting > Slacker > Slacker Probe and press OK.")
        reports = [p for p in sorted(self.settings.probe_dir.glob("probe-*.json"), key=lambda p: p.stat().st_mtime)
                   if p.stat().st_mtime >= started - 1]
        if reports:
            probe = json.loads(reports[-1].read_text(encoding="utf-8"))
            count = probe.get("noteCount")
            if count == total:
                semantics = "all notes exposed; selection reported per note"
            elif count == selected:
                semantics = "only selected notes exposed while a selection exists"
            else:
                semantics = f"unexpected: noteCount={count}, total={total}, selected={selected}"
            self.ev.facts.update({
                "fl_build": (probe.get("host", {}).get("fl") or {}).get("version"),
                "embedded_python": probe.get("host", {}).get("python"),
                "selection_semantics": semantics,
                "script_module_name": probe.get("module_name"),
                "stdlib_modules": probe.get("modules"),
                "note_field_errors": probe.get("note_errors"),
                "extra_note_attributes": probe.get("first_note_extras"),
                "probe_report": reports[-1].name,
            })
            self.ev.step("probe", True, report=reports[-1].name, noteCount=count)
        else:
            self.ev.step("probe", False, reason="no new probe report found")
        self.ev.facts["script_runs_once"] = self.op.ask_yes(
            "probe", "Did the Probe run exactly once, and did no Slacker dialog appear merely from opening the menu?")

        # Capture
        snap = self.capture("capture")
        ok = bool(snap) and snap["in_scope_count"] == selected and not snap["unsupported_fields"]
        if snap:
            fingerprints = [n["fingerprint"] for n in snap["notes"]]
            duplicates = len(fingerprints) - len(set(fingerprints))
            self.ev.facts["identical_notes_in_capture"] = duplicates
            self.ev.facts.setdefault("fl_build", (snap["host"].get("fl") or {}).get("version"))
            self.ev.step("capture", ok, snapshot_id=snap["snapshot_id"], exposed=snap["selection"]["exposed_count"],
                         in_scope=snap["in_scope_count"], unsupported=snap["unsupported_fields"])
        else:
            self.ev.step("capture", False)
        self.ev.mark("bridge.mailbox", bool(snap), "capture request/snapshot round trip inside FL")
        self.ev.mark("notes.capture", ok, f"in-scope {snap and snap['in_scope_count']} vs selected {selected}")
        if not snap:
            return

        # Dialog cancel writes nothing.
        result = self.api.tool("fls_capture_score", {"session_id": self.session_id, "scope": "selected",
                                                     "target_label": LABEL})
        job = result["data"]
        self.op.instruct("cancel", f"Run Slacker Capture, choose 'Request {job['request_short_id']}', then press "
                                   "Cancel (not OK).")
        self.sleep(2 * self.poll)
        state = self.job(job["job_id"])["job"]["state"]
        self.ev.facts["dialog_cancel_writes_nothing"] = state == "awaiting_fl_action"
        self.ev.step("dialog_cancel", state == "awaiting_fl_action", state=state)
        self.api.tool("fls_cancel_job", {"job_id": job["job_id"]})

        # Update (pitch, velocity, time) and verification.
        in_scope = [n for n in snap["notes"] if n["in_scope"]]
        first = in_scope[0]
        ops = [{"op": "note.update", "note_id": first["note_id"],
                "set": {"pitch": min(127, first["pitch"] + 1), "velocity": 0.5}}]
        if len(in_scope) > 1:
            second = in_scope[1]
            ops.append({"op": "note.update", "note_id": second["note_id"],
                        "set": {"start_tick": second["start_tick"] + snap["ppq"]}})
        view = self.apply("update", snap["snapshot_id"], ops)
        applied = view["job"]["state"] == "applied"
        self.ev.mark("notes.patch.update", applied, f"job {view['job']['job_id']} ended {view['job']['state']}")
        self.ev.mark("notes.verify", applied, "fresh capture after dialog closure matched the prediction"
                     if applied else f"verification ended {view['job']['state']}")
        self.ev.step("update", applied, job=view["job"]["job_id"], state=view["job"]["state"],
                     error=view["job"].get("error"))

        # Undo behaviour.
        if applied:
            self.op.instruct("undo", "In FL press Ctrl+Z once (Edit > Undo).")
            after_undo = self.capture("undo", text="Now capture again.")
            if after_undo:
                reverted = after_undo["content_hash"] == snap["content_hash"]
                self.ev.facts["undo_reverts_whole_script"] = reverted
                self.ev.step("undo", True, reverted=reverted, snapshot_id=after_undo["snapshot_id"])

        # Insert + delete, then restore.
        snap = self.capture("insert-delete", text="Keep 3 notes selected (reselect if needed).")
        if snap:
            in_scope = [n for n in snap["notes"] if n["in_scope"]]
            ops = [{"op": "note.insert", "client_id": "m0-insert",
                    "note": {"pitch": 72, "start_tick": snap["ppq"] * 8, "duration_tick": snap["ppq"]}}]
            if in_scope:
                ops.append({"op": "note.delete", "note_id": in_scope[-1]["note_id"]})
            view = self.apply("insert-delete", snap["snapshot_id"], ops)
            applied = view["job"]["state"] == "applied"
            self.ev.mark("notes.patch.insert", applied, f"job ended {view['job']['state']}")
            self.ev.mark("notes.patch.delete", applied and bool(in_scope), f"job ended {view['job']['state']}")
            self.ev.step("insert_delete", applied, job=view["job"]["job_id"], state=view["job"]["state"])
            receipt = view.get("receipt")
            if applied and receipt and receipt["recovery_available"]:
                status, plan = self.api.post(f"/v1/ui/receipts/{receipt['receipt_id']}/restore-plan")
                if status < 400:
                    status, job = self.api.post(f"/v1/ui/plans/{plan['plan_id']}/apply")
                    self.op.instruct("restore", job["next_action"]["instruction"])
                    restored = self.wait(job["job_id"], TERMINAL | {"awaiting_verification"})
                    if restored["job"]["state"] == "awaiting_verification":
                        verify_job = restored["job"]["verify_job_id"]
                        self.op.instruct("restore-verify", self.job(verify_job)["job"]["next_action"]["instruction"]
                                         + f" Use the target label '{LABEL}'.")
                        self.wait(verify_job, TERMINAL)
                        restored = self.job(job["job_id"])
                    self.ev.facts["restore_verified"] = restored["job"]["state"] == "applied"
                    self.ev.step("restore", restored["job"]["state"] == "applied", state=restored["job"]["state"])

        # Stale-state refusal inside FL.
        snap = self.capture("stale")
        if snap and snap["in_scope_count"]:
            note = [n for n in snap["notes"] if n["in_scope"]][0]
            view = self.apply("stale", snap["snapshot_id"],
                              [{"op": "note.update", "note_id": note["note_id"], "set": {"pitch": max(0, note["pitch"] - 1)}}],
                              text="BEFORE running Apply, drag any selected note slightly in FL. Then:")
            refused = view["job"]["state"] == "failed" and (view["job"].get("error") or {}).get("code") == "STALE_SNAPSHOT"
            self.ev.facts["stale_state_refused_in_fl"] = refused
            self.ev.step("stale", refused, state=view["job"]["state"])

    def write_record(self) -> Path:
        facts = self.ev.facts
        build = facts.get("fl_build")
        installs = hostinfo.installed_fl()
        record = {
            "record_id": f"local-m0-{build}-{uuid.uuid4().hex[:8]}",
            "fl_build": build,
            "os": hostinfo.os_description(),
            "os_family": os_family(),
            "embedded_python": facts.get("embedded_python"),
            "adapter_version": __version__,
            "midi_api_version": None,
            "recorded_at": date.today().isoformat(),
            "evidence": self.ev.steps,
            "facts": facts,
            "installed_builds": [i.version for i in installs],
            "selection_semantics": facts.get("selection_semantics", "unverified"),
            "bridge_transport": "mailbox_file: " + (
                "verified inside FL" if self.ev.capabilities.get("bridge.mailbox", {}).get("status") == "verified"
                else "unverified"),
            "preview_cancel": "dialog cancel writes nothing" if facts.get("dialog_cancel_writes_nothing")
            else "unverified",
            "undo": {True: "one Ctrl+Z reverts the whole script", False: "one Ctrl+Z did not restore the capture",
                     None: "unverified"}[facts.get("undo_reverts_whole_script")],
            "identity_strength": "user_attested",
            "capabilities": self.ev.capabilities,
        }
        path = self.settings.home / "compatibility.local.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "format": "flslacker-compatibility/1", "records": []}
        if build:
            existing["records"].append(record)
            write_private_json(path, existing)
        else:
            path = self.settings.home / f"m0-unrecorded-{uuid.uuid4().hex[:8]}.json"
            write_private_json(path, record)
        self.op.say("\nM0 results:")
        for name, value in sorted(self.ev.capabilities.items()):
            self.op.say(f"  {name}: {value['status']} ({value['note']})")
        for key in ("selection_semantics", "undo", "preview_cancel"):
            self.op.say(f"  {key}: {record[key]}")
        self.op.say(f"Recorded in {path}. Restart `flslacker serve` (without --allow-unverified-host) to use it.")
        return path
