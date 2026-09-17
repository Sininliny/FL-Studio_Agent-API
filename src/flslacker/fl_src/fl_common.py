"""FL-side helpers shared by the Slacker Piano Roll scripts.

Runs inside FL Studio's embedded interpreter with ``flp`` bound to ``flpianoroll``.
Inlined after ``canonical`` and ``wire``; STDLIB ONLY.
"""

import hashlib
import os
import sys
import time
import uuid

from flslacker.contracts.canonical import *  # fl-inline: skip
from flslacker.contracts.wire import *  # fl-inline: skip

SCRIPT_VERSION = "0.1.3"
RESPONSE_TTL_SECONDS = 7 * 24 * 3600


def slacker_show(message):
    try:
        flp.Utils.ShowMessage(message)
    except Exception:
        pass


def slacker_log(message):
    try:
        flp.Utils.log("[Slacker] " + message)
    except Exception:
        pass


def slacker_fl_version():
    """Exact build of the host executable, read from its Windows version resource."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:
        # Piano Roll scripts have been seen to get ImportError here; the companion reads the build from the install.
        return {"executable": None, "version": None, "error": "ctypes unavailable: %s" % type(exc).__name__}
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        kernel32.GetModuleFileNameW.restype = wintypes.DWORD
        if not kernel32.GetModuleFileNameW(None, buffer, 32768):
            return None
        exe = buffer.value
        if not os.path.basename(exe).lower().startswith("fl"):
            return {"executable": os.path.basename(exe)[:60], "version": None}
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL
        size = version.GetFileVersionInfoSizeW(exe, None)
        if not size:
            return {"executable": os.path.basename(exe), "version": None}
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(exe, 0, size, data):
            return {"executable": os.path.basename(exe), "version": None}
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return {"executable": os.path.basename(exe), "version": None}

        class FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("signature", wintypes.DWORD),
                ("struct_version", wintypes.DWORD),
                ("file_version_ms", wintypes.DWORD),
                ("file_version_ls", wintypes.DWORD),
            ]

        info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
        if info.signature != 0xFEEF04BD:
            return {"executable": os.path.basename(exe), "version": None}
        ms = info.file_version_ms
        ls = info.file_version_ls
        text = "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        return {"executable": os.path.basename(exe), "version": text}
    except Exception as exc:
        return {"executable": None, "version": None, "error": type(exc).__name__}


def slacker_host_info():
    return {
        "script_version": SCRIPT_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "fl": slacker_fl_version(),
    }


def slacker_read_score(score):
    count = score.noteCount
    if count > MAX_SNAPSHOT_NOTES:
        raise BridgeError(
            "LIMIT_EXCEEDED: %d notes exposed; the limit is %d. Nothing was recorded." % (count, MAX_SNAPSHOT_NOTES)
        )
    notes = []
    errors = {}
    for index in range(count):
        record, problems = read_note(score.getNote(index))
        errors.update(problems)
        notes.append(record)
    markers = []
    for index in range(score.markerCount):
        record, problems = read_marker(score.getMarker(index))
        for key, value in problems.items():
            errors["marker." + key] = value
        markers.append(record)
    return notes, markers, errors


class SlackerContext:
    """Paired mailbox session as seen from FL."""

    def __init__(self):
        # read_pairing() reads pairing.json first, so a host that refuses file reads (as FL
        # 26.1.6.5639 does) raises BridgeUnavailable here, before any dialog and before any write.
        self.base = base_dir()
        pairing = read_pairing(self.base)
        self.session_id = pairing["session_id"]
        self.secret = pairing["secret"]
        self.dir = session_path(self.base, self.session_id)
        for folder in MAILBOX_FOLDERS:
            if not os.path.isdir(os.path.join(self.dir, folder)):
                raise BridgeError(
                    "The mailbox for the paired session is missing. Restart `flslacker serve`."
                )

    def pending(self, kinds):
        folder = os.path.join(self.dir, "requests")
        found = []
        for name in sorted(bridge_call("list", os.listdir, folder)):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            request_id = name[:-5]
            if not is_uuid(request_id) or bridge_call("read", read_claim, self.dir, request_id) is not None:
                continue
            try:
                envelope = bridge_call("read", load_json_file, os.path.join(folder, name))
            except BridgeUnavailable:
                raise
            except Exception:
                continue
            if check_envelope(self.secret, envelope, self.session_id, kinds) is not None:
                continue
            if envelope["request_id"] != request_id:
                continue
            found.append(envelope)
        found.sort(key=lambda item: item["sequence"])
        return found

    def claim(self, request_id, holder):
        return bridge_call("write", try_claim, self.dir, request_id, holder)

    def send(self, kind, body, in_reply_to=None):
        envelope = make_envelope(
            self.secret, kind, self.session_id, time.time_ns(), body, RESPONSE_TTL_SECONDS, in_reply_to
        )
        folder = os.path.join(self.dir, MESSAGE_KINDS[kind][1])
        bridge_call("write", atomic_write_text, folder, str(uuid.uuid4()) + ".json", canonical_dumps(envelope))
        return envelope


def slacker_short(identifier):
    return str(identifier)[:8]


def slacker_run(title, main, after_failure):
    """Run a script entry point; never let FL show a raw traceback for an expected condition."""
    try:
        main()
    except BridgeError as exc:
        slacker_show(title + "\n\n" + str(exc))
    except Exception as exc:
        text = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        slacker_log(title + " failed: " + text)
        try:
            import traceback

            traceback.print_exc()  # FL shows stdout/stderr in its Script output window
        except Exception:
            pass
        slacker_show("%s\n\nUnexpected error: %s\n\n%s" % (title, text, after_failure))


def slacker_emit_report(report, stem):
    """Save ``report`` to <home>/probe/ if the host allows it; always print it to the Script output.

    Also records the outcome in ``report`` so the saved and printed copies both carry it.
    Returns (saved file name or None, reason it was not saved or None).
    """
    # Optimistic, so a successfully saved file records report_saved=True; corrected below on failure.
    report["report_saved"] = True
    report["report_save_error"] = None
    report["report_save_refused"] = False
    saved, reason = None, None
    folder = os.path.join(base_dir(), "probe")
    name = "%s-%s-%s.json" % (stem, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8])
    if not os.path.isdir(folder):
        reason = "the companion has not created %s yet" % folder
    else:
        try:
            atomic_write_text(folder, name, canonical_dumps(report))
            saved = name
        except Exception as exc:
            if is_host_refusal(exc):
                reason = "FL refused the file write: %s" % type(exc).__name__
                report["report_save_refused"] = True
                if "bridge_file_access" in report:
                    # The mailbox writes in this same folder, so a refused save settles the verdict.
                    report["bridge_file_access"] = "blocked"
            else:
                reason = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    if not saved:
        report["report_saved"] = False
        report["report_save_error"] = reason
    text = canonical_dumps(report)  # final copy for printing, with the outcome recorded
    try:
        # base64 survives copying from the output window even if lines are trimmed or re-wrapped.
        import base64

        data = text.encode("utf-8")
        encoded = base64.b64encode(data).decode("ascii")
        print("%s %s %d" % (REPORT_BEGIN, stem, len(data)))
        for start in range(0, len(encoded), REPORT_LINE_CHARS):
            print(encoded[start:start + REPORT_LINE_CHARS])
        print("%s %s" % (REPORT_END, hashlib.sha256(data).hexdigest()))
    except Exception:
        pass
    return saved, reason


def slacker_report_location(saved, reason):
    if saved:
        return "Report saved: " + saved
    return (
        "The report was NOT saved (%s). It was printed to FL's Script output window instead (VIEW > Script "
        "output): copy everything from the %s line to the %s line into a text file and run "
        "`flslacker import-report <file>`."
        % (reason, REPORT_BEGIN, REPORT_END)
    )
