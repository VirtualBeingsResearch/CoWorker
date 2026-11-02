from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

import httpx

from coworker.core.exceptions import ModelNotSupportedError, ProviderError, ProviderNotFoundError
from coworker.i18n import tr


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONTEXT_LENGTH = "context_length"
    CONTENT_POLICY = "content_policy"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


class ProviderCallError(ProviderError):
    """A safe, structured failure. Never copy provider bodies or credentials into logs."""

    def __init__(
        self,
        category: FailureCategory,
        *,
        status_code: int | None = None,
        vendor_code: str = "",
        connection_scope: bool = False,
        retry_after: float | None = None,
        request_id: str = "",
        usage: Mapping[str, Any] | None = None,
        usage_model: str = "",
    ) -> None:
        self.category = category
        self.status_code = status_code
        self.vendor_code = vendor_code
        self.connection_scope = connection_scope
        self.retry_after = retry_after
        self.request_id = request_id
        # Keep accounting separate from safe health/error metadata. Only known
        # numeric counters may cross this boundary, never an SDK body or prompt.
        self.usage = _known_usage(usage)
        self.usage_model = usage_model if self.usage is not None else ""
        super().__init__(
            tr(
                "brain.routing.failure",
                category=category.value,
                code=vendor_code or status_code or "-",
            )
        )

    @property
    def retryable(self) -> bool:
        return self.category in {FailureCategory.TRANSIENT, FailureCategory.INVALID_RESPONSE}

    @property
    def allow_fallback(self) -> bool:
        return self.category not in {
            FailureCategory.CONTENT_POLICY,
            FailureCategory.CONTEXT_LENGTH,
            FailureCategory.INVALID_REQUEST,
        }

    @property
    def blocked(self) -> bool:
        return self.category in {
            FailureCategory.AUTHENTICATION,
            FailureCategory.PERMISSION,
            FailureCategory.MODEL_UNAVAILABLE,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "status_code": self.status_code,
            "vendor_code": self.vendor_code,
            "scope": "connection" if self.connection_scope else "model",
            "retry_after": self.retry_after,
            "request_id": self.request_id,
        }


class ModelRoutingUnavailable(ProviderError):
    def __init__(self, failures: list[dict[str, Any]], retry_after: float = 30) -> None:
        self.failures = failures
        self.retry_after = max(1, retry_after)
        super().__init__(tr("brain.routing.unavailable"))


class ModelPreferencePending(ProviderError):
    def __init__(self) -> None:
        super().__init__(tr("brain.routing.preference_pending"))


