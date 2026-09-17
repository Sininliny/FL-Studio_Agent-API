"""Structured error codes shared by every transport."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    ADAPTER_OFFLINE = "ADAPTER_OFFLINE"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    BUSY = "BUSY"
    EXPIRED = "EXPIRED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PARTIAL_APPLY = "PARTIAL_APPLY"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.UNSUPPORTED_CAPABILITY: 400,
    ErrorCode.ADAPTER_OFFLINE: 503,
    ErrorCode.USER_ACTION_REQUIRED: 409,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.STALE_SNAPSHOT: 409,
    ErrorCode.TARGET_MISMATCH: 409,
    ErrorCode.BUSY: 409,
    ErrorCode.EXPIRED: 410,
    ErrorCode.LIMIT_EXCEEDED: 413,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.PARTIAL_APPLY: 409,
    ErrorCode.OUTCOME_UNKNOWN: 409,
    ErrorCode.VERIFICATION_FAILED: 409,
}

RETRYABLE = {
    ErrorCode.ADAPTER_OFFLINE,
    ErrorCode.USER_ACTION_REQUIRED,
    ErrorCode.BUSY,
    ErrorCode.MODEL_UNAVAILABLE,
    ErrorCode.STALE_SNAPSHOT,
}


class FlsError(Exception):
    """An expected failure with a stable code; never carries secrets or paths."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        required_action: dict[str, Any] | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.details = details or {}
        self.required_action = required_action
        self.retryable = self.code in RETRYABLE if retryable is None else retryable
        self.http_status = http_status or HTTP_STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "required_action": self.required_action,
        }


def not_found(kind: str, identifier: str) -> FlsError:
    return FlsError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown {kind}.",
        details={"reason": "not_found", "kind": kind, "id": str(identifier)[:64]},
        http_status=404,
    )
