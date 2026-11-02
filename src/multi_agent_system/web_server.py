"""AgentG Web 服务器 — 基于 FastAPI 的多智能体协作 Web 系统。

提供 REST API + SSE 流式推送 + Web 前端界面。
"""

import asyncio
import json
import os
import queue
import sys
import threading
import time
import uuid
from typing import Literal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader
from redis.exceptions import RedisError

from .storage import RedisCheckpointSaver, RedisStateStore
from .memory import ContextBudgetExceeded, compact_conversation_history
from .memory_paths import configured_memory_roots, is_memory_path
from .memory_store import resolve_memory_db_path

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
if sys.version_info < (3, 10):
    raise RuntimeError("当前项目需要 Python 3.10+，请切换解释器后重试。")

app = FastAPI(title="AgentG — 多智能体协作系统", version="1.0.0")
from .memory_api import router as memory_router
app.include_router(memory_router)

_executor = ThreadPoolExecutor(max_workers=4)

_BASE_DIR = Path(__file__).resolve().parent

# 模板引擎
_templates_dir = _BASE_DIR / "web_templates"
_templates_dir.mkdir(exist_ok=True)
_jinja_env = Environment(loader=FileSystemLoader(str(_templates_dir)), autoescape=True)

# 内存任务存储是进程内缓存；启用 Redis 时以 Redis 记录为准。
_task_store: Dict[str, Dict[str, Any]] = {}
_task_repository: RedisStateStore | None = None
_repository_url: str | None = None
_task_store_lock = threading.RLock()
_active_threads: set[str] = set()
_active_threads_lock = threading.Lock()

_CONVERSATION_FIELDS = (
    "conversation_id",
    "conversation_title",
    "parent_thread_id",
    "turn_index",
    "conversation_context",
    "workspace_path",
    "is_pinned",
    "conversation_updated_at",
    "memory_binding",
)


def _get_task_repository() -> RedisStateStore | None:
    """Lazily create the Redis task repository when persistence is enabled."""
    global _task_repository, _repository_url
    from .config import get_settings

    settings = get_settings()
    if not settings.use_redis:
        return None
    if _task_repository is None or _repository_url != settings.redis_url:
        _task_repository = RedisStateStore(settings.redis_url)
        _repository_url = settings.redis_url
    return _task_repository


def _save_task_record(thread_id: str, record: Dict[str, Any]) -> None:
    """Persist a task without letting stale execution writes undo UI metadata.

    A model/tool call can hold an older record while the user renames or pins
    the conversation.  The monotonically increasing conversation timestamp
    makes the later execution write preserve that newer metadata.
    """
    repository = _get_task_repository()
    with _task_store_lock:
        current = (
            repository.load_task(thread_id)
            if repository is not None
            else _task_store.get(thread_id)
        )
        if current:
            current_version = float(current.get("conversation_updated_at", 0) or 0)
            incoming_version = float(record.get("conversation_updated_at", 0) or 0)
            if current_version > incoming_version:
                for field in _CONVERSATION_FIELDS:
                    if field in current:
                        record[field] = current[field]
        _task_store[thread_id] = record
        if repository is not None:
            repository.save_task(thread_id, record)


def _load_task_record(thread_id: str) -> Dict[str, Any] | None:
    repository = _get_task_repository()
    with _task_store_lock:
        if repository is not None:
            record = repository.load_task(thread_id)
            if record is not None:
                _task_store[thread_id] = record
            return record
        return _task_store.get(thread_id)


def _list_task_records() -> list[tuple[str, Dict[str, Any]]]:
    repository = _get_task_repository()
    if repository is not None:
        records = repository.list_tasks()
        _task_store.update(dict(records))
        return records
    return sorted(
        _task_store.items(),
        key=lambda item: item[1].get("created_at", 0),
        reverse=True,
    )


def _delete_task_record(thread_id: str) -> bool:
    repository = _get_task_repository()
    existed = thread_id in _task_store
    if repository is not None:
        existed = repository.delete_task(thread_id) or existed
    _task_store.pop(thread_id, None)
    return existed


def _task_outcome(record: Dict[str, Any]) -> str:
    """Map internal workflow states to the four UI lifecycle labels."""
    status = record.get("status", "pending")
    if status == "running":
        return "run"
    if status == "completed":
        return "success" if record.get("is_pass") else "failure"
    if status in {"failed", "terminated"}:
        return "failure"
    return "pending"


def _is_thread_active(thread_id: str) -> bool:
    with _active_threads_lock:
        return thread_id in _active_threads


