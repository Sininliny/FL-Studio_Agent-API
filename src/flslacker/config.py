"""Companion settings and private per-user paths."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from flslacker.contracts.wire import base_dir, ensure_private_dir

SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync", "nextcloud", "syncthing")


def is_synchronized_path(path: Path) -> bool:
    lowered = [part.lower() for part in Path(path).resolve().parts]
    if any(marker in part for part in lowered for marker in SYNC_MARKERS):
        return True
    for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(variable)
        if root:
            try:
                Path(path).resolve().relative_to(Path(root).resolve())
                return True
            except ValueError:
                pass
    return False


def default_fl_user_dir() -> Path | None:
    """FL's default user data folder; the installer asks when this does not exist."""
    candidates = [Path.home() / "Documents" / "Image-Line" / "FL Studio"]
    for candidate in candidates:
        if (candidate / "Settings").is_dir():
            return candidate
    return None


@dataclass
class Settings:
    home: Path = field(default_factory=lambda: Path(base_dir()))
    host: str = "127.0.0.1"
    port: int = 8765
    fl_user_dir: str | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str | None = None
    local_only: bool = True
    allow_unverified_host: bool = False
    capture_ttl_minutes: int = 30
    apply_ttl_minutes: int = 20
    plan_ttl_minutes: int = 30
    outcome_timeout_minutes: int = 10
    poll_interval_seconds: float = 0.25

    CONFIG_KEYS = (
        "port",
        "fl_user_dir",
        "ollama_url",
        "ollama_model",
        "local_only",
        "allow_unverified_host",
        "capture_ttl_minutes",
        "apply_ttl_minutes",
        "plan_ttl_minutes",
        "outcome_timeout_minutes",
    )

    # ---------------------------------------------------------------- paths
    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "flslacker.sqlite3"

    @property
    def credentials_path(self) -> Path:
        return self.home / "credentials.json"

    @property
    def ui_credentials_path(self) -> Path:
        return self.home / "ui-credentials.json"

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def probe_dir(self) -> Path:
        return self.home / "probe"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.data_dir, self.probe_dir, self.log_dir, self.home / "bridge", self.home / "mailbox"):
            ensure_private_dir(str(path))

    # ---------------------------------------------------------------- persistence
    @classmethod
    def load(cls, home: Path | None = None, **overrides: Any) -> "Settings":
        settings = cls(home=Path(home)) if home else cls()
        if settings.config_path.exists():
            data = json.loads(settings.config_path.read_text(encoding="utf-8"))
            for key in cls.CONFIG_KEYS:
                if key in data:
                    setattr(settings, key, data[key])
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        return settings

    def save(self) -> None:
        self.ensure_dirs()
        data = {key: getattr(self, key) for key in self.CONFIG_KEYS}
        write_private_json(self.config_path, data)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["home"] = str(self.home)
        return data


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass
class Credentials:
    """Two owner-only files: agents read only ``credentials.json``; the UI token stays separate.

    Any process running as the same OS user can read both; do not give agents shell or
    file access if they must not be able to approve their own plans.
    """

    agent_token: str
    ui_token: str
    port: int

    @classmethod
    def load_or_create(cls, settings: Settings, rotate: bool = False) -> "Credentials":
        existing = None if rotate else cls.load(settings)
        if existing is not None:
            if existing.port != settings.port:
                existing.port = settings.port
                existing.save(settings)
            return existing
        creds = cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32), settings.port)
        creds.save(settings)
        return creds

    @classmethod
    def load(cls, settings: Settings) -> "Credentials | None":
        agent = load_agent_credentials(settings)
        ui_path = settings.ui_credentials_path
        if agent is None or not ui_path.exists():
            return None
        ui = json.loads(ui_path.read_text(encoding="utf-8"))
        return cls(agent["agent_token"], ui["ui_token"], int(agent["port"]))

    def save(self, settings: Settings) -> None:
        url = f"http://127.0.0.1:{self.port}"
        write_private_json(settings.credentials_path, {"agent_token": self.agent_token, "port": self.port, "url": url})
        write_private_json(settings.ui_credentials_path, {"ui_token": self.ui_token, "port": self.port, "url": url})


def load_agent_credentials(settings: Settings) -> dict[str, Any] | None:
    if not settings.credentials_path.exists():
        return None
    data = json.loads(settings.credentials_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("agent_token"), str) or not isinstance(data.get("port"), int):
        return None
    return data
