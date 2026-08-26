"""Fail-open Langfuse v4/OpenTelemetry instrumentation for the MCP service.

FastMCP already extracts W3C ``traceparent``/``tracestate`` from MCP request ``_meta`` and
creates one SERVER span around each tool execution. Tool decorators in this module enrich that
existing span; they deliberately do not create a second tool observation. Service decorators
create the semantic retriever/data child observations beneath it.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from threading import Lock
from typing import Any, Literal, ParamSpec, TypeVar

from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.telemetry import server_span
from fastmcp.tools import FunctionTool, ToolResult
from fastmcp.utilities.types import get_cached_typeadapter
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import SchemaValidator

from . import config

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")
ObservationType = Literal["span", "retriever"]

_MAX_DEPTH = 4
_MAX_ITEMS = 20
_MAX_STRING = 500
_REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "connection_string",
    "contact",
    "cookie",
    "email",
    "full_name",
    "note",
    "password",
    "phone",
    "preferred_time",
    "refresh_token",
    "secret",
    "secret_key",
    "service_role_key",
    "set_cookie",
    "token",
}
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_LANGFUSE_KEY_RE = re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9_-]+\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\s().-]*){8,15}(?!\w)")
_CONNECTION_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,;]+"
)
_SAFE_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")

_state_lock = Lock()
_initialization_attempted = False
_shutdown_done = False
_client: Any | None = None


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("password", "secret", "secret_key", "token")
    )


def _mask_string(value: str) -> str:
    masked = _CONNECTION_RE.sub(_REDACTED, value)
    masked = _BEARER_RE.sub(_REDACTED, masked)
    masked = _JWT_RE.sub(_REDACTED, masked)
    masked = _LANGFUSE_KEY_RE.sub(_REDACTED, masked)
    masked = _EMAIL_RE.sub(_REDACTED, masked)
    masked = _CARD_RE.sub(_REDACTED, masked)
    masked = _PHONE_RE.sub(_REDACTED, masked)
    masked = _SECRET_ASSIGNMENT_RE.sub(_REDACTED, masked)
    if len(masked) > _MAX_STRING:
        return f"{masked[:_MAX_STRING]}...[TRUNCATED]"
    return masked


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a bounded, JSON-safe copy with credentials and common PII removed."""
    if _depth >= _MAX_DEPTH:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, bytes):
        return f"[BINARY:{len(value)} bytes]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:_MAX_ITEMS]:
            text_key = str(key)[:100]
            result[text_key] = (
                _REDACTED if _is_sensitive_key(text_key) else redact(item, _depth=_depth + 1)
            )
        if len(items) > _MAX_ITEMS:
            result["_truncated_keys"] = len(items) - _MAX_ITEMS
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        result = [redact(item, _depth=_depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            result.append(f"[TRUNCATED:{len(items) - _MAX_ITEMS} items]")
        return result
    return _mask_string(str(value))


def summarize_input(operation_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Build useful but bounded input metadata, with stricter rules for sensitive tools."""
    if operation_name.endswith("submit_booking"):
        payload = arguments.get("payload")
        payload_fields = sorted(str(key) for key in payload) if isinstance(payload, Mapping) else []
        return {
            "kind": redact(arguments.get("kind")),
            "project_id": redact(arguments.get("project_id")),
            "is_authenticated": bool(arguments.get("is_authenticated", False)),
            "payload_fields": payload_fields[:_MAX_ITEMS],
            "has_contact_fields": any(
                field in payload_fields for field in ("full_name", "phone", "email")
            ),
            "has_note": "note" in payload_fields,
            "has_preferred_time": "preferred_time" in payload_fields,
        }
    if operation_name.endswith("calculate_commute_matrix"):
        origins = arguments.get("origins") or []
        destinations = arguments.get("destinations") or []
        return {
            "origin_count": len(origins) if isinstance(origins, Sequence) else 0,
            "destination_count": len(destinations) if isinstance(destinations, Sequence) else 0,
            "vehicle": redact(arguments.get("vehicle")),
        }
    if operation_name.startswith("osm."):
        summary: dict[str, Any] = {}
        for key in ("profile", "radius"):
            if key in arguments:
                summary[key] = redact(arguments[key])
        for key in ("origins", "destinations"):
            value = arguments.get(key)
            if isinstance(value, Sequence):
                summary[f"{key}_count"] = len(value)
        if "lat" in arguments or "origin_lat" in arguments:
            summary["coordinates_present"] = True
        return summary
    return redact(dict(arguments))


def summarize_output(value: Any) -> dict[str, Any]:
    """Summarize tool/service output without shipping listings, images, or contact data."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, Mapping):
        keys = sorted(str(key) for key in value)
        summary: dict[str, Any] = {"type": "object", "keys": keys[:_MAX_ITEMS]}
        for key in (
            "count",
            "duplicate_of_existing",
            "has_more",
            "matched",
            "offset",
            "persisted",
            "scope",
            "status",
            "total",
            "vehicle",
        ):
            if key in value and isinstance(value[key], (type(None), bool, int, float, str)):
                summary[key] = redact(value[key])
        for key in ("amenities", "candidates", "items", "listings", "matrix", "points"):
            if isinstance(value.get(key), Sequence):
                summary[f"{key}_count"] = len(value[key])
        return summary
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"type": "list", "count": len(value)}
    return {"type": type(value).__name__, "value": redact(value)}


def safe_error_message(exc: BaseException) -> str:
    """Keep validation errors useful while never exporting raw infrastructure errors."""
    if isinstance(exc, ToolError):
        return _mask_string(str(exc))
    return f"{type(exc).__name__}: operation failed"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _request_metadata() -> dict[str, str]:
    """Read safe correlation IDs without changing the MCP tool protocol or schema."""
    try:
        from fastmcp.server.dependencies import get_context

        context = get_context()
        request_context = context.request_context
        if request_context is None:
            return {}
        result: dict[str, str] = {}
        request_id = str(context.request_id)
        if _SAFE_CORRELATION_ID_RE.fullmatch(request_id):
            result["mcp_request_id"] = request_id
        meta = request_context.meta
        if meta is not None:
            raw_meta = meta.model_dump(exclude_none=True) if hasattr(meta, "model_dump") else dict(meta)
            message_id = raw_meta.get("message_id")
            if isinstance(message_id, str) and _SAFE_CORRELATION_ID_RE.fullmatch(message_id):
                result["message_id"] = message_id
        return result
    except (LookupError, RuntimeError, TypeError, ValueError, AttributeError):
        return {}


def _set_span_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    if not span.is_recording():
        return
    try:
        for key, value in attributes.items():
            span.set_attribute(key, value)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a tool request
        logger.warning("Unable to enrich telemetry span (%s)", type(exc).__name__)


class ToolArgumentPrivacyMiddleware(Middleware):
    """Reject invalid tool arguments before FastMCP logs or records their raw values.

    FastMCP normally validates inside its native SERVER span. Pydantic validation errors retain
    the original input, which can then reach warning logs and exception events before an export
    masking hook runs. This middleware uses the same callable argument schema without invoking
    the function. Valid calls continue through FastMCP unchanged. Invalid calls get one native
    FastMCP SERVER span containing only field names and validation error types.
    """

    def __init__(self) -> None:
        self._validators: dict[int, SchemaValidator] = {}

    def _validator_for(self, tool: FunctionTool) -> SchemaValidator:
        cache_key = id(tool)
        validator = self._validators.get(cache_key)
        if validator is not None:
            return validator

        callable_without_dependencies = without_injected_parameters(
            tool.fn,
            run_in_thread=tool.run_in_thread,
        )
        call_schema = get_cached_typeadapter(callable_without_dependencies).core_schema
        if call_schema.get("type") != "call" or "arguments_schema" not in call_schema:
            raise TypeError("FastMCP function tool has no callable arguments schema")
        validator = SchemaValidator(call_schema["arguments_schema"])
        if len(self._validators) >= 128:
            self._validators.clear()
        self._validators[cache_key] = validator
        return validator

    @staticmethod
    def _known_parameters(tool: FunctionTool) -> set[str]:
        properties = tool.parameters.get("properties", {})
        if not isinstance(properties, Mapping):
            return set()
        return {
            key
            for key in properties
            if isinstance(key, str) and _SAFE_PARAMETER_NAME_RE.fullmatch(key)
        }

    @staticmethod
    def _safe_issues(
        exc: PydanticValidationError,
        known_parameters: set[str],
    ) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[:10]:
            raw_location = error.get("loc", ())
            top_level = raw_location[0] if raw_location else None
            location = (
                top_level
                if isinstance(top_level, str) and top_level in known_parameters
                else "[unknown]"
            )
            error_type = re.sub(r"[^a-z0-9_.-]", "_", str(error.get("type", "invalid")))
            issues.append({"field": location, "type": error_type[:80]})
        return issues or [{"field": "[unknown]", "type": "invalid"}]

    @staticmethod
    def _safe_validation_message(tool_name: str, issues: Sequence[Mapping[str, str]]) -> str:
        fields = ", ".join(dict.fromkeys(issue["field"] for issue in issues[:5]))
        return f"Invalid arguments for tool {tool_name!r}: check {fields}"

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, ToolResult],
    ) -> ToolResult:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            return await call_next(context)

        tool_name = context.message.name
        tool = await fastmcp_context.fastmcp.get_tool(tool_name)
        if not isinstance(tool, FunctionTool):
            return await call_next(context)

        arguments = context.message.arguments or {}
        known_parameters = self._known_parameters(tool)
        try:
            self._validator_for(tool).validate_python(arguments)
        except PydanticValidationError as exc:
            issues = self._safe_issues(exc, known_parameters)
        except Exception as exc:  # noqa: BLE001 - preserve service availability on SDK drift
            logger.warning(
                "Tool privacy prevalidation unavailable for %r (%s)",
                tool_name,
                type(exc).__name__,
            )
            return await call_next(context)
        else:
            return await call_next(context)

        safe_message = self._safe_validation_message(tool_name, issues)
        safe_argument_keys = sorted(
            key for key in arguments if isinstance(key, str) and key in known_parameters
        )
        if len(safe_argument_keys) != len(arguments):
            safe_argument_keys.append("[unknown]")
        logger.warning("Rejected invalid arguments for tool %r", tool_name)
        with server_span(
            f"tools/call {tool_name}",
            "tools/call",
            fastmcp_context.fastmcp.name,
            "tool",
            tool_name,
            tool_name=tool_name,
        ) as span:
            span.set_attributes(tool.get_span_attributes())
            metadata: dict[str, Any] = {
                "langfuse.observation.type": "tool",
                "langfuse.observation.input": _json(
                    {
                        "argument_keys": safe_argument_keys[:_MAX_ITEMS],
                        "validation_issues": issues,
                    }
                ),
                "langfuse.observation.output": _json(
                    {"is_error": True, "error_type": "argument_validation"}
                ),
                "langfuse.observation.level": "ERROR",
                "langfuse.observation.status_message": safe_message,
                "langfuse.observation.metadata.service": "real-estate-mcp",
                "langfuse.observation.metadata.tool_name": tool_name,
                "langfuse.observation.metadata.tool_success": "false",
                "langfuse.observation.metadata.validation_error_count": str(len(issues)),
                "error.type": "argument_validation",
            }
            for key, value in _request_metadata().items():
                metadata[f"langfuse.observation.metadata.{key}"] = value
            _set_span_attributes(span, metadata)
            span.set_status(Status(StatusCode.ERROR, safe_message))

        # Raise only after the native span is closed. The span already has ERROR status and no
        # exception events; FastMCP therefore cannot attach an immutable stacktrace event later.
        # This fresh exception also has no raw Pydantic cause/context.
        raise ValidationError(safe_message, log_level=logging.WARNING)


