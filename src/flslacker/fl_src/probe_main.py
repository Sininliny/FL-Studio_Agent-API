"""Slacker Probe: record what this FL build exposes to Piano Roll scripts (read-only).

Writes a JSON report to <FLSlacker home>/probe/ for `flslacker doctor`. Needs no pairing.
"""

from flslacker.fl_src.fl_common import *  # fl-inline: skip

PROBE_MODULES = (
    "json", "hmac", "hashlib", "os", "uuid", "time", "calendar", "ctypes",
    "tempfile", "secrets", "fractions", "threading", "socket", "sqlite3",
)


def slacker_public_names(obj):
    try:
        return sorted(name for name in dir(obj) if not name.startswith("_"))
    except Exception as exc:
        return ["<dir failed: %s>" % type(exc).__name__]


def slacker_probe_report():
    import importlib

    score = flp.score
    report = {
        "kind": "piano_roll_probe",
        "probe_version": SCRIPT_VERSION,
        "captured_at": utc_iso(),
        "module_name": globals().get("__name__"),
        "host": slacker_host_info(),
        "python_path": list(sys.path)[:20],
        "modules": {},
        "flpianoroll": slacker_public_names(flp),
        "score_attributes": slacker_public_names(score),
        "note_attributes": slacker_public_names(flp.Note()),
        "dialog_attributes": slacker_public_names(flp.ScriptDialog("probe", "")),
        "utils_attributes": slacker_public_names(getattr(flp, "Utils", None)),
        "environment": {
            "LOCALAPPDATA": bool(os.environ.get("LOCALAPPDATA")),
            "FLSLACKER_HOME": bool(os.environ.get("FLSLACKER_HOME")),
        },
        "base_dir": base_dir(),
    }
    for name in PROBE_MODULES:
        try:
            importlib.import_module(name)
            report["modules"][name] = True
        except Exception as exc:
            report["modules"][name] = type(exc).__name__
    for attribute in ("PPQ", "tsnum", "tsden", "noteCount", "markerCount"):
        try:
            value = getattr(score, attribute)
            report[attribute] = value
            report[attribute + "_type"] = type(value).__name__
        except Exception as exc:
            report[attribute] = "<error %s>" % type(exc).__name__
    try:
        report["timeline_selection"] = list(score.getTimelineSelection())
    except Exception as exc:
        report["timeline_selection"] = "<error %s>" % type(exc).__name__
    try:
        default_note = score.getDefaultNoteProperties()
        report["default_note"], report["default_note_errors"] = read_note(default_note)
        report["default_note_types"] = dict(
            (name, type(getattr(default_note, name)).__name__) for name in NOTE_FIELD_NAMES
        )
    except Exception as exc:
        report["default_note"] = "<error %s>" % type(exc).__name__
    try:
        blank = flp.Note()
        report["new_note"], report["new_note_errors"] = read_note(blank)
        report["new_note_extras"] = extra_attributes(blank, NOTE_FIELD_NAMES)
    except Exception as exc:
        report["new_note"] = "<error %s>" % type(exc).__name__
    notes = []
    errors = {}
    try:
        for index in range(min(score.noteCount, 500)):
            note = score.getNote(index)
            record, problems = read_note(note)
            errors.update(problems)
            if index == 0:
                report["first_note_types"] = dict(
                    (name, type(getattr(note, name)).__name__) for name in NOTE_FIELD_NAMES if name in record
                )
                report["first_note_extras"] = extra_attributes(note, NOTE_FIELD_NAMES)
                report["getNote_same_object"] = score.getNote(0) is note
            notes.append(record)
    except Exception as exc:
        errors["<loop>"] = type(exc).__name__ + ": " + str(exc)[:200]
    report["notes"] = notes
    report["note_errors"] = errors
    report["selected_count"] = len([n for n in notes if n.get("selected")])
    markers = []
    try:
        for index in range(min(score.markerCount, 200)):
            markers.append(read_marker(score.getMarker(index)))
    except Exception as exc:
        markers.append("<error %s>" % type(exc).__name__)
    report["markers"] = markers
    try:
        report["state_hash"] = state_hash(score.PPQ, notes, [m[0] for m in markers if isinstance(m, tuple)])
    except Exception as exc:
        report["state_hash"] = "<error %s>" % type(exc).__name__
    return report


def slacker_probe_main():
    try:
        report = slacker_probe_report()
        folder = ensure_private_dir(os.path.join(base_dir(), "probe"))
        name = "probe-%s-%s.json" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8])
        atomic_write_text(folder, name, canonical_dumps(report))
    except Exception as exc:
        slacker_show("Slacker Probe\n\nThe probe failed: %s: %s" % (type(exc).__name__, str(exc)[:300]))
        return
    fl = report["host"].get("fl") or {}
    slacker_show(
        "Slacker Probe\n\nFL %s, Python %s\nnoteCount=%s, selected=%s, PPQ=%s\nReport: %s"
        % (fl.get("version"), report["host"]["python"], report.get("noteCount"), report["selected_count"], report.get("PPQ"), name)
    )


if not globals().get("_SLACKER_NO_AUTORUN"):
    slacker_probe_main()