def _known_usage(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    if usage is None:
        return None
    counters = {
        key: value
        for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
        if isinstance(value := usage.get(key), int) and not isinstance(value, bool) and value >= 0
    }
    return counters if {"input_tokens", "output_tokens"} <= counters.keys() else None


@contextmanager
def capture_response_usage(response: Any) -> Iterator[None]:
    """Preserve measured usage when a returned response is rejected locally."""
    try:
        yield
    except ProviderCallError as error:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        details = getattr(usage, "input_tokens_details", None)
        if input_tokens is None:
            input_tokens = getattr(usage, "prompt_tokens", None)
        if output_tokens is None:
            output_tokens = getattr(usage, "completion_tokens", None)
        if details is None:
            details = getattr(usage, "prompt_tokens_details", None)
        output_details = getattr(usage, "completion_tokens_details", None) or getattr(usage, "output_tokens_details", None)
        reasoning_tokens = getattr(output_details, "reasoning_tokens", None)
        if reasoning_tokens is None:
            reasoning_tokens = getattr(output_details, "thinking_tokens", None)
        known = _known_usage({
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": getattr(details, "cached_tokens", None)
            if details is not None else getattr(usage, "cache_read_input_tokens", None),
            "reasoning_tokens": reasoning_tokens,
        })
        if known is not None:
            error.usage = known
            model = getattr(response, "model", "")
            error.usage_model = model if isinstance(model, str) else ""
        raise


def _safe_identifier(value: Any) -> str:
    value = str(value) if isinstance(value, (str, int)) else ""
    return value if re.fullmatch(r"[\w.:-]{1,120}", value) else ""


def _delay(value: Any, now: float, *, absolute: bool = False) -> float | None:
    if (
        isinstance(value, (int, float))
        or isinstance(value, str)
        and re.fullmatch(r"\d+(?:\.\d+)?", value)
    ):
        number = float(value)
        if absolute:
            number = number / 1000 if number > 1e12 else number
            number -= now
        return max(0, number) if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # A timestamp without a timezone is ambiguous. Do not guess a quota reset.
            if timestamp.tzinfo is not None:
                return max(0, timestamp.timestamp() - now)
        except ValueError:
            pass
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is not None:
                return max(0, timestamp.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def normalize_provider_error(
    error: Exception,
    provider_type: str = "",
    *,
    now: float | None = None,
) -> ProviderCallError | None:
    """Classify SDK metadata, including legacy ProviderError exception chains.

    Unrelated programming errors are deliberately not turned into provider outages.
    Vendor codes take precedence over HTTP status (notably Zhipu's many 429 variants).
    """
    if isinstance(error, ProviderCallError):
        return error
    if isinstance(error, ProviderNotFoundError):
        return ProviderCallError(FailureCategory.MODEL_UNAVAILABLE)
    if isinstance(error, ModelNotSupportedError):
        return ProviderCallError(FailureCategory.MODEL_UNAVAILABLE)
    original = error
    sdk_error: Exception | None = None
    seen: set[int] = set()
    while error.__cause__ is not None and id(error) not in seen:
        # Preserve the nearest HTTP/SDK envelope instead of throwing away its
        # status/body when it also chains a lower-level transport exception.
        if isinstance(getattr(error, "status_code", None), int) or isinstance(
            getattr(error, "response", None), httpx.Response
        ):
            break
        if isinstance(error, (httpx.HTTPError, TimeoutError, ConnectionError)):
            break
        if type(error).__module__.split(".")[0] in {"openai", "anthropic"} and sdk_error is None:
            sdk_error = error
        seen.add(id(error))
        if not isinstance(error.__cause__, Exception):
            break
        error = error.__cause__
        if isinstance(error, ProviderCallError):
            return error
    if (
        sdk_error is not None
        and not isinstance(error, (httpx.HTTPError, TimeoutError, ConnectionError))
        and type(error).__module__.split(".")[0] not in {"openai", "anthropic"}
    ):
        error = sdk_error
    is_sdk = type(error).__module__.split(".")[0] in {"openai", "anthropic"}
    if (
        not isinstance(original, ProviderError)
        and not is_sdk
        and not isinstance(error, (httpx.HTTPError, TimeoutError, ConnectionError))
    ):
        return None
    now = datetime.now(UTC).timestamp() if now is None else now
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) else None
    body = getattr(error, "body", None)
    if not isinstance(body, dict) and isinstance(response, httpx.Response):
        try:
            body = response.json()
        except ValueError:
            body = None
    envelope = body if isinstance(body, dict) else {}
    detail = envelope.get("error", envelope)
    detail = detail if isinstance(detail, dict) else {}
    code = _safe_identifier(detail.get("code") or detail.get("type") or getattr(error, "code", ""))
    headers = getattr(response, "headers", {})
    headers = headers if isinstance(headers, Mapping) else {}
    retry_after = _delay(headers.get("retry-after"), now)
    for source in (detail, envelope):
        for key in ("reset_at", "next_flush_time"):
            reset = _delay(source.get(key), now, absolute=True)
            if reset is not None:
                retry_after = max(retry_after or 0, reset)
        retry = _delay(source.get("retry_after"), now)
        if retry is not None:
            retry_after = max(retry_after or 0, retry)
    request_id = _safe_identifier(
        getattr(error, "request_id", "") or headers.get("x-request-id") or headers.get("request-id")
    )
    category = FailureCategory.UNKNOWN
    connection_scope = False
    if (
        code in {"content_policy_violation", "content_filter", "sensitive", "refusal"}
        or provider_type in {"zhipu", "opencode-go"}
        and code == "1301"
    ):
        category = FailureCategory.CONTENT_POLICY
    elif (
        code
        in {"context_length_exceeded", "model_context_window_exceeded", "context_window_exceeded"}
        or provider_type in {"zhipu", "opencode-go"}
        and code == "1261"
    ):
        category = FailureCategory.CONTEXT_LENGTH
    elif code in {"api_key_expired", "invalid_api_key", "authentication_error"}:
        category, connection_scope = FailureCategory.AUTHENTICATION, True
    elif code in {
        "insufficient_quota",
        "insufficient_balance",
        "credit_balance_exhausted",
        "organization_usage_limit_exceeded",
    }:
        category, connection_scope = FailureCategory.QUOTA, True
    elif code in {"model_not_found", "model_not_supported"}:
        category = FailureCategory.MODEL_UNAVAILABLE
    elif code in {"invalid_request_error", "invalid_parameter", "invalid_parameters"}:
        category = FailureCategory.INVALID_REQUEST
    elif code in {"rate_limit_exceeded", "rate_limit_error"}:
        category = FailureCategory.RATE_LIMIT
    elif code in {"server_error", "api_error", "overloaded_error"}:
        category = FailureCategory.TRANSIENT
    elif provider_type == "zhipu" and code in {
        "1301",
        "1261",
        "1302",
        "1305",
        "1308",
        "1309",
        "1310",
        "1311",
        "1313",
        "1314",
        "1315",
        "1316",
        "1317",
        "1318",
        "1319",
        "1320",
        "1321",
        "1113",
        "1211",
        "1220",
    }:
        if code == "1301":
            category = FailureCategory.CONTENT_POLICY
        elif code == "1261":
            category = FailureCategory.CONTEXT_LENGTH
        elif code == "1302":
            category, connection_scope = FailureCategory.RATE_LIMIT, True
        elif code == "1305":
            category = FailureCategory.TRANSIENT
        elif code in {"1113", "1308", "1310", "1316", "1317", "1318", "1319", "1320", "1321"}:
            category, connection_scope = FailureCategory.QUOTA, True
        elif code in {"1309", "1313", "1314", "1315"}:
            category, connection_scope = FailureCategory.PERMISSION, True
        elif code in {"1311", "1220"}:
            category = FailureCategory.PERMISSION
        else:
            category = FailureCategory.MODEL_UNAVAILABLE
    elif status == 401:
        category, connection_scope = FailureCategory.AUTHENTICATION, True
    elif status == 402:
        category, connection_scope = FailureCategory.QUOTA, True
    elif status == 403:
        category = FailureCategory.PERMISSION
    elif status == 404:
        category = FailureCategory.MODEL_UNAVAILABLE
    elif status == 429:
        category = FailureCategory.RATE_LIMIT
    elif status in {408, 409} or status is not None and status >= 500:
        category = FailureCategory.TRANSIENT
    elif status in {400, 413, 422}:
        category = FailureCategory.INVALID_REQUEST
    elif (
        isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError))
        or is_sdk
        and status is None
    ):
        category = FailureCategory.TRANSIENT
        connection_scope = isinstance(
            error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, ConnectionError)
        )
    return ProviderCallError(
        category,
        status_code=status,
        vendor_code=code,
        connection_scope=connection_scope,
        retry_after=retry_after,
        request_id=request_id,
    )


def check_response_finish(reason: Any) -> None:
    """A successful HTTP response may still report a policy or inference failure."""
    if not isinstance(reason, str):
        return
    if reason in {"content_filter", "sensitive", "refusal"}:
        raise ProviderCallError(FailureCategory.CONTENT_POLICY)
    if reason == "model_context_window_exceeded":
        raise ProviderCallError(FailureCategory.CONTEXT_LENGTH)
    if reason in {"network_error", "error"}:
        raise ProviderCallError(FailureCategory.TRANSIENT)


def response_failure(detail: Any, provider_type: str) -> ProviderCallError:
    if hasattr(detail, "model_dump"):
        detail = detail.model_dump()
    error = ProviderError()
    setattr(error, "body", {"error": detail if isinstance(detail, dict) else {}})
    failure = normalize_provider_error(error, provider_type)
    return (
        failure
        if failure is not None and failure.category != FailureCategory.UNKNOWN
        else ProviderCallError(FailureCategory.INVALID_RESPONSE)
    )