def observe_tool(function: Callable[P, R]) -> Callable[P, R]:
    """Enrich FastMCP's current native SERVER span; do not create another tool span."""
    function_signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        span = trace.get_current_span()
        if not span.is_recording():
            return function(*args, **kwargs)

        try:
            arguments = function_signature.bind_partial(*args, **kwargs).arguments
        except TypeError:
            arguments = {"args_count": len(args), "kwargs": sorted(kwargs)}
        tool_name = function.__name__
        metadata = {
            "langfuse.observation.type": "tool",
            "langfuse.observation.input": _json(summarize_input(tool_name, arguments)),
            "langfuse.observation.metadata.service": "real-estate-mcp",
            "langfuse.observation.metadata.tool_name": tool_name,
            "langfuse.observation.metadata.tool_success": "false",
        }
        for key, value in _request_metadata().items():
            metadata[f"langfuse.observation.metadata.{key}"] = value
        _set_span_attributes(span, metadata)

        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            safe_message = safe_error_message(exc)
            _set_span_attributes(
                span,
                {
                    "langfuse.observation.level": "ERROR",
                    "langfuse.observation.status_message": safe_message,
                    "langfuse.observation.metadata.tool_success": "false",
                },
            )
            try:
                span.set_status(Status(StatusCode.ERROR, safe_message))
            except Exception as status_exc:  # noqa: BLE001 - fail-open telemetry boundary
                logger.debug("Unable to set telemetry status (%s)", type(status_exc).__name__)
            try:
                # FastMCP's outer native span records the exception after this wrapper re-raises
                # it. Sanitize the same instance so its event/status cannot bypass export masking.
                exc.args = (safe_message,)
            except Exception as sanitize_exc:  # noqa: BLE001 - best-effort security boundary
                logger.debug(
                    "Unable to sanitize telemetry exception (%s)", type(sanitize_exc).__name__
                )
            raise

        _set_span_attributes(
            span,
            {
                "langfuse.observation.output": _json(summarize_output(result)),
                "langfuse.observation.metadata.tool_success": "true",
            },
        )
        return result

    return wrapped


