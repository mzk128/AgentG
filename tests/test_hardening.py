"""Regression coverage for memory boundaries and task-level tool capabilities."""
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from multi_agent_system.agents import (
    OrchestrationPlan, SubTaskSpec, _normalise_subtasks,
    prepare_simple_task_node, research_worker_node, reviewer_node, code_worker_node,
    task_router_node, TaskRoutingDecision,
)
from multi_agent_system.graph import dispatch_workers, prepare_batch_node, initialize_resources_node


def test_explicit_empty_tools_stay_empty():
    plan = OrchestrationPlan(summary="explain", subtasks=[
        SubTaskSpec(id="code", agent_type="code", objective="Explain Python", allowed_tools=[]),
    ])
    tasks, _ = _normalise_subtasks(plan)
    assert tasks[0]["allowed_tools"] == []


def test_missing_tool_field_is_not_an_implicit_grant():
    plan = OrchestrationPlan(summary="explain", subtasks=[
        SubTaskSpec(id="research", agent_type="research", objective="Explain research methods"),
    ])
    tasks, _ = _normalise_subtasks(plan)
    assert tasks[0]["allowed_tools"] == []


def test_simple_uses_router_capabilities():
    result = prepare_simple_task_node({
        "task": "Explain a CSV file format",
        "routing": {"worker_type": "data", "allowed_tools": [], "required_tools": []},
    })
    assert result["subtasks"][0]["allowed_tools"] == []


def test_simple_weather_requires_dedicated_observation():
    result = prepare_simple_task_node({
        "task": "查询北京当前天气",
        "routing": {"worker_type": "research", "allowed_tools": []},
    })
    assert result["subtasks"][0]["allowed_tools"] == ["current_weather"]
    assert result["subtasks"][0]["required_tools"] == ["current_weather"]


def test_upstream_payload_excludes_private_diagnostics():
    state = {
        "current_batch_ids": ["summary"],
        "subtasks": [{"id": "summary", "agent_type": "synthesis", "objective": "Summarize", "dependencies": ["source"]}],
        "worker_results": [{"task_id": "source", "content": "Useful fact", "status": "completed", "attempt": 1,
                            "tool_observations": [{"content": "PRIVATE_OBSERVATION"}],
                            "error_logs": ["PRIVATE_ERROR"], "usage": {"model_calls": 2}}],
    }
    upstream = dispatch_workers(state)[0].arg["upstream_results"]
    assert upstream[0]["content"] == "Useful fact"
    assert "tool_observations" not in upstream[0]
    assert "error_logs" not in upstream[0]
    assert "usage" not in upstream[0]


def test_required_tool_cannot_be_replaced_by_fabricated_answer():
    llm = MagicMock()
    llm.bind_tools.return_value.invoke.return_value = AIMessage(content="北京今天 20 度")
    result = research_worker_node({
        "task_id": "weather", "objective": "北京当前天气", "allowed_tools": ["current_weather"],
        "required_tools": ["current_weather"], "budget_quota": 8,
    }, llm)
    worker = result["worker_results"][0]
    assert worker["status"] == "error"
    assert worker["missing_required_tools"] == ["current_weather"]
    assert "20 度" not in worker["content"]


def test_reviewer_cannot_pass_without_required_tool_evidence():
    llm = MagicMock()
    state = {
        "subtasks": [{"id": "weather", "required_tools": ["current_weather"]}],
        "worker_results": [{"task_id": "weather", "content": "unverified", "tool_observations": []}],
    }
    result = reviewer_node(state, llm)
    assert result["review_status"] == "revise"
    assert result["revision_targets"] == ["weather"]
    assert result["is_pass"] is False
    llm.with_structured_output.assert_not_called()


def test_successful_output_containing_old_error_word_does_not_fail():
    llm = MagicMock()
    llm.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": "python_repl", "args": {"code": "print('执行报错只是文案')"}, "id": "quoted"}]),
        AIMessage(content="完成"),
    ]
    result = code_worker_node({"task_id": "code", "allowed_tools": ["python_repl"]}, llm)
    assert result["worker_results"][0]["status"] == "completed"
    assert result["worker_results"][0]["tool_observations"][0]["is_error"] is False


