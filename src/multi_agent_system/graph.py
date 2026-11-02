"""AgentG 的 Orchestrator-Worker LangGraph 拓扑。"""

from __future__ import annotations

from functools import partial
import time
from typing import Any, Literal

import httpx
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .artifact_runtime import (
    RevisionInstruction,
    bind_revision_base,
    coerce_previous_artifact,
)
from .agents import (
    WorkerType,
    code_worker_node,
    data_worker_node,
    latest_worker_results,
    orchestrator_node,
    prepare_simple_task_node,
    research_worker_node,
    resource_stop_reason,
    reviewer_node,
    summarize_resource_events,
    synthesis_worker_node,
    task_router_node,
)
from .config import Settings
from .memory import ContextBudgetExceeded, compact_conversation_history, project_upstream_results
from .state import MultiAgentState
from .storage import RedisCheckpointSaver
from .memory_runtime import GraphMemoryRuntime
from .memory_paths import configured_memory_roots


# Fallback for checkpoints created before review limits became configurable.
MAX_REVIEW_ROUNDS = 2
WORKER_NODE_NAMES = {
    WorkerType.RESEARCH.value: "research_worker",
    WorkerType.CODE.value: "code_worker",
    WorkerType.DATA.value: "data_worker",
    WorkerType.SYNTHESIS.value: "synthesis_worker",
}


def initialize_resources_node(
    state: MultiAgentState, settings: Settings
) -> MultiAgentState:
    """为一次图运行创建固定截止时间和资源上限。"""
    started_at = time.time()
    termination = {}
    if state.get("memory_required_ids") and not settings.long_term_memory_enabled:
        termination = {"source": "memory_read", "reason": "memory_disabled"}
    try:
        conversation_context = compact_conversation_history(
            state.get("conversation_context", []),
            max_tokens=settings.conversation_max_tokens,
            summary_tokens=settings.summary_max_tokens,
            recent_turns=settings.recent_turns,
        )
    except ContextBudgetExceeded:
        # Keep the stored source intact; no model call may use partial context.
        conversation_context = state.get("conversation_context", [])
        termination = {"source": "initialize_resources", "reason": "context_budget_exceeded"}
    return {
        "memory_policy": {
            "context_max_tokens": settings.context_max_tokens,
            "context_output_reserve": settings.context_output_reserve,
            "conversation_max_tokens": settings.conversation_max_tokens,
            "summary_max_tokens": settings.summary_max_tokens,
            "recent_turns": settings.recent_turns,
            "upstream_max_tokens": settings.upstream_max_tokens,
            "python_denied_paths": configured_memory_roots(settings),
            "long_term_top_k": settings.memory_top_k,
            "long_term_max_tokens": settings.memory_max_tokens,
            "long_term_scan_limit": settings.memory_scan_limit,
            "long_term_auto_extract": settings.memory_auto_extract,
        },
        "conversation_context": conversation_context,
        "resource_policy": {
            "max_review_rounds": settings.max_review_rounds,
            "max_tool_rounds": settings.max_tool_rounds,
            "max_tool_calls": settings.max_tool_calls,
            "max_consecutive_errors": settings.max_consecutive_errors,
            "max_run_seconds": settings.max_run_seconds,
            "total_action_budget": settings.total_action_budget,
            "python_timeout_seconds": settings.python_timeout_seconds,
            "tool_output_max_chars": settings.tool_output_max_chars,
            "started_at": started_at,
            "deadline_at": started_at + settings.max_run_seconds,
            "token_budget_enabled": False,
        },
        "resource_events": [],
        "termination": termination,
    }


def route_task(
    state: MultiAgentState,
) -> Literal["prepare_simple", "orchestrator", "resource_terminated"]:
    if state.get("termination"):
        return "resource_terminated"
    return (
        "prepare_simple"
        if state.get("routing", {}).get("route") == "simple"
        else "orchestrator"
    )


