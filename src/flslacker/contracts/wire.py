"""Mailbox wire protocol: pairing, signed envelopes and atomic files.

STDLIB ONLY and inlined into the FL-side scripts together with ``canonical``.
Lines ending in ``# fl-inline: skip`` are dropped when inlined.

Layout below the per-user base directory (never a synchronized folder)::

    bridge/pairing.json                         session id + shared secret
    mailbox/sessions/<session_id>/requests/     companion -> FL (named <request_id>.json)
    mailbox/sessions/<session_id>/responses/    FL -> companion (random UUID names)
    mailbox/sessions/<session_id>/snapshots/    FL -> companion (random UUID names)
    mailbox/sessions/<session_id>/claims/       <request_id>.claim, created exclusively
"""

import calendar
import json
import os
import sys
import time
import uuid

from flslacker.contracts.canonical import PROTOCOL, hmac_hex, hmac_matches  # fl-inline: skip

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_NOTES = 10000
MAX_PATCH_OPERATIONS = 1000
CLOCK_SKEW_SECONDS = 300

ENVELOPE_FIELDS = (
    "protocol",
    "kind",
    "session_id",
    "request_id",
    "in_reply_to",
    "sequence",
    "sender",
    "created_at",
    "expires_at",
    "body",
    "auth",
)
# kind -> (sender, mailbox folder)
MESSAGE_KINDS = {
    "capture_request": ("companion", "requests"),
    "apply_request": ("companion", "requests"),
    "snapshot": ("fl", "snapshots"),
    "capture_error": ("fl", "responses"),
    "apply_response": ("fl", "responses"),
    "midi_status": ("fl", "responses"),
}
MAILBOX_FOLDERS = ("requests", "responses", "snapshots", "claims")


class BridgeError(Exception):
    pass


def utc_iso(epoch=None):
    if epoch is None:
        epoch = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def parse_utc_iso(text):
    if not isinstance(text, str):
        raise BridgeError("timestamp must be a string")
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        raise BridgeError("bad timestamp")


def is_uuid(text):
    if not isinstance(text, str) or len(text) != 36:
        return False
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def base_dir():
    override = os.environ.get("FLSLACKER_HOME")
    if override:
        return os.path.abspath(override)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(root, "FLSlacker")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "FLSlacker")
    root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, "flslacker")


def pairing_path(base):
    return os.path.join(base, "bridge", "pairing.json")


def session_path(base, session_id):
    if not is_uuid(session_id):
        raise BridgeError("invalid session id")
    return os.path.join(base, "mailbox", "sessions", session_id)


def ensure_private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def atomic_write_text(directory, name, text):
    """Write ``text`` to ``directory/name`` via an exclusive temp file and rename."""
    if os.sep in name or (os.altsep and os.altsep in name) or name.startswith("."):
        raise BridgeError("invalid file name")
    data = text.encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise BridgeError("message exceeds size limit")
    temp = os.path.join(directory, ".tmp-" + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, os.path.join(directory, name))
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return os.path.join(directory, name)


def _reject_constant(name):
    raise BridgeError("non-finite number in JSON")


def load_json_file(path, max_bytes=MAX_MESSAGE_BYTES):
    size = os.path.getsize(path)
    if size > max_bytes:
        raise BridgeError("file exceeds size limit")
    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BridgeError("file exceeds size limit")
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)


def claim_path(session_dir, request_id):
    if not is_uuid(request_id):
        raise BridgeError("invalid request id")
    return os.path.join(session_dir, "claims", request_id + ".claim")


def try_claim(session_dir, request_id, holder):
    """Exclusively create the claim for ``request_id``. Returns False if already claimed."""
    path = claim_path(session_dir, request_id)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as handle:
        handle.write(holder.encode("utf-8")[:200])
        handle.flush()
        os.fsync(handle.fileno())
    return True


def read_claim(session_dir, request_id):
    path = claim_path(session_dir, request_id)
    try:
        with open(path, "rb") as handle:
            return handle.read(200).decode("utf-8", "replace")
    except FileNotFoundError:
        return None


def make_envelope(secret, kind, session_id, sequence, body, ttl_seconds, in_reply_to=None, request_id=None, now=None):
    if kind not in MESSAGE_KINDS:
        raise BridgeError("unknown message kind")
    if now is None:
        now = time.time()
    envelope = {
        "protocol": PROTOCOL,
        "kind": kind,
        "session_id": session_id,
        "request_id": request_id or str(uuid.uuid4()),
        "in_reply_to": in_reply_to,
        "sequence": int(sequence),
        "sender": MESSAGE_KINDS[kind][0],
        "created_at": utc_iso(now),
        "expires_at": utc_iso(now + ttl_seconds),
        "body": body,
    }
    envelope["auth"] = hmac_hex(secret, envelope)
    return envelope


def check_envelope(secret, envelope, session_id, kinds, now=None):
    """Return None if ``envelope`` is acceptable, otherwise a short reason string."""
    if now is None:
        now = time.time()
    if not isinstance(envelope, dict):
        return "not an object"
    if set(envelope) != set(ENVELOPE_FIELDS):
        return "unexpected or missing envelope fields"
    if envelope["protocol"] != PROTOCOL:
        return "unsupported protocol"
    if envelope["kind"] not in kinds or envelope["kind"] not in MESSAGE_KINDS:
        return "unexpected message kind"
    if envelope["sender"] != MESSAGE_KINDS[envelope["kind"]][0]:
        return "wrong sender"
    if envelope["session_id"] != session_id:
        return "wrong session"
    if not is_uuid(envelope["request_id"]):
        return "bad request id"
    if envelope["in_reply_to"] is not None and not is_uuid(envelope["in_reply_to"]):
        return "bad in_reply_to"
    sequence = envelope["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        return "bad sequence"
    if not isinstance(envelope["body"], dict):
        return "bad body"
    try:
        created = parse_utc_iso(envelope["created_at"])
        expires = parse_utc_iso(envelope["expires_at"])
    except BridgeError:
        return "bad timestamp"
    if created > now + CLOCK_SKEW_SECONDS:
        return "created in the future"
    if expires < now:
        return "expired"
    unsigned = dict(envelope)
    tag = unsigned.pop("auth")
    if not hmac_matches(secret, unsigned, tag):
        return "bad authentication tag"
    return None


def read_pairing(base):
    path = pairing_path(base)
    if not os.path.exists(path):
        raise BridgeError(
            "FL Slacker companion is not paired. Start it with `flslacker serve` and try again."
        )
    try:
        pairing = load_json_file(path, 64 * 1024)
    except (OSError, ValueError, BridgeError) as exc:
        raise BridgeError("pairing file is unreadable: " + str(exc)[:120])
    if not isinstance(pairing, dict) or pairing.get("protocol") != PROTOCOL:
        raise BridgeError("pairing file has an unsupported protocol version")
    secret = pairing.get("secret")
    if not is_uuid(pairing.get("session_id")) or not isinstance(secret, str) or len(secret) != 64:
        raise BridgeError("pairing file is malformed")
    try:
        bytes.fromhex(secret)
    except ValueError:
        raise BridgeError("pairing file is malformed")
    return pairing
