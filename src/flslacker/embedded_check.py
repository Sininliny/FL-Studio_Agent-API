"""Bridge self-test for FL's embedded interpreter (stdlib only).

Run as ``<FL>/Shared/Python/python.exe embedded_check.py "<Slacker Capture.pyscript>"``.
It loads the generated script without running it and exercises signing, atomic writes
and exclusive claims in a temporary folder. This runs *outside* FL: it shows the
interpreter can do the work, not that FL allows it at runtime.
"""

import importlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import types
import uuid

MODULES = ("json", "hmac", "hashlib", "os", "uuid", "time", "calendar", "ctypes")


def main(script_path):
    report = {"ok": False, "python": sys.version.split()[0], "modules": {}}
    for name in MODULES:
        try:
            importlib.import_module(name)
            report["modules"][name] = True
        except Exception as exc:
            report["modules"][name] = type(exc).__name__
    sys.modules["flpianoroll"] = types.ModuleType("flpianoroll")
    namespace = {"_SLACKER_NO_AUTORUN": True}
    with open(script_path, encoding="utf-8") as handle:
        exec(compile(handle.read(), script_path, "exec"), namespace)
    home = tempfile.mkdtemp(prefix="flslacker-check-")
    try:
        session_id = str(uuid.uuid4())
        secret = secrets.token_hex(32)
        session_dir = namespace["session_path"](home, session_id)
        for folder in namespace["MAILBOX_FOLDERS"]:
            os.makedirs(os.path.join(session_dir, folder))
        envelope = namespace["make_envelope"](secret, "capture_request", session_id, 1, {"probe": True}, 60)
        folder = os.path.join(session_dir, "requests")
        namespace["atomic_write_text"](folder, envelope["request_id"] + ".json", namespace["canonical_dumps"](envelope))
        loaded = namespace["load_json_file"](os.path.join(folder, envelope["request_id"] + ".json"))
        report["signature"] = namespace["check_envelope"](secret, loaded, session_id, ("capture_request",)) is None
        tampered = dict(loaded)
        tampered["body"] = {"probe": False}
        report["tamper_rejected"] = namespace["check_envelope"](secret, tampered, session_id, ("capture_request",)) is not None
        first = namespace["try_claim"](session_dir, envelope["request_id"], "a")
        second = namespace["try_claim"](session_dir, envelope["request_id"], "b")
        report["exclusive_claim"] = first and not second
        report["ok"] = report["signature"] and report["tamper_rejected"] and report["exclusive_claim"]
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print(json.dumps(report))


if __name__ == "__main__":
    main(sys.argv[1])