def prepare_batch_node(state: MultiAgentState) -> MultiAgentState:
    """从剩余任务中选择一个依赖已经完成的并行批次。"""
    stop_reason = resource_stop_reason(state)
    if stop_reason:
        return {
            "current_batch_ids": [],
            "termination": {"source": "scheduler", "reason": stop_reason},
        }
    subtasks = state.get("subtasks", [])
    pending_ids = set(state.get("pending_task_ids", []))
    ready_ids = [
        item["id"]
        for item in subtasks
        if item["id"] in pending_ids
        and not (set(item.get("dependencies", [])) & pending_ids)
    ]
    if pending_ids and not ready_ids:
        # 正常计划已在 Orchestrator 中消除循环；这里是防御性兜底。
        ready_ids = [next(item["id"] for item in subtasks if item["id"] in pending_ids)]

    policy = state.get("resource_policy", {})
    total_budget = int(policy.get("total_action_budget", 0) or 0)
    if total_budget:
        used = summarize_resource_events(state.get("resource_events", []))["budget_used"]
        ready_ids = ready_ids[: max(0, total_budget - used)]
    latest_results = latest_worker_results(state.get("worker_results", []))
    try:
        for spec in subtasks:
            if spec["id"] in ready_ids:
                project_upstream_results(
                    [latest_results[dep] for dep in spec.get("dependencies", []) if dep in latest_results],
                    max_tokens=int(state.get("memory_policy", {}).get("upstream_max_tokens", 3000)),
                )
    except ContextBudgetExceeded:
        return {"current_batch_ids": [], "termination": {
            "source": "scheduler", "reason": "context_budget_exceeded",
        }}
    return {"current_batch_ids": ready_ids}


