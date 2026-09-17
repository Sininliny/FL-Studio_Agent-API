"""Capture -> analyze -> preview -> apply -> verify -> restore, end to end through the real scripts."""

from __future__ import annotations

import os

import pytest

from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.models import (
    AnalysisParameters,
    DeleteOp,
    GrantConstraints,
    GrantRequest,
    InsertOp,
    NoteSet,
    NoteTemplate,
    UpdateOp,
)

from conftest import AGENT, LOCAL_UI, OTHER_AGENT, captured, key, verify


def update(note_id: str, **values) -> UpdateOp:
    return UpdateOp(op="note.update", note_id=note_id, set=NoteSet(**values))


def test_capabilities_are_honest_about_verification(service):
    caps = {c.name: c for c in service.get_capabilities(AGENT).capabilities}
    assert caps["notes.patch.update"].supported  # --allow-unverified-host in tests
    assert "EXPERIMENTAL" in " ".join(caps["notes.patch.update"].limitations)
    assert caps["notes.patch.update"].verified_build is None
    assert caps["notes.patch.update"].status == "not_connected"
    assert caps["notes.live_preview"].supported is False
    assert caps["playlist.edit"].status == "unavailable"


def test_unverified_host_blocks_writes_by_default(home, clock):
    from conftest import make_service

    svc = make_service(home, clock, allow_unverified_host=False)
    try:
        caps = {c.name: c for c in svc.get_capabilities(AGENT).capabilities}
        assert caps["notes.patch.update"].supported is False
        assert "M0" in caps["notes.patch.update"].reason
        with pytest.raises(FlsError) as err:
            svc.capture_score(AGENT, svc.session_id, "selected")
        assert err.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        svc.stop()
        svc.db.close()


def test_full_cycle(service, fl):
    snap = captured(service, fl)
    assert snap.selection.exposed_count == 13
    assert snap.in_scope_count == 12
    assert snap.freshness.is_latest_for_target
    caps = {c.name: c for c in service.get_capabilities(AGENT).capabilities}
    assert caps["notes.patch.update"].status == "requires_user_action"

    analysis = service.analyze_score(AGENT, snap.snapshot_id, ["motifs", "key", "pitch_outliers"],
                                     AnalysisParameters())
    outliers = [f for f in analysis.findings if f.kind == "pitch_outlier"]
    assert [f.note_ids for f in outliers] == [["n10"]]
    assert outliers[0].suggestion == {"op": "note.update", "note_id": "n10", "set": {"pitch": 69}}

    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n10", pitch=69)], "align with motif M1")
    assert plan.diff[0].before["pitch"] == 70 and plan.diff[0].after["pitch"] == 69
    preview = service.preview_plan(AGENT, plan.plan_id)
    assert not preview.grant_coverage.covered

    with pytest.raises(FlsError) as err:
        service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    assert err.value.code == ErrorCode.PERMISSION_DENIED
    assert err.value.required_action["where"] == "companion_ui"

    with pytest.raises(FlsError):
        service.approve_plan(AGENT, plan.plan_id)  # agents cannot approve
    service.approve_plan(LOCAL_UI, plan.plan_id)

    idem = key()
    job = service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, idem)
    assert job.state == "awaiting_fl_action"
    assert job.next_action.script == "Slacker Apply"
    assert service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, idem).job_id == job.job_id
    with pytest.raises(FlsError):
        service.apply_plan(OTHER_AGENT, plan.plan_id, plan.plan_hash, idem)

    assert "Applied 1 operations" in fl.apply(job.job_id)
    service.pump()
    view = service.get_job(AGENT, job.job_id)
    assert view.job.state == "awaiting_verification"
    assert view.receipt is None
    assert "Verify" in view.job.next_action.instruction

    view = verify(service, fl, job.job_id)
    assert view.job.state == "applied"
    assert view.receipt.verification == "verified"
    assert view.receipt.recovery_available
    assert [n["number"] for n in fl.notes()][10] == 69
    assert fl.notes()[3]["pan"] == 0.3  # untouched properties survive

    # Restore: an agent needs a grant; the UI issues a bounded recipe grant.
    with pytest.raises(FlsError) as err:
        service.restore_edit(AGENT, view.receipt.receipt_id, key())
    assert err.value.code == ErrorCode.PERMISSION_DENIED
    service.issue_grant(LOCAL_UI, GrantRequest(session_id=service.session_id, constraints=GrantConstraints()))
    restore = service.restore_edit(AGENT, view.receipt.receipt_id, key())
    fl.apply(restore.job_id)
    service.pump()
    assert verify(service, fl, restore.job_id).job.state == "applied"
    assert [n["number"] for n in fl.notes()][10] == 70


