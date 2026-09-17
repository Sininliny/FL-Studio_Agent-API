"""MCP shim and the Ollama agent loop: tool calls are requests, validated by the common service."""

from __future__ import annotations

import json
import uuid

import anyio
import httpx
import pytest
from mcp import Client

from flslacker.config import Settings
from flslacker.contracts.errors import FlsError
from flslacker.providers.ollama import AgentLimits, AgentLoop, OllamaProvider, ollama_tools, system_prompt
from flslacker.service.core import Actor
from flslacker.transports.client import HttpToolClient, LocalToolClient
from flslacker.transports.mcp import build_server

from conftest import captured


def test_mcp_lists_and_calls_tools(service, dispatcher, fl):
    client = LocalToolClient(dispatcher, Actor("mcp:test", "agent"))
    snap = captured(service, fl)

    async def run():
        async with Client(build_server(client), raise_exceptions=True) as mcp:
            tools = (await mcp.list_tools()).tools
            names = {t.name for t in tools}
            assert names == {t["function"]["name"] for t in ollama_tools()}
            apply = next(t for t in tools if t.name == "fls_apply_plan")
            assert apply.annotations.destructive_hint and not apply.annotations.read_only_hint
            assert apply.input_schema["required"] == ["plan_id", "expected_plan_hash", "idempotency_key"]
            caps = await mcp.call_tool("fls_get_capabilities", {})
            assert not caps.is_error and caps.structured_content["data"]["protocol"] == "flslacker/1"
            got = await mcp.call_tool("fls_get_snapshot", {"snapshot_id": snap.snapshot_id, "include_notes": False})
            assert not got.is_error and got.structured_content["data"]["notes"] == []
            bad = await mcp.call_tool("fls_get_job", {"job_id": "not-a-uuid"})
            assert bad.is_error and bad.structured_content["error"]["code"] == "INVALID_ARGUMENT"
            assert json.loads(bad.content[0].text)["ok"] is False

    anyio.run(run)


def test_http_client_reports_offline_companion(tmp_path):
    settings = Settings(home=tmp_path)
    result = HttpToolClient(settings).call("fls_get_capabilities", {})
    assert result["error"]["code"] == "ADAPTER_OFFLINE"
    (tmp_path / "credentials.json").write_text(json.dumps({"agent_token": "t", "port": 1}), encoding="utf-8")

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    result = HttpToolClient(settings, transport=httpx.MockTransport(refuse)).call("fls_get_capabilities", {})
    assert result["error"]["code"] == "ADAPTER_OFFLINE" and "not running" in result["error"]["message"]


