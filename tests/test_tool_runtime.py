"""Tool dispatch policy, strict argument validation, and bounded observations."""

import json
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError

import pytest
from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, Field

from multi_agent_system.tool_runtime import ToolExecutionResult, dispatch_tool


@tool
def echo(text: str, count: int = 1) -> str:
    """Repeat the supplied text."""
    return text * count


def test_dispatch_validates_and_executes_authorized_tool():
    result = dispatch_tool("echo", {"text": "hello", "count": 2}, [echo])
    assert result.ok and result.executed
    assert result.data == "hellohello"
    assert result.elapsed_ms >= 0


@pytest.mark.parametrize("args", [None, "hello", ["hello"], {"text": 123}, {"text": "x", "count": "2"}, {"text": "x", "count": True}, {"text": "x", "extra": "unexpected"}])
def test_invalid_arguments_never_execute_handler(args, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid arguments reached the handler")
    monkeypatch.setattr(echo, "func", forbidden)
    result = dispatch_tool("echo", args, {"echo": echo})
    assert not result.ok and not result.executed
    assert result.error_code == "invalid_arguments"


def test_unknown_tool_does_not_execute():
    result = dispatch_tool("echo", {"text": "x"}, [])
    assert result.error_code == "tool_not_allowed"
    assert not result.executed


def test_injected_runtime_arguments_are_hidden_and_cannot_be_overridden():
    @tool
    def scoped(text: str, workspace_path: Annotated[str, InjectedToolArg] = "", timeout_seconds: Annotated[float, InjectedToolArg] = 30.0) -> str:
        """Echo text in the assigned scope."""
        return f"{text}:{workspace_path}:{timeout_seconds}"

    result = dispatch_tool("scoped", {"text": "ok"}, [scoped], context={"workspace_path": "trusted", "timeout_seconds": 1.0, "text": "ignored"})
    assert result.ok and result.data == "ok:trusted:1.0"
    assert "workspace_path" not in scoped.tool_call_schema.model_fields
    blocked = dispatch_tool("scoped", {"text": "ok", "workspace_path": "attacker"}, [scoped], context={"workspace_path": "trusted"})
    assert blocked.error_code == "invalid_arguments"
    assert not blocked.executed


def test_invalid_injected_arguments_are_reported_without_exposing_values():
    @tool
    def scoped(text: str, timeout_seconds: Annotated[float, InjectedToolArg] = 30.0) -> str:
        """Echo in the assigned scope."""
        return text
    result = dispatch_tool("scoped", {"text": "x"}, [scoped], context={"timeout_seconds": "secret-value"})
    assert result.error_code == "invalid_tool_context"
    assert not result.executed
    assert "secret-value" not in result.to_message()


def test_nested_extra_fields_are_rejected():
    class Payload(BaseModel):
        city: str
    @tool
    def nested(payload: Payload) -> str:
        """Read the nested city."""
        return payload.city
    result = dispatch_tool("nested", {"payload": {"city": "Paris", "hidden": True}}, [nested])
    assert result.error_code == "invalid_arguments"
    assert not result.executed


@pytest.mark.parametrize("discriminated", [False, True])
def test_union_cannot_authorize_extra_fields_using_wrong_branch(discriminated):
    class First(BaseModel):
        kind: Literal["a"]
        city: str

    class Second(BaseModel):
        kind: Literal["b"]
        city: str
        hidden: str

    payload_type = Annotated[First | Second, Field(discriminator="kind")] if discriminated else First | Second

    @tool
    def union_tool(payload: payload_type) -> str:
        """Read a tagged city payload."""
        return payload.city

    rejected = dispatch_tool("union_tool", {"payload": {"kind": "a", "city": "Paris", "hidden": "unexpected"}}, [union_tool])
    assert rejected.error_code == "invalid_arguments" and not rejected.executed
    accepted = dispatch_tool("union_tool", {"payload": {"kind": "b", "city": "Paris", "hidden": "declared"}}, [union_tool])
    assert accepted.ok and accepted.data == "Paris"


def test_typed_error_result_is_preserved_without_string_heuristics():
    @tool
    def typed() -> ToolExecutionResult:
        """Return a structured error."""
        return ToolExecutionResult(ok=False, error_code="provider_unavailable", message="provider busy", retryable=True)
    result = dispatch_tool("typed", {}, [typed])
    assert not result.ok and result.executed and result.retryable
    assert result.error_code == "provider_unavailable"
    legacy = dispatch_tool("echo", {"text": "执行报错 is quoted source text"}, [echo])
    assert legacy.ok


@pytest.mark.parametrize("error,code,retryable", [
    (TimeoutError("private endpoint"), "tool_timeout", True),
    (URLError("private endpoint"), "network_error", True),
    (HTTPError("private endpoint", 429, "busy", None, None), "http_error", True),
    (HTTPError("private endpoint", 401, "denied", None, None), "http_error", False),
    (RuntimeError("secret argument"), "tool_execution_error", False),
])
def test_handler_exceptions_are_structured_and_executed_once(error, code, retryable):
    calls = []
    @tool
    def broken() -> str:
        """Raise the test exception."""
        calls.append(True)
        raise error
    result = dispatch_tool("broken", {}, [broken])
    assert result.error_code == code and result.retryable == retryable
    assert result.executed and len(calls) == 1
    assert "private endpoint" not in result.to_message()
    assert "secret argument" not in result.to_message()


def test_large_result_is_bounded_for_storage_and_model_without_losing_error():
    @tool
    def large() -> ToolExecutionResult:
        """Return long diagnostic output."""
        return ToolExecutionResult(ok=False, error_code="execution_error", message="problem " * 3000, data={"stdout": "abc\n\"" * 3000}, provenance={"source": "long" * 3000})
    result = dispatch_tool("large", {}, [large], max_output_chars=800)
    stored = json.dumps(result.model_dump(), ensure_ascii=False, separators=(",", ":"))
    assert len(stored) <= 800
    assert result.truncated and result.executed and not result.ok
    assert result.error_code == "execution_error"
    model_payload = json.loads(result.to_message(512))
    assert len(result.to_message(512)) <= 512
    assert model_payload["error_code"] == "execution_error"
    assert model_payload["truncated"]
    assert "截断" in model_payload["message"]


def test_small_payload_retains_structured_data():
    result = ToolExecutionResult(ok=True, data={"temperature": 23.2}, provenance={"source": "weather"})
    assert result.bounded(1024).data == {"temperature": 23.2}
    assert not result.truncated


def test_tiny_budget_is_rejected_instead_of_producing_invalid_json():
    with pytest.raises(ValueError, match="max_chars"):
        ToolExecutionResult(ok=False, error_code="failure").to_message(10)