def test_stale_snapshot_refused_by_fl_and_budget_refunded(service, fl):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    grant = service.issue_grant(LOCAL_UI, GrantRequest(session_id=service.session_id,
                                                       constraints=GrantConstraints(max_notes=1)))
    job = service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    fl.score.getNote(5).number = 50  # intervening edit in FL
    assert "Nothing was changed" in fl.apply(job.job_id)
    service.pump()
    view = service.get_job(AGENT, job.job_id)
    assert view.job.state == "failed"
    assert view.job.error.code == "STALE_SNAPSHOT"
    assert fl.notes()[0]["number"] == 60
    refunded = [g for g in service._grants() if g.grant_id == grant.grant_id][0]
    assert refunded.budget_used == 0 and refunded.uses == 0


def test_companion_detects_newer_capture(service, fl):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    fl.score.getNote(1).number = 63
    captured(service, fl)
    service.approve_plan(LOCAL_UI, plan.plan_id)
    with pytest.raises(FlsError) as err:
        service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    assert err.value.code == ErrorCode.STALE_SNAPSHOT


def test_cancel_before_fl_acts(service, fl):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    service.approve_plan(LOCAL_UI, plan.plan_id)
    job = service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    with pytest.raises(FlsError):
        service.cancel_job(OTHER_AGENT, job.job_id)
    assert service.cancel_job(AGENT, job.job_id).job.state == "cancelled"
    assert "no pending apply jobs" in fl.apply().lower()
    assert fl.notes()[0]["number"] == 60


def test_fl_dialog_cancel_and_unconfirmed_target_change_nothing(service, fl):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    service.approve_plan(LOCAL_UI, plan.plan_id)
    job = service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    assert fl.apply(job.job_id, cancel=True) is None
    assert "not confirmed" in fl.apply(job.job_id, confirm=False)
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "awaiting_fl_action"
    assert fl.notes()[0]["number"] == 60
    fl.apply(job.job_id, reject=True)
    service.pump()
    view = service.get_job(AGENT, job.job_id)
    assert view.job.state == "cancelled"
    assert fl.notes()[0]["number"] == 60


def test_expiry_prevents_late_apply(service, fl, clock):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    service.approve_plan(LOCAL_UI, plan.plan_id)
    job = service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    clock.advance(21 * 60)
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "expired"
    assert "no pending apply jobs" in fl.apply().lower()


def test_empty_selection_is_never_widened(service, fl):
    for note in fl.score._notes:
        note.selected = False
    snap = captured(service, fl, scope="selected")
    assert snap.in_scope_count == 0
    with pytest.raises(FlsError) as err:
        service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    assert "outside the captured scope" in err.value.message


def test_duplicates_stay_distinct_and_insert_delete(service, home):
    from flslacker.adapters.fake import FakeFl

    same = {"number": 60, "time": 0, "length": 96, "selected": True}
    fl = FakeFl(home, notes=[dict(same), dict(same), {"number": 64, "time": 96, "length": 96, "selected": True}])
    snap = captured(service, fl)
    ops = [
        update("n1", pitch=62),
        DeleteOp(op="note.delete", note_id="n2"),
        InsertOp(op="note.insert", client_id="new-1", note=NoteTemplate(pitch=67, start_tick=192, duration_tick=48)),
    ]
    with pytest.raises(FlsError):
        service.propose_patch(AGENT, snap.snapshot_id, ops + [update("n1", velocity=0.5)], "conflict")
    plan = service.propose_patch(AGENT, snap.snapshot_id, ops, "dupes")
    assert plan.limits.insert_count == 1 and plan.limits.delete_count == 1
    service.approve_plan(LOCAL_UI, plan.plan_id)
    job = service.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
    fl.apply(job.job_id)
    service.pump()
    view = verify(service, fl, job.job_id)
    assert view.job.state == "applied", view.job
    assert sorted(n["number"] for n in fl.notes()) == [60, 62, 67]
    inserted = [n for n in fl.notes() if n["number"] == 67][0]
    assert inserted["selected"] is True and inserted["length"] == 48

    # Restore re-inserts the deleted note from its before-image and removes the insert.
    plan = service.prepare_restore(LOCAL_UI, view.receipt.receipt_id)
    assert service.prepare_restore(LOCAL_UI, view.receipt.receipt_id).plan_id == plan.plan_id
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    fl.apply(job.job_id)
    service.pump()
    assert verify(service, fl, job.job_id).job.state == "applied"
    assert sorted(n["number"] for n in fl.notes()) == [60, 60, 64]