def _mask_export_attribute(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _json(redact(json.loads(value)))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return _mask_string(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        masked = [_mask_export_attribute(item) for item in value]
        return tuple(masked) if isinstance(value, tuple) else masked
    return value


def _mask_otel_spans(*, params: Any) -> Any:
    """Langfuse v4 export-stage defense for native FastMCP and Langfuse spans."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches: dict[Any, Any] = {}
    for identifier, span in params.spans.items():
        replacements: dict[str, Any] = {}
        for key, value in span.attributes.items():
            masked = _REDACTED if _is_sensitive_key(key.rsplit(".", 1)[-1]) else _mask_export_attribute(value)
            if masked != value:
                replacements[key] = masked
        if replacements:
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)
    return MaskOtelSpansResult(span_patches=patches)


def initialize_observability() -> Any | None:
    """Initialize exactly one Langfuse v4 client and global OTel provider, fail-open."""
    global _client, _initialization_attempted

    if _initialization_attempted:
        return _client
    with _state_lock:
        if _initialization_attempted:
            return _client
        _initialization_attempted = True

        if not config.langfuse_enabled():
            return None
        public_key = config.langfuse_public_key()
        secret_key = config.langfuse_secret_key()
        if not public_key or not secret_key:
            logger.warning("Langfuse tracing is enabled but credentials are incomplete; tracing disabled")
            return None
        try:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=config.langfuse_base_url(),
                environment=config.langfuse_environment(),
                tracing_enabled=True,
                mask_otel_spans=_mask_otel_spans,
            )
        except Exception as exc:  # noqa: BLE001 - initialization must be fail-open
            logger.warning("Langfuse initialization failed (%s); tracing disabled", type(exc).__name__)
            _client = None
        return _client


def get_observability_client() -> Any | None:
    return initialize_observability()


def shutdown_observability() -> None:
    """Flush pending telemetry on process shutdown without affecting server shutdown."""
    global _shutdown_done

    if _shutdown_done:
        return
    with _state_lock:
        if _shutdown_done:
            return
        _shutdown_done = True
        if _client is None:
            return
        try:
            _client.shutdown()
        except Exception as exc:  # noqa: BLE001 - shutdown must be fail-open
            logger.warning("Langfuse shutdown failed (%s)", type(exc).__name__)


def _close_observation(manager: Any) -> None:
    try:
        # Close as a completed context after setting a sanitized ERROR level ourselves. Passing
        # the raw exception to the SDK would duplicate exception events and could expose PII.
        manager.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break request cleanup
        logger.warning("Unable to close telemetry observation (%s)", type(exc).__name__)


def observe_operation(
    name: str,
    *,
    as_type: ObservationType = "span",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Create one fail-open Langfuse child observation for a service operation."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        function_signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            client = get_observability_client()
            if client is None:
                return function(*args, **kwargs)
            try:
                arguments = function_signature.bind_partial(*args, **kwargs).arguments
                manager = client.start_as_current_observation(
                    name=name,
                    as_type=as_type,
                    input=summarize_input(name, arguments),
                    metadata={"service": "real-estate-mcp"},
                )
                observation = manager.__enter__()
            except Exception as exc:  # noqa: BLE001 - instrumentation must be fail-open
                logger.warning("Unable to start telemetry observation (%s)", type(exc).__name__)
                return function(*args, **kwargs)

            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                try:
                    observation.update(level="ERROR", status_message=safe_error_message(exc))
                except Exception as update_exc:  # noqa: BLE001 - fail-open telemetry boundary
                    logger.debug(
                        "Unable to update telemetry error (%s)", type(update_exc).__name__
                    )
                _close_observation(manager)
                raise

            try:
                observation.update(output=summarize_output(result))
            except Exception as update_exc:  # noqa: BLE001 - fail-open telemetry boundary
                logger.debug("Unable to update telemetry output (%s)", type(update_exc).__name__)
            _close_observation(manager)
            return result

        return wrapped

    return decorate


def mark_current_observation_error(exc: BaseException) -> None:
    """Mark a degraded child operation whose business fallback intentionally swallows errors."""
    safe_message = safe_error_message(exc)
    span = trace.get_current_span()
    _set_span_attributes(
        span,
        {
            "langfuse.observation.level": "ERROR",
            "langfuse.observation.status_message": safe_message,
        },
    )
    client = get_observability_client()
    if client is not None:
        try:
            client.update_current_span(level="ERROR", status_message=safe_message)
        except Exception as update_exc:  # noqa: BLE001 - fail-open telemetry boundary
            logger.debug("Unable to mark degraded telemetry (%s)", type(update_exc).__name__)
    logger.warning("Upstream operation degraded (%s)", type(exc).__name__)


def _reset_observability_for_tests() -> None:
    """Reset process-global state; intended only for isolated unit tests."""
    global _client, _initialization_attempted, _shutdown_done

    _client = None
    _initialization_attempted = False
    _shutdown_done = False