def _normalise_workspace_path(value: Any) -> str:
    """Validate a user-selected local workspace without reading its contents."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="工作目录必须是字符串路径")
    value = value.strip()
    if not value:
        return ""
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="工作目录不存在") from exc
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="工作目录必须是已存在的文件夹")
    from .config import get_settings
    settings = get_settings()
    roots = configured_memory_roots(settings)
    if is_memory_path(path, roots):
        raise HTTPException(status_code=400, detail="长期记忆存储目录不能作为任务工作目录")
    return str(path)


def _conversation_result(record: Dict[str, Any]) -> str:
    """Extract compact prior-turn context instead of replaying execution logs."""
    result = record.get("execution_result") or ""
    if record.get("status") == "failed":
        return "上一轮执行失败；未产生已验证结果。"
    if not result and record.get("termination"):
        result = f"任务终止：{record['termination'].get('reason', 'unknown')}"
    return str(result)


_TERMINATION_SUMMARIES = {
    "context_budget_exceeded": "任务所需上下文超过当前容量，请缩小任务或调整上下文配置。",
    "invalid_tool_history": "工具记录不完整，无法继续本轮执行。",
    "max_run_seconds_exceeded": "任务超过了本次允许的运行时间。",
    "max_tool_rounds_exceeded": "任务需要的工具交互轮次超过了当前上限。",
    "max_tool_calls_exceeded": "任务需要的工具调用次数超过了当前上限。",
    "max_consecutive_errors_exceeded": "工具连续执行失败，系统已停止继续尝试。",
    "max_action_budget_exceeded": "任务消耗的操作预算超过了当前上限。",
    "worker_resource_limit": "执行任务的专业 Agent 达到了资源上限。",
}

_RESULT_KEY_LABELS = {
    "summary": "概要",
    "details": "详细结果",
    "result": "结果",
    "results": "结果",
    "city": "城市",
    "cities": "城市",
    "condition": "天气状况",
    "weather": "天气状况",
    "temperature": "气温",
    "temperature_2m": "气温",
    "apparent_temperature": "体感温度",
    "humidity": "湿度",
    "relative_humidity_2m": "湿度",
    "wind": "风速",
    "wind_speed": "风速",
    "wind_speed_10m": "风速",
    "status": "状态",
    "reason": "原因",
    "message": "说明",
}


def _sentence(text: Any) -> str:
    value = str(text).strip()
    if not value:
        return ""
    return value if value[-1] in "。！？.!?" else f"{value}。"


def _display_scalar(value: Any) -> str:
    if value is None:
        return "暂无"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value).strip()


def _first_present(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _weather_result_sentence(data: Dict[str, Any]) -> str:
    city = _first_present(data, "city", "name")
    condition = _first_present(data, "condition", "weather")
    temperature = _first_present(data, "temperature", "temperature_2m")
    humidity = _first_present(data, "humidity", "relative_humidity_2m")
    wind = _first_present(data, "wind", "wind_speed", "wind_speed_10m")
    if not city or not any(value is not None for value in (condition, temperature, humidity, wind)):
        return ""
    facts: list[str] = []
    if condition is not None:
        facts.append(f"天气为{_display_scalar(condition)}")
    if temperature is not None:
        facts.append(f"气温为{_display_scalar(temperature)}")
    if humidity is not None:
        facts.append(f"湿度为{_display_scalar(humidity)}")
    if wind is not None:
        facts.append(f"风速为{_display_scalar(wind)}")
    return _sentence(f"{_display_scalar(city)}：{'，'.join(facts)}")


def _naturalize_parsed_result(value: Any) -> str:
    if isinstance(value, dict):
        weather_sentence = _weather_result_sentence(value)
        if weather_sentence:
            return weather_sentence

        paragraphs: list[str] = []
        summary = value.get("summary")
        if summary is not None:
            paragraphs.append(_sentence(_display_scalar(summary)))

        for key, item in value.items():
            if key == "summary":
                continue
            label = _RESULT_KEY_LABELS.get(key, key.replace("_", " "))
            if isinstance(item, list):
                if not item:
                    continue
                if all(not isinstance(entry, (dict, list)) for entry in item):
                    paragraphs.append(_sentence(f"{label}包括{'、'.join(_display_scalar(entry) for entry in item)}"))
                else:
                    entries = [
                        _naturalize_parsed_result(entry)
                        for entry in item
                    ]
                    entries = [entry for entry in entries if entry]
                    if entries:
                        paragraphs.append(
                            f"{label}：\n"
                            + "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, 1))
                        )
            elif isinstance(item, dict):
                nested = _naturalize_parsed_result(item)
                if nested:
                    paragraphs.append(f"{label}：{nested}")
            else:
                paragraphs.append(_sentence(f"{label}为{_display_scalar(item)}"))
        return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)

    if isinstance(value, list):
        if not value:
            return "本轮没有返回结果。"
        if all(not isinstance(entry, (dict, list)) for entry in value):
            return _sentence(f"结果包括{'、'.join(_display_scalar(entry) for entry in value)}")
        entries = [_naturalize_parsed_result(entry) for entry in value]
        return "\n".join(
            f"{index}. {entry}" for index, entry in enumerate(entries, 1) if entry
        )
    return _sentence(_display_scalar(value))


def _naturalize_user_result(value: Any) -> str:
    """Convert JSON-shaped final output into readable prose when possible."""
    if isinstance(value, (dict, list)):
        return _naturalize_parsed_result(value)
    text = str(value or "").strip()
    if not text:
        return ""

    candidate = text
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        return text
    return _naturalize_parsed_result(parsed)


def _base_user_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic, user-facing terminal summary without exposing CoT."""
    outcome = _task_outcome(record)
    if outcome not in {"success", "failure"}:
        return {}

    subtasks = record.get("subtasks") or []
    step_count = len(subtasks) if isinstance(subtasks, list) else 0
    if outcome == "success":
        result = str(record.get("execution_result") or "").strip()
        if not result:
            for worker_result in reversed(record.get("worker_results") or []):
                if isinstance(worker_result, dict) and worker_result.get("content"):
                    result = str(worker_result["content"]).strip()
                    break
        overview = "任务已完成并通过结果审查。"
        if step_count:
            overview = f"系统完成了 {step_count} 个子任务，并通过结果审查。"
        return {
            "status": "success",
            "title": "任务已完成",
            "overview": overview,
            "content": _naturalize_user_result(result)
            or "任务已经完成，但本轮没有生成可展示的文本结果。",
            "step_count": step_count,
        }

    termination = record.get("termination") or {}
    if termination:
        reason = str(termination.get("reason") or "resource_limit")
        return {
            "status": "failure",
            "title": "任务因资源限制停止",
            "overview": _TERMINATION_SUMMARIES.get(
                reason, "任务达到了当前资源限制，尚未完整执行。"
            ),
            "content": "可以调整任务范围或资源上限后重新执行；详细停止位置仍可在上方步骤中查看。",
            "reason": reason,
            "step_count": step_count,
        }

    if record.get("status") == "failed":
        return {
            "status": "failure",
            "title": "任务执行失败",
            "overview": "执行过程中发生异常，本轮任务未能完成。",
            "content": "请展开上方失败步骤查看具体错误；修正配置、网络或输入后可以重新执行。",
            "step_count": step_count,
        }

    review_decision = record.get("review_decision") or {}
    feedback = ""
    if isinstance(review_decision, dict):
        feedback = str(review_decision.get("feedback") or "").strip()
    feedback = feedback or str(record.get("review") or "").strip()
    return {
        "status": "failure",
        "title": "任务未通过结果审查",
        "overview": "执行流程已经结束，但结果尚未满足验收标准。",
        "content": _naturalize_user_result(feedback[:2000])
        or "请根据上方审查结论调整任务要求后重新执行。",
        "step_count": step_count,
    }