def test_quantized_floats_verify_within_tolerance(service, home):
    from flslacker.adapters.fake import FakeFl, FakeHost

    fl = FakeFl(home, host=FakeHost(float_quantum=1 / 128),
                notes=[{"number": 60, "time": 0, "length": 96, "selected": True, "velocity": 100 / 128}])
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", velocity=0.7)], "softer")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    fl.apply(job.job_id)
    service.pump()
    assert verify(service, fl, job.job_id).job.state == "applied"
    assert fl.notes()[0]["velocity"] == 90 / 128


def test_partial_apply_holds_writer_lock_until_acknowledged(service, home):
    from flslacker.adapters.fake import FakeFl, FakeHost

    notes = [{"number": 60 + i, "time": i * 96, "length": 96, "selected": True} for i in range(3)]
    host = FakeHost()
    fl = FakeFl(home, host=host, notes=notes)
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id,
                                 [update("n0", pitch=50), update("n1", pitch=51), update("n2", pitch=52)], "x")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    host.writes = 0
    host.fail_after_writes = 1
    assert "did not complete" in fl.apply(job.job_id)
    host.fail_after_writes = None
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "partial_apply"
    view = verify(service, fl, job.job_id)
    assert view.job.state == "verification_failed"

    snap2 = captured(service, fl)
    plan2 = service.propose_patch(AGENT, snap2.snapshot_id, [update("n2", pitch=40)], "y")
    service.approve_plan(LOCAL_UI, plan2.plan_id)
    with pytest.raises(FlsError) as err:
        service.apply_plan(AGENT, plan2.plan_id, plan2.plan_hash, key())
    assert err.value.code == ErrorCode.BUSY
    with pytest.raises(FlsError):
        service.acknowledge_job(AGENT, job.job_id, "checked")
    service.acknowledge_job(LOCAL_UI, job.job_id, "checked in FL")
    assert service.apply_plan(AGENT, plan2.plan_id, plan2.plan_hash, key()).state == "awaiting_fl_action"


def test_outcome_unknown_reconciles_to_not_applied(service, fl, clock):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    # FL claimed the job and then died before reporting.
    assert service.mailbox.try_claim(service.session_id, service._request_id(job.job_id), "fl:applying")
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "applying"
    claim = service.mailbox.session_dir(service.session_id) / "claims" / f"{service._request_id(job.job_id)}.claim"
    stale = clock() - 11 * 60
    os.utime(claim, (stale, stale))
    service.pump()
    view = service.get_job(AGENT, job.job_id)
    assert view.job.state == "outcome_unknown"
    assert view.job.error.code == "OUTCOME_UNKNOWN"
    view = verify(service, fl, job.job_id)
    assert view.job.state == "failed"
    assert "not applied" in view.job.error.message


def test_restart_retires_session_and_expires_pending(home, clock, service, fl):
    from conftest import make_service

    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    old_session = service.session_id
    service.stop()
    svc = make_service(home, clock)
    try:
        assert svc.session_id != old_session
        assert svc.get_job(AGENT, job.job_id).job.state == "expired"
        with pytest.raises(FlsError) as err:
            svc.capture_score(AGENT, old_session, "selected")
        assert err.value.code == ErrorCode.EXPIRED
        with pytest.raises(FlsError) as err:
            svc.apply_plan(AGENT, plan.plan_id, plan.plan_hash, key())
        assert err.value.code == ErrorCode.EXPIRED
        assert "no pending apply jobs" in fl.apply().lower()
    finally:
        svc.stop()
        svc.db.close()


def test_recipe_one_click(service, fl):
    run = service.run_recipe(LOCAL_UI, "slacker-fix-outliers", service.session_id, "Demo pattern")
    fl.capture(service._request_id(run["capture_job_id"]))
    service.pump()
    state = [r for r in service.ui_state()["recipe_runs"] if r["run_id"] == run["run_id"]][0]
    assert state["state"] == "awaiting_apply", state
    fl.apply(state["apply_job_id"])
    service.pump()
    assert verify(service, fl, state["apply_job_id"]).job.state == "applied"
    assert fl.notes()[10]["number"] == 69


