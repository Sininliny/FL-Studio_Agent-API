"""Companion side of the file mailbox bridge (protocol flslacker/1).

File I/O is a design choice validated per FL build, not an official FL bridge. The
companion writes signed requests; FL scripts write signed snapshots and responses.
Whoever creates ``claims/<request_id>.claim`` first owns the request, which is how
cancellation and expiry exclude a concurrent FL-side apply.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flslacker import __version__
from flslacker.contracts import wire
from flslacker.contracts.canonical import PROTOCOL, canonical_dumps

COMPANION_FOLDERS = wire.MAILBOX_FOLDERS + ("archive", "rejected")
INBOUND_FOLDERS = {"snapshots": ("snapshot",), "responses": ("capture_error", "apply_response", "midi_status")}


@dataclass
class Inbound:
    session_id: str
    kind: str
    request_id: str
    in_reply_to: str | None
    sequence: int
    created_at: str
    body: dict[str, Any]
    path: Path


@dataclass
class Rejected:
    session_id: str
    path: Path
    reason: str


class MailboxAdapter:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)

    # ---------------------------------------------------------------- sessions
    def session_dir(self, session_id: str) -> Path:
        return Path(wire.session_path(str(self.home), session_id))

    def open_session(self, session_id: str) -> None:
        root = self.session_dir(session_id)
        for folder in COMPANION_FOLDERS:
            wire.ensure_private_dir(str(root / folder))

    def write_pairing(self, session_id: str, secret: str, epoch: int) -> None:
        bridge = self.home / "bridge"
        wire.ensure_private_dir(str(bridge))
        pairing = {
            "protocol": PROTOCOL,
            "session_id": session_id,
            "secret": secret,
            "adapter_epoch": epoch,
            "created_at": wire.utc_iso(),
            "companion_version": __version__,
            "companion_pid": os.getpid(),
        }
        wire.atomic_write_text(str(bridge), "pairing.json", canonical_dumps(pairing))

    def read_pairing(self) -> dict[str, Any] | None:
        try:
            return wire.read_pairing(str(self.home))
        except wire.BridgeError:
            return None

    def remove_pairing(self, session_id: str) -> None:
        pairing = self.read_pairing()
        if pairing and pairing["session_id"] == session_id:
            try:
                os.remove(wire.pairing_path(str(self.home)))
            except FileNotFoundError:
                pass

    # ---------------------------------------------------------------- requests
    def send(self, session_id: str, secret: str, kind: str, body: dict[str, Any], ttl_seconds: int,
             sequence: int, request_id: str | None = None, now: float | None = None) -> dict[str, Any]:
        envelope = wire.make_envelope(secret, kind, session_id, sequence, body, ttl_seconds,
                                      request_id=request_id, now=now)
        folder = self.session_dir(session_id) / "requests"
        wire.atomic_write_text(str(folder), envelope["request_id"] + ".json", canonical_dumps(envelope))
        return envelope

    def request_exists(self, session_id: str, request_id: str) -> bool:
        return (self.session_dir(session_id) / "requests" / f"{request_id}.json").exists()

    def withdraw(self, session_id: str, request_id: str) -> None:
        """Remove a request file. Only call after the companion owns its claim."""
        path = self.session_dir(session_id) / "requests" / f"{request_id}.json"
        try:
            path.replace(self.session_dir(session_id) / "archive" / f"request-{request_id}.json")
        except FileNotFoundError:
            pass

    def try_claim(self, session_id: str, request_id: str, holder: str) -> bool:
        return wire.try_claim(str(self.session_dir(session_id)), request_id, holder)

    def claim_info(self, session_id: str, request_id: str) -> tuple[str, float] | None:
        holder = wire.read_claim(str(self.session_dir(session_id)), request_id)
        if holder is None:
            return None
        path = wire.claim_path(str(self.session_dir(session_id)), request_id)
        try:
            return holder, os.path.getmtime(path)
        except OSError:
            return holder, time.time()

    # ---------------------------------------------------------------- inbound
    def poll(self, sessions: dict[str, str], now: float | None = None) -> tuple[list[Inbound], list[Rejected]]:
        accepted: list[Inbound] = []
        rejected: list[Rejected] = []
        for session_id, secret in sessions.items():
            root = self.session_dir(session_id)
            for folder, kinds in INBOUND_FOLDERS.items():
                directory = root / folder
                if not directory.is_dir():
                    continue
                for path in sorted(directory.iterdir()):
                    if path.name.startswith(".") or path.suffix != ".json":
                        continue
                    try:
                        envelope = wire.load_json_file(str(path))
                    except (OSError, ValueError, wire.BridgeError) as exc:
                        rejected.append(Rejected(session_id, path, f"unreadable: {exc}"[:200]))
                        continue
                    reason = wire.check_envelope(secret, envelope, session_id, kinds, now=now)
                    if reason is not None:
                        rejected.append(Rejected(session_id, path, reason))
                        continue
                    accepted.append(
                        Inbound(
                            session_id=session_id,
                            kind=envelope["kind"],
                            request_id=envelope["request_id"],
                            in_reply_to=envelope["in_reply_to"],
                            sequence=envelope["sequence"],
                            created_at=envelope["created_at"],
                            body=envelope["body"],
                            path=path,
                        )
                    )
        accepted.sort(key=lambda item: (item.session_id, item.sequence))
        return accepted, rejected

    def archive(self, item: Inbound) -> None:
        target = self.session_dir(item.session_id) / "archive" / f"{item.kind}-{item.path.name}"
        try:
            item.path.replace(target)
        except FileNotFoundError:
            pass

    def reject(self, item: Rejected | Inbound, reason: str | None = None) -> None:
        session_id = item.session_id
        path = item.path
        folder = self.session_dir(session_id) / "rejected"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(folder / path.name)
            (folder / f"{path.stem}.reason.txt").write_text(reason or getattr(item, "reason", ""), encoding="utf-8")
        except FileNotFoundError:
            pass

    def purge_session(self, session_id: str) -> None:
        shutil.rmtree(self.session_dir(session_id), ignore_errors=True)

    # ---------------------------------------------------------------- probe reports
    def probe_reports(self) -> list[Path]:
        folder = self.home / "probe"
        if not folder.is_dir():
            return []
        return sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
