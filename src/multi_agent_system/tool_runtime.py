"""Validated tool dispatch and bounded, machine-readable tool observations.

The caller owns resource accounting and retries. This module invokes an
authorized handler at most once and reports whether that invocation happened.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError


DEFAULT_MAX_OUTPUT_CHARS = 12_000
_TRUNCATED = "[工具输出已截断]"

# Tool Message 工具执行的结果
class ToolExecutionResult(BaseModel):
    """A tool's observation; execution status never depends on output wording."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Any = None
    error_code: str | None = Field(default=None, max_length=128)
    message: str = ""
    retryable: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: float = Field(default=0.0, ge=0)
    executed: bool = False
    truncated: bool = False

    def _json(self) -> str:
        return json.dumps(
            self.model_dump(), ensure_ascii=False, separators=(",", ":"), default=str
        )

    def bounded(self, max_chars: int = DEFAULT_MAX_OUTPUT_CHARS) -> ToolExecutionResult:
        """Bound serialized storage size, keeping errors and truncation explicit.

        Small structured data is preserved. Oversized data becomes a text
        preview; it must not be interpreted as a complete structured response.
        """
        if max_chars < 256:
            raise ValueError("max_chars must be at least 256")
        if len(self._json()) <= max_chars:
            return self.model_copy()

        result = self.model_copy(update={
            "data": None, "message": _TRUNCATED, "provenance": {}, "truncated": True,
        })
        if len(result._json()) > max_chars:
            raise ValueError("max_chars is too small to preserve the tool status")

        def fill(field: str, value: str, limit: int = max_chars) -> None:
            low, high = 0, len(value)
            while low < high:
                middle = (low + high + 1) // 2
                preview = value[:middle]
                if field == "message":
                    preview += "\n" + _TRUNCATED
                candidate = result.model_copy(update={field: preview})
                if len(candidate._json()) <= limit:
                    low = middle
                else:
                    high = middle - 1
            if low:
                preview = value[:low]
                setattr(result, field, preview + "\n" + _TRUNCATED if field == "message" else preview)

        # Retain a short useful diagnostic before filling the remaining space.
        if self.message:
            fill("message", self.message[: max_chars // 4])
        candidate = result.model_copy(update={"provenance": self.provenance})
        if len(candidate._json()) <= max_chars // 2:
            result = candidate
        if self.data is not None:
            candidate = result.model_copy(update={"data": self.data})
            if len(candidate._json()) <= max_chars:
                result = candidate
            else:
                preview = self.data if isinstance(self.data, str) else json.dumps(
                    self.data, ensure_ascii=False, separators=(",", ":"), default=str
                )
                fill("data", preview)
        return result

    def to_message(self, max_chars: int = DEFAULT_MAX_OUTPUT_CHARS) -> str:
        """Return a valid JSON envelope suitable for ``ToolMessage.content``."""
        return self.bounded(max_chars)._json()


def _matching_json_type(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    if "$ref" in schema:
        resolved: Any = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        return _matching_json_type(value, resolved, root)
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    alternatives = schema.get("anyOf", schema.get("oneOf", []))
    if alternatives:
        return any(_matching_json_type(value, branch, root) for branch in alternatives)
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or any(key not in value for key in schema.get("required", [])):
            return False
        # Respect literal discriminators before selecting a union branch. A
        # different branch's field list must not authorize ignored parameters.
        return all(_matching_json_type(value[key], child, root)
                   for key, child in schema.get("properties", {}).items() if key in value)
    if kind == "array":
        return isinstance(value, (list, tuple))
    if kind == "null":
        return value is None
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind in ("integer", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _reject_extra_arguments(
    value: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "arguments"
) -> None:
    """Forbid undeclared model fields, including nested Pydantic objects.

    Pydantic's default extra policy silently ignores unknown keys. Walking its
    JSON schema avoids relying on the newer model_validate(extra=...) API while
    preserving explicitly declared open dictionaries.
    """
    if "$ref" in schema:
        resolved: Any = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        _reject_extra_arguments(value, resolved, root, path)
        return
    alternatives = schema.get("anyOf", schema.get("oneOf", []))
    if alternatives:
        errors = []
        for branch in alternatives:
            if not _matching_json_type(value, branch, root):
                continue
            try:
                _reject_extra_arguments(value, branch, root, path)
                return
            except ValueError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
    if isinstance(value, dict):
        properties = schema.get("properties")
        if properties is not None:
            unknown = set(value) - set(properties)
            if unknown:
                raise ValueError(f"{path}: undeclared fields")
            for key, item in value.items():
                _reject_extra_arguments(item, properties[key], root, f"{path}.{key}")
        elif isinstance(schema.get("additionalProperties"), dict):
            for item in value.values():
                _reject_extra_arguments(item, schema["additionalProperties"], root, path)
    elif isinstance(value, (list, tuple)) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _reject_extra_arguments(item, schema["items"], root, f"{path}[{index}]")


def _validate(schema: type[BaseModel], values: dict[str, Any]) -> dict[str, Any]:
    json_schema = schema.model_json_schema()
    _reject_extra_arguments(values, json_schema, json_schema)
    return schema.model_validate(values, strict=True).model_dump()

# 工具执行的统一入口
def dispatch_tool(
    name: str,
    args: Any,
    available_tools: Mapping[str, BaseTool] | Sequence[BaseTool],
    *,
    context: Mapping[str, Any] | None = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> ToolExecutionResult:
    """Validate and invoke a tool from the caller's already authorized set.

    Model arguments use ``tool_call_schema``. Host context may fill only fields
    omitted from that schema (e.g. ``InjectedToolArg``), and cannot overwrite
    model-visible parameters. There are no hidden retries or budget increments.
    Legacy string results are treated as successful data, never heuristically
    classified by Chinese or English diagnostic words.
    """
    started = time.perf_counter()
    executed = False

    def finish(result: ToolExecutionResult) -> ToolExecutionResult:
        return result.model_copy(update={
            "executed": executed,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }).bounded(max_output_chars)

    registry = dict(available_tools) if isinstance(available_tools, Mapping) else {
        item.name: item for item in available_tools
    }
    selected = registry.get(name)
    if selected is None:
        return finish(ToolExecutionResult(ok=False, error_code="tool_not_allowed", message="工具不在当前任务的授权集合中。"))
    if not isinstance(args, dict):
        return finish(ToolExecutionResult(ok=False, error_code="invalid_arguments", message="工具参数必须是 JSON 对象。"))

    visible_schema = selected.tool_call_schema
    full_schema = selected.args_schema
    if not isinstance(visible_schema, type) or not issubclass(visible_schema, BaseModel):
        return finish(ToolExecutionResult(ok=False, error_code="invalid_tool_schema", message="工具需要可验证的 Pydantic 参数模型。"))
    try:
        validated = _validate(visible_schema, args)
    except (ValidationError, ValueError, TypeError):
        return finish(ToolExecutionResult(ok=False, error_code="invalid_arguments", message="工具参数缺失、类型不匹配或包含未声明字段，请按工具 Schema 修正。"))

    if isinstance(full_schema, type) and issubclass(full_schema, BaseModel):
        hidden_fields = set(full_schema.model_fields) - set(visible_schema.model_fields)
        validated.update({key: value for key, value in (context or {}).items() if key in hidden_fields})
        try:
            validated = _validate(full_schema, validated)
        except (ValidationError, ValueError, TypeError):
            return finish(ToolExecutionResult(ok=False, error_code="invalid_tool_context", message="宿主提供的工具运行参数无效。"))

    try:
        executed = True
        raw_result = selected.invoke(validated)
        result = raw_result if isinstance(raw_result, ToolExecutionResult) else ToolExecutionResult(ok=True, data=raw_result)
    except HTTPError as exc:
        result = ToolExecutionResult(ok=False, error_code="http_error", message=f"上游服务返回 HTTP {exc.code}。", retryable=exc.code == 429 or 500 <= exc.code < 600)
    except TimeoutError:
        result = ToolExecutionResult(ok=False, error_code="tool_timeout", message="工具调用超时。", retryable=True)
    except URLError as exc:
        timed_out = isinstance(exc.reason, TimeoutError)
        result = ToolExecutionResult(ok=False, error_code="tool_timeout" if timed_out else "network_error", message="工具网络请求失败。", retryable=True)
    except Exception as exc:
        result = ToolExecutionResult(ok=False, error_code="tool_execution_error", message=f"工具执行失败（{type(exc).__name__}）。")
    return finish(result)
