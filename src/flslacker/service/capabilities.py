"""Capability discovery from versioned compatibility records and live session state."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from flslacker.contracts.models import Capability

FL_NOTE_CAPABILITIES = (
    "bridge.mailbox",
    "notes.capture",
    "notes.verify",
    "notes.patch.update",
    "notes.patch.insert",
    "notes.patch.delete",
)
OP_CAPABILITY = {
    "note.update": "notes.patch.update",
    "note.insert": "notes.patch.insert",
    "note.delete": "notes.patch.delete",
}

UNSUPPORTED = {
    "notes.live_preview": "Scripts use an explicit execution path; FL preview callbacks are not used.",
    "notes.autonomous_apply": "No verified target binding exists; every Apply is invoked and confirmed in FL.",
    "markers.write": "Markers are captured read-only in the MVP.",
    "project.control": "Project-control writes are post-MVP and need per-operation host tests.",
    "playlist.edit": "Playlist arrangement editing is not offered.",
    "automation.write": "Automation-curve writing is not offered.",
    "project.save": "The companion never saves or exports projects; use File > Save As in FL.",
    "audio.analysis": "Audio analysis and rendering are outside the MVP.",
    "plugin.vfx_preset": "The VFX Script preset is post-MVP.",
    "plugin.native_shell": "The compiled plugin shell is post-MVP.",
    "multi_instance": "Only one explicitly paired FL instance is supported.",
}


def os_family() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "other"


@dataclass
class CompatibilityRecord:
    source: str
    data: dict[str, Any]

    @property
    def record_id(self) -> str:
        return self.data.get("record_id", "?")

    def status(self, capability: str) -> str:
        return self.data.get("capabilities", {}).get(capability, {}).get("status", "unverified")

    def note(self, capability: str) -> str:
        return self.data.get("capabilities", {}).get(capability, {}).get("reason", "")


def load_records(home: Path) -> list[CompatibilityRecord]:
    records: list[CompatibilityRecord] = []
    packaged = json.loads(resources.files("flslacker").joinpath("compatibility.json").read_text(encoding="utf-8"))
    records += [CompatibilityRecord("packaged", r) for r in packaged.get("records", [])]
    local = Path(home) / "compatibility.local.json"
    if local.exists():
        data = json.loads(local.read_text(encoding="utf-8"))
        records += [CompatibilityRecord("local", r) for r in data.get("records", [])]
    return records


def find_record(records: list[CompatibilityRecord], fl_build: str | None) -> CompatibilityRecord | None:
    if not fl_build:
        return None
    matches = [r for r in records if r.data.get("fl_build") == fl_build and r.data.get("os_family") == os_family()]
    # Prefer local (user-run M0) records, then the newest.
    matches.sort(key=lambda r: (r.source == "local", r.data.get("recorded_at", "")))
    return matches[-1] if matches else None


def compute(
    record: CompatibilityRecord | None,
    fl_build: str | None,
    fl_build_source: str,
    fl_contact: bool,
    allow_unverified: bool,
    midi_connected: bool,
    ollama_model: str | None,
) -> list[Capability]:
    caps: list[Capability] = []
    build_text = fl_build or "unknown"

    for name in FL_NOTE_CAPABILITIES:
        status_text = record.status(name) if record else "missing"
        verified = status_text == "verified"
        # Observed as impossible on this build: --allow-unverified-host does not override it.
        refuted = status_text == "unsupported"
        supported = verified or (allow_unverified and not refuted)
        limitations = ["requires explicit FL invocation", "coverage: exposed_score", "identity: user_attested"]
        reason = None
        if refuted:
            reason = f"Unsupported on FL build {build_text} ({fl_build_source}): {record.note(name)}"
        elif not verified:
            reason = (
                f"Not verified on FL build {build_text} ({fl_build_source}); record status: {status_text}. "
                "Run the M0 checklist in docs/COMPATIBILITY.md"
                + (" (running with --allow-unverified-host)." if allow_unverified else ".")
            )
            if allow_unverified:
                limitations.append("EXPERIMENTAL: unverified host behaviour")
        if not supported:
            status = "unavailable"
        elif name == "notes.capture" or name == "bridge.mailbox":
            status = "requires_user_action"
        else:
            status = "requires_user_action" if fl_contact else "not_connected"
        caps.append(
            Capability(
                name=name,
                supported=supported,
                status=status,
                execution="manual_piano_roll",
                scope="exposed_score",
                identity="user_attested",
                verified_build=fl_build if verified else None,
                limitations=limitations,
                reason=reason,
            )
        )

    patch = next(c for c in caps if c.name == "notes.patch.update")
    caps.append(patch.model_copy(update={"name": "notes.patch"}))
    caps.append(
        patch.model_copy(
            update={
                "name": "notes.restore",
                "limitations": patch.limitations
                + ["guarded inverse patch; not a global Undo; requires current state == recorded after-state"],
            }
        )
    )

    midi_status = record.status("project.metadata") if record else "missing"
    midi_supported = midi_status == "verified" or (allow_unverified and midi_status != "unsupported")
    caps.append(
        Capability(
            name="project.metadata",
            supported=midi_supported,
            status=("supported" if midi_connected else "not_connected") if midi_supported else "unavailable",
            execution="midi_controller_script",
            scope="tempo, transport, channel/pattern/mixer names",
            identity="none",
            verified_build=fl_build if midi_status == "verified" else None,
            limitations=["optional device_Slacker.py on a dedicated MIDI input", "read-only"],
            reason=None if midi_status == "verified"
            else f"Unsupported on FL build {build_text}: {record.note('project.metadata')}"
            if midi_status == "unsupported" else f"MIDI adapter not verified on FL build {build_text}.",
        )
    )

    for name, reason in UNSUPPORTED.items():
        caps.append(
            Capability(
                name=name,
                supported=False,
                status="unavailable",
                execution="none",
                scope="none",
                identity="none",
                reason=reason,
            )
        )

    for name in ("analysis.motifs", "analysis.key", "analysis.chords", "analysis.pitch_outliers"):
        caps.append(
            Capability(
                name=name,
                supported=True,
                status="supported",
                execution="companion",
                scope="snapshot",
                identity="none",
                limitations=["deterministic; findings are suggestions, not certainty"],
            )
        )
    caps.append(
        Capability(
            name="agent.mcp_stdio",
            supported=True,
            status="supported",
            execution="companion",
            scope="tools",
            identity="none",
            limitations=["`flslacker mcp` forwards to the running companion"],
        )
    )
    caps.append(
        Capability(
            name="agent.ollama",
            supported=True,
            status="supported" if ollama_model else "requires_user_action",
            execution="companion",
            scope="tools",
            identity="none",
            limitations=["loopback Ollama only", "tool-capable model required"],
            reason=None if ollama_model else "Choose an installed tool-capable model (ollama_model).",
        )
    )
    return caps
