"""In-memory FastMCP/OTel integration checks isolated from the pytest process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_probe(probe: str) -> subprocess.CompletedProcess[str]:
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    source_path = str(repository / "src")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else source_path
    )
    environment["LANGFUSE_ENABLED"] = "false"
    environment["OTEL_SDK_DISABLED"] = "false"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, (
        f"FastMCP/OTel probe failed.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return completed


def test_native_server_tool_span_is_enriched_once_without_duplicate_observation():
    """The custom decorator must enrich FastMCP's SERVER span, not start another one."""
    probe = r'''
import asyncio
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

from fastmcp import Client, FastMCP

from app.observability import ToolArgumentPrivacyMiddleware, observe_tool

server = FastMCP(
    "observability-probe",
    middleware=[ToolArgumentPrivacyMiddleware()],
)


@server.tool
@observe_tool
def ping(value: int) -> dict:
    return {"count": value}


async def main() -> None:
    # Passing the server object selects FastMCP's in-memory transport; no socket or network.
    async with Client(server) as client:
        await client.call_tool("ping", {"value": 2})

    spans = exporter.get_finished_spans()
    native_server_tool_spans = [
        span
        for span in spans
        if span.kind is SpanKind.SERVER and span.attributes.get("gen_ai.tool.name") == "ping"
    ]
    enriched_server_tool_spans = [
        span
        for span in spans
        if span.kind is SpanKind.SERVER
        and span.attributes.get("langfuse.observation.type") == "tool"
    ]

    assert len(native_server_tool_spans) == 1, [
        (span.name, span.kind.name, dict(span.attributes)) for span in spans
    ]
    assert enriched_server_tool_spans == native_server_tool_spans

    tool_span = native_server_tool_spans[0]
    assert tool_span.attributes["langfuse.observation.type"] == "tool"
    assert tool_span.attributes["langfuse.observation.metadata.tool_success"] == "true"
    assert json.loads(tool_span.attributes["langfuse.observation.input"]) == {"value": 2}
    assert json.loads(tool_span.attributes["langfuse.observation.output"]) == {
        "type": "object",
        "keys": ["count"],
        "count": 2,
    }

    print(json.dumps({"native_server_tool_spans": len(native_server_tool_spans)}))


asyncio.run(main())
'''

    completed = _run_probe(probe)
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {"native_server_tool_spans": 1}


def test_invalid_booking_arguments_never_reach_server_span_or_logs_as_pii():
    """Prevalidation must sanitize before FastMCP's logger and exception recorder run."""
    probe = r'''
import asyncio
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

from fastmcp import Client

from app.server import mcp

SENSITIVE_VALUES = (
    "PRIVACY_SENTINEL_NAME",
    "privacy-sentinel@example.invalid",
    "090987654321",
    "PRIVACY_SENTINEL_NOTE",
)
invalid_payload = " | ".join(SENSITIVE_VALUES)
arguments = {
    "kind": "visit_booking",
    "project_id": "vhm:privacy-probe",
    "payload": invalid_payload,
}


def serialize_span(span) -> str:
    return json.dumps(
        {
            "name": span.name,
            "kind": span.kind.name,
            "attributes": dict(span.attributes),
            "status": {
                "code": span.status.status_code.name,
                "description": span.status.description,
            },
            "events": [
                {"name": event.name, "attributes": dict(event.attributes)}
                for event in span.events
            ],
        },
        ensure_ascii=False,
        default=str,
    )


async def main() -> None:
    tracer = trace.get_tracer("privacy-probe")
    async with Client(mcp) as client:
        with tracer.start_as_current_span("agent.root") as root:
            root_trace_id = root.get_span_context().trace_id
            result = await client.call_tool(
                "submit_booking",
                arguments,
                raise_on_error=False,
            )

    spans = exporter.get_finished_spans()
    server_tool_spans = [
        span
        for span in spans
        if span.kind is SpanKind.SERVER
        and span.attributes.get("gen_ai.tool.name") == "submit_booking"
    ]
    assert len(server_tool_spans) == 1, [
        (span.name, span.kind.name, dict(span.attributes)) for span in spans
    ]

    tool_span = server_tool_spans[0]
    serialized = serialize_span(tool_span)
    for sensitive in SENSITIVE_VALUES:
        assert sensitive not in serialized, serialized

    assert result.is_error is True
    assert tool_span.context.trace_id == root_trace_id
    assert tool_span.parent is not None
    assert tool_span.status.status_code is StatusCode.ERROR
    assert not tool_span.events
    assert tool_span.attributes["langfuse.observation.type"] == "tool"
    assert tool_span.attributes["langfuse.observation.level"] == "ERROR"
    assert tool_span.attributes["langfuse.observation.metadata.tool_success"] == "false"
    assert json.loads(tool_span.attributes["langfuse.observation.input"]) == {
        "argument_keys": ["kind", "payload", "project_id"],
        "validation_issues": [{"field": "payload", "type": "dict_type"}],
    }

    print(
        json.dumps(
            {
                "native_server_tool_spans": len(server_tool_spans),
                "is_error": result.is_error,
                "w3c_trace_preserved": tool_span.context.trace_id == root_trace_id,
            }
        )
    )


asyncio.run(main())
'''

    completed = _run_probe(probe)
    combined_output = f"{completed.stdout}\n{completed.stderr}"
    for sensitive in (
        "PRIVACY_SENTINEL_NAME",
        "privacy-sentinel@example.invalid",
        "090987654321",
        "PRIVACY_SENTINEL_NOTE",
    ):
        assert sensitive not in combined_output
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {
        "native_server_tool_spans": 1,
        "is_error": True,
        "w3c_trace_preserved": True,
    }


