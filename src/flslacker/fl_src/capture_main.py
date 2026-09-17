"""Slacker Capture: read the notes FL exposes to this script and write a signed snapshot.

Never modifies the score. Runs once per explicit invocation (no live preview).
"""

from flslacker.fl_src.fl_common import *  # fl-inline: skip

CAPTURE_SCOPES = ("selected", "all_exposed")


def slacker_capture_payload(scope, label):
    score = flp.score
    notes, markers, errors = slacker_read_score(score)
    ppq = score.PPQ
    payload = {
        "scope": scope,
        "target_label": label,
        "captured_at": utc_iso(),
        "ppq": ppq,
        "time_signature": None,
        "timeline_selection": None,
        "notes": notes,
        "markers": markers,
        "default_note": None,
        "field_errors": errors,
        "extra_note_attributes": [],
        "state_hash": state_hash(ppq, notes, markers),
        "content_hash": content_hash(ppq, notes, markers),
        "host": slacker_host_info(),
    }
    try:
        payload["time_signature"] = [score.tsnum, score.tsden]
    except Exception:
        pass
    try:
        payload["timeline_selection"] = list(score.getTimelineSelection())
    except Exception:
        pass
    try:
        payload["default_note"] = read_note(score.getDefaultNoteProperties())[0]
    except Exception:
        pass
    if notes:
        try:
            payload["extra_note_attributes"] = sorted(extra_attributes(score.getNote(0), NOTE_FIELD_NAMES))
        except Exception:
            pass
    return payload


def slacker_capture_main():
    try:
        context = SlackerContext()
    except BridgeError as exc:
        slacker_show("Slacker Capture\n\n" + str(exc))
        return
    pending = context.pending(("capture_request",))

    labels = ["(choose what to capture)"]
    choices = [None]
    lines = []
    for envelope in pending:
        body = envelope["body"]
        purpose = "Verify" if body.get("purpose") == "verify" else "Request"
        text = "%s %s: %s notes" % (purpose, slacker_short(envelope["request_id"]), body.get("scope"))
        if body.get("target_label"):
            text += " of '%s'" % str(body.get("target_label"))[:60]
        labels.append(text)
        choices.append(envelope)
        lines.append(text)
    labels.append("New capture: selected notes only")
    choices.append("selected")
    labels.append("New capture: all notes visible to scripts")
    choices.append("all_exposed")

    description = (
        "Records the notes FL exposes to this script for the FL Slacker companion.\r\n"
        "The score is not changed. An empty selection is never widened.\r\n\r\n"
    )
    if lines:
        description += "Pending requests:\r\n" + "\r\n".join(lines) + "\r\n\r\n"
    else:
        description += "No pending requests from the companion.\r\n\r\n"
    description += "Name the target (for example 'Pattern 2 / Piano') and confirm it."

    form = flp.ScriptDialog("Slacker Capture", description)
    form.AddInputCombo("Capture", labels, 0)
    form.AddInputText("Target label", "")
    form.AddInputCheckbox("This Piano Roll is the named target", False)
    if not form.execute():
        return

    choice = choices[int(form.GetInputValue("Capture"))]
    label = str(form.GetInputValue("Target label") or "").strip()[:120]
    confirmed = bool(form.GetInputValue("This Piano Roll is the named target"))
    if choice is None:
        slacker_show("Slacker Capture\n\nNothing was chosen, so nothing was recorded.")
        return
    if not label or not confirmed:
        slacker_show("Slacker Capture\n\nName the target and tick the confirmation. Nothing was recorded.")
        return

    request = None
    if isinstance(choice, dict):
        request = choice
        scope = request["body"].get("scope")
        if scope not in CAPTURE_SCOPES:
            slacker_show("Slacker Capture\n\nThe request has an unsupported scope. Nothing was recorded.")
            return
        if not context.claim(request["request_id"], "fl:capture"):
            slacker_show("Slacker Capture\n\nThat request was already handled, cancelled or expired.")
            return
    else:
        scope = choice

    body = {
        "job_id": request["body"].get("job_id") if request else None,
        "purpose": request["body"].get("purpose") if request else "adhoc",
    }
    try:
        body["snapshot"] = slacker_capture_payload(scope, label)
    except BridgeError as exc:
        if request is not None:
            context.send(
                "capture_error",
                {"job_id": body["job_id"], "code": "LIMIT_EXCEEDED", "message": str(exc)[:300]},
                request["request_id"],
            )
        slacker_show("Slacker Capture\n\n" + str(exc))
        return
    try:
        context.send("snapshot", body, request["request_id"] if request else None)
    except Exception as exc:
        slacker_show("Slacker Capture\n\nCould not write the snapshot: " + str(exc)[:200])
        return

    snapshot = body["snapshot"]
    in_scope = len(snapshot["notes"])
    if scope == "selected":
        in_scope = len([n for n in snapshot["notes"] if n.get("selected")])
    slacker_show(
        "Slacker Capture\n\nRecorded %d exposed notes (%d in scope '%s') of '%s'.\nState %s"
        % (len(snapshot["notes"]), in_scope, scope, label, snapshot["state_hash"][:12])
    )


if not globals().get("_SLACKER_NO_AUTORUN"):
    slacker_capture_main()
