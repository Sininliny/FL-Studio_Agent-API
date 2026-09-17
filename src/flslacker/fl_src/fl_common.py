"""FL-side helpers shared by the Slacker Piano Roll scripts.

Runs inside FL Studio's embedded interpreter with ``flp`` bound to ``flpianoroll``.
Inlined after ``canonical`` and ``wire``; STDLIB ONLY.
"""

import os
import sys
import time
import uuid

from flslacker.contracts.canonical import *  # fl-inline: skip
from flslacker.contracts.wire import *  # fl-inline: skip

SCRIPT_VERSION = "0.1.0"
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
        for name in sorted(os.listdir(folder)):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            request_id = name[:-5]
            if not is_uuid(request_id) or read_claim(self.dir, request_id) is not None:
                continue
            try:
                envelope = load_json_file(os.path.join(folder, name))
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
        return try_claim(self.dir, request_id, holder)

    def send(self, kind, body, in_reply_to=None):
        envelope = make_envelope(
            self.secret, kind, self.session_id, time.time_ns(), body, RESPONSE_TTL_SECONDS, in_reply_to
        )
        folder = os.path.join(self.dir, MESSAGE_KINDS[kind][1])
        atomic_write_text(folder, str(uuid.uuid4()) + ".json", canonical_dumps(envelope))
        return envelope


def slacker_short(identifier):
    return str(identifier)[:8]
