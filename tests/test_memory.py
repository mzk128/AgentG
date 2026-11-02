"""Offline regression coverage for bounded, isolated AgentG context."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from multi_agent_system.memory import (
    SUMMARY_PREFIX,
    ContextBudgetExceeded,
    InvalidToolHistory,
    compact_conversation_history,
    compact_worker_messages,
    estimate_message_tokens,
    estimate_tokens,
    project_upstream_results,
    truncate_text,
)


def history_cost(history):
    return estimate_tokens(json.dumps(history, ensure_ascii=False))


def tool_group(call_id, output="observation", *, args=None):
    return [
        AIMessage(
            content="",
            tool_calls=[{"id": call_id, "name": "lookup", "args": args or {}}],
        ),
        ToolMessage(content=output, name="lookup", tool_call_id=call_id),
    ]


def test_estimator_is_offline_conservative_and_supports_injection():
    assert estimate_tokens("北京abc") == len("北京abc".encode("utf-8"))
    assert estimate_tokens("北京abc", token_counter=lambda value: len(value)) == 5


def test_truncation_stays_in_budget_without_corrupting_unicode():
    value = truncate_text("北京天气晴朗☀️" * 100, 100)
    assert "截断" in value
    assert estimate_tokens(value) <= 100
    assert "�" not in value


def test_history_strips_diagnostics_and_untrusted_roles():
    raw = [
        {"role": "system", "content": "must not enter history"},
        {"role": "user", "content": "Weather?", "tool_observations": "secret"},
        {"role": "tool", "content": "raw diagnostic secret"},
        {"role": "assistant", "content": "Sunny", "error_logs": ["secret"]},
    ]
    assert compact_conversation_history(raw) == [
        {"role": "user", "content": "Weather?"},
        {"role": "assistant", "content": "Sunny"},
    ]


def test_history_retains_recent_pairs_and_extracts_older_turns():
    raw = [
        {"role": role, "content": f"{role} turn {index}"}
        for index in range(10)
        for role in ("user", "assistant")
    ]
    context = compact_conversation_history(raw, max_tokens=900, summary_tokens=400, recent_turns=2)
    assert context[0]["role"] == "assistant"
    assert context[0]["content"].startswith(SUMMARY_PREFIX)
    assert context[-4:] == raw[-4:]
    assert "turn 0" in context[0]["content"]
    assert history_cost(context) <= 900
    assert all(set(item) == {"role", "content"} for item in context)


def test_existing_summary_is_not_recursively_rewritten():
    raw = [
        {"role": role, "content": f"{role} turn {index}"}
        for index in range(12)
        for role in ("user", "assistant")
    ]
    once = compact_conversation_history(raw, max_tokens=1000, summary_tokens=450, recent_turns=2)
    twice = compact_conversation_history(once, max_tokens=1000, summary_tokens=450, recent_turns=2)
    assert twice == once
    assert twice[0]["content"].count(SUMMARY_PREFIX) == 1
    appended = once + [{"role": "user", "content": "new request"}, {"role": "assistant", "content": "new result"}]
    updated = compact_conversation_history(appended, max_tokens=1000, summary_tokens=450, recent_turns=2)
    assert updated[-2:] == appended[-2:]
    assert updated[0]["content"].count(SUMMARY_PREFIX) == 1
    assert history_cost(updated) <= 1000


def test_single_oversized_turn_is_bounded_as_a_pair():
    raw = [{"role": "user", "content": "用户问题" * 1000}, {"role": "assistant", "content": "回答" * 1000}]
    context = compact_conversation_history(raw, max_tokens=500, summary_tokens=100)
    assert [item["role"] for item in context] == ["user", "assistant"]
    assert all("截断" in item["content"] for item in context)
    assert history_cost(context) <= 500
    assert raw[0]["content"] == "用户问题" * 1000


def test_unpaired_history_is_not_replayed_as_a_completed_turn():
    context = compact_conversation_history([
        {"role": "assistant", "content": "orphan answer"},
        {"role": "user", "content": "unanswered"},
    ])
    assert context == []


def test_upstream_projection_never_copies_observations_or_unknown_fields():
    raw = [{"task_id": "a", "agent_type": "research", "attempt": 2, "status": "completed", "content": "Deliverable", "tool_observations": [{"content": "secret raw response"}], "error_logs": ["secret"], "workspace_path": "secret"}]
    projected = project_upstream_results(raw)
    assert projected == [{key: raw[0][key] for key in ("task_id", "agent_type", "attempt", "status", "content")}]
    assert "secret" not in json.dumps(projected)
    projected[0]["content"] = "changed"
    assert raw[0]["content"] == "Deliverable"


def test_upstream_budget_keeps_all_dependency_ids_and_caps_content():
    raw = [{"task_id": str(index), "agent_type": "research", "attempt": 1, "status": "completed", "content": "结果" * 1000} for index in range(3)]
    projected = project_upstream_results(raw, max_tokens=900, per_result_tokens=400)
    assert [item["task_id"] for item in projected] == ["0", "1", "2"]
    assert all("截断" in item["content"] for item in projected)
    assert history_cost(projected) <= 900


def test_upstream_metadata_overflow_is_explicit():
    with pytest.raises(ContextBudgetExceeded):
        project_upstream_results([{"task_id": "x" * 1000, "content": "answer"}], max_tokens=100)


def test_worker_tool_pairs_survive_and_original_messages_are_unchanged():
    messages = [HumanMessage(content="The exact current objective"), *tool_group("older", "old" * 300), *tool_group("newer", "new" * 300)]
    original = [item.model_dump() for item in messages]
    compact = compact_worker_messages(messages, max_tokens=600, tool_output_tokens=300)
    assert compact[0].content == messages[0].content
    assert [item.tool_call_id for item in compact if isinstance(item, ToolMessage)] == ["newer"]
    assert compact[1].tool_calls[0]["id"] == "newer"
    assert estimate_message_tokens(compact) <= 600
    assert [item.model_dump() for item in messages] == original
    assert compact[0] is not messages[0]


def test_worker_parallel_tool_group_cannot_be_split():
    messages = [
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[{"id": "one", "name": "lookup", "args": {}}, {"id": "two", "name": "lookup", "args": {}}]),
        ToolMessage(content="A" * 3000, tool_call_id="one"),
        ToolMessage(content="B" * 3000, tool_call_id="two"),
    ]
    compact = compact_worker_messages(messages, max_tokens=550, tool_output_tokens=1000)
    assert len(compact[1].tool_calls) == 2
    assert [item.tool_call_id for item in compact if isinstance(item, ToolMessage)] == ["one", "two"]
    assert estimate_message_tokens(compact) <= 550


@pytest.mark.parametrize("messages", [
    [HumanMessage(content="task"), ToolMessage(content="orphan", tool_call_id="x")],
    [HumanMessage(content="task"), tool_group("x")[0]],
    [HumanMessage(content="task"), *tool_group("x"), ToolMessage(content="duplicate", tool_call_id="x")],
    [HumanMessage(content="task"), tool_group("x")[0], ToolMessage(content="wrong", tool_call_id="y")],
])
def test_worker_rejects_incomplete_or_orphaned_tool_history(messages):
    with pytest.raises(InvalidToolHistory):
        compact_worker_messages(messages)


def test_worker_never_silently_truncates_task_or_call_arguments():
    with pytest.raises(ContextBudgetExceeded):
        compact_worker_messages([HumanMessage(content="essential objective" * 100)], max_tokens=100)
    with pytest.raises(ContextBudgetExceeded):
        compact_worker_messages([HumanMessage(content="task"), *tool_group("one", args={"code": "x" * 2000})], max_tokens=500)


def test_worker_reserves_tool_schema_and_output_space():
    messages = [HumanMessage(content="objective"), *tool_group("one", "result" * 300)]
    compact = compact_worker_messages(messages, max_tokens=1000, reserved_tokens=500, tool_output_tokens=1000)
    assert estimate_message_tokens(compact) <= 500


def test_worker_small_complete_history_is_kept_in_full():
    messages = [HumanMessage(content="objective"), *tool_group("one"), *tool_group("two")]
    compact = compact_worker_messages(messages)
    assert [item.model_dump() for item in compact] == [item.model_dump() for item in messages]


def test_typed_tool_output_compaction_preserves_valid_json_and_error_status():
    payload = json.dumps({"ok": False, "error_code": "network_error", "message": "temporarily unavailable " * 1000})
    messages = [HumanMessage(content="objective"), *tool_group("one", payload)]
    compact = compact_worker_messages(messages, max_tokens=900, tool_output_tokens=400)
    observation = json.loads(compact[-1].content)
    assert observation["ok"] is False
    assert observation["error_code"] == "network_error"
    assert observation["truncated"] is True
