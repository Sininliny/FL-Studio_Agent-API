"""The M0 wizard end to end, with the fake FL standing in for the user's hands."""

from __future__ import annotations

import json
import re

from flslacker.adapters.fake import FakeFl
from flslacker.config import Credentials
from flslacker.m0 import Api, Wizard
from flslacker.service import capabilities as capmod
from flslacker.transports.http import App, Server

from conftest import make_service

SIX_NOTES = [{"number": 60 + i * 2, "time": i * 96, "length": 96, "selected": i < 3} for i in range(6)]
SIX_NOTES[4].update(velocity=0.4, pan=0.2)


class FakeHands:
    """Performs each wizard instruction on the fake FL, as a user would."""

    def __init__(self, home, service, pretend_build=True):
        self.fl = FakeFl(home, notes=[dict(n) for n in SIX_NOTES], label="M0 test")
        if pretend_build:  # let the probe report the interpreter's version as the "FL build"
            for name, source in self.fl.sources.items():
                self.fl.sources[name] = source.replace(
                    'if not os.path.basename(exe).lower().startswith("fl"):', "if False:")
        self.service = service
        self.log = []

    def say(self, text):
        self.log.append(text)

    def ask_int(self, step, text):
        return 6 if "How many notes" in text else 3

    def ask_yes(self, step, text):
        return True

    def instruct(self, step, text):
        self.log.append(f"{step}: {text}")
        if "Slacker Probe" in text:
            self.fl.probe()
        elif "press Cancel" in text:
            request = re.search(r"'Request ([0-9a-f]{8})'", text).group(1)
            self.fl.capture(request, cancel=True)
        elif "Slacker Capture" in text:
            request = re.search(r"'(?:Request|Verify) ([0-9a-f]{8})'", text).group(1)
            self.fl.capture(request, label="M0 test")
        elif "Slacker Apply" in text:
            job = re.search(r"'Job ([0-9a-f]{8})'", text).group(1)
            if "BEFORE running Apply" in text:
                self.fl.score._notes[0].time = 5
            self.fl.apply(job)
        self.service.pump()


def run_wizard(home, clock):
    service = make_service(home, clock, port=0)
    creds = Credentials.load_or_create(service.settings)
    server = Server(App(service, creds, service.settings))
    server.start()
    creds.port = server.port
    creds.save(service.settings)
    hands = FakeHands(home, service)
    try:
        api = Api(service.settings)
        wizard = Wizard(service.settings, hands, api=api, poll=0.01, timeout=10, sleep=lambda s: service.pump())
        path = wizard.run()
    finally:
        server.stop()
        service.stop()
        service.db.close()
    return path, wizard, hands


def test_wizard_records_observed_evidence(home, clock):
    path, wizard, hands = run_wizard(home, clock)
    assert path.name == "compatibility.local.json", hands.log
    record = json.loads(path.read_text(encoding="utf-8"))["records"][-1]
    caps = record["capabilities"]
    assert {k: v["status"] for k, v in caps.items()} == {
        "bridge.mailbox": "verified",
        "notes.capture": "verified",
        "notes.patch.update": "verified",
        "notes.verify": "verified",
        "notes.patch.insert": "verified",
        "notes.patch.delete": "verified",
    }, [s for s in record["evidence"] if not s["ok"]]
    assert record["selection_semantics"] == "all notes exposed; selection reported per note"
    assert record["preview_cancel"] == "dialog cancel writes nothing"
    assert record["undo"] == "one Ctrl+Z did not restore the capture"  # the fake has no undo
    assert record["facts"]["restore_verified"] is True
    assert record["facts"]["stale_state_refused_in_fl"] is True
    assert "project.metadata" not in caps

    # The local record now makes writes available without --allow-unverified-host for that build.
    records = capmod.load_records(home)
    found = capmod.find_record(records, record["fl_build"])
    assert found.source == "local"
    computed = {c.name: c for c in capmod.compute(found, record["fl_build"], "test", True, False, False, None)}
    assert computed["notes.patch.update"].supported and computed["notes.patch.update"].verified_build
    assert not computed["project.metadata"].supported


def test_wizard_without_fl_build_does_not_record(home, clock):
    service = make_service(home, clock, port=0)
    creds = Credentials.load_or_create(service.settings)
    server = Server(App(service, creds, service.settings))
    server.start()
    creds.port = server.port
    creds.save(service.settings)
    hands = FakeHands(home, service, pretend_build=False)
    try:
        api = Api(service.settings)
        wizard = Wizard(service.settings, hands, api=api, poll=0.01, timeout=10, sleep=lambda s: service.pump())
        path = wizard.run()
    finally:
        server.stop()
        service.stop()
        service.db.close()
    assert path.name.startswith("m0-unrecorded-")
    assert not (home / "compatibility.local.json").exists()


def test_wizard_requires_experimental_mode(home, clock):
    service = make_service(home, clock, port=0, allow_unverified_host=False)
    creds = Credentials.load_or_create(service.settings)
    server = Server(App(service, creds, service.settings))
    server.start()
    creds.port = server.port
    creds.save(service.settings)
    try:
        wizard = Wizard(service.settings, FakeHands(home, service), api=Api(service.settings))
        try:
            wizard.run()
        except SystemExit as exc:
            assert "--allow-unverified-host" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("wizard ran without experimental mode")
    finally:
        server.stop()
        service.stop()
        service.db.close()

