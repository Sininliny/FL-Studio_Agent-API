"""FL Slacker MIDI metadata adapter (optional, read-only).

Assign this controller script to a dedicated MIDI input (for example a loopMIDI port).
It never edits the project. From OnIdle it periodically writes a small signed status
message (tempo, transport, channel/pattern/mixer names) and reports project loads so
the companion can invalidate its session. All host calls go through a literal,
version-gated allowlist; nothing is dispatched by caller-supplied name.
"""

import os
import time

from flslacker.contracts.canonical import *  # fl-inline: skip
from flslacker.contracts.wire import *  # fl-inline: skip

import channels
import general
import mixer
import patterns
import transport
import ui

SLACKER_MIDI_VERSION = "0.1.0"
STATUS_INTERVAL_SECONDS = 2.0
NAMES_INTERVAL_SECONDS = 10.0
HEARTBEAT_SECONDS = 30.0
NAME_LIMIT = 128
PL_LOAD_OK = 100


def _api_version():
    try:
        return int(general.getVersion())
    except Exception:
        return 0


def _call(minimum_api, function, *args):
    """Call an allowlisted host function if the API version supports it."""
    if _state["api"] < minimum_api:
        return None
    try:
        return function(*args)
    except Exception:
        return None


def _names(count_function, name_function, first, api_needed):
    count = _call(api_needed, count_function)
    if not isinstance(count, int):
        return None
    items = []
    for index in range(first, min(count, NAME_LIMIT) + first):
        items.append(_call(api_needed, name_function, index))
    return {"index_origin": first, "count": count, "names": items, "truncated": count > NAME_LIMIT}


_state = {
    "api": 0,
    "last_status": 0.0,
    "last_names": 0.0,
    "last_sent": 0.0,
    "last_hash": None,
    "names": None,
    "pairing_mtime": None,
    "pairing": None,
}


def _pairing():
    path = pairing_path(base_dir())
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _state["pairing"] = None
        return None
    if mtime != _state["pairing_mtime"]:
        _state["pairing_mtime"] = mtime
        try:
            _state["pairing"] = read_pairing(base_dir())
        except BridgeError:
            _state["pairing"] = None
    return _state["pairing"]


def _send(body):
    pairing = _pairing()
    if pairing is None:
        return False
    try:
        folder = os.path.join(session_path(base_dir(), pairing["session_id"]), "responses")
        if not os.path.isdir(folder):
            return False
        envelope = make_envelope(
            pairing["secret"], "midi_status", pairing["session_id"], time.time_ns(), body, 300
        )
        atomic_write_text(folder, str(uuid.uuid4()) + ".json", canonical_dumps(envelope))
        return True
    except Exception:
        return False


def _status(event):
    now = time.monotonic()
    if _state["names"] is None or now - _state["last_names"] >= NAMES_INTERVAL_SECONDS:
        _state["last_names"] = now
        _state["names"] = {
            "channels": _names(channels.channelCount, channels.getChannelName, 0, 1),
            "patterns": _names(patterns.patternCount, patterns.getPatternName, 1, 1),
            "mixer_tracks": _names(mixer.trackCount, mixer.getTrackName, 0, 1),
        }
    return {
        "event": event,
        "adapter_version": SLACKER_MIDI_VERSION,
        "api_version": _state["api"],
        "safe_to_edit": _call(29, general.safeToEdit),
        "tempo_bpm": _call(38, mixer.getCurrentTempo, 0),
        "playing": _call(1, transport.isPlaying),
        "recording": _call(1, transport.isRecording),
        "current_pattern": _call(1, patterns.patternNumber),
        "selected_channel": _call(5, channels.selectedChannel, 1, 0, 1),
        "title": _call(1, ui.getProgTitle),
        "undo_history_count": _call(1, general.getUndoHistoryCount),
        "names": _state["names"],
    }


def OnInit():
    _state["api"] = _api_version()
    status = _status("adapter_started")
    if _send(status):
        now = time.monotonic()
        _state["last_status"] = _state["last_sent"] = now
        status["event"] = "status"
        _state["last_hash"] = sha256_hex(status)


def OnDeInit():
    _send({"event": "adapter_stopped", "adapter_version": SLACKER_MIDI_VERSION})


def OnIdle():
    now = time.monotonic()
    if now - _state["last_status"] < STATUS_INTERVAL_SECONDS:
        return
    _state["last_status"] = now
    status = _status("status")
    digest = sha256_hex(status)
    if digest == _state["last_hash"] and now - _state["last_sent"] < HEARTBEAT_SECONDS:
        return
    if _send(status):
        _state["last_hash"] = digest
        _state["last_sent"] = now


def OnProjectLoad(status):
    if status == PL_LOAD_OK or status == 0:
        _state["names"] = None
        _send({"event": "project_load", "load_status": int(status), "adapter_version": SLACKER_MIDI_VERSION})
