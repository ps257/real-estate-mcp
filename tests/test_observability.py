"""No-network tests for Langfuse/OpenTelemetry instrumentation and data minimization."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from app import observability


class FakeSpan:
    def __init__(self, *, recording: bool = True) -> None:
        self.recording = recording
        self.attributes: dict[str, object] = {}
        self.statuses: list[object] = []

    def is_recording(self) -> bool:
        return self.recording

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status: object) -> None:
        self.statuses.append(status)


class FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class FakeObservationManager:
    def __init__(self, observation: FakeObservation) -> None:
        self.observation = observation
        self.exits: list[tuple] = []

    def __enter__(self) -> FakeObservation:
        return self.observation

    def __exit__(self, *args) -> None:
        self.exits.append(args)


@pytest.fixture(autouse=True)
def reset_observability_state():
    observability._reset_observability_for_tests()
    yield
    observability._reset_observability_for_tests()


def test_initialize_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(observability.config, "langfuse_enabled", lambda: False)
    assert observability.initialize_observability() is None
    assert observability.initialize_observability() is None


def test_initialize_is_noop_when_credentials_are_missing(monkeypatch):
    monkeypatch.setattr(observability.config, "langfuse_enabled", lambda: True)
    monkeypatch.setattr(observability.config, "langfuse_public_key", lambda: None)
    monkeypatch.setattr(observability.config, "langfuse_secret_key", lambda: None)
    assert observability.initialize_observability() is None


def test_redact_masks_nested_secrets_and_common_pii():
    raw = {
        "authorization": "Bearer top-secret",
        "nested": {
            "password": "do-not-send",
            "comment": "Email person@example.com, phone 0901234567, card 4111 1111 1111 1111",
        },
        "connection_string": "postgresql://user:password@example/db",
    }
    encoded = json.dumps(observability.redact(raw))
    for sensitive in (
        "top-secret",
        "do-not-send",
        "person@example.com",
        "0901234567",
        "4111 1111 1111 1111",
        "postgresql://",
    ):
        assert sensitive not in encoded


def test_booking_summary_never_contains_contact_values():
    summary = observability.summarize_input(
        "submit_booking",
        {
            "kind": "visit_booking",
            "project_id": "vhm:demo",
            "is_authenticated": False,
            "payload": {
                "full_name": "Nguyen Van A",
                "phone": "0901234567",
                "email": "person@example.com",
                "preferred_time": "2026-09-01T09:00:00+07:00",
                "note": "Call me at home",
            },
        },
    )
    encoded = json.dumps(summary)
    assert summary["payload_fields"] == [
        "email",
        "full_name",
        "note",
        "phone",
        "preferred_time",
    ]
    assert summary["has_contact_fields"] is True
    for sensitive in ("Nguyen Van A", "0901234567", "person@example.com", "Call me at home"):
        assert sensitive not in encoded


def test_tool_decorator_enriches_current_span_without_manual_observation(monkeypatch):
    span = FakeSpan()
    monkeypatch.setattr(observability.trace, "get_current_span", lambda: span)
    monkeypatch.setattr(
        observability,
        "get_observability_client",
        lambda: pytest.fail("tool enrichment must not create a Langfuse observation"),
    )
    monkeypatch.setattr(
        observability,
        "_request_metadata",
        lambda: {"message_id": "msg-123", "mcp_request_id": "req-456"},
    )

    @observability.observe_tool
    def search_demo(query: str) -> dict:
        return {"count": 2, "points": [{"large": "payload"}] * 2}

    result = search_demo("Vinhomes")

    assert result["count"] == 2
    assert span.attributes["langfuse.observation.type"] == "tool"
    assert span.attributes["langfuse.observation.metadata.tool_success"] == "true"
    assert span.attributes["langfuse.observation.metadata.message_id"] == "msg-123"
    output = json.loads(str(span.attributes["langfuse.observation.output"]))
    assert output == {
        "type": "object",
        "keys": ["count", "points"],
        "count": 2,
        "points_count": 2,
    }


def test_tool_decorator_preserves_exception_and_masks_error(monkeypatch):
    span = FakeSpan()
    monkeypatch.setattr(observability.trace, "get_current_span", lambda: span)
    failure = ToolError("bad email person@example.com and phone 0901234567")

    @observability.observe_tool
    def failing_tool() -> None:
        raise failure

    with pytest.raises(ToolError) as caught:
        failing_tool()

    assert caught.value is failure
    assert "person@example.com" not in str(caught.value)
    assert "0901234567" not in str(caught.value)
    status_message = str(span.attributes["langfuse.observation.status_message"])
    assert "person@example.com" not in status_message
    assert "0901234567" not in status_message
    assert span.attributes["langfuse.observation.level"] == "ERROR"
    assert span.attributes["langfuse.observation.metadata.tool_success"] == "false"


def test_service_observation_is_fail_open_when_sdk_start_fails(monkeypatch):
    class BrokenClient:
        def start_as_current_observation(self, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(observability, "get_observability_client", lambda: BrokenClient())

    @observability.observe_operation("db.demo", as_type="retriever")
    def operation(value: int) -> int:
        return value + 1

    assert operation(2) == 3


def test_service_observation_preserves_error_and_closes_without_raw_exception(monkeypatch):
    observation = FakeObservation()
    manager = FakeObservationManager(observation)
    calls: list[dict] = []

    class FakeClient:
        def start_as_current_observation(self, **kwargs):
            calls.append(kwargs)
            return manager

    monkeypatch.setattr(observability, "get_observability_client", lambda: FakeClient())
    failure = RuntimeError("database connection contained a secret")

    @observability.observe_operation("db.demo", as_type="retriever")
    def operation() -> None:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        operation()

    assert caught.value is failure
    assert calls[0]["as_type"] == "retriever"
    assert observation.updates == [
        {"level": "ERROR", "status_message": "RuntimeError: operation failed"}
    ]
    assert manager.exits == [(None, None, None)]


def test_request_metadata_accepts_only_bounded_correlation_ids(monkeypatch):
    from fastmcp.server import dependencies

    context = SimpleNamespace(
        request_id="req-123",
        request_context=SimpleNamespace(
            meta=SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "message_id": "msg-456",
                    "traceparent": "00-ignored-here",
                }
            )
        ),
    )
    monkeypatch.setattr(dependencies, "get_context", lambda: context)
    assert observability._request_metadata() == {
        "mcp_request_id": "req-123",
        "message_id": "msg-456",
    }

    context.request_context.meta = SimpleNamespace(
        model_dump=lambda **_kwargs: {"message_id": "contains whitespace and is rejected"}
    )
    assert observability._request_metadata() == {"mcp_request_id": "req-123"}


def test_export_mask_always_returns_patch_result_and_masks_json():
    params = SimpleNamespace(
        spans={
            "span-id": SimpleNamespace(
                attributes={
                    "langfuse.observation.input": json.dumps(
                        {"password": "secret-value", "email": "person@example.com"}
                    )
                }
            )
        }
    )
    result = observability._mask_otel_spans(params=params)
    patch = result.span_patches["span-id"]
    masked = patch.set_attributes["langfuse.observation.input"]
    assert "secret-value" not in masked
    assert "person@example.com" not in masked

    empty = observability._mask_otel_spans(
        params=SimpleNamespace(spans={"span-id": SimpleNamespace(attributes={"safe": 1})})
    )
    assert empty is not None
    assert empty.span_patches == {}