def test_grant_limits(service, fl):
    snap = captured(service, fl)
    service.issue_grant(LOCAL_UI, GrantRequest(session_id=service.session_id, constraints=GrantConstraints(
        max_notes=2, max_pitch_delta=2)))
    big = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=65)], "big jump")
    reason = service.preview_plan(AGENT, big.plan_id).grant_coverage.reason
    assert "pitch change 5 exceeds 2" in reason
    vel = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", velocity=0.1)], "fields")
    assert "fields ['velocity'] not granted" in service.preview_plan(AGENT, vel.plan_id).grant_coverage.reason
    three = service.propose_patch(AGENT, snap.snapshot_id,
                                  [update("n0", pitch=61), update("n1", pitch=63), update("n2", pitch=65)], "many")
    assert "budget" in service.preview_plan(AGENT, three.plan_id).grant_coverage.reason
    delete = service.propose_patch(AGENT, snap.snapshot_id, [DeleteOp(op="note.delete", note_id="n0")], "del")
    assert "not granted" in service.preview_plan(AGENT, delete.plan_id).grant_coverage.reason


def test_project_load_invalidates_session(service, home):
    from flslacker.contracts import wire
    from flslacker.contracts.canonical import canonical_dumps

    pairing = wire.read_pairing(str(home))
    envelope = wire.make_envelope(pairing["secret"], "midi_status", pairing["session_id"], 5,
                                  {"event": "project_load", "load_status": 100, "adapter_version": "0.1.0"}, 300)
    folder = service.mailbox.session_dir(pairing["session_id"]) / "responses"
    wire.atomic_write_text(str(folder), "11111111-1111-4111-8111-111111111111.json", canonical_dumps(envelope))
    old = service.session_id
    service.pump()
    assert service.session_id != old
    assert wire.read_pairing(str(home))["session_id"] == service.session_id


def test_captures_older_than_the_edit_attempt_are_not_evidence(service, fl, clock):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    request_id = service._request_id(job.job_id)
    assert service.mailbox.try_claim(service.session_id, request_id, "fl:applying")
    claim = service.mailbox.session_dir(service.session_id) / "claims" / f"{request_id}.claim"
    stale = clock() - 11 * 60
    os.utime(claim, (stale, stale))
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "outcome_unknown"
    # The pre-edit capture shows unchanged notes; it must not prove "not applied".
    service._reconcile_with(snap, None, int(stale * 1_000_000_000) - 1)
    assert service.get_job(AGENT, job.job_id).job.state == "outcome_unknown"
    assert service.db.all("SELECT 1 FROM journal WHERE event='reconcile_skipped'")


def test_midi_and_piano_roll_sequences_are_separate_streams(service, fl, home):
    import time as _time

    from flslacker.contracts import wire
    from flslacker.contracts.canonical import canonical_dumps

    pairing = wire.read_pairing(str(home))
    future = _time.time_ns() + 10**12
    envelope = wire.make_envelope(pairing["secret"], "midi_status", pairing["session_id"], future,
                                  {"event": "status", "tempo_bpm": 120.0}, 300)
    folder = service.mailbox.session_dir(service.session_id) / "responses"
    wire.atomic_write_text(str(folder), "22222222-2222-4222-8222-222222222222.json", canonical_dumps(envelope))
    service.pump()
    snap = captured(service, fl)  # a Piano Roll message with a lower sequence is still accepted
    assert snap.selection.exposed_count == 13


def test_ended_verify_request_points_parent_at_a_fresh_capture(service, fl):
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", pitch=61)], "test")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    fl.apply(job.job_id)
    service.pump()
    parent = service._job(job.job_id)
    assert parent.state == "awaiting_verification"
    service.cancel_job(LOCAL_UI, parent.verify_job_id)
    parent = service._job(job.job_id)
    assert "New capture: selected notes only" in parent.next_action.instruction
    fl.capture(scope="selected")
    service.pump()
    assert service.get_job(AGENT, job.job_id).job.state == "applied"


def test_restore_unavailable_when_quantization_undid_the_change(service, home):
    from flslacker.adapters.fake import FakeFl, FakeHost

    fl = FakeFl(home, host=FakeHost(float_quantum=1 / 128),
                notes=[{"number": 60, "time": 0, "length": 96, "selected": True, "velocity": 100 / 128}])
    snap = captured(service, fl)
    plan = service.propose_patch(AGENT, snap.snapshot_id, [update("n0", velocity=0.7815)], "tiny")
    job = service.approve_and_apply(LOCAL_UI, plan.plan_id)
    fl.apply(job.job_id)
    service.pump()
    view = verify(service, fl, job.job_id)
    assert view.job.state == "applied"
    assert not view.receipt.recovery_available
    assert "already equals" in view.receipt.recovery_note
