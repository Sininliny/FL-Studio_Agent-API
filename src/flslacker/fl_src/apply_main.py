"""Slacker Apply: apply exactly one selected, validated, signed patch to the exposed score.

Runs once per explicit invocation (no live preview). Cancelling the dialog changes
nothing and writes nothing. The result is provisional until a fresh capture verifies it.
"""

from flslacker.fl_src.fl_common import *  # fl-inline: skip

CONFIRM_LABEL = "I confirm this Piano Roll is the job's target"
# `time` is written last so a host that re-sorts on time changes cannot redirect later writes.
UPDATE_WRITE_ORDER = tuple(f for f in UPDATABLE_NOTE_FIELDS if f != "time") + ("time",)


def slacker_apply_result(job, status, code=None, message=None, **extra):
    body = {
        "job_id": job.get("job_id"),
        "plan_hash": job.get("plan_hash"),
        "status": status,
        "error": None if code is None else {"code": code, "message": str(message)[:500]},
        "applied_operations": 0,
        "before_state_hash": None,
        "after_state_hash": None,
        "after_content_hash": None,
        "after_note_count": None,
        "mismatches": [],
        "host": slacker_host_info(),
    }
    body.update(extra)
    return body


def slacker_execute_apply(job):
    """Validate everything, then mutate. Returns the response body."""
    score = flp.score
    operations = job.get("operations")
    if not isinstance(operations, list) or not operations or len(operations) > MAX_PATCH_OPERATIONS:
        return slacker_apply_result(job, "failed", "LIMIT_EXCEEDED", "operation count outside 1..%d" % MAX_PATCH_OPERATIONS)
    try:
        records, markers, _ = slacker_read_score(score)
    except BridgeError as exc:
        return slacker_apply_result(job, "failed", "LIMIT_EXCEEDED", exc)
    live_hash = state_hash(score.PPQ, records, markers)
    if live_hash != job.get("base_state_hash"):
        return slacker_apply_result(
            job,
            "failed",
            "STALE_SNAPSHOT",
            "The exposed notes differ from the captured snapshot (%d live, %s captured). Nothing was changed."
            % (len(records), job.get("base_note_count")),
            before_state_hash=live_hash,
        )
    problems = validate_operations(records, operations)
    if problems:
        return slacker_apply_result(
            job, "failed", "INVALID_ARGUMENT", "; ".join(problems[:5]), before_state_hash=live_hash
        )
    expected = predict_after(records, operations)

    updates = [op for op in operations if op["op"] == "update"]
    deletes = sorted((op for op in operations if op["op"] == "delete"), key=lambda op: op["ordinal"], reverse=True)
    inserts = [op for op in operations if op["op"] == "insert"]
    applied = 0
    try:
        proxies = dict((op["ordinal"], score.getNote(op["ordinal"])) for op in updates)
        for op in updates:
            note = proxies[op["ordinal"]]
            for field in UPDATE_WRITE_ORDER:
                if field in op["set"]:
                    setattr(note, field, op["set"][field])
            applied += 1
        for op in deletes:
            score.deleteNote(op["ordinal"])
            applied += 1
        for op in inserts:
            note = flp.Note()
            for field in NOTE_FIELD_NAMES:
                setattr(note, field, op["note"][field])
            score.addNote(note)
            applied += 1
    except Exception as exc:
        observed = None
        try:
            observed_notes, observed_markers, _ = slacker_read_score(score)
            observed = {
                "after_state_hash": state_hash(score.PPQ, observed_notes, observed_markers),
                "after_content_hash": content_hash(score.PPQ, observed_notes, observed_markers),
                "after_note_count": len(observed_notes),
            }
        except Exception:
            observed = {}
        return slacker_apply_result(
            job,
            "partial_apply",
            "PARTIAL_APPLY",
            "%s after %d of %d operations: %s" % (type(exc).__name__, applied, len(operations), str(exc)[:200]),
            applied_operations=applied,
            before_state_hash=live_hash,
            **observed
        )

    after, after_markers, _ = slacker_read_score(score)
    ok, mismatches = match_contents(expected, after)
    return slacker_apply_result(
        job,
        "applied_provisional" if ok else "verification_mismatch",
        None if ok else "VERIFICATION_FAILED",
        None if ok else "The score read back after editing differs from the prediction.",
        applied_operations=applied,
        before_state_hash=live_hash,
        after_state_hash=state_hash(score.PPQ, after, after_markers),
        after_content_hash=content_hash(score.PPQ, after, after_markers),
        after_note_count=len(after),
        mismatches=mismatches,
    )


