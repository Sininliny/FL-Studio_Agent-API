"""A simulated FL Studio that runs the real generated Piano Roll scripts.

Used by tests and ``flslacker serve --fake``. It exercises the actual mailbox protocol,
claims, signatures and script logic against ``fake_flpianoroll``. Behavioural claims
about real FL still require the M0 checklist.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flslacker import fl_build
from flslacker.adapters import fake_flpianoroll as fake
from flslacker.contracts import wire

FakeHost = fake.FakeHost

DEMO_NOTES = [
    # A four-note motif (steps +2 +2 +3) played three times; in the transposed third copy
    # n10 is 70 where the motif implies 69.
    {"number": 60, "time": 0, "length": 48, "selected": True},
    {"number": 62, "time": 48, "length": 48, "selected": True},
    {"number": 64, "time": 96, "length": 48, "selected": True},
    {"number": 67, "time": 144, "length": 96, "selected": True, "pan": 0.3},
    {"number": 60, "time": 384, "length": 48, "selected": True},
    {"number": 62, "time": 432, "length": 48, "selected": True},
    {"number": 64, "time": 480, "length": 48, "selected": True},
    {"number": 67, "time": 528, "length": 96, "selected": True},
    {"number": 65, "time": 768, "length": 48, "selected": True},
    {"number": 67, "time": 816, "length": 48, "selected": True},
    {"number": 70, "time": 864, "length": 48, "selected": True, "velocity": 0.6},
    {"number": 72, "time": 912, "length": 96, "selected": True},
    {"number": 48, "time": 0, "length": 384, "selected": False},
]

_env_lock = threading.RLock()


@contextmanager
def _home(home: Path):
    with _env_lock:
        previous = os.environ.get("FLSLACKER_HOME")
        os.environ["FLSLACKER_HOME"] = str(home)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("FLSLACKER_HOME", None)
            else:
                os.environ["FLSLACKER_HOME"] = previous


class FakeFl:
    """One fake Piano Roll with a fake score and the Slacker scripts."""

    def __init__(self, home: Path, notes: list[dict] | None = None, host: fake.FakeHost | None = None,
                 label: str = "Demo pattern") -> None:
        self.home = Path(home)
        self.host = host or fake.FakeHost()
        self.module = fake.make_module(self.host, notes if notes is not None else DEMO_NOTES)
        self.label = label
        self.sources = {name: fl_build.render(fl_build.script(name)) for name in (
            "Slacker Capture.pyscript", "Slacker Apply.pyscript", "Slacker Probe.pyscript")}

    # ---------------------------------------------------------------- inspection
    @property
    def score(self):
        return self.module.score

    def notes(self) -> list[dict[str, Any]]:
        return self.module.score.dump()

    def pending(self, kind: str) -> list[dict[str, Any]]:
        pairing = wire.read_pairing(str(self.home))
        folder = Path(wire.session_path(str(self.home), pairing["session_id"])) / "requests"
        found = []
        for path in sorted(folder.glob("*.json")):
            if wire.read_claim(str(folder.parent), path.stem) is not None:
                continue
            envelope = wire.load_json_file(str(path))
            if envelope["kind"] == kind and wire.check_envelope(
                    pairing["secret"], envelope, pairing["session_id"], (kind,)) is None:
                found.append(envelope)
        found.sort(key=lambda e: e["sequence"])
        return found

    def last_message(self) -> str | None:
        return self.host.messages[-1] if self.host.messages else None

    # ---------------------------------------------------------------- running scripts
    def _run(self, name: str, responder: Callable[[Any], Any]) -> str | None:
        self.host.dialog_responder = responder
        before = len(self.host.messages)
        with _home(self.home):
            fake.run_script(self.sources[name], self.module, filename=name)
        self.host.dialog_responder = None
        return self.host.messages[-1] if len(self.host.messages) > before else None

    def capture(self, request_id: str | None = None, *, scope: str | None = None, label: str | None = None,
                confirm: bool = True, cancel: bool = False) -> str | None:
        """Run Slacker Capture. Picks the request by id prefix, or an ad-hoc scope."""

        def respond(dialog):
            if cancel:
                return False
            if scope is not None:
                wanted = "New capture: selected" if scope == "selected" else "New capture: all"
                index = fake.pick_option(dialog, "Capture", lambda o: o.startswith(wanted))
            else:
                index = fake.pick_option(
                    dialog, "Capture",
                    lambda o: (o.startswith("Request") or o.startswith("Verify"))
                    and (request_id is None or o.split()[1].rstrip(":") == request_id[:8]))
            return {"Capture": index, "Target label": label or self.label,
                    "This Piano Roll is the named target": confirm}

        return self._run("Slacker Capture.pyscript", respond)

    def apply(self, job_id: str | None = None, *, reject: bool = False, confirm: bool = True,
              cancel: bool = False) -> str | None:
        def respond(dialog):
            if cancel:
                return False
            index = fake.pick_option(
                dialog, "Job",
                lambda o: o.startswith("Job ") and (job_id is None or o.split()[1].rstrip(":") == job_id[:8]))
            return {"Job": index, "Action": 1 if reject else 0,
                    "I confirm this Piano Roll is the job's target": confirm}

        return self._run("Slacker Apply.pyscript", respond)

    def probe(self) -> str | None:
        return self._run("Slacker Probe.pyscript", lambda dialog: False)

    def process_pending(self) -> int:
        """Handle every pending request as a cooperative user would (demo mode)."""
        handled = 0
        for envelope in self.pending("capture_request"):
            self.capture(envelope["request_id"], label=envelope["body"].get("target_label") or self.label)
            handled += 1
        for envelope in self.pending("apply_request"):
            self.apply(envelope["body"]["job_id"])
            handled += 1
        return handled