def _split_quota(total: int, count: int, *, minimum: int = 0) -> list[int]:
    """把不可共享的并行资源安全切片，切片之和不超过 total。"""
    if count <= 0:
        return []
    base = min(total // count, max(minimum, 0))
    quotas = [base] * count
    remaining = max(0, total - sum(quotas))
    for index in range(count):
        extra, remainder = divmod(remaining, count - index)
        quotas[index] += extra
        remaining -= extra
        if remainder and extra == 0:
            quotas[index] += 1
            remaining -= 1
    return quotas


# Fan-out 到对应专业的 Worker
def dispatch_workers(
    state: MultiAgentState,
) -> list[Send] | Literal["reviewer", "resource_terminated"]:
    """把当前批次 fan-out 到对应专业 Worker。"""
    current_batch = state.get("current_batch_ids", [])
    if not current_batch:
        if state.get("termination") or resource_stop_reason(state):
            return "resource_terminated"
        return "reviewer"

    specs = {item["id"]: item for item in state.get("subtasks", [])}
    latest_results = latest_worker_results(state.get("worker_results", []))
    attempts = state.get("worker_attempts", {})
    revision_feedback = state.get("revision_feedback", "")
    revision_instructions = {
        str(item.get("task_id")): item
        for item in state.get("revision_instructions", [])
        if isinstance(item, dict) and item.get("task_id")
    }
    policy = state.get("resource_policy", {})
    usage = summarize_resource_events(state.get("resource_events", []))
    remaining_budget = max(
        0,
        int(policy.get("total_action_budget", 40)) - usage["budget_used"],
    )
    remaining_tools = max(
        0, int(policy.get("max_tool_calls", 12)) - usage["tool_calls"]
    )
    budget_quotas = _split_quota(remaining_budget, len(current_batch), minimum=1)
    tool_quotas = _split_quota(remaining_tools, len(current_batch))
    sends: list[Send] = []

    for index, task_id in enumerate(current_batch):
        spec = specs[task_id]
        previous_result = latest_results.get(task_id)
        previous_deliverable: dict[str, Any] = {}
        revision_instruction: dict[str, Any] = {}
        if previous_result and int(attempts.get(task_id, 0)) > 0:
            previous_deliverable = {
                key: previous_result.get(key)
                for key in (
                    "task_id", "agent_type", "attempt", "status", "content",
                    "artifact", "artifact_id", "artifact_version",
                    "base_artifact_version", "changed_paths", "change_summary",
                )
            }
            try:
                instruction = RevisionInstruction.model_validate(
                    revision_instructions.get(task_id)
                    or {
                        "task_id": task_id,
                        "mode": "replace",
                        "requested_changes": [
                            revision_feedback or "根据最新上游结果更新受影响内容"
                        ],
                    }
                )
                instruction = bind_revision_base(
                    instruction, coerce_previous_artifact(previous_deliverable)
                )
                revision_instruction = instruction.model_dump(mode="json")
            except (ValueError, TypeError):
                revision_instruction = RevisionInstruction(
                    task_id=task_id,
                    mode="regenerate",
                    requested_changes=["旧产物版本信息无效，请重新生成完整产物"],
                ).model_dump(mode="json")
        elif revision_instructions.get(task_id):
            try:
                instruction = RevisionInstruction.model_validate(
                    revision_instructions[task_id]
                )
                if instruction.mode != "regenerate":
                    instruction = RevisionInstruction(
                        task_id=task_id,
                        mode="regenerate",
                        requested_changes=instruction.requested_changes,
                        evidence_requirements=instruction.evidence_requirements,
                    )
                revision_instruction = instruction.model_dump(mode="json")
            except (ValueError, TypeError):
                revision_instruction = RevisionInstruction(
                    task_id=task_id,
                    mode="regenerate",
                    requested_changes=["没有可用旧产物，请重新生成完整产物"],
                ).model_dump(mode="json")
        upstream_results = project_upstream_results([
            latest_results[dependency]
            for dependency in spec.get("dependencies", [])
            if dependency in latest_results
        ], max_tokens=int(state.get("memory_policy", {}).get("upstream_max_tokens", 3000)))
        worker_input = {
            "task_id": task_id,
            "agent_type": spec["agent_type"],
            "objective": spec["objective"],
            "workspace_path": state.get("workspace_path", ""),
            "upstream_results": upstream_results,
            "allowed_tools": spec.get("allowed_tools", []),
            "required_tools": spec.get("required_tools", []),
            "memory_policy": state.get("memory_policy", {}),
            "memory_binding": state.get("memory_binding", {}),
            "retrieved_memories": state.get("worker_memories", {}).get(
                f"{task_id}:{int(attempts.get(task_id, 0)) + 1}", {}),
            "memory_artifact_grants": state.get("worker_memory_grants", {}).get(
                f"{task_id}:{int(attempts.get(task_id, 0)) + 1}", []),
            "python_timeout_seconds": float(policy.get("python_timeout_seconds", 15.0)),
            "tool_output_max_chars": int(policy.get("tool_output_max_chars", 12000)),
            "output_format": spec.get("output_format", "清晰文本"),
            "acceptance_criteria": spec.get("acceptance_criteria", []),
            "revision_feedback": revision_feedback,
            "revision_instruction": revision_instruction,
            "previous_deliverable": previous_deliverable,
            "attempt": int(attempts.get(task_id, 0)) + 1,
            "max_tool_rounds": int(policy.get("max_tool_rounds", 4)),
            "tool_call_quota": tool_quotas[index],
            "max_consecutive_errors": int(
                policy.get("max_consecutive_errors", 2)
            ),
            "deadline_at": float(policy.get("deadline_at", 0) or 0),
            "budget_quota": budget_quotas[index],
        }
        sends.append(Send(WORKER_NODE_NAMES[spec["agent_type"]], worker_input))
    return sends


def fan_in_node(state: MultiAgentState) -> MultiAgentState:
    """合并一个批次的结果，并推进依赖调度。"""
    completed_batch = set(state.get("current_batch_ids", []))
    remaining = [
        task_id
        for task_id in state.get("pending_task_ids", [])
        if task_id not in completed_batch
    ]
    latest_results = latest_worker_results(state.get("worker_results", []))
    subtasks = state.get("subtasks", [])
    terminated_results = [
        latest_results[task_id]
        for task_id in completed_batch
        if task_id in latest_results
        and latest_results[task_id].get("status") == "terminated"
    ]

    synthesis_ids = [
        item["id"]
        for item in subtasks
        if item.get("agent_type") == WorkerType.SYNTHESIS.value
    ]
    final_result = ""
    if synthesis_ids and synthesis_ids[-1] in latest_results:
        final_result = latest_results[synthesis_ids[-1]].get("content", "")
    elif not remaining:
        final_result = "\n\n".join(
            latest_results[item["id"]].get("content", "")
            for item in subtasks
            if item["id"] in latest_results
        )

    update: MultiAgentState = {
        "pending_task_ids": remaining,
        "current_batch_ids": [],
        "execution_result": final_result,
    }
    if terminated_results:
        result = terminated_results[0]
        update["termination"] = {
            "source": f"worker:{result.get('task_id', 'unknown')}",
            "reason": result.get("termination_reason") or "worker_resource_limit",
        }
    return update


def route_after_fan_in(
    state: MultiAgentState,
) -> Literal["prepare_batch", "reviewer", "resource_terminated"]:
    if state.get("termination"):
        return "resource_terminated"
    return "prepare_batch" if state.get("pending_task_ids") else "reviewer"


def resource_terminated_node(state: MultiAgentState) -> MultiAgentState:
    """把任一资源上限转换成稳定、可观测的 blocked 终态。"""
    termination = state.get("termination", {})
    reason = termination.get("reason", "resource_limit")
    source = termination.get("source", "workflow")
    feedback = f"工作流因资源限制终止（{source}: {reason}）。"
    return {
        "review": feedback,
        "review_status": "blocked",
        "review_decision": {
            "status": "blocked",
            "failed_criteria": ["resource_policy"],
            "revision_targets": [],
            "revision_instructions": [],
            "feedback": feedback,
            "confidence": 1.0,
        },
        "revision_targets": [],
        "revision_instructions": [],
        "is_pass": False,
    }

# Affected_Task Reviewer给出的返工合集 (指定目标 + 所有依赖此目标的下游任务)
def _affected_task_ids(
    subtasks: list[dict], revision_targets: list[str]
) -> list[str]:
    """计算定向返工目标及所有依赖这些目标的下游任务。"""
    affected = set(revision_targets)
    changed = True
    while changed:
        changed = False
        for item in subtasks:
            if item["id"] in affected:
                continue
            if set(item.get("dependencies", [])) & affected:
                affected.add(item["id"])
                changed = True
    return [item["id"] for item in subtasks if item["id"] in affected]


def prepare_revision_node(state: MultiAgentState) -> MultiAgentState:
    """仅重新调度 Reviewer 指定目标及受其影响的下游任务。"""
    subtasks = state.get("subtasks", [])
    targets = state.get("revision_targets", [])
    pending = _affected_task_ids(subtasks, targets)
    return {
        "pending_task_ids": pending,
        "current_batch_ids": [],
        "is_pass": False,
    }


def route_after_review(
    state: MultiAgentState,
) -> Literal["prepare_revision", "__end__"]:
    if (
        state.get("review_status") == "revise"
        and int(state.get("attempts", 0)) < int(
            state.get("resource_policy", {}).get("max_review_rounds", MAX_REVIEW_ROUNDS)
        )
        and state.get("revision_targets")
    ):
        return "prepare_revision"
    return "__end__"


def refresh_resume_deadline(graph: Any, config: dict, settings: Settings) -> bool:
    """Refresh the cooperative wall-clock window for an unfinished checkpoint.

    Tool/action/error consumption remains in the checkpoint.  Only downtime is
    excluded from the wall-clock limit so a restart can actually continue.
    """
    snapshot = graph.get_state(config)
    if not snapshot.values or not snapshot.next:
        return False
    now = time.time()
    policy = dict(snapshot.values.get("resource_policy", {}))
    policy["resumed_at"] = now
    policy["deadline_at"] = now + settings.max_run_seconds
    graph.update_state(config, {"resource_policy": policy})
    return True

# build graph/Agentic Workflow
def build_graph(settings: Settings, *, checkpointer: Any | None = None, memory_service=None):
    http_client = httpx.Client(trust_env=False)
    llm = ChatOpenAI(
        model=settings.model_name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.2,
        timeout=min(
            settings.model_request_timeout_seconds,
            settings.max_run_seconds,
        ),
        http_client=http_client,
    )

    runtime = GraphMemoryRuntime(settings, memory_service)
    graph = StateGraph(MultiAgentState)
    graph.add_node(
        "initialize_resources", partial(initialize_resources_node, settings=settings)
    )
    graph.add_node("task_router", runtime.wrap(partial(task_router_node, llm=llm), "task_router"))
    graph.add_node("prepare_simple", prepare_simple_task_node)
    graph.add_node("orchestrator", runtime.wrap(partial(orchestrator_node, llm=llm), "orchestrator"))
    graph.add_node("prepare_batch", partial(runtime.prepare_batch, prepare_batch_node))
    graph.add_node("research_worker", runtime.wrap(partial(research_worker_node, llm=llm), "research_worker", worker=True))
    graph.add_node("code_worker", runtime.wrap(partial(code_worker_node, llm=llm), "code_worker", worker=True))
    graph.add_node("data_worker", runtime.wrap(partial(data_worker_node, llm=llm), "data_worker", worker=True))
    graph.add_node("synthesis_worker", runtime.wrap(partial(synthesis_worker_node, llm=llm), "synthesis_worker", worker=True))
    graph.add_node("fan_in", fan_in_node)
    graph.add_node("reviewer", runtime.wrap(partial(reviewer_node, llm=llm), "reviewer"))
    graph.add_node("prepare_revision", prepare_revision_node)
    graph.add_node("resource_terminated", resource_terminated_node)

    graph.add_edge(START, "initialize_resources")
    # Retain node names even when disabled so an old pending memory checkpoint
    # can finish with an explicit not-saved result after the feature is turned off.
    graph.add_node("prepare_memory", runtime.prepare)
    graph.add_node("stage_memory_writes", runtime.stage_final)
    graph.add_node("commit_memory_writes", runtime.commit_final)
    graph.add_edge("stage_memory_writes", "commit_memory_writes")
    graph.add_edge("commit_memory_writes", END)
    if settings.long_term_memory_enabled or memory_service is not None:
        graph.add_edge("initialize_resources", "prepare_memory")
        graph.add_edge("prepare_memory", "task_router")
        terminal = "stage_memory_writes"
    else:
        graph.add_edge("initialize_resources", "task_router")
        graph.add_edge("prepare_memory", "task_router")
        terminal = END
    graph.add_conditional_edges(
        "task_router",
        route_task,
        {
            "prepare_simple": "prepare_simple",
            "orchestrator": "orchestrator",
            "resource_terminated": "resource_terminated",
        },
    )
    graph.add_edge("prepare_simple", "prepare_batch")
    graph.add_edge("orchestrator", "prepare_batch")
    graph.add_conditional_edges(
        "prepare_batch",
        dispatch_workers,
        [*WORKER_NODE_NAMES.values(), "reviewer", "resource_terminated"],
    )

    for worker_node in WORKER_NODE_NAMES.values():
        graph.add_edge(worker_node, "fan_in")

    graph.add_conditional_edges(
        "fan_in",
        route_after_fan_in,
        {
            "prepare_batch": "prepare_batch",
            "reviewer": "reviewer",
            "resource_terminated": "resource_terminated",
        },
    )
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"prepare_revision": "prepare_revision", "__end__": terminal},
    )
    graph.add_edge("prepare_revision", "prepare_batch")
    graph.add_edge("resource_terminated", terminal)

    if checkpointer is None:
        checkpointer = (
            RedisCheckpointSaver(settings.redis_url)
            if settings.use_redis
            else MemorySaver()
        )
    return graph.compile(checkpointer=checkpointer)
