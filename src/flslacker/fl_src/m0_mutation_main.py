"""Slacker M0 Mutation Probe (developer tool; DISPOSABLE PROJECTS ONLY).

Adds, edits and deletes probe notes to learn how this FL build handles writes:
value quantization, insertion order, proxy stability after time changes, index
shifts after deletion and clone() fidelity. Writes a report to <home>/probe/.
Afterwards run Slacker Probe to see the committed state.
"""

from flslacker.fl_src.fl_common import *  # fl-inline: skip

M0_CONFIRM = "This is a disposable project; add and delete probe notes"


def slacker_m0_find(score, predicate):
    for index in range(score.noteCount):
        if predicate(score.getNote(index)):
            return index
    return None


def slacker_m0_experiments(report):
    score = flp.score
    ppq = score.PPQ
    steps = report["steps"]

    def snap(label):
        steps.append({"step": label, "notes": [read_note(score.getNote(i))[0] for i in range(score.noteCount)]})

    snap("initial")

    # E1: write non-default values to a new note, read back before and after adding.
    probe = flp.Note()
    wanted = {
        "number": 61, "time": 0, "length": ppq, "group": 0, "pan": 0.33, "velocity": 0.77,
        "release": 0.21, "color": 3, "fcut": 0.61, "fres": 0.44, "pitchofs": 12,
        "slide": False, "porta": True, "muted": False, "selected": True, "repeats": 2,
    }
    write_errors = {}
    for field in NOTE_FIELD_NAMES:
        try:
            setattr(probe, field, wanted[field])
        except Exception as exc:
            write_errors[field] = type(exc).__name__ + ": " + str(exc)[:120]
    report["e1_written"] = wanted
    report["e1_write_errors"] = write_errors
    report["e1_before_add"] = read_note(probe)[0]
    score.addNote(probe)
    index = slacker_m0_find(score, lambda n: n.number == 61 and n.pitchofs == 12)
    report["e1_index_after_add"] = index
    report["e1_after_add"] = None if index is None else read_note(score.getNote(index))[0]
    report["e1_probe_object_after_add"] = read_note(probe)[0]
    snap("after e1")

    # E2: insertion order for notes added out of time order.
    for number, beat in ((70, 3), (71, 2), (72, 1)):
        note = flp.Note()
        note.number = number
        note.time = beat * ppq
        note.length = ppq // 2
        score.addNote(note)
    report["e2_positions"] = dict(
        (str(number), slacker_m0_find(score, lambda n, k=number: n.number == k)) for number in (70, 71, 72)
    )
    snap("after e2")

    # E3: does changing time through a proxy re-sort indices within the script?
    index = slacker_m0_find(score, lambda n: n.number == 72)
    proxy = score.getNote(index)
    proxy.time = 6 * ppq
    report["e3_index"] = index
    report["e3_proxy_after"] = read_note(proxy)[0]
    report["e3_same_index_after"] = read_note(score.getNote(index))[0]
    report["e3_new_position"] = slacker_m0_find(score, lambda n: n.number == 72)
    proxy.velocity = 0.5
    report["e3_proxy_second_write_hits"] = [
        read_note(score.getNote(i))[0] for i in range(score.noteCount) if score.getNote(i).velocity == 0.5
    ]
    snap("after e3")

    # E4: deletion index shifts and proxy stability across deletion.
    victim = slacker_m0_find(score, lambda n: n.number == 70)
    later_index = score.noteCount - 1
    later = score.getNote(later_index)
    later_before = read_note(later)[0]
    count_before = score.noteCount
    score.deleteNote(victim)
    report["e4_victim_index"] = victim
    report["e4_count_before_after"] = [count_before, score.noteCount]
    report["e4_later_index"] = later_index
    report["e4_later_proxy_before"] = later_before
    try:
        report["e4_later_proxy_after"] = read_note(later)[0]
    except Exception as exc:
        report["e4_later_proxy_after"] = "<error %s>" % type(exc).__name__
    snap("after e4")

    # E5: clone() fidelity.
    source_index = slacker_m0_find(score, lambda n: n.number == 61 and n.pitchofs == 12)
    clone = score.getNote(source_index).clone()
    report["e5_clone"] = read_note(clone)[0]
    report["e5_clone_extras"] = extra_attributes(clone, NOTE_FIELD_NAMES)
    clone.number = 62
    score.addNote(clone)
    report["e5_source_after_clone_edit"] = read_note(score.getNote(source_index))[0]
    snap("after e5")

    # E6: out-of-range writes on a note that is never added.
    loose = flp.Note()
    for field, value in (("velocity", 1.5), ("number", 200), ("color", 40), ("length", 0), ("time", -5)):
        try:
            setattr(loose, field, value)
            report.setdefault("e6_out_of_range", {})[field] = read_note(loose)[0].get(field)
        except Exception as exc:
            report.setdefault("e6_out_of_range", {})[field] = "<error %s>" % type(exc).__name__
    try:
        loose.not_a_field = 1
        report["e6_unknown_attribute"] = "accepted"
    except Exception as exc:
        report["e6_unknown_attribute"] = "<error %s>" % type(exc).__name__


def slacker_m0_main():
    form = flp.ScriptDialog(
        "Slacker M0 Mutation Probe",
        "Developer probe. It ADDS, EDITS and DELETES probe notes (pitches 61, 62, 70-72).\r\n"
        "Use only in a new, disposable project. Run Slacker Probe afterwards.",
    )
    form.AddInputCheckbox(M0_CONFIRM, False)
    if not form.execute():
        return
    if not bool(form.GetInputValue(M0_CONFIRM)):
        slacker_show("Slacker M0 Mutation Probe\n\nNot confirmed. Nothing was changed.")
        return
    report = {
        "kind": "piano_roll_m0_mutation",
        "probe_version": SCRIPT_VERSION,
        "captured_at": utc_iso(),
        "host": slacker_host_info(),
        "PPQ": flp.score.PPQ,
        "steps": [],
    }
    try:
        slacker_m0_experiments(report)
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    saved, reason = slacker_emit_report(report, "m0")
    slacker_show("Slacker M0 Mutation Probe\n\nDone%s.\n%s" % (
        "" if "error" not in report else " with error " + report["error"], slacker_report_location(saved, reason)))


if not globals().get("_SLACKER_NO_AUTORUN"):
    slacker_run("Slacker M0 Mutation Probe", slacker_m0_main, "Check the Piano Roll for probe notes (pitches 61, 62, 70-72).")