def test_worker_context_overflow_does_not_call_model_or_lose_objective():
    llm = MagicMock()
    result = code_worker_node({
        "task_id": "code", "objective": "critical requirement " * 1000,
        "memory_policy": {"context_max_tokens": 1000, "context_output_reserve": 200},
    }, llm)
    assert result["worker_results"][0]["termination_reason"] == "context_budget_exceeded"
    assert result["worker_results"][0]["usage"]["model_calls"] == 0
    llm.invoke.assert_not_called()


def test_router_also_applies_context_budget():
    llm = MagicMock()
    result = task_router_node({
        "task": "x" * 30000,
        "memory_policy": {"context_max_tokens": 1000, "context_output_reserve": 200},
    }, llm)
    assert result["termination"]["reason"] == "context_budget_exceeded"
    llm.with_structured_output.return_value.invoke.assert_not_called()


def test_invalid_arguments_do_not_consume_actual_tool_quota():
    llm = MagicMock()
    llm.bind_tools.return_value.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": "python_repl", "args": {"code": 123}, "id": "invalid"}]),
        AIMessage(content="参数有误"),
    ]
    with patch("multi_agent_system.agents.execute_python") as execute:
        result = code_worker_node({"task_id": "code", "allowed_tools": ["python_repl"]}, llm)
    execute.assert_not_called()
    usage = result["worker_results"][0]["usage"]
    assert usage["tool_calls"] == 0
    assert usage["budget_used"] == usage["model_calls"] == 2
    assert result["worker_results"][0]["tool_observations"][0]["result"]["error_code"] == "invalid_arguments"


def test_tool_group_ids_are_normalized_without_mutating_model_response():
    llm = MagicMock()
    first = AIMessage(content="")
    first.tool_calls = [{"name": "python_repl", "args": {"code": "print(42)"}}]
    llm.bind_tools.return_value.invoke.side_effect = [first, AIMessage(content="42")]
    result = code_worker_node({"task_id": "code", "allowed_tools": ["python_repl"]}, llm)
    assert result["worker_results"][0]["status"] == "completed"
    assert "id" not in first.tool_calls[0]


def test_duplicate_tool_ids_rejected_before_any_execution():
    llm = MagicMock()
    llm.bind_tools.return_value.invoke.return_value = AIMessage(content="", tool_calls=[
        {"name": "python_repl", "args": {"code": "print(1)"}, "id": "duplicate"},
        {"name": "python_repl", "args": {"code": "print(2)"}, "id": "duplicate"},
    ])
    with patch("multi_agent_system.agents.execute_python") as execute:
        result = code_worker_node({"task_id": "code", "allowed_tools": ["python_repl"]}, llm)
    execute.assert_not_called()
    assert result["worker_results"][0]["termination_reason"] == "invalid_tool_history"
    assert result["worker_results"][0]["usage"]["tool_calls"] == 0


def test_scheduler_context_overflow_becomes_controlled_terminal():
    result = prepare_batch_node({
        "pending_task_ids": ["summary"],
        "subtasks": [{"id": "summary", "dependencies": ["source"]}],
        "worker_results": [{"task_id": "source", "content": "verified data"}],
        "memory_policy": {"upstream_max_tokens": 10},
    })
    assert result["current_batch_ids"] == []
    assert result["termination"]["reason"] == "context_budget_exceeded"


def test_initialization_reports_undersized_conversation_budget():
    from multi_agent_system.config import Settings
    settings = Settings("test", "https://unused", "test", False, "redis://unused", conversation_max_tokens=10)
    history = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
    result = initialize_resources_node({"conversation_context": history}, settings)
    assert result["termination"]["reason"] == "context_budget_exceeded"
    assert result["conversation_context"] == history
    llm = MagicMock()
    routed = task_router_node(result, llm)
    assert routed["termination"]["reason"] == "context_budget_exceeded"
    llm.with_structured_output.assert_not_called()


def test_model_exception_details_stay_out_of_worker_deliverable():
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("PRIVATE_PROVIDER_DIAGNOSTIC")
    result = code_worker_node({"task_id": "code"}, llm)
    assert "PRIVATE_PROVIDER_DIAGNOSTIC" not in result["worker_results"][0]["content"]
    assert "PRIVATE_PROVIDER_DIAGNOSTIC" in result["error_logs"][0]
