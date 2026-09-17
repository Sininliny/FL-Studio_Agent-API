"""Local Ollama provider and the bounded agent loop.

Model tool calls are requests, not execution: each is validated and dispatched through the
common service. Malformed or unsupported tool output fails without editing anything.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import httpx

from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.models import TERMINAL_STATES
from flslacker.contracts.tools import TOOLS, TOOLS_BY_NAME, WRITE_TOOLS
from flslacker.transports.client import ToolClient, error_envelope

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def system_prompt() -> str:
    return resources.files("flslacker").joinpath("agent_guide.md").read_text(encoding="utf-8")


def ollama_tools() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema()}}
        for t in TOOLS
    ]


class OllamaProvider:
    def __init__(self, base_url: str, model: str, *, local_only: bool = True, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "Invalid Ollama URL.")
        if local_only and parts.hostname not in LOOPBACK_HOSTS:
            raise FlsError(ErrorCode.PERMISSION_DENIED, "Local-only mode allows only a loopback Ollama endpoint.")
        if not model:
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, "Choose an installed, tool-capable Ollama model.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout, transport=self.transport, trust_env=False)

    def list_models(self) -> list[str]:
        try:
            with self._client(10) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                return sorted(m.get("name", "") for m in response.json().get("models", []))
        except (httpx.HTTPError, ValueError) as exc:
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, f"Ollama is not reachable: {type(exc).__name__}")

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "tools": tools, "stream": False,
                   "options": {"temperature": 0}}
        try:
            with self._client(timeout or self.timeout) as client:
                response = client.post("/api/chat", json=payload)
        except httpx.TimeoutException:
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, "The model did not answer in time.")
        except httpx.TransportError as exc:
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, f"Ollama is not reachable ({type(exc).__name__}).")
        if response.status_code != 200:
            detail = ""
            try:
                detail = str(response.json().get("error", ""))[:200]
            except ValueError:
                pass
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, f"Ollama error {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError:
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, "Ollama returned invalid JSON.")
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise FlsError(ErrorCode.MODEL_UNAVAILABLE, "Ollama returned no assistant message.")
        return message


@dataclass
class AgentLimits:
    max_rounds: int = 8
    max_tool_calls: int = 32
    max_active_write_jobs: int = 1
    inference_budget_seconds: float = 120.0
    request_timeout_seconds: float = 60.0
    max_tool_result_chars: int = 24000


@dataclass
class AgentTranscript:
    run_id: str
    prompt: str
    state: str = "running"  # running | completed | stopped | failed
    stop_reason: str | None = None
    final: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "prompt": self.prompt,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "final": self.final,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


def _parse_arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


class AgentLoop:
    def __init__(self, provider: OllamaProvider, client: ToolClient, limits: AgentLimits | None = None,
                 clock=time.monotonic) -> None:
        self.provider = provider
        self.client = client
        self.limits = limits or AgentLimits()
        self.clock = clock

    def _write_jobs_active(self, jobs: list[str]) -> int:
        active = 0
        for job_id in jobs:
            result = self.client.call("fls_get_job", {"job_id": job_id})
            state = ((result.get("data") or {}).get("job") or {}).get("state")
            if state is None or state not in TERMINAL_STATES:
                active += 1
        return active

    def run(self, prompt: str, transcript: AgentTranscript | None = None) -> AgentTranscript:
        t = transcript or AgentTranscript(run_id=uuid.uuid4().hex, prompt=prompt)
        t.messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": prompt}]
        tools = ollama_tools()
        limits = self.limits
        spent = 0.0
        calls = 0
        write_jobs: list[str] = []
        for _ in range(limits.max_rounds):
            remaining = limits.inference_budget_seconds - spent
            if remaining <= 0:
                t.state, t.stop_reason = "stopped", "inference_budget_exhausted"
                return t
            started = self.clock()
            try:
                message = self.provider.chat(t.messages, tools, timeout=min(limits.request_timeout_seconds, remaining))
            except FlsError as exc:
                t.state, t.stop_reason, t.error = "failed", "model_error", exc.to_dict()
                return t
            spent += self.clock() - started
            tool_calls = message.get("tool_calls") or []
            t.messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "thinking")})
            if not isinstance(tool_calls, list):
                tool_calls = []
                t.messages.append({"role": "tool", "content": json.dumps(error_envelope(
                    ErrorCode.INVALID_ARGUMENT, "tool_calls must be a list", retryable=False))})
            if not tool_calls:
                t.state, t.stop_reason, t.final = "completed", "answered", str(message.get("content") or "")
                return t
            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                arguments = _parse_arguments(function.get("arguments")) if isinstance(function, dict) else None
                if calls >= limits.max_tool_calls:
                    t.state, t.stop_reason = "stopped", "tool_call_budget_exhausted"
                    return t
                calls += 1
                if not isinstance(name, str) or name not in TOOLS_BY_NAME:
                    result = error_envelope(ErrorCode.INVALID_ARGUMENT, "Unknown tool; nothing was executed.",
                                            retryable=False)
                elif arguments is None:
                    result = error_envelope(ErrorCode.INVALID_ARGUMENT,
                                            "Tool arguments were not a JSON object; nothing was executed.",
                                            retryable=False)
                elif name in WRITE_TOOLS and self._write_jobs_active(write_jobs) >= limits.max_active_write_jobs:
                    result = error_envelope(ErrorCode.BUSY, "One write job is already active in this conversation.")
                else:
                    result = self.client.call(name, arguments)
                    if name in WRITE_TOOLS and result.get("ok"):
                        write_jobs.append(result["data"]["job_id"])
                t.tool_calls.append({
                    "tool": name if isinstance(name, str) else None,
                    "ok": bool(result.get("ok")),
                    "code": None if result.get("ok") else (result.get("error") or {}).get("code"),
                })
                content = json.dumps(result)
                if len(content) > limits.max_tool_result_chars:
                    content = json.dumps({"ok": result.get("ok"), "truncated": True,
                                          "note": "Result too large; request less (e.g. include_notes=false).",
                                          "preview": content[: limits.max_tool_result_chars // 2]})
                t.messages.append({"role": "tool", "tool_name": str(name)[:64], "content": content})
        t.state, t.stop_reason = "stopped", "max_rounds"
        return t


class AgentRuns:
    """Background agent conversations started from the local UI."""

    def __init__(self, loop_factory) -> None:
        self.loop_factory = loop_factory
        self.runs: dict[str, AgentTranscript] = {}
        self.lock = threading.Lock()

    def start(self, prompt: str) -> dict[str, Any]:
        transcript = AgentTranscript(run_id=uuid.uuid4().hex, prompt=prompt)
        with self.lock:
            if any(r.state == "running" for r in self.runs.values()):
                raise FlsError(ErrorCode.BUSY, "An agent conversation is already running.")
            self.runs[transcript.run_id] = transcript

        def work() -> None:
            try:
                self.loop_factory().run(prompt, transcript)
            except FlsError as exc:
                transcript.state, transcript.error = "failed", exc.to_dict()
            except Exception as exc:  # keep the companion alive
                transcript.state = "failed"
                transcript.error = {"code": "MODEL_UNAVAILABLE", "message": type(exc).__name__}

        threading.Thread(target=work, name="flslacker-agent", daemon=True).start()
        return transcript.public()

    def get(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            transcript = self.runs.get(run_id)
        if transcript is None:
            raise FlsError(ErrorCode.INVALID_ARGUMENT, "Unknown agent run.", http_status=404)
        return transcript.public()