def slacker_apply_main():
    try:
        context = SlackerContext()
    except BridgeError as exc:
        slacker_show("Slacker Apply\n\n" + str(exc))
        return
    pending = context.pending(("apply_request",))
    if not pending:
        slacker_show("Slacker Apply\n\nThere are no pending apply jobs. Nothing was changed.")
        return

    labels = ["(choose a job)"]
    lines = []
    for envelope in pending:
        body = envelope["body"]
        text = "Job %s: %s on '%s'" % (
            slacker_short(body.get("job_id")),
            str(body.get("summary"))[:60],
            str(body.get("target_label"))[:40],
        )
        labels.append(text)
        lines.append(text + " (expires %s)" % envelope["expires_at"])

    description = (
        "Applies one reviewed FL Slacker plan to the notes this script can see.\r\n"
        "The live notes must match the captured snapshot exactly; otherwise nothing changes.\r\n\r\n"
        + "\r\n".join(lines)
        + "\r\n\r\nAfterwards, run Slacker Capture and choose the Verify request."
    )
    form = flp.ScriptDialog("Slacker Apply", description)
    form.AddInputCombo("Job", labels, 0)
    form.AddInputCombo("Action", ["Apply this job", "Reject this job"], 0)
    form.AddInputCheckbox(CONFIRM_LABEL, False)
    if not form.execute():
        return

    index = int(form.GetInputValue("Job"))
    if index <= 0 or index > len(pending):
        slacker_show("Slacker Apply\n\nNo job was chosen. Nothing was changed.")
        return
    request = pending[index - 1]
    job = request["body"]
    reject = int(form.GetInputValue("Action")) == 1

    if reject:
        if not context.claim(request["request_id"], "fl:rejected"):
            slacker_show("Slacker Apply\n\nThat job was already handled, cancelled or expired.")
            return
        context.send("apply_response", slacker_apply_result(job, "rejected", "USER_ACTION_REQUIRED", "Rejected in FL."), request["request_id"])
        slacker_show("Slacker Apply\n\nJob %s was rejected. Nothing was changed." % slacker_short(job.get("job_id")))
        return
    if not bool(form.GetInputValue(CONFIRM_LABEL)):
        slacker_show("Slacker Apply\n\nThe target was not confirmed. Nothing was changed.")
        return
    if not context.claim(request["request_id"], "fl:applying"):
        slacker_show("Slacker Apply\n\nThat job was already handled, cancelled or expired. Nothing was changed.")
        return

    result = slacker_execute_apply(job)
    try:
        context.send("apply_response", result, request["request_id"])
    except Exception as exc:
        slacker_show(
            "Slacker Apply\n\nThe edit result could not be reported (%s). Run Slacker Capture so the companion can reconcile."
            % str(exc)[:120]
        )
        return

    status = result["status"]
    if status == "applied_provisional":
        text = (
            "Applied %d operations to '%s' (provisional).\n\nRun Slacker Capture and choose the Verify request to confirm."
            % (result["applied_operations"], job.get("target_label"))
        )
    elif status == "failed":
        text = "Nothing was changed.\n\n%s: %s" % (result["error"]["code"], result["error"]["message"])
    else:
        text = "The edit did not complete as planned (%s).\n\n%s\n\nRun Slacker Capture so the companion can reconcile." % (
            status,
            result["error"]["message"] if result["error"] else "",
        )
    slacker_show("Slacker Apply\n\n" + text)


if not globals().get("_SLACKER_NO_AUTORUN"):
    slacker_apply_main()
