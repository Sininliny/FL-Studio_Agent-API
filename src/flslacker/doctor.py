"""`flslacker doctor`: environment, bridge prerequisites, installed adapters and host evidence."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import httpx

from flslacker import __version__, fl_build, hostinfo, installer
from flslacker import reports as probe_reports
from flslacker.config import Settings, default_fl_user_dir, is_synchronized_path, load_agent_credentials
from flslacker.service import capabilities as capmod

REFERENCE_FEATURES = ("getTimelineSelection", "getDefaultNoteProperties", "getNextFreeGroupIndex", "clone()",
                      "addInputSurface", "execute()")


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    data: Any = None


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def embedded_self_test(python_exe: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="flslacker-doctor-") as temp:
        script = Path(temp) / "Slacker Capture.pyscript"
        script.write_text(fl_build.render(fl_build.script("Slacker Capture.pyscript")), encoding="utf-8")
        checker = Path(temp) / "embedded_check.py"
        checker.write_text(resources.files("flslacker").joinpath("embedded_check.py").read_text(encoding="utf-8"),
                           encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        out = subprocess.run([str(python_exe), "-I", str(checker), str(script)], capture_output=True, text=True,
                             timeout=60, env=env)
        if out.returncode != 0:
            return {"ok": False, "error": (out.stderr or out.stdout)[-400:]}
        return json.loads(out.stdout.strip().splitlines()[-1])


def summarize_probe(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    fl = (data.get("host") or {}).get("fl") or {}
    summary = {"file": path.name, "kind": data.get("kind"), "captured_at": data.get("captured_at"),
               "fl_version": fl.get("version"), "fl_version_error": fl.get("error"),
               "python": (data.get("host") or {}).get("python")}
    if data.get("kind") == "piano_roll_probe":
        summary.update({
            "noteCount": data.get("noteCount"),
            "selected_count": data.get("selected_count"),
            "PPQ": data.get("PPQ"),
            "module_name": data.get("module_name"),
            "modules_missing": sorted(k for k, v in (data.get("modules") or {}).items() if v is not True),
            "note_errors": data.get("note_errors"),
            "first_note_types": data.get("first_note_types"),
            "first_note_extras": data.get("first_note_extras"),
            "getNote_same_object": data.get("getNote_same_object"),
            "timeline_selection": data.get("timeline_selection"),
            "bridge_file_access": probe_reports.bridge_verdict(data),
            "report_saved": data.get("report_saved"),
            "report_save_error": data.get("report_save_error"),
            "report_save_refused": probe_reports.save_refused(data),
            "file_access": data.get("file_access"),
        })
    else:
        summary.update({k: data.get(k) for k in (
            "error", "e1_write_errors", "e1_after_add", "e2_positions", "e3_index", "e3_new_position",
            "e3_proxy_after", "e4_count_before_after", "e4_later_proxy_before", "e4_later_proxy_after",
            "e5_clone", "e6_out_of_range", "e6_unknown_attribute")})
    return summary


def run(settings: Settings, fl_user_dir: Path | None = None, self_test: bool = True) -> list[Check]:
    checks: list[Check] = []
    add = lambda *a, **k: checks.append(Check(*a, **k))  # noqa: E731

    add("companion", "ok", f"flslacker {__version__} on Python {platform.python_version()} ({sys.executable})",
        {"pydantic": _version("pydantic"), "mcp": _version("mcp"), "httpx": _version("httpx")})
    add("os", "info", hostinfo.os_description())

    home = settings.home
    if is_synchronized_path(home):
        add("home", "fail", f"{home} is inside a synchronized folder; set FLSLACKER_HOME to a local path.")
    else:
        add("home", "ok", f"{home} (local, per-user)")
    try:
        settings.ensure_dirs()
        probe = home / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        add("home.writable", "fail", str(exc))

    creds = load_agent_credentials(settings)
    if creds is None:
        add("credentials", "warn", "No credentials yet; `flslacker serve` creates them.")
        add("companion.running", "info", "unknown (no credentials)")
    else:
        try:
            response = httpx.get(f"http://127.0.0.1:{creds['port']}/v1/health", timeout=2, trust_env=False,
                                 headers={"Authorization": f"Bearer {creds['agent_token']}"})
            body = response.json()
            add("companion.running", "ok" if body.get("ok") else "warn",
                f"port {creds['port']}: {body.get('version')} session_active={body.get('session_active')}")
        except (httpx.HTTPError, ValueError):
            add("companion.running", "info", f"not running on port {creds['port']}")

    installs = hostinfo.installed_fl()
    if not installs:
        add("fl.install", "warn", "No FL Studio installation found in the default location.")
    detected_build = None
    for install in installs:
        detected_build = detected_build or install.version
        add("fl.install", "ok", f"{install.path} build {install.version}, embedded Python {install.embedded_python}")
        if install.piano_roll_reference:
            text = install.piano_roll_reference.read_text(encoding="utf-8", errors="replace")
            found = {f: f in text for f in REFERENCE_FEATURES}
            add("fl.reference", "ok", "installed Piano Roll reference lists: "
                + ", ".join(f for f, v in found.items() if v), found)
        else:
            add("fl.reference", "warn", "installed Piano Roll reference not found")
        exe = install.path / "Shared" / "Python" / "python.exe"
        if self_test and exe.exists():
            try:
                result = embedded_self_test(exe)
                add("bridge.embedded_self_test", "ok" if result.get("ok") else "fail",
                    "FL's interpreter (outside FL) can sign, write atomically and claim exclusively"
                    if result.get("ok") else f"failed: {result}", result)
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                add("bridge.embedded_self_test", "fail", f"{type(exc).__name__}: {exc}")

    user_dir = fl_user_dir or (Path(settings.fl_user_dir) if settings.fl_user_dir else default_fl_user_dir())
    if user_dir is None:
        add("fl.user_dir", "warn", "FL user data folder not found; pass --fl-user-dir.")
    else:
        add("fl.user_dir", "ok", str(user_dir))
        state = installer.status(user_dir)
        optional = {s.filename for s in fl_build.SCRIPTS if s.dev_only or s.kind == "midi"}
        bad = {k: v for k, v in state.items()
               if not v.startswith("installed, current") and not (k in optional and v == "not installed")}
        required = {"Slacker Capture.pyscript", "Slacker Apply.pyscript"}
        missing = [k for k in required if state.get(k, "").startswith("not installed")]
        add("fl.adapters", "fail" if missing else ("ok" if not bad else "warn"),
            "; ".join(f"{k}: {v}" for k, v in state.items()), state)

    reports = sorted(settings.probe_dir.glob("*.json"), key=lambda p: p.stat().st_mtime) if settings.probe_dir.is_dir() else []
    if not reports:
        add("fl.probe", "warn", "No probe report yet. In FL run Piano Roll > Tools > Scripting > Slacker > Slacker Probe; "
            "if FL cannot save the report, import the printed block with `flslacker import-report`.")
    access = None
    for path in reports[-2:]:
        try:
            summary = summarize_probe(path)
            add("fl.probe", "info", f"{summary['kind']} from FL {summary['fl_version'] or 'version unknown'} "
                f"at {summary['captured_at']}", summary)
            if summary.get("bridge_file_access"):
                access = (path.name, summary)
        except (ValueError, OSError) as exc:
            add("fl.probe", "warn", f"{path.name}: unreadable ({exc})")
    if access:
        name, summary = access
        verdict = summary["bridge_file_access"]
        saved = summary.get("report_saved")
        where = "; ".join(
            f"{place}: " + (", ".join(f"{op} {result[op]}" for op in ("list", "read") if op in result)
                            if isinstance(result, dict) and result.get("exists") else "folder not found")
            for place, result in (summary.get("file_access") or {}).items() if isinstance(result, dict))
        if verdict == "blocked":
            refused = "FL refused the report save" if summary.get("report_save_refused") else "FL refused file reads"
            status, note = "fail", f"the file bridge cannot work on this build ({refused})"
        elif verdict == "reads_ok" and saved:
            status, note = "ok", "reads and one write succeeded"
        else:
            status, note = "warn", "inconclusive"
        add("fl.bridge_access", status,
            f"{name}: {note} (verdict {verdict}, report_saved={saved}; {where})", summary.get("file_access"))

    records = capmod.load_records(home)
    record = capmod.find_record(records, detected_build)
    if record is None:
        add("compatibility", "warn", f"No compatibility record for FL build {detected_build}.")
    else:
        statuses = record.data.get("capabilities", {})
        verified = [k for k, v in statuses.items() if v.get("status") == "verified"]
        unsupported = [k for k, v in statuses.items() if v.get("status") == "unsupported"]
        detail = f"{record.source}:{record.record_id}: verified {len(verified)}/{len(statuses)} capabilities"
        if unsupported:
            detail += f"; unsupported on this build: {', '.join(unsupported)}. {record.note('bridge.mailbox')}"
        elif not verified:
            detail += " (run the M0 checklist; writes stay disabled unless --allow-unverified-host)"
        status = "ok" if len(verified) == len(statuses) else "fail" if "bridge.mailbox" in unsupported else "warn"
        add("compatibility", status, detail, {k: v.get("status") for k, v in statuses.items()})

    if settings.ollama_model:
        from flslacker.contracts.errors import FlsError
        from flslacker.providers.ollama import OllamaProvider

        try:
            models = OllamaProvider(settings.ollama_url, settings.ollama_model, local_only=settings.local_only).list_models()
            present = settings.ollama_model in models or f"{settings.ollama_model}:latest" in models
            add("ollama", "ok" if present else "warn",
                f"{settings.ollama_url}: model {settings.ollama_model} {'installed' if present else 'NOT installed'}")
        except FlsError as exc:
            add("ollama", "warn", exc.message)
    else:
        add("ollama", "info", "No model configured (optional). Set ollama_model in config.json.")
    return checks


def write_report(settings: Settings, checks: list[Check]) -> Path:
    path = settings.home / "doctor-report.json"
    path.write_text(json.dumps([asdict(c) for c in checks], indent=2, default=str), encoding="utf-8")
    return path
