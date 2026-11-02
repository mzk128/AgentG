"""LangGraph 共享状态与 Worker 隔离输入定义。"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, TypedDict

from langgraph.graph.message import add_messages


def append_items(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """合并并行 Worker 产生的列表型结果。"""
    return list(left or []) + list(right or [])


def merge_max_values(
    left: dict[str, int] | None, right: dict[str, int] | None
) -> dict[str, int]:
    """按任务 ID 合并执行次数，并保留较大值。"""
    merged = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = max(merged.get(key, 0), value)
    return merged

# Graph State
class MultiAgentState(TypedDict, total=False):
    """主图状态；作为 Agent 之间的结构化共享黑板。"""

    messages: Annotated[List[Any], add_messages]
    task: str
    conversation_context: list[Dict[str, str]]
    memory_policy: Dict[str, Any]
    memory_binding: Dict[str, Any]
    memory_required_ids: list[str]
    memory_event_at: str
    retrieved_memories: Dict[str, Any]
    worker_memories: Dict[str, Any]
    worker_memory_grants: Dict[str, Any]
    memory_write_plans: list[Dict[str, Any]]
    memory_write_results: list[Dict[str, Any]]
    memory_notices: list[str]
    workspace_path: str
    routing: Dict[str, Any]
    plan: str
    subtasks: list[Dict[str, Any]]
    pending_task_ids: list[str]
    current_batch_ids: list[str]
    worker_results: Annotated[list[Dict[str, Any]], append_items]
    worker_attempts: Annotated[dict[str, int], merge_max_values]
    decision_traces: Annotated[list[Dict[str, Any]], append_items]
    execution_result: str
    review: str
    review_status: str
    review_decision: Dict[str, Any]
    revision_targets: list[str]
    revision_feedback: str
    revision_instructions: list[Dict[str, Any]]
    is_pass: bool
    attempts: int
    error_logs: Annotated[list[str], append_items]
    code_snippets: Annotated[list[str], append_items]
    resource_policy: Dict[str, Any]
    resource_events: Annotated[list[Dict[str, Any]], append_items]
    termination: Dict[str, Any]
    metadata: Dict[str, Any]

# Worker State
class WorkerInputState(TypedDict, total=False):
    """单个 Worker 的最小上下文，避免读取完整主图状态。"""

    task_id: str
    agent_type: str
    objective: str
    workspace_path: str
    upstream_results: list[Dict[str, Any]]
    allowed_tools: list[str]
    required_tools: list[str]
    memory_policy: Dict[str, Any]
    # Host validation binding is not included in the model prompt.
    memory_binding: Dict[str, Any]
    retrieved_memories: Dict[str, Any]
    memory_artifact_grants: list[Dict[str, Any]]
    python_timeout_seconds: float
    tool_output_max_chars: int
    output_format: str
    acceptance_criteria: list[str]
    revision_feedback: str
    revision_instruction: Dict[str, Any]
    previous_deliverable: Dict[str, Any]
    attempt: int
    max_tool_rounds: int
    tool_call_quota: int
    max_consecutive_errors: int
    deadline_at: float
    budget_quota: int


def create_initial_state(
    task: str,
    thread_id: str,
    *,
    conversation_context: list[Dict[str, str]] | None = None,
    workspace_path: str = "",
    conversation_id: str | None = None,
    memory_required_ids: list[str] | None = None,
) -> MultiAgentState:
    """为 CLI、Web 和测试创建一致的初始状态。"""
    return {
        "messages": [],
        "task": task,
        "conversation_context": list(conversation_context or []),
        "memory_policy": {},
        "memory_binding": {},
        "memory_required_ids": list(memory_required_ids or []),
        "memory_event_at": "",
        "retrieved_memories": {},
        "worker_memories": {},
        "worker_memory_grants": {},
        "memory_write_plans": [],
        "memory_write_results": [],
        "memory_notices": [],
        "workspace_path": workspace_path,
        "routing": {},
        "plan": "",
        "subtasks": [],
        "pending_task_ids": [],
        "current_batch_ids": [],
        "worker_results": [],
        "worker_attempts": {},
        "decision_traces": [],
        "execution_result": "",
        "review": "",
        "review_status": "",
        "review_decision": {},
        "revision_targets": [],
        "revision_feedback": "",
        "revision_instructions": [],
        "is_pass": False,
        "attempts": 0,
        "error_logs": [],
        "code_snippets": [],
        "resource_policy": {},
        "resource_events": [],
        "termination": {},
        "metadata": {"thread_id": thread_id, "conversation_id": conversation_id or thread_id},
    }
