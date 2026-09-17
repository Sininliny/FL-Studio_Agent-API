"""One dispatcher for HTTP, MCP and the Ollama agent loop."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from flslacker.contracts import models as m
from flslacker.contracts.errors import ErrorCode, FlsError
from flslacker.contracts.tools import TOOLS_BY_NAME
from flslacker.service.core import Actor, Service

log = logging.getLogger("flslacker.dispatcher")


def _validation_details(exc: ValidationError) -> dict[str, Any]:
    return {
        "errors": [
            {"loc": [str(p) for p in e["loc"]], "msg": e["msg"][:200], "type": e["type"]}
            for e in exc.errors(include_url=False, include_input=False)[:10]
        ]
    }


class Dispatcher:
    def __init__(self, service: Service) -> None:
        self.service = service
        s = service
        self.handlers: dict[str, Callable[[Actor, Any], BaseModel]] = {
            "fls_get_capabilities": lambda a, x: s.get_capabilities(a),
            "fls_get_project_summary": lambda a, x: s.get_project_summary(a, x.session_id),
            "fls_capture_score": lambda a, x: s.capture_score(a, x.session_id, x.scope, x.target_label),
            "fls_get_snapshot": lambda a, x: s.get_snapshot(a, x.snapshot_id, x.include_notes),
            "fls_analyze_score": lambda a, x: s.analyze_score(a, x.snapshot_id, x.analyses, x.parameters),
            "fls_propose_patch": lambda a, x: s.propose_patch(a, x.snapshot_id, x.operations, x.rationale),
            "fls_preview_plan": lambda a, x: s.preview_plan(a, x.plan_id),
            "fls_apply_plan": lambda a, x: s.apply_plan(a, x.plan_id, x.expected_plan_hash, x.idempotency_key),
            "fls_get_job": lambda a, x: s.get_job(a, x.job_id),
            "fls_cancel_job": lambda a, x: s.cancel_job(a, x.job_id),
            "fls_restore_edit": lambda a, x: s.restore_edit(a, x.receipt_id, x.idempotency_key),
        }
        assert set(self.handlers) == set(TOOLS_BY_NAME)

    def call(self, actor: Actor, tool_name: str, arguments: Any) -> tuple[dict[str, Any], int]:
        """Validate and run one tool. Returns (result envelope, HTTP status)."""
        request_id = str(uuid.uuid4())
        spec = TOOLS_BY_NAME.get(tool_name)
        status = 200
        try:
            if spec is None:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "Unknown tool.", details={"tool": str(tool_name)[:64]},
                               http_status=404)
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "Tool arguments must be a JSON object.")
            try:
                args = spec.input_model.model_validate(arguments)
            except ValidationError as exc:
                raise FlsError(ErrorCode.INVALID_ARGUMENT, "Invalid tool arguments.", details=_validation_details(exc))
            data = self.handlers[tool_name](actor, args)
            envelope = m.ToolResult[spec.output_model](ok=True, request_id=request_id, data=data)
            if spec.accepted_job:
                status = 202
        except FlsError as exc:
            envelope = m.ToolResult[m.NoArgs](ok=False, request_id=request_id, error=m.ErrorInfo(**exc.to_dict()))
            status = exc.http_status
        except Exception:
            log.exception("tool %s failed", tool_name)
            error = m.ErrorInfo(
                code=ErrorCode.OUTCOME_UNKNOWN.value,
                message="Internal error. Do not assume the request had any effect; check fls_get_job or recapture.",
                retryable=False,
            )
            envelope = m.ToolResult[m.NoArgs](ok=False, request_id=request_id, error=error)
            status = 500
        result = envelope.model_dump(mode="json")
        try:
            self.service.db.journal(
                "tool_call",
                {"tool": str(tool_name)[:64], "ok": result["ok"],
                 "code": None if result["ok"] else result["error"]["code"], "request_id": request_id},
                actor=actor.name,
            )
        except Exception:
            log.exception("journal write failed")
        return result, status