def _user_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    summary = _base_user_summary(record)
    if not summary:
        return summary
    reason = str(record.get("termination", {}).get("reason", ""))
    if reason.startswith("memory_"):
        summary.update(title="必要记忆无法安全使用", overview="任务因长期记忆的权限、时效、版本或容量检查未通过而停止。",
                       content="请检查记忆状态、宿主身份和独立记忆预算；历史记忆不能代替本轮实时查询。")
    results = record.get("memory_write_results", [])
    if results:
        saved = sum(item.get("status") == "saved" for item in results)
        candidates = sum(item.get("status") == "candidate" for item in results)
        failed = len(results) - saved - candidates
        summary["content"] += f"\n\n长期记忆：已保存 {saved} 条；待确认候选 {candidates} 条；未保存 {failed} 条。"
    if record.get("memory_notices"):
        summary["content"] += "\n\n长期记忆提示：部分记忆未能读取或保存，本轮没有据此声明已完成记忆操作。"
    return summary


def _conversation_turns(
    thread_id: str, current_record: Dict[str, Any] | None = None
) -> list[Dict[str, Any]]:
    """Follow parent links and return a chronological conversation projection."""
    turns: list[Dict[str, Any]] = []
    seen: set[str] = set()
    current_id: str | None = thread_id
    while current_id and current_id not in seen and len(turns) < 50:
        seen.add(current_id)
        record = current_record if current_id == thread_id else _load_task_record(current_id)
        if record is None:
            break
        turns.append(
            {
                "thread_id": current_id,
                **{key: value for key, value in record.items() if key != "metadata"},
                "outcome": _task_outcome(record),
                "is_active": _is_thread_active(current_id),
                "user_summary": _user_summary(record),
            }
        )
        parent_id = record.get("parent_thread_id")
        current_id = str(parent_id) if parent_id else None
    turns.reverse()
    return turns


def _conversation_records(conversation_id: str) -> list[tuple[str, Dict[str, Any]]]:
    """Return every stored run that belongs to one UI conversation."""
    return [
        (thread_id, record)
        for thread_id, record in _list_task_records()
        if str(record.get("conversation_id") or thread_id) == conversation_id
    ]


