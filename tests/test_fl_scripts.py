"""The FL-side scripts: generated copies are current, run on FL's interpreter, and the MIDI adapter works."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from flslacker import fl_build, hostinfo
from flslacker.contracts import wire

from conftest import AGENT

ROOT = Path(__file__).resolve().parents[1]


def test_committed_scripts_match_sources():
    for name, text in fl_build.render_all().items():
        committed = (ROOT / "fl_scripts" / name).read_text(encoding="utf-8")
        assert committed == text, f"{name} is stale: run `flslacker build-fl-scripts`"


def test_scripts_are_self_contained():
    for name, text in fl_build.render_all().items():
        assert not re.search(r"^\s*(from|import)\s+(flslacker|pydantic|httpx|mcp)", text, re.M), name
        assert "from __future__" not in text
    midi = fl_build.render(fl_build.script("device_Slacker.py"))
    assert midi.startswith("# name=FL Slacker (metadata)\n")


def test_smoke_on_current_interpreter():
    out = subprocess.run([sys.executable, str(ROOT / "tests" / "embedded_smoke.py")], capture_output=True, text=True,
                         timeout=120)
    assert "SMOKE OK" in out.stdout, out.stderr


@pytest.mark.skipif(not any(i.embedded_python for i in hostinfo.installed_fl()), reason="FL Studio not installed")
def test_smoke_on_fl_embedded_interpreter():
    install = next(i for i in hostinfo.installed_fl() if i.embedded_python)
    exe = install.path / "Shared" / "Python" / "python.exe"
    out = subprocess.run([str(exe), "-I", str(ROOT / "tests" / "embedded_smoke.py")], capture_output=True, text=True,
                         timeout=120)
    assert f"SMOKE OK {install.embedded_python}" in out.stdout, out.stderr


def test_probe_report(home, service, fl):
    message = fl.probe()
    assert "Slacker Probe" in message
    report = json.loads(next((home / "probe").glob("probe-*.json")).read_text(encoding="utf-8"))
    assert report["noteCount"] == 13 and report["selected_count"] == 12
    assert report["modules"]["json"] is True
    assert report["host"]["fl"]["version"] is None  # not running inside FL
    assert set(report["note_attributes"]) >= {"number", "time", "length", "clone"}


def _fake_fl_modules(api_version=38):
    calls = []

    def module(name, **functions):
        mod = types.ModuleType(name)
        for key, value in functions.items():
            setattr(mod, key, value)
        return mod

    def record(name, value):
        def fn(*args):
            calls.append((name, args))
            return value
        return fn

    return calls, {
        "general": module("general", getVersion=record("getVersion", api_version), safeToEdit=record("safeToEdit", 1),
                          getUndoHistoryCount=record("getUndoHistoryCount", 3)),
        "mixer": module("mixer", getCurrentTempo=record("getCurrentTempo", 128.0), trackCount=record("trackCount", 3),
                        getTrackName=record("getTrackName", "Insert")),
        "channels": module("channels", channelCount=record("channelCount", 2), getChannelName=record("getChannelName", "Kick"),
                           selectedChannel=record("selectedChannel", 0)),
        "patterns": module("patterns", patternCount=record("patternCount", 1), getPatternName=record("getPatternName", "Pattern 1"),
                           patternNumber=record("patternNumber", 1)),
        "transport": module("transport", isPlaying=record("isPlaying", 0), isRecording=record("isRecording", 0)),
        "ui": module("ui", getProgTitle=record("getProgTitle", "demo.flp")),
    }


def test_midi_adapter_reports_status_and_project_load(home, service, monkeypatch):
    calls, modules = _fake_fl_modules()
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("FLSLACKER_HOME", str(home))
    namespace: dict = {}
    exec(compile(fl_build.render(fl_build.script("device_Slacker.py")), "device_Slacker.py", "exec"), namespace)
    namespace["OnInit"]()
    service.pump()
    summary = service.get_project_summary(AGENT, service.session_id)
    assert summary.fields["tempo_bpm"].value == 128.0
    assert summary.fields["channel_names"].value["names"] == ["Kick", "Kick"]
    assert summary.fields["pattern_names"].value["index_origin"] == 1
    namespace["OnIdle"]()  # throttled: no second message within the interval
    assert len(list((service.mailbox.session_dir(service.session_id) / "responses").glob("*.json"))) == 0
    host_calls = {name for name, _ in calls}
    assert host_calls <= {"getVersion", "safeToEdit", "getUndoHistoryCount", "getCurrentTempo", "trackCount",
                          "getTrackName", "channelCount", "getChannelName", "selectedChannel", "patternCount",
                          "getPatternName", "patternNumber", "isPlaying", "isRecording", "getProgTitle"}
    old = service.session_id
    namespace["OnProjectLoad"](100)
    service.pump()
    assert service.session_id != old
    assert wire.read_pairing(str(home))["session_id"] == service.session_id


def test_midi_adapter_respects_api_version(home, service, monkeypatch):
    calls, modules = _fake_fl_modules(api_version=20)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("FLSLACKER_HOME", str(home))
    namespace: dict = {}
    exec(compile(fl_build.render(fl_build.script("device_Slacker.py")), "device_Slacker.py", "exec"), namespace)
    namespace["OnInit"]()
    assert not {"getCurrentTempo", "safeToEdit"} & {name for name, _ in calls}
