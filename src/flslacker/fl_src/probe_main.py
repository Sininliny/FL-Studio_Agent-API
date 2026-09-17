"""Slacker Probe: record what this FL build exposes to Piano Roll scripts.

READ-ONLY and deliberately gentle. On FL 26.1.6.5639 every file call the sandbox refuses returns
NULL to CPython without setting an exception, which corrupts the interpreter; enough of them in one
run crashed FL (heap corruption). So this probe never writes, never creates folders, and makes at
most one read attempt per folder. It never writes into FL's own script folder, which FL watches.

Saves its report to <FLSlacker home>/probe/ when FL allows it (one write attempt) and always prints
it to FL's Script output window (see `flslacker import-report`). Needs no pairing.
"""

from flslacker.fl_src.fl_common import *  # fl-inline: skip

PROBE_MODULES = (
    "json", "hmac", "hashlib", "os", "uuid", "time", "calendar", "ctypes",
    "tempfile", "secrets", "fractions", "threading", "socket", "sqlite3",
)
PROBE_NOTE_LIMIT = 2000
PROBE_REPORTED_NOTES = 64


def slacker_public_names(obj):
    try:
        return sorted(name for name in dir(obj) if not name.startswith("_"))
    except Exception as exc:
        return ["<dir failed: %s>" % type(exc).__name__]


def slacker_try(function, *args):
    try:
        function(*args)
        return "ok"
    except Exception as exc:
        return type(exc).__name__


def slacker_read_one(path):
    with open(path, "rb") as handle:
        handle.read(1)


def slacker_fl_scripts_dir():
    # FLSLACKER_FL_SCRIPT_DIR exists for tests; FL itself does not set it.
    override = os.environ.get("FLSLACKER_FL_SCRIPT_DIR")
    if override:
        return override
    return os.path.join(
        os.path.expanduser("~"), "Documents", "Image-Line", "FL Studio", "Settings", "Piano roll scripts", "Slacker"
    )


def slacker_existing_file(directory, preferred, depth=1):
    """``preferred`` if it exists, otherwise the first visible file up to ``depth`` folders down."""
    if preferred:
        path = os.path.join(directory, preferred)
        return path if os.path.isfile(path) else None
    try:
        names = sorted(name for name in os.listdir(directory) if not name.startswith("."))
    except Exception:
        return None
    for name in names:
        if os.path.isfile(os.path.join(directory, name)):
            return os.path.join(directory, name)
    if depth > 0:
        for name in names:
            if os.path.isdir(os.path.join(directory, name)):
                found = slacker_existing_file(os.path.join(directory, name), None, depth - 1)
                if found:
                    return found
    return None


def slacker_location_access(directory, read_name=None, any_file=False):
    """List ``directory`` and make at most one read attempt. No writes, ever (see the module docstring)."""
    result = {"path": directory, "exists": bool(directory) and os.path.isdir(directory)}
    if not result["exists"]:
        return result
    result["list"] = slacker_try(os.listdir, directory)
    if read_name or any_file:
        target = slacker_existing_file(directory, read_name)
        result["read"] = slacker_try(slacker_read_one, target) if target else "skipped: no file"
    return result


def slacker_file_access():
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    scripts = slacker_fl_scripts_dir()
    user_data = os.path.dirname(os.path.dirname(os.path.dirname(scripts)))
    # Read-only everywhere. Writes were observed refused on FL 26.1.6.5639 and repeated refused
    # calls crashed FL, so the probe does not test them; the report-save below is the only write.
    return {
        "companion_home": slacker_location_access(base_dir(), os.path.join("bridge", "pairing.json")),
        "fl_user_scripts": slacker_location_access(scripts, "Slacker Probe.pyscript"),
        "fl_user_data": slacker_location_access(user_data, any_file=True),
        "python_install": slacker_location_access(sys.base_prefix, "LICENSE.txt"),
        "temp": slacker_location_access(temp, any_file=True),
        "writes_tested": False,
    }


def slacker_bridge_verdict(access):
    """Read-only verdict; slacker_emit_report turns it into "blocked" if FL refuses the report save."""
    home = access["companion_home"]
    if not home["exists"]:
        return "unknown: start `flslacker serve` first"
    if home.get("read") == "skipped: no file":
        return "unknown: the companion is not paired"
    if home.get("list") == "ok" and home.get("read") == "ok":
        return "reads_ok"
    return "blocked"


def slacker_probe_report():
    import importlib

    score = flp.score
    report = {
        "kind": "piano_roll_probe",
        "probe_version": SCRIPT_VERSION,
        "captured_at": utc_iso(),
        "module_name": globals().get("__name__"),
        "has_file_global": "__file__" in globals(),
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
    access = slacker_file_access()
    report["file_access"] = access
    report["bridge_file_access"] = slacker_bridge_verdict(access)
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
        for index in range(min(score.noteCount, PROBE_NOTE_LIMIT)):
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
    report["notes"] = notes[:PROBE_REPORTED_NOTES]
    report["notes_read"] = len(notes)
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


def slacker_access_text(result):
    if not result.get("exists"):
        return "folder not found"
    return ", ".join("%s %s" % (key, result[key]) for key in ("list", "read") if key in result)


def slacker_probe_main():
    report = slacker_probe_report()
    # The one write the probe makes: try to save the report. Its result is the live write signal,
    # recorded in the report by slacker_emit_report (report_saved / report_save_error).
    saved, reason = slacker_emit_report(report, "probe")
    fl = report["host"].get("fl") or {}
    access = report["file_access"]
    version = fl.get("version") or "version unknown (%s)" % (fl.get("error") or fl.get("executable") or "not Windows")
    if report["bridge_file_access"] == "blocked":
        refused = [kind for kind, bad in (
            ("reads", slacker_bridge_verdict(access) == "blocked"), ("writes", report["report_save_refused"])) if bad]
        bridge = "BLOCKED - FL refuses file %s in the FL Slacker folder, so the bridge cannot work" % " and ".join(refused)
    elif report["bridge_file_access"] == "reads_ok":
        bridge = "reads OK; writes " + ("OK - the bridge should work" if saved else "not confirmed (see below)")
    else:
        bridge = report["bridge_file_access"]
    lines = [
        "FL %s, Python %s" % (version, report["host"]["python"]),
        "noteCount=%s, selected=%s, PPQ=%s" % (report.get("noteCount"), report["selected_count"], report.get("PPQ")),
        "",
        "File bridge: %s" % bridge,
        "Write test (save report to the FL Slacker folder): " + ("OK" if saved else "FAILED - %s" % reason),
        "",
        "Read access (the probe does not write elsewhere, to avoid destabilizing FL):",
        "  FL Slacker folder: " + slacker_access_text(access["companion_home"]),
        "  FL script folder: " + slacker_access_text(access["fl_user_scripts"]),
        "  FL user data folder: " + slacker_access_text(access["fl_user_data"]),
        "  TEMP: " + slacker_access_text(access["temp"]),
        "",
        slacker_report_location(saved, reason),
    ]
    slacker_show("Slacker Probe\n\n" + "\n".join(lines))


if not globals().get("_SLACKER_NO_AUTORUN"):
    slacker_run("Slacker Probe", slacker_probe_main, "The score was not changed.")
