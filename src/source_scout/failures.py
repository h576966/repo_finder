from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

ERROR_SCHEMA_VERSION = "source-scout-error-v1"


@dataclass(frozen=True)
class FailureDetails:
    error_type: str
    stage: str
    message: str
    retryable: bool
    status_code: int | None = None
    schema_version: str = ERROR_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.status_code is None:
            result.pop("status_code")
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def failure_from_exception(exc: Exception, *, stage: str) -> FailureDetails:
    from . import deepseek
    from .fastcontext_types import FastContextError

    if isinstance(exc, deepseek.ModelConfigurationError):
        return FailureDetails("configuration", stage, str(exc), retryable=False)
    if isinstance(exc, deepseek.ModelTimeoutError) or isinstance(exc, TimeoutError):
        return FailureDetails("timeout", stage, "The model operation timed out.", retryable=True)
    if isinstance(exc, deepseek.ModelConnectionError):
        return FailureDetails("connection", stage, str(exc), retryable=True)
    if isinstance(exc, deepseek.ModelStatusError):
        return FailureDetails(
            _http_error_type(exc.status_code),
            stage,
            str(exc),
            retryable=_http_retryable(exc.status_code),
            status_code=exc.status_code,
        )
    if isinstance(exc, deepseek.ModelResponseError):
        return FailureDetails("model_response", stage, str(exc), retryable=False)
    if isinstance(exc, FastContextError) or isinstance(exc, ValueError):
        return FailureDetails("validation", stage, str(exc), retryable=False)
    if isinstance(exc, OSError):
        return FailureDetails("filesystem", stage, str(exc), retryable=False)
    if isinstance(exc, deepseek.ModelError):
        return FailureDetails("model_response", stage, str(exc), retryable=False)
    return FailureDetails("internal", stage, "Source Scout failed unexpectedly.", retryable=False)


def _http_error_type(status_code: int) -> str:
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 402:
        return "billing"
    if status_code == 429:
        return "rate_limit"
    if status_code >= 500:
        return "service"
    return "http"


def _http_retryable(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500
