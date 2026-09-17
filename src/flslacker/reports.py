"""Import FL-side reports that were printed to FL's Script output because FL refused to save them."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from flslacker.contracts.wire import REPORT_BEGIN, REPORT_END

# report kind -> file name stem (the names doctor and the M0 wizard look for)
KINDS = {"piano_roll_probe": "probe", "piano_roll_m0_mutation": "m0"}


class ReportError(ValueError):
    pass


def save_refused(report: dict[str, Any]) -> bool:
    """Whether FL refused the probe's one write (saving its report)."""
    refused = report.get("report_save_refused")
    if refused is None:  # older scripts recorded only the reason text
        refused = report.get("report_saved") is False and str(report.get("report_save_error") or "").startswith(
            "FL refused")
    return bool(refused)


def bridge_verdict(report: dict[str, Any]) -> str | None:
    """The probe's file bridge verdict; a refused write means blocked whatever the read tests said."""
    verdict = report.get("bridge_file_access")
    if verdict is not None and save_refused(report):
        return "blocked"
    return verdict


def fl_version_text(report: dict[str, Any]) -> str:
    fl = (report.get("host") or {}).get("fl") or {}
    return fl.get("version") or "version unknown"


def _decode_block(header: str, lines: list[str], footer: str) -> dict[str, Any]:
    parts = header.split()
    try:
        expected_size = int(parts[2])
    except (IndexError, ValueError):
        expected_size = None
    digest = footer.split()[1] if len(footer.split()) > 1 else ""
    encoded = "".join("".join(line.split()) for line in lines)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReportError(f"the copied report is damaged ({exc}); copy the whole block again") from None
    if expected_size is not None and len(data) != expected_size:
        raise ReportError(f"the copied report is incomplete ({len(data)} of {expected_size} bytes); copy the whole block")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ReportError("the copied report does not match its checksum; copy the whole block again")
    report = json.loads(data.decode("utf-8"))
    if not isinstance(report, dict) or report.get("kind") not in KINDS:
        raise ReportError("not an FL Slacker probe report")
    return report


def parse(text: str) -> list[dict[str, Any]]:
    """Extract every complete report block from ``text`` (or accept a saved JSON report)."""
    reports = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if REPORT_BEGIN not in line:
            index += 1
            continue
        header = line[line.index(REPORT_BEGIN):]
        body: list[str] = []
        index += 1
        while index < len(lines) and REPORT_END not in lines[index]:
            if REPORT_BEGIN in lines[index]:
                raise ReportError(f"a report block has no {REPORT_END} line; copy the whole block")
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ReportError(f"a report block has no {REPORT_END} line; copy the whole block")
        footer = lines[index][lines[index].index(REPORT_END):]
        reports.append(_decode_block(header, body, footer))
        index += 1
    if reports:
        return reports
    try:
        report = json.loads(text)
    except ValueError:
        raise ReportError(f"no {REPORT_BEGIN} ... {REPORT_END} block found") from None
    if not isinstance(report, dict) or report.get("kind") not in KINDS:
        raise ReportError("not an FL Slacker probe report")
    return [report]


def save(probe_dir: Path, report: dict[str, Any]) -> Path:
    probe_dir.mkdir(parents=True, exist_ok=True)
    stem = KINDS[report["kind"]]
    path = probe_dir / f"{stem}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-imported-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return path