def _save_running_progress(
    thread_id: str,
    task: str,
    node_name: str,
    state_update: Dict[str, Any],
    content: str,
    *,
    current_agent: str | None = None,
) -> Dict[str, Any]:
    """Persist one observable node update while the graph is still running."""
    record = _load_task_record(thread_id) or {
        "task": task,
        "metadata": {"thread_id": thread_id},
        "created_at": time.time(),
    }
    messages = list(record.get("messages", []))
    decision_traces = list(record.get("decision_traces", []))
    decision_traces.extend(state_update.get("decision_traces", []))
    if content:
        messages.append(
            {
                "content": content,
                "type": "AIMessage",
                "source": node_name,
            }
        )

    record.update(
        {
            "task": task,
            "status": "running",
            "current_agent": current_agent or node_name,
            "messages": messages,
            "decision_traces": decision_traces,
            "updated_at": time.time(),
        }
    )
    for field in (
        "routing",
        "plan",
        "subtasks",
        "worker_results",
        "execution_result",
        "review",
        "review_status",
        "review_decision",
        "revision_targets",
        "revision_instructions",
        "is_pass",
        "attempts",
        "error_logs",
        "code_snippets",
        "resource_policy",
        "memory_policy",
        "memory_binding", "memory_required_ids", "memory_event_at", "retrieved_memories",
        "worker_memories", "worker_memory_grants", "memory_write_plans", "memory_write_results", "memory_notices",
        "conversation_context",
        "termination",
    ):
        if field in state_update:
            record[field] = state_update[field]
    _save_task_record(thread_id, record)
    return record


def _next_running_agent(node_name: str, state_update: Dict[str, Any]) -> str:
    """Return the next user-visible phase after a completed graph node."""
    if node_name == "initialize_resources":
        return "resource_terminated" if state_update.get("termination") else "task_router"
    if node_name == "prepare_memory":
        return "resource_terminated" if state_update.get("termination") else "task_router"
    if node_name == "stage_memory_writes":
        return "commit_memory_writes"
    if node_name == "commit_memory_writes":
        return "done"
    if node_name == "task_router":
        return (
            "orchestrator"
            if state_update.get("routing", {}).get("route") == "team"
            else "workers"
        )
    if node_name in {"prepare_simple", "orchestrator", "prepare_batch", "prepare_revision"}:
        return "workers"
    if node_name in {"research_worker", "code_worker", "data_worker", "synthesis_worker"}:
        return "fan_in"
    if node_name == "fan_in":
        return "prepare_batch"
    if node_name == "reviewer":
        from .graph import route_after_review

        return "revision" if route_after_review(state_update) == "prepare_revision" else "done"
    if node_name == "resource_terminated":
        return "done"
    return node_name


def _message_source(content: str) -> str:
    """从 Agent 消息前缀提取前端展示节点。"""
    if not content.startswith("[") or "]" not in content:
        return "node"
    label = content[1 : content.index("]")].split(":", 1)[0].lower()
    return {
        "taskrouter": "task_router",
        "orchestrator": "orchestrator",
        "researchagent": "research_worker",
        "codeagent": "code_worker",
        "dataagent": "data_worker",
        "synthesisagent": "synthesis_worker",
        "reviewer": "reviewer",
    }.get(label, "node")