class ScriptedOllama:
    """A fake /api/chat that replays assistant messages and records requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        if callable(reply):
            reply = reply(body)
        return httpx.Response(200, json={"model": body["model"], "message": {"role": "assistant", **reply}, "done": True})


def call(name, arguments):
    return {"tool_calls": [{"function": {"name": name, "arguments": arguments}}], "content": ""}


def provider(script):
    return OllamaProvider("http://127.0.0.1:11434", "qwen-test", transport=httpx.MockTransport(script))


def test_agent_loop_captures_and_stops_at_permission(service, dispatcher, fl):
    snap = captured(service, fl)
    plan_holder = {}

    def apply_last(body):
        result = json.loads(body["messages"][-1]["content"])
        plan_holder.update(result["data"])
        return call("fls_apply_plan", {"plan_id": result["data"]["plan_id"],
                                       "expected_plan_hash": result["data"]["plan_hash"],
                                       "idempotency_key": str(uuid.uuid4())})

    script = ScriptedOllama([
        call("fls_get_capabilities", {}),
        call("fls_propose_patch", json.dumps({
            "snapshot_id": snap.snapshot_id, "rationale": "fix n10",
            "operations": [{"op": "note.update", "note_id": "n10", "set": {"pitch": 69}}]})),
        apply_last,
        {"content": "I proposed the change; please approve it in the FL Slacker UI."},
    ])
    loop = AgentLoop(provider(script), LocalToolClient(dispatcher, Actor("ollama", "agent")))
    transcript = loop.run("Fix the wrong note")
    assert transcript.state == "completed"
    assert [c["tool"] for c in transcript.tool_calls] == ["fls_get_capabilities", "fls_propose_patch", "fls_apply_plan"]
    assert transcript.tool_calls[-1]["code"] == "PERMISSION_DENIED"
    assert script.requests[0]["messages"][0]["content"] == system_prompt()
    assert script.requests[0]["stream"] is False and script.requests[0]["tools"]
    assert fl.notes()[10]["number"] == 70


def test_agent_loop_rejects_malformed_calls_without_dispatch(dispatcher, fl):
    script = ScriptedOllama([
        {"tool_calls": [{"function": {"name": "shell", "arguments": {"cmd": "rm -rf /"}}},
                        {"function": {"name": "fls_get_job", "arguments": "{not json"}},
                        {"nonsense": True}]},
        {"content": "done"},
    ])
    transcript = AgentLoop(provider(script), LocalToolClient(dispatcher, Actor("ollama", "agent"))).run("x")
    assert transcript.state == "completed"
    assert [c["code"] for c in transcript.tool_calls] == ["INVALID_ARGUMENT"] * 3
    journal = dispatcher.service.db.all("SELECT 1 FROM journal WHERE event='tool_call'")
    assert journal == []


def test_agent_loop_budgets_and_model_failures(dispatcher):
    client = LocalToolClient(dispatcher, Actor("ollama", "agent"))
    endless = ScriptedOllama([call("fls_get_capabilities", {})] * 10)
    transcript = AgentLoop(provider(endless), client, AgentLimits(max_rounds=3)).run("x")
    assert transcript.stop_reason == "max_rounds" and len(transcript.tool_calls) == 3
    many = ScriptedOllama([{"tool_calls": [{"function": {"name": "fls_get_capabilities", "arguments": {}}}] * 5}] * 2)
    transcript = AgentLoop(provider(many), client, AgentLimits(max_tool_calls=4)).run("x")
    assert transcript.stop_reason == "tool_call_budget_exhausted"
    ticks = iter(range(0, 1000, 70))
    slow = ScriptedOllama([call("fls_get_capabilities", {})] * 5)
    transcript = AgentLoop(provider(slow), client, AgentLimits(inference_budget_seconds=100),
                           clock=lambda: next(ticks)).run("x")
    assert transcript.stop_reason == "inference_budget_exhausted"
    missing = ScriptedOllama([httpx.Response(404, json={"error": "model 'qwen-test' not found"})])
    transcript = AgentLoop(provider(missing), client).run("x")
    assert transcript.state == "failed" and transcript.error["code"] == "MODEL_UNAVAILABLE"
    garbage = ScriptedOllama([httpx.Response(200, text="<html>")])
    assert AgentLoop(provider(garbage), client).run("x").error["code"] == "MODEL_UNAVAILABLE"


def test_agent_loop_allows_one_active_write_job(service, dispatcher, fl):
    from flslacker.contracts.models import GrantConstraints, GrantRequest
    from conftest import LOCAL_UI

    snap = captured(service, fl)
    service.issue_grant(LOCAL_UI, GrantRequest(session_id=service.session_id, constraints=GrantConstraints()))
    from flslacker.contracts.models import NoteSet, UpdateOp

    plans = [service.propose_patch(Actor("ollama", "agent"), snap.snapshot_id,
                                   [UpdateOp(op="note.update", note_id=note_id, set=NoteSet(pitch=pitch))], "x")
             for note_id, pitch in (("n0", 61), ("n1", 63))]

    def apply(plan):
        return call("fls_apply_plan", {"plan_id": plan.plan_id, "expected_plan_hash": plan.plan_hash,
                                       "idempotency_key": str(uuid.uuid4())})

    script = ScriptedOllama([apply(plans[0]), apply(plans[1]), {"content": "waiting"}])
    transcript = AgentLoop(provider(script), LocalToolClient(dispatcher, Actor("ollama", "agent"))).run("x")
    assert [c["code"] for c in transcript.tool_calls] == [None, "BUSY"]


def test_local_only_and_model_required():
    with pytest.raises(FlsError) as err:
        OllamaProvider("http://10.0.0.5:11434", "m")
    assert err.value.code.value == "PERMISSION_DENIED"
    with pytest.raises(FlsError):
        OllamaProvider("http://127.0.0.1:11434", "")
    OllamaProvider("http://10.0.0.5:11434", "m", local_only=False)
