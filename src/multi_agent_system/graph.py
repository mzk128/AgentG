"""AgentG 的 Orchestrator-Worker LangGraph 拓扑。"""

from __future__ import annotations

from functools import partial
import time
from typing import Literal

import httpx
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

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
from .state import MultiAgentState


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
    return {
        "resource_policy": {
            "max_tool_rounds": settings.max_tool_rounds,
            "max_tool_calls": settings.max_tool_calls,
            "max_consecutive_errors": settings.max_consecutive_errors,
            "max_run_seconds": settings.max_run_seconds,
            "total_action_budget": settings.total_action_budget,
            "started_at": started_at,
            "deadline_at": started_at + settings.max_run_seconds,
            "token_budget_enabled": False,
        },
        "resource_events": [],
        "termination": {},
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
        upstream_results = [
            latest_results[dependency]
            for dependency in spec.get("dependencies", [])
            if dependency in latest_results
        ]
        worker_input = {
            "task_id": task_id,
            "agent_type": spec["agent_type"],
            "objective": spec["objective"],
            "upstream_results": upstream_results,
            "allowed_tools": spec.get("allowed_tools", []),
            "output_format": spec.get("output_format", "清晰文本"),
            "acceptance_criteria": spec.get("acceptance_criteria", []),
            "revision_feedback": revision_feedback,
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
            "feedback": feedback,
            "confidence": 1.0,
        },
        "revision_targets": [],
        "is_pass": False,
    }


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
        and int(state.get("attempts", 0)) < MAX_REVIEW_ROUNDS
        and state.get("revision_targets")
    ):
        return "prepare_revision"
    return "__end__"


def build_graph(settings: Settings):
    http_client = httpx.Client(trust_env=False)
    llm = ChatOpenAI(
        model=settings.model_name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.2,
        http_client=http_client,
    )

    graph = StateGraph(MultiAgentState)
    graph.add_node(
        "initialize_resources", partial(initialize_resources_node, settings=settings)
    )
    graph.add_node("task_router", partial(task_router_node, llm=llm))
    graph.add_node("prepare_simple", prepare_simple_task_node)
    graph.add_node("orchestrator", partial(orchestrator_node, llm=llm))
    graph.add_node("prepare_batch", prepare_batch_node)
    graph.add_node("research_worker", partial(research_worker_node, llm=llm))
    graph.add_node("code_worker", partial(code_worker_node, llm=llm))
    graph.add_node("data_worker", partial(data_worker_node, llm=llm))
    graph.add_node("synthesis_worker", partial(synthesis_worker_node, llm=llm))
    graph.add_node("fan_in", fan_in_node)
    graph.add_node("reviewer", partial(reviewer_node, llm=llm))
    graph.add_node("prepare_revision", prepare_revision_node)
    graph.add_node("resource_terminated", resource_terminated_node)

    graph.add_edge(START, "initialize_resources")
    graph.add_edge("initialize_resources", "task_router")
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
        {"prepare_revision": "prepare_revision", "__end__": END},
    )
    graph.add_edge("prepare_revision", "prepare_batch")
    graph.add_edge("resource_terminated", END)

    return graph.compile(checkpointer=MemorySaver())