# ---------------------------------------------------------------------------
# 工作流执行（独立线程）
# ---------------------------------------------------------------------------
def _run_graph_stream(
    task: str, thread_id: str, event_queue: queue.Queue, resume: bool = False
) -> None:
    """在独立线程中执行 LangGraph 工作流，并将每个节点的输出和状态变化放入队列。"""
    from .config import get_settings
    from .agents import summarize_resource_events
    from .graph import build_graph, refresh_resume_deadline
    from .state import create_initial_state

    settings = get_settings()
    if not settings.openai_api_key:
        try:
            record = _load_task_record(thread_id) or {
                "task": task,
                "metadata": {"thread_id": thread_id},
                "created_at": time.time(),
            }
            record.update({"status": "failed", "error": "OPENAI_API_KEY 未配置"})
            record.update({"current_agent": "error", "updated_at": time.time()})
            _save_task_record(thread_id, record)
        except RedisError:
            pass
        event_queue.put(
            {"type": "error", "error": "请先在 .env 中设置 OPENAI_API_KEY"}
        )
        with _active_threads_lock:
            _active_threads.discard(thread_id)
        return

    try:
        graph = build_graph(settings)
    except Exception:
        # Constructor/configuration failures must close SSE too; otherwise the
        # UI keeps displaying an initial phase forever with no graph running.
        error = "无法初始化工作流，请核查模型或长期记忆配置。"
        try:
            record = _load_task_record(thread_id) or {"task": task, "created_at": time.time()}
            record.update(status="failed", error=error, current_agent="error", updated_at=time.time())
            _save_task_record(thread_id, record)
        except RedisError:
            pass
        event_queue.put({"type": "error", "error": error})
        with _active_threads_lock:
            _active_threads.discard(thread_id)
        return
    config = {"configurable": {"thread_id": thread_id}}
    runtime_record = _load_task_record(thread_id) or {}
    graph_input: Dict[str, Any] | None = create_initial_state(
        task,
        thread_id,
        conversation_context=runtime_record.get("conversation_context", []),
        workspace_path=runtime_record.get("workspace_path", ""),
        conversation_id=runtime_record.get("conversation_id", thread_id),
        memory_required_ids=runtime_record.get("memory_required_ids", []),
    )
    final_state: Dict[str, Any] = dict(graph_input)

    if resume:
        snapshot = graph.get_state(config)
        if snapshot.values and snapshot.next:
            refresh_resume_deadline(graph, config, settings)
            graph_input = None
            final_state = dict(graph.get_state(config).values)
            event_queue.put(
                {"type": "status", "agent": "workflow", "state": "resuming"}
            )
        elif snapshot.values:
            # The graph had already finished before the process stopped; rebuild
            # the task record from its terminal checkpoint without re-execution.
            graph_input = None
            final_state = dict(snapshot.values)

    event_queue.put({"type": "status", "agent": "task_router", "state": "thinking"})

    try:
        for output in graph.stream(graph_input, config=config):
            for node_name, state_update in output.items():
                # 当前节点完成，发送结果
                content = ""
                if "messages" in state_update and state_update["messages"]:
                    last_msg = state_update["messages"][-1]
                    content = (
                        last_msg.content
                        if hasattr(last_msg, "content")
                        else str(last_msg)
                    )

                if content:
                    event_queue.put(
                        {
                            "type": "node",
                            "node": node_name,
                            "content": content,
                        }
                    )
                for trace in state_update.get("decision_traces", []):
                    event_queue.put(
                        {
                            "type": "trace",
                            "node": node_name,
                            "trace": trace,
                        }
                    )
                final_state.update(state_update)
                next_agent = _next_running_agent(node_name, final_state)
                _save_running_progress(
                    thread_id,
                    task,
                    node_name,
                    state_update,
                    content,
                    current_agent=next_agent,
                )

                event_queue.put(
                    {"type": "status", "agent": node_name, "state": "complete"}
                )

                status_event = {
                    "type": "status",
                    "agent": next_agent,
                    "state": "thinking",
                }
                if node_name == "prepare_batch":
                    status_event["batch"] = state_update.get("current_batch_ids", [])
                if node_name == "reviewer":
                    if next_agent == "revision":
                        status_event.update(
                            state="retrying",
                            targets=state_update.get("revision_targets", []),
                        )
                    else:
                        status_event["state"] = "complete"
                event_queue.put(status_event)

        # 并行 Worker 的 reducer 合并结果以 checkpoint 中的最终状态为准。
        final_state = dict(graph.get_state(config).values)

        # 保存生成的代码片段
        code_snippets = final_state.get("code_snippets", [])
        saved_files = []
        save_errors: list[str] = []
        if code_snippets:
            bound_workspace = final_state.get("workspace_path", "")
            workspace_dir = (
                Path(bound_workspace) / ".agentg" / thread_id
                if bound_workspace
                else _BASE_DIR / "workspace" / thread_id
            )
            try:
                if is_memory_path(workspace_dir, configured_memory_roots(settings)):
                    raise PermissionError("代码产物不能写入长期记忆目录")
                workspace_dir.mkdir(parents=True, exist_ok=True)
                for i, code in enumerate(code_snippets):
                    file_path = workspace_dir / f"agent_generated_code_{i + 1}.py"
                    file_path.write_text(code, encoding="utf-8")
                    saved_files.append(str(file_path))
            except OSError as exc:
                save_errors.append(f"代码产物保存失败: {exc}")

        # 写入内存存储
        messages_for_store = []
        for m in final_state.get("messages", []):
            content = m.content if hasattr(m, "content") else str(m)
            messages_for_store.append(
                {
                    "content": content,
                    "type": m.__class__.__name__,
                    "source": _message_source(content),
                }
            )

        termination = final_state.get("termination", {})
        previous_record = _load_task_record(thread_id) or {}
        completed_record = {
            **{
                field: previous_record[field]
                for field in _CONVERSATION_FIELDS
                if field in previous_record
            },
            "task": task,
            "status": "terminated" if termination else "completed",
            "routing": final_state.get("routing", {}),
            "plan": final_state.get("plan", ""),
            "subtasks": final_state.get("subtasks", []),
            "worker_results": final_state.get("worker_results", []),
            "execution_result": final_state.get("execution_result", ""),
            "review": final_state.get("review", ""),
            "review_status": final_state.get("review_status", ""),
            "review_decision": final_state.get("review_decision", {}),
            "revision_targets": final_state.get("revision_targets", []),
            "revision_instructions": final_state.get("revision_instructions", []),
            "is_pass": final_state.get("is_pass", False),
            "attempts": final_state.get("attempts", 0),
            "error_logs": final_state.get("error_logs", []) + save_errors,
            "code_snippets": final_state.get("code_snippets", []),
            "resource_policy": final_state.get("resource_policy", {}),
            "memory_policy": final_state.get("memory_policy", {}),
            **{field: final_state.get(field, default) for field, default in (
                ("memory_binding", {}), ("memory_required_ids", []), ("memory_event_at", ""),
                ("retrieved_memories", {}), ("worker_memories", {}), ("worker_memory_grants", {}), ("memory_write_plans", []),
                ("memory_write_results", []), ("memory_notices", []))},
            "conversation_context": final_state.get("conversation_context", []),
            "resource_usage": summarize_resource_events(
                final_state.get("resource_events", [])
            ),
            "termination": termination,
            "saved_files": saved_files,
            "messages": messages_for_store,
            "decision_traces": final_state.get("decision_traces", []),
            "metadata": final_state.get("metadata", {}),
            "created_at": previous_record.get("created_at", time.time()),
            "run_started_at": previous_record.get("run_started_at"),
            "finished_at": time.time(),
            "updated_at": time.time(),
            "current_agent": "done",
        }
        _save_task_record(thread_id, completed_record)

        event_queue.put({"type": "done", "thread_id": thread_id})

    except Exception as exc:
        try:
            previous_record = _load_task_record(thread_id) or {}
            _save_task_record(
                thread_id,
                {
                    **{
                        field: previous_record[field]
                        for field in _CONVERSATION_FIELDS
                        if field in previous_record
                    },
                    "task": task,
                    "status": "failed",
                    "error": str(exc),
                    "metadata": {"thread_id": thread_id},
                    "created_at": previous_record.get("created_at", time.time()),
                    "run_started_at": previous_record.get("run_started_at"),
                    "finished_at": time.time(),
                    "updated_at": time.time(),
                    "current_agent": "error",
                },
            )
        except RedisError:
            pass
        event_queue.put({"type": "error", "error": str(exc)})
    finally:
        with _active_threads_lock:
            _active_threads.discard(thread_id)


