"""Round-trip the generated Piano Roll scripts against the fake host.

Stdlib only, so it also runs under FL Studio's embedded interpreter:

    "<FL>/Shared/Python/python.exe" tests/embedded_smoke.py

Prints ``SMOKE OK`` on success. Used by tests/test_fl_scripts.py.
"""

import importlib.util
import json
import os
import secrets
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "fl_scripts")


def load_fake():
    path = os.path.join(ROOT, "src", "flslacker", "adapters", "fake_flpianoroll.py")
    spec = importlib.util.spec_from_file_location("fake_flpianoroll", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(name):
    with open(os.path.join(SCRIPTS, name), encoding="utf-8") as handle:
        return handle.read()


def main():
    fake = load_fake()
    home = tempfile.mkdtemp(prefix="flslacker-smoke-")
    os.environ["FLSLACKER_HOME"] = home
    fl_scripts = os.path.join(home, "fl-user", "Slacker")  # never the real FL user folder
    os.makedirs(fl_scripts)
    os.environ["FLSLACKER_FL_SCRIPT_DIR"] = fl_scripts

    # Borrow the protocol helpers from the generated script itself.
    helpers = fake.make_module()
    capture_source = read("Slacker Capture.pyscript")
    sys.modules["flpianoroll"] = helpers
    ns = {"_SLACKER_NO_AUTORUN": True}
    exec(compile(capture_source, "capture", "exec"), ns)

    session_id = str(uuid.uuid4())
    secret = secrets.token_hex(32)
    session_dir = ns["session_path"](home, session_id)
    for folder in ns["MAILBOX_FOLDERS"]:
        os.makedirs(os.path.join(session_dir, folder))
    os.makedirs(os.path.join(home, "bridge"))
    with open(os.path.join(home, "bridge", "pairing.json"), "w") as handle:
        json.dump({"protocol": ns["PROTOCOL"], "session_id": session_id, "secret": secret}, handle)

    notes = [
        {"number": 60, "time": 0, "length": 48, "selected": True},
        {"number": 62, "time": 48, "length": 48, "selected": True, "pan": 0.25},
        {"number": 65, "time": 96, "length": 48, "selected": False},
        {"number": 60, "time": 0, "length": 48, "selected": True},
    ]
    host = fake.FakeHost()
    module = fake.make_module(host, notes)

    request = ns["make_envelope"](
        secret, "capture_request", session_id, 1,
        {"job_id": str(uuid.uuid4()), "scope": "selected", "purpose": "capture", "target_label": None}, 600,
    )
    ns["atomic_write_text"](os.path.join(session_dir, "requests"), request["request_id"] + ".json",
                            ns["canonical_dumps"](request))

    def answer_capture(dialog):
        index = fake.pick_option(dialog, "Capture", lambda o: o.startswith("Request"))
        return {"Capture": index, "Target label": "Pattern 1 / Piano", "This Piano Roll is the named target": True}

    host.dialog_responder = answer_capture
    fake.run_script(capture_source, module)
    snaps = os.listdir(os.path.join(session_dir, "snapshots"))
    assert len(snaps) == 1, (snaps, host.messages)
    envelope = ns["load_json_file"](os.path.join(session_dir, "snapshots", snaps[0]))
    assert ns["check_envelope"](secret, envelope, session_id, ("snapshot",)) is None
    snapshot = envelope["body"]["snapshot"]
    assert len(snapshot["notes"]) == 4 and snapshot["scope"] == "selected"
    assert envelope["in_reply_to"] == request["request_id"]

    # Apply: pitch update on the duplicate at ordinal 3, delete ordinal 2, insert one.
    records = snapshot["notes"]
    template = dict(records[0])
    template.update({"number": 67, "time": 144, "selected": True})
    operations = [
        {"op": "update", "ordinal": 3, "fingerprint": ns["note_fingerprint"](records[3]), "set": {"number": 64, "time": 192}},
        {"op": "delete", "ordinal": 2, "fingerprint": ns["note_fingerprint"](records[2])},
        {"op": "insert", "note": template},
    ]
    job_id = str(uuid.uuid4())
    apply_request = ns["make_envelope"](
        secret, "apply_request", session_id, 2,
        {"job_id": job_id, "plan_id": str(uuid.uuid4()), "plan_hash": "0" * 64, "snapshot_id": str(uuid.uuid4()),
         "target_label": "Pattern 1 / Piano", "scope": "selected", "base_state_hash": snapshot["state_hash"],
         "base_note_count": 4, "ppq": 96, "operations": operations, "summary": "1 update, 1 delete, 1 insert"}, 600,
    )
    ns["atomic_write_text"](os.path.join(session_dir, "requests"), apply_request["request_id"] + ".json",
                            ns["canonical_dumps"](apply_request))

    def answer_apply(dialog):
        return {"Job": 1, "Action": 0, "I confirm this Piano Roll is the job's target": True}

    host.dialog_responder = answer_apply
    fake.run_script(read("Slacker Apply.pyscript"), module)
    responses = os.listdir(os.path.join(session_dir, "responses"))
    assert len(responses) == 1, (responses, host.messages)
    response = ns["load_json_file"](os.path.join(session_dir, "responses", responses[0]))
    body = response["body"]
    assert body["status"] == "applied_provisional", body
    assert body["applied_operations"] == 3
    after = module.score.dump()
    assert sorted(n["number"] for n in after) == [60, 62, 64, 67], after
    assert [n["number"] for n in after if n["time"] == 192] == [64]

    # Replaying the same job must not apply twice: it is claimed now.
    fake.run_script(read("Slacker Apply.pyscript"), module)
    assert "no pending apply jobs" in host.messages[-1].lower(), host.messages[-1]

    # Probe writes a report without pairing requirements (the companion creates the folder).
    os.makedirs(os.path.join(home, "probe"))
    host.dialog_responder = None
    fake.run_script(read("Slacker Probe.pyscript"), module)
    reports = os.listdir(os.path.join(home, "probe"))
    assert len(reports) == 1, (reports, host.messages)
    report = ns["load_json_file"](os.path.join(home, "probe", reports[0]))
    assert report["bridge_file_access"] == "reads_ok", report["file_access"]
    assert report["report_saved"] is True and report["file_access"]["writes_tested"] is False
    assert sorted(os.listdir(fl_scripts)) == [], "probe left scratch files behind"

    # A host that refuses file access in the FL Slacker folder, as FL 26.1.6 does.
    refusing = fake.FakeHost(refused_paths=[home])
    blocked = fake.make_module(refusing, notes)
    before = blocked.score.dump()
    fresh = ns["make_envelope"](
        secret, "capture_request", session_id, 3,
        {"job_id": str(uuid.uuid4()), "scope": "selected", "purpose": "capture", "target_label": None}, 600,
    )
    ns["atomic_write_text"](os.path.join(session_dir, "requests"), fresh["request_id"] + ".json",
                            ns["canonical_dumps"](fresh))
    for name in ("Slacker Capture.pyscript", "Slacker Apply.pyscript"):
        refusing.dialog_responder = lambda dialog: {"Capture": 1, "Job": 1, "Action": 0}
        fake.run_script(read(name), blocked)
        assert refusing.dialogs == [], name  # refused before any dialog
        assert refusing.messages[-1].count("UNSUPPORTED_CAPABILITY (bridge.mailbox)") == 1, refusing.messages[-1]
        assert "SystemError" in refusing.messages[-1]
    assert blocked.score.dump() == before
    assert os.listdir(os.path.join(session_dir, "snapshots")) == snaps
    assert ns["read_claim"](session_dir, fresh["request_id"]) is None

    fake.run_script(read("Slacker Probe.pyscript"), blocked)
    assert "File bridge: BLOCKED" in refusing.messages[-1], refusing.messages[-1]
    assert "NOT saved" in refusing.messages[-1] and "import-report" in refusing.messages[-1]
    assert len(os.listdir(os.path.join(home, "probe"))) == 1
    print("SMOKE OK", sys.version.split()[0], time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    main()
