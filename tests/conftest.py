from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from flslacker.adapters.fake import FakeFl
from flslacker.config import Settings
from flslacker.service.core import LOCAL_UI, Actor, Service
from flslacker.service.dispatcher import Dispatcher

AGENT = Actor("test-agent", "agent")
OTHER_AGENT = Actor("other-agent", "agent")


class Clock:
    """Real time plus an adjustable offset (FL-side scripts use real time)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_service(home: Path, clock: Clock, **settings) -> Service:
    options = {"allow_unverified_host": True, **settings}
    service = Service(Settings(home=home, **options), clock=clock)
    service.start()
    return service


@pytest.fixture
def service(home: Path, clock: Clock):
    svc = make_service(home, clock)
    yield svc
    svc.stop()
    svc.db.close()


@pytest.fixture
def dispatcher(service: Service) -> Dispatcher:
    return Dispatcher(service)


@pytest.fixture
def fl(home: Path, service: Service) -> FakeFl:
    return FakeFl(home)


def key() -> str:
    return str(uuid.uuid4())


def captured(service: Service, fl: FakeFl, scope: str = "selected", label: str | None = None, actor: Actor = AGENT):
    """Request a capture, run Slacker Capture in the fake FL and ingest it."""
    job = service.capture_score(actor, service.session_id, scope, label)
    fl.capture(service._request_id(job.job_id), label=label)
    service.pump()
    job = service.get_job(actor, job.job_id).job
    assert job.state == "completed", (job, fl.last_message())
    return service.get_snapshot(actor, job.snapshot_id)


def verify(service: Service, fl: FakeFl, job_id: str):
    """Run the verify capture that the companion requested for ``job_id``."""
    job = service._job(job_id)
    assert job.verify_job_id, job
    fl.capture(service._request_id(job.verify_job_id), label=job.target_label)
    service.pump()
    return service.get_job(AGENT, job_id)


__all__ = ["AGENT", "OTHER_AGENT", "LOCAL_UI", "Clock", "captured", "key", "make_service", "verify"]