# ---------------------------------------------------------------------------
# SSE 事件生成器
# ---------------------------------------------------------------------------
async def _sse_generator(event_queue: queue.Queue):
    """异步生成器：从队列读取事件 → SSE 格式字符串。"""
    while True:
        try:
            event = await asyncio.get_event_loop().run_in_executor(
                None, lambda: event_queue.get(timeout=30)
            )
        except queue.Empty:
            # 心跳防止连接超时
            yield "event: heartbeat\ndata: {}\n\n"
            continue

        event_type = event.get("type", "message")
        payload = json.dumps(event, ensure_ascii=False)
        yield f"event: {event_type}\ndata: {payload}\n\n"

        if event_type in ("done", "error"):
            break


# ===================================================================
# API 端点
# ===================================================================
@app.get("/api/health")
async def health_check():
    """健康检查。"""
    try:
        repository = _get_task_repository()
        tasks_stored = len(_list_task_records())
        persistence = "redis" if repository is not None else "memory"
        redis_connected = repository.ping() if repository is not None else None
    except RedisError:
        return {
            "status": "degraded",
            "version": "1.0.0",
            "tasks_stored": len(_task_store),
            "persistence": "redis",
            "redis_connected": False,
        }
    return {
        "status": "ok",
        "version": "1.0.0",
        "tasks_stored": tasks_stored,
        "persistence": persistence,
        "redis_connected": redis_connected,
    }


