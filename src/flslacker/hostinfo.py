"""Local discovery of installed FL Studio builds (for doctor and capability reports)."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FlInstall:
    path: Path
    version: str | None
    embedded_python: str | None
    piano_roll_reference: Path | None


def file_version(path: Path) -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 4)).contents
    if values[0] != 0xFEEF04BD:
        return None
    ms, ls = values[2], values[3]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _embedded_python(root: Path) -> str | None:
    exe = root / "Shared" / "Python" / ("python.exe" if os.name == "nt" else "python3")
    if not exe.exists():
        return None
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip()
    return text.split()[-1] if text else None


def installed_fl() -> list[FlInstall]:
    roots: list[Path] = []
    if os.name == "nt":
        for base in {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", "")}:
            if base:
                roots += sorted(Path(base, "Image-Line").glob("FL Studio*"))
    elif sys.platform == "darwin":
        roots += sorted(Path("/Applications").glob("FL Studio*.app"))
    found = []
    for root in roots:
        exe = root / "FL64.exe"
        if os.name == "nt" and not exe.exists():
            continue
        reference = root / "System" / "Config" / "Piano roll scripts" / "Piano roll script reference.txt"
        found.append(
            FlInstall(
                path=root,
                version=file_version(exe) if os.name == "nt" else None,
                embedded_python=_embedded_python(root),
                piano_roll_reference=reference if reference.exists() else None,
            )
        )
    return found


def os_description() -> str:
    return f"{platform.system()} {platform.release()} {platform.version()}"
