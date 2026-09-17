"""Machine-readable contracts: JSON Schemas, tool definitions and OpenAPI, all from the models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from flslacker import __version__
from flslacker.contracts import models as m
from flslacker.contracts.canonical import PROTOCOL
from flslacker.contracts.tools import TOOLS

EXPORTED_MODELS = (
    m.CapabilitiesView,
    m.Capability,
    m.SessionInfo,
    m.Target,
    m.Snapshot,
    m.Note,
    m.Analysis,
    m.AnalysisParameters,
    m.Plan,
    m.PlanPreview,
    m.Job,
    m.JobView,
    m.Receipt,
    m.Grant,
    m.GrantRequest,
    m.Recipe,
    m.ProjectSummary,
    m.ErrorInfo,
    m.UpdateOp,
    m.InsertOp,
    m.DeleteOp,
)


def model_schemas() -> dict[str, Any]:
    return {model.__name__: model.model_json_schema() for model in EXPORTED_MODELS}


def _error_response(description: str) -> dict[str, Any]:
    return {"description": description,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}}


def openapi(server_url: str) -> dict[str, Any]:
    tool_models = []
    for tool in TOOLS:
        tool_models.append((tool.input_model, "validation"))
        tool_models.append((m.ToolResult[tool.output_model], "serialization"))
    tool_models.append((m.ToolResult[m.NoArgs], "serialization"))
    refs, defs = models_json_schema(tool_models, ref_template="#/components/schemas/{model}")
    components = dict(defs.get("$defs", {}))

    def ref(model: Any, mode: str = "serialization") -> dict[str, str]:
        return dict(refs[(model, mode)])

    components["ErrorEnvelope"] = ref(m.ToolResult[m.NoArgs])

    errors = {
        "400": _error_response("INVALID_ARGUMENT / UNSUPPORTED_CAPABILITY"),
        "401": _error_response("Missing or invalid bearer token"),
        "403": _error_response("PERMISSION_DENIED"),
        "409": _error_response("STALE_SNAPSHOT, TARGET_MISMATCH, BUSY, USER_ACTION_REQUIRED, conflicts"),
        "410": _error_response("EXPIRED"),
        "413": _error_response("LIMIT_EXCEEDED"),
        "503": _error_response("ADAPTER_OFFLINE / MODEL_UNAVAILABLE"),
    }
    paths: dict[str, Any] = {
        "/v1/health": {"get": {"summary": "Minimal process health", "responses": {"200": {"description": "OK"}}}},
        "/v1/capabilities": {"get": {
            "summary": "Tested session capabilities",
            "responses": {"200": {"description": "Capabilities", "content": {"application/json": {"schema": ref(m.ToolResult[m.CapabilitiesView])}}}, **errors},
        }},
        "/v1/schema": {"get": {"summary": "Machine-readable contracts", "responses": {"200": {"description": "Bundle"}}}},
        "/v1/jobs/{job_id}": {"get": {
            "summary": "Job status and required user action",
            "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}],
            "responses": {"200": {"description": "Job", "content": {"application/json": {"schema": ref(m.ToolResult[m.JobView])}}}, **errors},
        }},
        "/v1/jobs/{job_id}/cancel": {"post": {
            "summary": "Cancel queued work (owning client)",
            "parameters": [{"name": "job_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}],
            "responses": {"202": {"description": "Cancellation status"}, **errors},
        }},
    }
    for tool in TOOLS:
        ok = "202" if tool.accepted_job else "200"
        paths[f"/v1/tools/{tool.name}"] = {"post": {
            "operationId": tool.name,
            "summary": tool.description,
            "x-permission": tool.permission,
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": ref(tool.input_model, "validation")}}},
            "responses": {ok: {"description": "Result envelope", "content": {"application/json": {"schema": ref(m.ToolResult[tool.output_model])}}}, **errors},
        }}
    return {
        "openapi": "3.1.0",
        "info": {"title": "FL Slacker companion API", "version": __version__,
                 "description": f"Protocol {PROTOCOL}. Loopback only; bearer token required."},
        "servers": [{"url": server_url}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {"schemas": components,
                       "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    }


def schema_bundle(server_url: str = "http://127.0.0.1:8765") -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "version": __version__,
        "tools": [tool.definition() for tool in TOOLS],
        "models": model_schemas(),
        "openapi": openapi(server_url),
    }


def export(directory: Path) -> list[Path]:
    directory = Path(directory) / PROTOCOL.replace("/", "-")
    directory.mkdir(parents=True, exist_ok=True)
    bundle = schema_bundle()
    written = []

    def write(name: str, data: Any) -> None:
        path = directory / name
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        written.append(path)

    write("tools.json", bundle["tools"])
    write("openapi.json", bundle["openapi"])
    for name, schema in bundle["models"].items():
        write(f"{name}.json", schema)
    return written