# ---- 任务 CRUD ----
@app.post("/api/tasks")
async def create_task(request: Request):
    """提交新任务，返回 thread_id。客户端随后应调用 /stream 端点开始执行。"""
    body = await request.json()
    if any((key.startswith("memory_") and key != "memory_required_ids") or key in {
            "retrieved_memories", "worker_memories", "worker_memory_grants"} for key in body):
        raise HTTPException(status_code=400, detail="长期记忆身份与投影只能由宿主绑定")
    required_ids = body.get("memory_required_ids", [])
    from .memory_records import Identifier
    from pydantic import TypeAdapter, ValidationError
    try:
        required_ids = TypeAdapter(list[Identifier]).validate_python(required_ids, strict=True)
        if len(required_ids) > 20 or len(set(required_ids)) != len(required_ids):
            raise ValueError("invalid required memories")
    except (ValueError, ValidationError):
        raise HTTPException(status_code=400, detail="必要记忆必须为不重复的 ID 数组，最多 20 条") from None
    task = body.get("task", "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="任务描述不能为空")

    thread_id = str(uuid.uuid4())[:8]
    parent_thread_id = str(body.get("parent_thread_id") or "").strip()
    requested_workspace = _normalise_workspace_path(body.get("workspace_path", ""))
    conversation_id = thread_id
    conversation_title = task
    turn_index = 1
    conversation_context: list[Dict[str, str]] = []
    workspace_path = requested_workspace
    is_pinned = False

    if parent_thread_id:
        try:
            parent = _load_task_record(parent_thread_id)
        except RedisError as exc:
            raise HTTPException(
                status_code=503, detail="Redis 持久化服务不可用"
            ) from exc
        if parent is None:
            raise HTTPException(status_code=404, detail="要继续的会话不存在")
        if parent.get("status") not in {"completed", "terminated", "failed"}:
            raise HTTPException(status_code=409, detail="当前会话运行结束后才能继续")

        parent_workspace = str(parent.get("workspace_path") or "")
        if parent_workspace and requested_workspace and requested_workspace != parent_workspace:
            raise HTTPException(status_code=409, detail="同一会话不能切换已绑定的工作目录")
        workspace_path = parent_workspace or requested_workspace
        if parent.get("memory_binding"):
            from .config import get_settings
            host = get_settings()
            binding = parent["memory_binding"]
            if (binding.get("user_id"), binding.get("project_id")) != (host.memory_user_id, host.memory_project_id or None):
                raise HTTPException(status_code=409, detail="当前宿主身份与原会话绑定不同，请恢复原配置或新建会话")
        conversation_id = str(parent.get("conversation_id") or parent_thread_id)
        conversation_title = str(parent.get("conversation_title") or parent.get("task") or task)
        turn_index = int(parent.get("turn_index", 1)) + 1
        conversation_context = list(parent.get("conversation_context", []))
        is_pinned = bool(parent.get("is_pinned", False))
        conversation_context.append({"role": "user", "content": str(parent.get("task", ""))})
        prior_result = _conversation_result(parent)
        if prior_result:
            conversation_context.append({"role": "assistant", "content": prior_result})
        else:
            conversation_context.append({"role": "assistant", "content": "上一轮未产生可交付结果。"})
        from .config import get_settings
        memory_settings = get_settings()
        try:
            conversation_context = compact_conversation_history(
                conversation_context,
                max_tokens=memory_settings.conversation_max_tokens,
                summary_tokens=memory_settings.summary_max_tokens,
                recent_turns=memory_settings.recent_turns,
            )
        except ContextBudgetExceeded as exc:
            raise HTTPException(status_code=422, detail="会话记忆容量不足，请增大上下文配置或新建会话。") from exc

    record = {
        "task": task,
        "status": "pending",
        "conversation_id": conversation_id,
        "conversation_title": conversation_title,
        "parent_thread_id": parent_thread_id or None,
        "turn_index": turn_index,
        "conversation_context": conversation_context,
        "workspace_path": workspace_path,
        "memory_required_ids": required_ids,
        "is_pinned": is_pinned,
        "metadata": {"thread_id": thread_id},
        "created_at": time.time(),
    }
    try:
        _save_task_record(thread_id, record)
    except RedisError as exc:
        _task_store.pop(thread_id, None)
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc

    return {
        "thread_id": thread_id,
        "conversation_id": conversation_id,
        "task": task,
        "status": "pending",
        "turn_index": turn_index,
        "workspace_path": workspace_path,
        "is_pinned": is_pinned,
    }


@app.get("/api/tasks")
async def list_tasks():
    """列出所有历史任务（按时间倒序）。"""
    items = []
    try:
        records = _list_task_records()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc
    for tid, rec in records:
        items.append(
            {
                "thread_id": tid,
                "task": rec.get("task", ""),
                "conversation_id": rec.get("conversation_id", tid),
                "conversation_title": rec.get("conversation_title", rec.get("task", "")),
                "parent_thread_id": rec.get("parent_thread_id"),
                "turn_index": rec.get("turn_index", 1),
                "workspace_path": rec.get("workspace_path", ""),
                "is_pinned": bool(rec.get("is_pinned", False)),
                "status": rec.get("status", "unknown"),
                "outcome": _task_outcome(rec),
                "is_active": _is_thread_active(tid),
                "is_pass": rec.get("is_pass"),
                "attempts": rec.get("attempts", 0),
                "created_at": rec.get("created_at", 0),
            }
        )
    return items


@app.get("/api/tasks/{thread_id}")
async def get_task(thread_id: str):
    """获取某个任务的完整详情。"""
    try:
        rec = _load_task_record(thread_id)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc
    if rec is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "thread_id": thread_id,
        **{k: v for k, v in rec.items() if k != "metadata"},
        "outcome": _task_outcome(rec),
        "is_active": _is_thread_active(thread_id),
        "user_summary": _user_summary(rec),
        "conversation_turns": _conversation_turns(thread_id, rec),
        "metadata": rec.get("metadata", {}),
    }


