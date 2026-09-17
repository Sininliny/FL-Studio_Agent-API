"""Loopback HTTP API and local UI (stdlib server).

Security: binds only to 127.0.0.1; every API route needs a bearer token (agent or UI)
or a UI session cookie; Host and Origin are validated; no CORS headers are ever sent;
tokens never appear in URLs or logs. Cookie-authenticated writes also need the
``X-FLS-CSRF`` header, which cross-origin pages cannot send without a preflight.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable
from urllib.parse import urlsplit

from pydantic import ValidationError

from flslacker import __version__
from flslacker.config import Credentials, Settings
from flslacker.contracts import models as m
from flslacker.contracts.canonical import PROTOCOL
from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.schema_export import schema_bundle
from flslacker.contracts.wire import MAX_MESSAGE_BYTES
from flslacker.service.core import LOCAL_UI, Actor, Service
from flslacker.service.dispatcher import Dispatcher
from flslacker.transports.client import safe_client_name

log = logging.getLogger("flslacker.http")

UI_FILES = {"/ui/": ("index.html", "text/html; charset=utf-8"),
            "/ui/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/ui/app.css": ("app.css", "text/css; charset=utf-8")}
COOKIE = "fls_ui"
SESSION_SECONDS = 12 * 3600
LOGIN_CODE_SECONDS = 300
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


class HttpError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class App:
    def __init__(self, service: Service, credentials: Credentials, settings: Settings, agents=None) -> None:
        self.service = service
        self.dispatcher = Dispatcher(service)
        self.credentials = credentials
        self.settings = settings
        self.agents = agents
        self.port = settings.port
        self.lock = threading.Lock()
        self.ui_sessions: dict[str, float] = {}
        self.login_codes: dict[str, float] = {}
        self.login_failures = 0
        self.routes: list[tuple[str, re.Pattern, Callable, str]] = []
        self._register()

    # ---------------------------------------------------------------- auth helpers
    def new_login_code(self) -> str:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        with self.lock:
            now = time.time()
            self.login_codes = {c: t for c, t in self.login_codes.items() if t > now}
            self.login_codes[code] = now + LOGIN_CODE_SECONDS
        return f"{code[:4]}-{code[4:]}"

    def redeem_login_code(self, code: str) -> str | None:
        normalized = re.sub(r"[^A-Z0-9]", "", str(code).upper())
        with self.lock:
            if self.login_failures >= 20:
                return None
            expiry = self.login_codes.pop(normalized, None)
            if expiry is None or expiry < time.time():
                self.login_failures += 1
                return None
            token = secrets.token_urlsafe(32)
            self.ui_sessions[token] = time.time() + SESSION_SECONDS
            return token

    def cookie_session(self, token: str | None) -> bool:
        if not token:
            return False
        with self.lock:
            expiry = self.ui_sessions.get(token)
            return expiry is not None and expiry > time.time()

    # ---------------------------------------------------------------- routes
    def route(self, method: str, pattern: str, role: str):
        def decorator(func):
            self.routes.append((method, re.compile(f"^{pattern}$"), func, role))
            return func
        return decorator

    def _register(self) -> None:
        r = self.route
        svc = self.service

        @r("GET", "/v1/health", "any")
        def health(ctx, match, body):
            return 200, {"ok": True, "protocol": PROTOCOL, "version": __version__,
                         "session_active": svc.session_id is not None}

        @r("GET", "/v1/capabilities", "any")
        def capabilities(ctx, match, body):
            return self.tool(ctx, "fls_get_capabilities", {})

        @r("POST", r"/v1/tools/(?P<name>[a-z_]{1,64})", "any")
        def tools(ctx, match, body):
            return self.tool(ctx, match["name"], body)

        @r("GET", rf"/v1/jobs/(?P<id>{UUID_RE})", "any")
        def job(ctx, match, body):
            return self.tool(ctx, "fls_get_job", {"job_id": match["id"]})

        @r("POST", rf"/v1/jobs/(?P<id>{UUID_RE})/cancel", "any")
        def cancel(ctx, match, body):
            return self.tool(ctx, "fls_cancel_job", {"job_id": match["id"]})

        @r("GET", "/v1/schema", "any")
        def schema(ctx, match, body):
            return 200, schema_bundle(f"http://127.0.0.1:{self.port}")

        @r("GET", "/v1/openapi.json", "any")
        def openapi(ctx, match, body):
            return 200, schema_bundle(f"http://127.0.0.1:{self.port}")["openapi"]

        # ------------------------------------------------------------ UI (role ui)
        @r("POST", "/v1/ui/login-code", "ui_token")
        def login_code(ctx, match, body):
            return 200, {"code": self.new_login_code(), "expires_in_seconds": LOGIN_CODE_SECONDS}

        @r("GET", "/v1/ui/state", "ui")
        def state(ctx, match, body):
            return 200, svc.ui_state()

        @r("GET", rf"/v1/ui/snapshots/(?P<id>{UUID_RE})", "ui")
        def snapshot(ctx, match, body):
            return 200, svc.get_snapshot(ctx, match["id"]).model_dump(mode="json")

        @r("GET", rf"/v1/ui/plans/(?P<id>{UUID_RE})", "ui")
        def plan(ctx, match, body):
            return 200, svc.preview_plan(ctx, match["id"]).model_dump(mode="json")

        @r("POST", rf"/v1/ui/plans/(?P<id>{UUID_RE})/approve", "ui")
        def approve(ctx, match, body):
            minutes = int((body or {}).get("lifetime_minutes", 10))
            return 200, svc.approve_plan(ctx, match["id"], max(1, min(minutes, 240))).model_dump(mode="json")

        @r("POST", rf"/v1/ui/plans/(?P<id>{UUID_RE})/apply", "ui")
        def approve_apply(ctx, match, body):
            return 202, svc.approve_and_apply(ctx, match["id"]).model_dump(mode="json")

        @r("POST", rf"/v1/ui/receipts/(?P<id>{UUID_RE})/restore-plan", "ui")
        def restore_plan(ctx, match, body):
            return 200, svc.prepare_restore(ctx, match["id"]).model_dump(mode="json")

        @r("POST", "/v1/ui/grants", "ui")
        def grant(ctx, match, body):
            request = m.GrantRequest.model_validate(body or {})
            return 200, svc.issue_grant(ctx, request).model_dump(mode="json")

        @r("POST", rf"/v1/ui/grants/(?P<id>{UUID_RE})/revoke", "ui")
        def revoke(ctx, match, body):
            return 200, svc.revoke_grant(ctx, match["id"]).model_dump(mode="json")

        @r("POST", rf"/v1/ui/jobs/(?P<id>{UUID_RE})/acknowledge", "ui")
        def acknowledge(ctx, match, body):
            note = str((body or {}).get("note", ""))[:200]
            return 200, svc.acknowledge_job(ctx, match["id"], note).model_dump(mode="json")

        @r("POST", r"/v1/ui/recipes/(?P<id>[a-z0-9_-]{1,40})/run", "ui")
        def run_recipe(ctx, match, body):
            body = body or {}
            label = body.get("target_label") or None
            if label is not None and (not isinstance(label, str) or len(label) > 120):
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "bad target label")
            return 202, svc.run_recipe(ctx, match["id"], body.get("session_id") or svc.session_id, label)

        @r("POST", "/v1/ui/session/new", "ui")
        def new_session(ctx, match, body):
            return 200, svc.invalidate_session("new session requested in the UI").model_dump(mode="json")

        @r("POST", "/v1/ui/agent", "ui")
        def agent_start(ctx, match, body):
            if self.agents is None:
                raise FlsError(ErrorCode.MODEL_UNAVAILABLE, "No Ollama model is configured (ollama_model).")
            prompt = (body or {}).get("prompt")
            if not isinstance(prompt, str) or not 0 < len(prompt) <= 4000:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "prompt must be 1-4000 characters")
            return 202, self.agents.start(prompt)

        @r("GET", r"/v1/ui/agent/(?P<id>[0-9a-f]{32})", "ui")
        def agent_get(ctx, match, body):
            if self.agents is None:
                raise FlsError(ErrorCode.MODEL_UNAVAILABLE, "No Ollama model is configured.")
            return 200, self.agents.get(match["id"])

    def tool(self, actor: Actor, name: str, body: Any) -> tuple[int, dict]:
        result, status = self.dispatcher.call(actor, name, body)
        return status, result

    # ---------------------------------------------------------------- request handling
    def authenticate(self, headers, cookie_token: str | None, role: str) -> tuple[Actor, bool]:
        """Returns (actor, via_cookie)."""
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if hmac.compare_digest(token.encode(), self.credentials.ui_token.encode()):
                return Actor("local-cli", "ui"), False
            if hmac.compare_digest(token.encode(), self.credentials.agent_token.encode()):
                if role in ("ui", "ui_token"):
                    raise HttpError(403, ErrorCode.PERMISSION_DENIED, "This endpoint is for the local UI only.")
                name = safe_client_name(headers.get("X-FLS-Client", "agent"))
                return Actor(f"agent:{name}", "agent"), False
            raise HttpError(401, ErrorCode.PERMISSION_DENIED, "Invalid token.")
        if role != "ui_token" and self.cookie_session(cookie_token):
            return LOCAL_UI, True
        raise HttpError(401, ErrorCode.PERMISSION_DENIED, "Authentication required.")

    def allowed_origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "flslacker"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            log.debug("%s %s", self.command, urlsplit(self.path).path)

        # ------------------------------------------------------------ plumbing
        def _send(self, status: int, payload: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _json(self, status: int, data: Any, extra: dict[str, str] | None = None) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json", extra)

        def _error(self, status: int, code: ErrorCode, message: str) -> None:
            self._json(status, {"ok": False, "request_id": None, "data": None,
                                "error": {"code": code.value, "message": message, "retryable": False,
                                          "details": {}, "required_action": None}})

        def _cookie(self) -> str | None:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return None

        def _check_request(self) -> None:
            if self.headers.get("Host", "") not in app.allowed_hosts():
                raise HttpError(421, ErrorCode.PERMISSION_DENIED, "Unexpected Host header.")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in app.allowed_origins():
                raise HttpError(403, ErrorCode.PERMISSION_DENIED, "Cross-origin requests are not allowed.")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise HttpError(403, ErrorCode.PERMISSION_DENIED, "Cross-site requests are not allowed.")

        def _body(self) -> Any:
            length = self.headers.get("Content-Length")
            if length is None:
                if self.headers.get("Transfer-Encoding"):
                    raise HttpError(411, ErrorCode.INVALID_ARGUMENT, "Content-Length is required.")
                return None
            try:
                size = int(length)
            except ValueError:
                raise HttpError(400, ErrorCode.INVALID_ARGUMENT, "Bad Content-Length.")
            if size > MAX_MESSAGE_BYTES:
                raise HttpError(413, ErrorCode.LIMIT_EXCEEDED, "Request body too large.")
            if size == 0:
                return None
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HttpError(415, ErrorCode.INVALID_ARGUMENT, "Content-Type must be application/json.")
            raw = self.rfile.read(size)
            try:
                return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            except (ValueError, UnicodeDecodeError):
                raise HttpError(400, ErrorCode.INVALID_ARGUMENT, "Body must be valid JSON.")

        # ------------------------------------------------------------ verbs
        def do_GET(self) -> None:
            self._handle("GET")

        def do_HEAD(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._error(405, ErrorCode.INVALID_ARGUMENT, "Method not allowed.")

        do_DELETE = do_PATCH = do_PUT

        def do_OPTIONS(self) -> None:
            self._error(405, ErrorCode.INVALID_ARGUMENT, "CORS is not supported.")

        def _handle(self, method: str) -> None:
            path = urlsplit(self.path).path
            try:
                self._check_request()
                if method == "GET" and path in ("/", "/ui"):
                    self._send(302, b"", "text/plain", {"Location": "/ui/"})
                    return
                if method == "GET" and path in UI_FILES:
                    name, ctype = UI_FILES[path]
                    payload = resources.files("flslacker").joinpath("ui", name).read_bytes()
                    self._send(200, payload, ctype)
                    return
                body = self._body() if method == "POST" else None
                if method == "POST" and path == "/v1/ui/login":
                    self._login(body)
                    return
                if method == "POST" and path == "/v1/ui/logout":
                    token = self._cookie()
                    with app.lock:
                        app.ui_sessions.pop(token or "", None)
                    self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
                    return
                for verb, pattern, func, role in app.routes:
                    match = pattern.match(path)
                    if verb != method or match is None:
                        continue
                    actor, via_cookie = app.authenticate(self.headers, self._cookie(), role)
                    if role == "ui" and not actor.is_ui:
                        raise HttpError(403, ErrorCode.PERMISSION_DENIED, "This endpoint is for the local UI only.")
                    if via_cookie and method == "POST":
                        if self.headers.get("X-FLS-CSRF") != "1" or self.headers.get("Origin") not in app.allowed_origins():
                            raise HttpError(403, ErrorCode.PERMISSION_DENIED, "Missing CSRF protection headers.")
                    status, data = func(actor, match, body)
                    self._json(status, data)
                    return
                raise HttpError(404, ErrorCode.INVALID_ARGUMENT, "Not found.")
            except HttpError as exc:
                self._error(exc.status, exc.code, exc.message)
            except FlsError as exc:
                self._json(exc.http_status, {"ok": False, "request_id": None, "data": None, "error": exc.to_dict()})
            except ValidationError as exc:
                self._error(400, ErrorCode.INVALID_ARGUMENT,
                            "Invalid request: " + "; ".join(e["msg"] for e in exc.errors(include_url=False)[:3]))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("request failed: %s %s", method, path)
                self._error(500, ErrorCode.OUTCOME_UNKNOWN, "Internal error.")

        def _login(self, body: Any) -> None:
            code = body.get("code") if isinstance(body, dict) else None
            if self.headers.get("X-FLS-CSRF") != "1":
                raise HttpError(403, ErrorCode.PERMISSION_DENIED, "Missing CSRF protection header.")
            token = app.redeem_login_code(code or "")
            if token is None:
                raise HttpError(401, ErrorCode.PERMISSION_DENIED, "Invalid or expired login code.")
            cookie = f"{COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict"
            self._json(200, {"ok": True}, {"Set-Cookie": cookie})

    return Handler


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            log.debug("client connection closed: %s", type(error).__name__)
            return
        log.exception("unhandled HTTP server error")


class Server:
    def __init__(self, app: App) -> None:
        if app.settings.host not in ("127.0.0.1",):
            raise ValueError("FL Slacker only binds to 127.0.0.1")
        self.app = app
        self.httpd = _HTTPServer((app.settings.host, app.settings.port), make_handler(app))
        self.port = self.httpd.server_address[1]
        app.port = self.port
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="flslacker-http", daemon=True)
        self.thread.start()

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)

