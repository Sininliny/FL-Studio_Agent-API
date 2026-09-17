"""Loopback HTTP API: authentication, Host/Origin checks, CSRF, status codes and the UI login."""

from __future__ import annotations

import socket
import uuid

import httpx
import pytest

from flslacker.config import Credentials
from flslacker.transports.http import App, Server

from conftest import captured


@pytest.fixture
def server(service):
    service.settings.port = 0
    creds = Credentials.load_or_create(service.settings)
    app = App(service, creds, service.settings)
    srv = Server(app)
    srv.start()
    yield srv, creds
    srv.stop()


def client(srv, token=None, **headers):
    base = {"Host": f"127.0.0.1:{srv.port}"}
    if token:
        base["Authorization"] = f"Bearer {token}"
    base.update(headers)
    return httpx.Client(base_url=f"http://127.0.0.1:{srv.port}", headers=base, trust_env=False, timeout=10)


def test_auth_host_origin(server):
    srv, creds = server
    with client(srv) as c:
        assert c.get("/v1/health").status_code == 401
    with client(srv, "wrong") as c:
        assert c.get("/v1/health").status_code == 401
    with client(srv, creds.agent_token) as c:
        body = c.get("/v1/health").json()
        assert body["ok"] and body["session_active"]
        assert c.get("/v1/health", headers={"Host": "evil.example:80"}).status_code == 421
        assert c.get("/v1/health", headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.get("/v1/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        response = c.options("/v1/health")
        assert response.status_code == 405
        assert "access-control-allow-origin" not in {k.lower() for k in response.headers}
        assert c.get("/v1/ui/state").status_code == 403
        assert c.post("/v1/ui/login-code").status_code == 403
        assert c.post("/v1/tools/fls_get_capabilities", content="{}",
                      headers={"Content-Type": "text/plain"}).status_code == 415
    assert raw_status(srv, creds.agent_token, 5 * 1024 * 1024) == 413


def raw_status(srv, token, declared_length):
    """Send headers that announce an oversized body (httpx refuses to fake Content-Length)."""
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
        lines = [
            "POST /v1/tools/fls_get_capabilities HTTP/1.1",
            f"Host: 127.0.0.1:{srv.port}",
            f"Authorization: Bearer {token}",
            "Content-Type: application/json",
            f"Content-Length: {declared_length}",
            "Connection: close",
            "",
            "{}",
        ]
        sock.sendall("\r\n".join(lines).encode())
        return int(sock.recv(64).split()[1])


def test_tools_and_jobs(server, service, fl):
    srv, creds = server
    with client(srv, creds.agent_token, **{"X-FLS-Client": "unit test!"}) as c:
        caps = c.get("/v1/capabilities").json()
        assert caps["ok"] and caps["data"]["protocol"] == "flslacker/1"
        response = c.post("/v1/tools/fls_capture_score", json={"session_id": service.session_id, "scope": "selected"})
        assert response.status_code == 202
        job = response.json()["data"]
        assert job["created_by"] == "agent:unit-test-"
        assert c.get(f"/v1/jobs/{job['job_id']}").json()["data"]["job"]["state"] == "awaiting_fl_action"
        cancelled = c.post(f"/v1/jobs/{job['job_id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["data"]["job"]["state"] == "cancelled"
        missing = c.get(f"/v1/jobs/{uuid.uuid4()}")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "INVALID_ARGUMENT"
        schema = c.get("/v1/schema").json()
        assert {t["name"] for t in schema["tools"]} >= {"fls_apply_plan", "fls_restore_edit"}
        openapi = c.get("/v1/openapi.json").json()
        assert "/v1/tools/fls_apply_plan" in openapi["paths"]
        assert "token" not in str(c.get("/v1/health").headers).lower()


def test_ui_login_and_csrf(server, service, fl):
    srv, creds = server
    with client(srv, creds.ui_token) as c:
        code = c.post("/v1/ui/login-code").json()["code"]
        assert c.get("/v1/ui/state").status_code == 200  # UI token is also a UI principal
    origin = f"http://127.0.0.1:{srv.port}"
    with client(srv) as browser:
        assert browser.get("/ui/").status_code == 200
        assert "Content-Security-Policy" in browser.get("/ui/app.js").headers
        assert browser.post("/v1/ui/login", json={"code": code}).status_code == 403  # no CSRF header
        assert browser.post("/v1/ui/login", json={"code": "AAAA-BBBB"}, headers={"X-FLS-CSRF": "1"}).status_code == 401
        response = browser.post("/v1/ui/login", json={"code": code.lower()}, headers={"X-FLS-CSRF": "1"})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=Strict" in response.headers["set-cookie"]
        assert browser.post("/v1/ui/login", json={"code": code}, headers={"X-FLS-CSRF": "1"}).status_code == 401
        state = browser.get("/v1/ui/state")
        assert state.status_code == 200 and state.json()["capabilities"]["active_session"]
        body = {"session_id": service.session_id, "scope": "selected"}
        assert browser.post("/v1/tools/fls_capture_score", json=body).status_code == 403
        assert browser.post("/v1/tools/fls_capture_score", json=body, headers={"X-FLS-CSRF": "1"}).status_code == 403
        ok = browser.post("/v1/tools/fls_capture_score", json=body, headers={"X-FLS-CSRF": "1", "Origin": origin})
        assert ok.status_code == 202 and ok.json()["data"]["created_by"] == "local-ui"
        grant = browser.post("/v1/ui/grants", json={"session_id": service.session_id},
                             headers={"X-FLS-CSRF": "1", "Origin": origin})
        assert grant.status_code == 200 and grant.json()["kind"] == "recipe"
        bad = browser.post("/v1/ui/grants", json={"session_id": service.session_id, "constraints": {"fields": ["x"]}},
                           headers={"X-FLS-CSRF": "1", "Origin": origin})
        assert bad.status_code == 400
        browser.post("/v1/ui/logout", headers={"X-FLS-CSRF": "1", "Origin": origin})
        assert browser.get("/v1/ui/state").status_code == 401


def test_agent_cannot_reach_ui_endpoints(server, service, fl):
    srv, creds = server
    snap = captured(service, fl)
    with client(srv, creds.agent_token) as c:
        plan = c.post("/v1/tools/fls_propose_patch", json={
            "snapshot_id": snap.snapshot_id, "rationale": "x",
            "operations": [{"op": "note.update", "note_id": "n0", "set": {"pitch": 61}}]}).json()["data"]
        assert c.post(f"/v1/ui/plans/{plan['plan_id']}/approve").status_code == 403
        assert c.post("/v1/ui/grants", json={"session_id": service.session_id}).status_code == 403
        denied = c.post("/v1/tools/fls_apply_plan", json={
            "plan_id": plan["plan_id"], "expected_plan_hash": plan["plan_hash"], "idempotency_key": str(uuid.uuid4())})
        assert denied.status_code == 403
        assert denied.json()["error"]["required_action"]["where"] == "companion_ui"