@app.patch("/api/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, request: Request):
    """Rename or pin every run belonging to one conversation."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    has_title = "title" in body
    has_pinned = "is_pinned" in body
    if not has_title and not has_pinned:
        raise HTTPException(status_code=400, detail="缺少可更新的会话字段")

    title: str | None = None
    if has_title:
        if not isinstance(body["title"], str):
            raise HTTPException(status_code=400, detail="会话名称必须是字符串")
        title = body["title"].strip()
        if not title:
            raise HTTPException(status_code=400, detail="会话名称不能为空")
        if len(title) > 120:
            raise HTTPException(status_code=400, detail="会话名称不能超过 120 个字符")

    is_pinned: bool | None = None
    if has_pinned:
        if not isinstance(body["is_pinned"], bool):
            raise HTTPException(status_code=400, detail="is_pinned 必须是布尔值")
        is_pinned = body["is_pinned"]

    try:
        with _task_store_lock:
            records = _conversation_records(conversation_id)
            if not records:
                raise HTTPException(status_code=404, detail="会话不存在")
            changed_at = time.time()
            for thread_id, record in records:
                if title is not None:
                    record["conversation_title"] = title
                if is_pinned is not None:
                    record["is_pinned"] = is_pinned
                record["conversation_updated_at"] = changed_at
                _save_task_record(thread_id, record)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc

    latest = records[0][1]
    return {
        "conversation_id": conversation_id,
        "conversation_title": latest.get("conversation_title", ""),
        "is_pinned": bool(latest.get("is_pinned", False)),
        "updated_runs": len(records),
    }


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str,
                              memory_cleanup: Literal["conversation", "all_related"] = "conversation"):
    """Delete an entire conversation and every persisted run checkpoint."""
    try:
        records = _conversation_records(conversation_id)
        if not records:
            raise HTTPException(status_code=404, detail="会话不存在")
        thread_ids = [thread_id for thread_id, _ in records]
        with _active_threads_lock:
            if any(thread_id in _active_threads for thread_id in thread_ids):
                raise HTTPException(status_code=409, detail="运行中的会话不能删除")

        # SQLite first, so an unavailable memory store cannot silently orphan
        # private memory. Retrying after a Redis failure sees terminal tombstones.
        from .memory_api import cleanup_conversation_memory
        deleted_memories = cleanup_conversation_memory(conversation_id, memory_cleanup)

        repository = _get_task_repository()
        checkpoint_saver = None
        if repository is not None:
            from .config import get_settings

            checkpoint_saver = RedisCheckpointSaver(get_settings().redis_url)
        for thread_id in thread_ids:
            if checkpoint_saver is not None:
                checkpoint_saver.delete_thread(thread_id)
            _delete_task_record(thread_id)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc
    return {
        "deleted": True,
        "conversation_id": conversation_id,
        "deleted_runs": len(records),
        "deleted_memories": deleted_memories,
        "memory_cleanup": memory_cleanup,
    }


@app.delete("/api/tasks/{thread_id}")
async def delete_task(thread_id: str):
    """删除任务记录及其全部 LangGraph checkpoints。"""
    with _active_threads_lock:
        if thread_id in _active_threads:
            raise HTTPException(status_code=409, detail="运行中的任务不能删除")
    try:
        record = _load_task_record(thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        conversation_id = record.get("conversation_id") or thread_id
        if len(_conversation_records(conversation_id)) == 1:
            from .memory_api import cleanup_conversation_memory
            cleanup_conversation_memory(conversation_id, "conversation")
        repository = _get_task_repository()
        if repository is not None:
            from .config import get_settings

            RedisCheckpointSaver(get_settings().redis_url).delete_thread(thread_id)
        _delete_task_record(thread_id)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc
    return {"deleted": True}


# ---- SSE 流式推送 ----
@app.get("/api/tasks/{thread_id}/stream")
async def stream_task(thread_id: str):
    """SSE 端点：实时推送工作流每个节点的执行结果。"""
    try:
        record = _load_task_record(thread_id)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    with _active_threads_lock:
        if thread_id in _active_threads:
            raise HTTPException(status_code=400, detail="任务已在当前进程运行")
        status = record.get("status", "pending")
        if status in {"completed", "terminated"}:
            raise HTTPException(status_code=400, detail="任务已完成")
        if status not in {"pending", "running", "failed", "interrupted"}:
            raise HTTPException(status_code=400, detail="任务状态不可恢复")
        _active_threads.add(thread_id)

    resume = status != "pending"
    run_started_at = time.time()
    record["status"] = "running"
    record["current_agent"] = "workflow"
    record["run_started_at"] = run_started_at
    record["updated_at"] = run_started_at
    record.pop("error", None)
    try:
        _save_task_record(thread_id, record)
    except RedisError as exc:
        with _active_threads_lock:
            _active_threads.discard(thread_id)
        raise HTTPException(status_code=503, detail="Redis 持久化服务不可用") from exc
    event_queue: queue.Queue = queue.Queue()

    threading.Thread(
        target=_run_graph_stream,
        args=(record["task"], thread_id, event_queue, resume),
        daemon=True,
    ).start()

    return StreamingResponse(
        _sse_generator(event_queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- 前端页面 ----
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """主页面。"""
    template = _jinja_env.get_template("index.html")
    html_content = template.render({"request": request})
    return HTMLResponse(content=html_content)


# ===================================================================
# 启动入口
# ===================================================================
def run_web_server(*, reload_override: bool | None = None) -> None:
    """Run the Web app; hot reload is an explicit local-development mode."""
    import uvicorn

    from .config import get_settings

    reload_enabled = (
        get_settings().web_reload if reload_override is None else reload_override
    )
    options: Dict[str, Any] = {
        "host": "0.0.0.0",
        "port": 8000,
        "reload": reload_enabled,
    }
    if reload_enabled:
        options.update(
            {
                "reload_dirs": [str(_BASE_DIR)],
                "reload_includes": ["*.py", "*.html", "*.css", "*.js"],
                "reload_excludes": [
                    "*/workspace/*",
                    "*/.agentg/*",
                    "*/__pycache__/*",
                    "*.pyc",
                    str(resolve_memory_db_path(get_settings().memory_db_path)) + "*",
                ],
            }
        )
    uvicorn.run("src.multi_agent_system.web_server:app", **options)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="启动 AgentG Web 服务")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="启用仅面向本地开发的源码与模板热加载",
    )
    arguments = parser.parse_args()
    run_web_server(reload_override=True if arguments.reload else None)
