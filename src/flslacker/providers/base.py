"""Model provider interface. An LLM is optional; deterministic operations never need one."""

from __future__ import annotations

from typing import Any, Protocol


class ChatProvider(Protocol):
    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             timeout: float | None = None) -> dict[str, Any]:
        """Return one assistant message; may contain ``tool_calls`` (requests, not execution)."""
        ...

    def list_models(self) -> list[str]: ...