def test_invalid_unknown_argument_name_is_replaced_before_span_or_log_output():
    """Unknown argument keys are untrusted data and must never become validation metadata."""
    probe = r'''
import asyncio
import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

from fastmcp import Client

from app.server import mcp

UNKNOWN_KEY = "privacy-unknown@example.invalid"
UNKNOWN_VALUE = "PRIVACY_UNKNOWN_VALUE_0901122334"
arguments = {
    "kind": "visit_booking",
    "project_id": "vhm:privacy-probe",
    "payload": {},
    UNKNOWN_KEY: UNKNOWN_VALUE,
}


def serialize_span(span) -> str:
    return json.dumps(
        {
            "name": span.name,
            "kind": span.kind.name,
            "attributes": dict(span.attributes),
            "status": {
                "code": span.status.status_code.name,
                "description": span.status.description,
            },
            "events": [
                {"name": event.name, "attributes": dict(event.attributes)}
                for event in span.events
            ],
        },
        ensure_ascii=False,
        default=str,
    )


async def main() -> None:
    tracer = trace.get_tracer("unknown-key-privacy-probe")
    async with Client(mcp) as client:
        with tracer.start_as_current_span("agent.root") as root:
            root_trace_id = root.get_span_context().trace_id
            result = await client.call_tool(
                "submit_booking",
                arguments,
                raise_on_error=False,
            )

    spans = exporter.get_finished_spans()
    server_tool_spans = [
        span
        for span in spans
        if span.kind is SpanKind.SERVER
        and span.attributes.get("gen_ai.tool.name") == "submit_booking"
    ]
    assert len(server_tool_spans) == 1, [
        (span.name, span.kind.name, dict(span.attributes)) for span in spans
    ]

    tool_span = server_tool_spans[0]
    serialized = serialize_span(tool_span)
    assert UNKNOWN_KEY not in serialized, serialized
    assert UNKNOWN_VALUE not in serialized, serialized

    assert result.is_error is True
    assert tool_span.context.trace_id == root_trace_id
    assert tool_span.parent is not None
    assert tool_span.status.status_code is StatusCode.ERROR
    assert not tool_span.events
    assert tool_span.attributes["langfuse.observation.type"] == "tool"
    assert tool_span.attributes["langfuse.observation.level"] == "ERROR"
    assert tool_span.attributes["langfuse.observation.metadata.tool_success"] == "false"
    assert json.loads(tool_span.attributes["langfuse.observation.input"]) == {
        "argument_keys": ["kind", "payload", "project_id", "[unknown]"],
        "validation_issues": [
            {"field": "[unknown]", "type": "unexpected_keyword_argument"}
        ],
    }

    print(
        json.dumps(
            {
                "native_server_tool_spans": len(server_tool_spans),
                "is_error": result.is_error,
                "w3c_trace_preserved": tool_span.context.trace_id == root_trace_id,
                "events": len(tool_span.events),
            }
        )
    )


asyncio.run(main())
'''

    completed = _run_probe(probe)
    combined_output = f"{completed.stdout}\n{completed.stderr}"
    assert "privacy-unknown@example.invalid" not in combined_output
    assert "PRIVACY_UNKNOWN_VALUE_0901122334" not in combined_output
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {
        "native_server_tool_spans": 1,
        "is_error": True,
        "w3c_trace_preserved": True,
        "events": 0,
    }
