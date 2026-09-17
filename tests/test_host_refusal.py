"""FL builds that refuse file access inside Piano Roll scripts (observed on FL 26.1.6.5639)."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from flslacker import doctor, reports
from flslacker.adapters.fake import FakeFl, FakeHost
from flslacker.cli import main
from flslacker.config import Settings
from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.service import capabilities as capmod

from conftest import AGENT, make_service


def refusing_fl(home: Path, tmp_path: Path) -> FakeFl:
    """File access as Slacker Probe observed it inside FL 26.1.6.5639."""
    scripts = tmp_path / "fl-user" / "Settings" / "Piano roll scripts" / "Slacker"
    fl = FakeFl(home, host=FakeHost(refused_paths=[home], read_only_paths=[scripts]), fl_script_dir=scripts)
    scripts.mkdir(parents=True)
    (scripts / "Slacker Probe.pyscript").write_text("# installed script", encoding="utf-8")
    return fl


def printed_probe(fl: FakeFl, capsys) -> tuple[str, str]:
    capsys.readouterr()
    message = fl.probe()
    return message, capsys.readouterr().out


def test_refusal_is_reported_before_any_dialog_and_changes_nothing(home, tmp_path, service):
    fl = refusing_fl(home, tmp_path)
    job = service.capture_score(AGENT, service.session_id, "selected", "Demo pattern")
    before = fl.notes()

    message = fl.capture(service._request_id(job.job_id))
    assert message.startswith("Slacker Capture\n\nUNSUPPORTED_CAPABILITY (bridge.mailbox)"), message
    assert "SystemError" in message and "Traceback" not in message
    message = fl.apply()
    assert message.startswith("Slacker Apply\n\nUNSUPPORTED_CAPABILITY (bridge.mailbox)"), message

    assert fl.host.refused and fl.host.dialogs == []
    assert fl.notes() == before
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "awaiting_fl_action"
    assert list((home / "mailbox").rglob("*.claim")) == []


def test_probe_prints_its_report_and_import_restores_it(home, tmp_path, service, capsys):
    fl = refusing_fl(home, tmp_path)
    message, printed = printed_probe(fl, capsys)
    assert "File bridge: BLOCKED" in message and "NOT saved" in message and "import-report" in message
    assert list((home / "probe").glob("*.json")) == []  # the one write attempt (the save) was refused

    # Copying from FL's output window may add indentation, trailing spaces and surrounding text.
    copied = "unrelated line\n" + "\n".join("  " + line + "  " for line in printed.splitlines()) + "\nmore\n"
    [report] = reports.parse(copied)
    access = report["file_access"]
    assert report["bridge_file_access"] == "blocked"
    assert report["report_saved"] is False and "SystemError" in report["report_save_error"]
    # The probe is read-only: only list and read per folder, no writes anywhere but the save above.
    assert access["writes_tested"] is False
    assert {k: v for k, v in access["companion_home"].items() if k != "path"} == {
        "exists": True, "list": "ok", "read": "SystemError"}
    assert access["fl_user_scripts"]["read"] == "ok"  # FL allows reads in its own script folder
    assert set(access["fl_user_scripts"]) == {"path", "exists", "list", "read"}  # never write-tested
    assert access["fl_user_data"]["list"] == "ok"
    assert "  FL Slacker folder: list ok, read SystemError\n" in message
    assert "  FL script folder: list ok, read ok\n" in message

    path = reports.save(home / "probe", report)
    assert path.name.startswith("probe-") and json.loads(path.read_text(encoding="utf-8")) == report
    checks = doctor.run(Settings(home=home), fl_user_dir=tmp_path / "no-fl", self_test=False)
    bridge = next(c for c in checks if c.name == "fl.bridge_access")
    assert bridge.status == "fail" and "cannot work" in bridge.detail
    assert "companion_home: list ok, read SystemError" in bridge.detail


def test_import_rejects_incomplete_or_altered_copies(home, tmp_path, service, capsys):
    _, printed = printed_probe(refusing_fl(home, tmp_path), capsys)
    lines = printed.splitlines()
    assert len(lines) > 4
    with pytest.raises(reports.ReportError, match="incomplete"):
        reports.parse("\n".join(lines[:2] + lines[3:]))
    altered = list(lines)
    altered[1] = ("A" if altered[1][0] != "A" else "B") + altered[1][1:]
    with pytest.raises(reports.ReportError, match="checksum"):
        reports.parse("\n".join(altered))
    with pytest.raises(reports.ReportError, match="END"):
        reports.parse("\n".join(lines[:-1]))
    with pytest.raises(reports.ReportError):
        reports.parse(json.dumps({"kind": "something_else"}))
    [report] = reports.parse("\n".join(lines))
    assert reports.parse(json.dumps(report)) == [report]


def test_refused_report_save_blocks_the_bridge_even_when_not_paired(home, tmp_path, capsys, monkeypatch):
    # As reported from FL: no pairing file yet, reads allowed, the report save refused, no ctypes.
    scripts = tmp_path / "fl-user" / "Settings" / "Piano roll scripts" / "Slacker"
    scripts.mkdir(parents=True)
    (scripts / "Slacker Probe.pyscript").write_text("# installed script", encoding="utf-8")
    (home / "probe").mkdir(parents=True)
    fl = FakeFl(home, host=FakeHost(read_only_paths=[home, scripts]), fl_script_dir=scripts)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "ctypes", None)
        message, printed = printed_probe(fl, capsys)
    if os.name == "nt":
        assert "FL version unknown (ctypes unavailable: ModuleNotFoundError)" in message
    assert "File bridge: BLOCKED - FL refuses file writes in the FL Slacker folder" in message
    assert "FL Slacker folder: list ok, read skipped: no file" in message
    [report] = reports.parse(printed)
    assert report["bridge_file_access"] == "blocked" and report["report_save_refused"] is True

    reports.save(home / "probe", report)
    checks = doctor.run(Settings(home=home), fl_user_dir=tmp_path / "no-fl", self_test=False)
    bridge = next(c for c in checks if c.name == "fl.bridge_access")
    assert bridge.status == "fail" and "FL refused the report save" in bridge.detail
    assert any(c.name == "fl.probe" and "FL version unknown" in c.detail for c in checks)


def test_reports_from_older_scripts_count_a_refused_save_as_blocked(home, tmp_path):
    # Printed by the 0.1.3 probe: read-only verdict, refusal recorded only as text.
    old = {"kind": "piano_roll_probe", "bridge_file_access": "unknown: the companion is not paired",
           "report_saved": False, "report_save_error": "FL refused the file write: SystemError",
           "host": {"fl": {"executable": None, "version": None, "error": "ImportError"}}}
    assert reports.bridge_verdict(old) == "blocked"
    missing_folder = dict(old, report_save_error="the companion has not created x yet")
    assert reports.bridge_verdict(missing_folder) == "unknown: the companion is not paired"
    reports.save(home / "probe", old)
    checks = doctor.run(Settings(home=home), fl_user_dir=tmp_path / "no-fl", self_test=False)
    assert next(c for c in checks if c.name == "fl.bridge_access").status == "fail"


def test_cli_import_report_from_file_stdin_and_missing_file(home, tmp_path, service, capsys, monkeypatch):
    _, printed = printed_probe(refusing_fl(home, tmp_path), capsys)
    missing = tmp_path / "probe-copy.txt"
    assert main(["--home", str(home), "import-report", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err and "VIEW > Script output" in err and "Traceback" not in err

    missing.write_text(printed, encoding="utf-8")
    assert main(["--home", str(home), "import-report", str(missing)]) == 0
    assert "Imported piano_roll_probe" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO(printed))
    assert main(["--home", str(home), "import-report"]) == 0
    assert "Imported piano_roll_probe" in capsys.readouterr().out
    assert len(list((home / "probe").glob("probe-*-imported-*.json"))) == 2


def test_probe_on_a_permissive_host_saves_and_says_so(home, service, fl, tmp_path):
    message = fl.probe()
    assert "reads OK; writes OK" in message
    assert "Write test (save report to the FL Slacker folder): OK" in message
    assert "Report saved: probe-" in message


def test_unsupported_record_disables_the_bridge_even_in_m0_mode(home, clock):
    home.mkdir(parents=True)
    record = {
        "record_id": "local-test", "fl_build": "99.0.0.1", "os_family": capmod.os_family(), "recorded_at": "2026-09-17",
        "capabilities": {
            "bridge.mailbox": {"status": "unsupported", "reason": "FL refuses file access."},
            "notes.capture": {"status": "unsupported", "reason": "Needs the mailbox."},
        },
    }
    (home / "compatibility.local.json").write_text(json.dumps({"records": [record]}), encoding="utf-8")
    service = make_service(home, clock)  # --allow-unverified-host
    try:
        service.installed_fl_build = "99.0.0.1"
        caps = {c.name: c for c in service.capability_list()}
        assert not caps["bridge.mailbox"].supported and caps["bridge.mailbox"].status == "unavailable"
        assert caps["bridge.mailbox"].reason.startswith("Unsupported on FL build 99.0.0.1")
        assert "FL refuses file access." in caps["bridge.mailbox"].reason
        assert caps["notes.patch.update"].supported  # merely unverified: M0 mode still allows it
        with pytest.raises(FlsError) as error:
            service.capture_score(AGENT, service.session_id, "selected", None)
        assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
        assert "Unsupported on FL build 99.0.0.1" in error.value.message and "Needs the mailbox." in error.value.message
    finally:
        service.stop()
        service.db.close()


def test_packaged_record_marks_the_mailbox_unsupported_on_fl_26_1_6(tmp_path):
    [record] = [r for r in capmod.load_records(tmp_path) if r.data["fl_build"] == "26.1.6.5639"]
    for name in capmod.FL_NOTE_CAPABILITIES:
        assert record.status(name) == "unsupported", name
        assert record.note(name), name
    assert record.status("project.metadata") == "unverified"
