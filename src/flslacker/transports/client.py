"""Tool clients: in-process (companion) and HTTP (MCP shim, CLI agent)."""

from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

import httpx

from flslacker.config import Settings, load_agent_credentials
from flslacker.contracts.errors import ErrorCode


class ToolClient(Protocol):
    def call(self, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]: ...


def error_envelope(code: ErrorCode, message: str, retryable: bool = True, **details: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": str(uuid.uuid4()),
        "data": None,
        "error": {"code": code.value, "message": message, "retryable": retryable, "details": details,
                  "required_action": None},
    }


class LocalToolClient:
    def __init__(self, dispatcher, actor) -> None:
        self.dispatcher = dispatcher
        self.actor = actor

    def call(self, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        result, _ = self.dispatcher.call(self.actor, tool, arguments)
        return result


def safe_client_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", name or "agent")[:40]
    return cleaned or "agent"


class HttpToolClient:
    """Forwards tool calls to the running companion with the agent token."""

    def __init__(self, settings: Settings, client_name: str = "mcp", transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0) -> None:
        self.settings = settings
        self.client_name = safe_client_name(client_name)
        self.transport = transport
        self.timeout = timeout

    def _client(self) -> httpx.Client | None:
        creds = load_agent_credentials(self.settings)
        if creds is None:
            return None
        return httpx.Client(
            base_url=f"http://127.0.0.1:{creds['port']}",
            headers={"Authorization": f"Bearer {creds['agent_token']}", "X-FLS-Client": self.client_name},
            timeout=self.timeout,
            transport=self.transport,
            trust_env=False,
        )

    def call(self, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        client = self._client()
        if client is None:
            return error_envelope(ErrorCode.ADAPTER_OFFLINE,
                                  "FL Slacker is not set up. Run `flslacker serve` first.")
        try:
            with client:
                response = client.post(f"/v1/tools/{tool}", json=arguments or {})
        except httpx.TransportError:
            return error_envelope(ErrorCode.ADAPTER_OFFLINE,
                                  "The FL Slacker companion is not running. Start it with `flslacker serve`.")
        try:
            body = response.json()
        except ValueError:
            return error_envelope(ErrorCode.ADAPTER_OFFLINE, f"Unexpected companion response ({response.status_code}).")
        if not isinstance(body, dict) or "ok" not in body:
            return error_envelope(ErrorCode.ADAPTER_OFFLINE, f"Unexpected companion response ({response.status_code}).")
        return body
