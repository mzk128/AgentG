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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
if sys.version_info < (3, 10):
    raise RuntimeError("当前项目需要 Python 3.10+，请切换解释器后重试。")

app = FastAPI(title="AgentG — 多智能体协作系统", version="1.0.0")

_executor = ThreadPoolExecutor(max_workers=4)

_BASE_DIR = Path(__file__).resolve().parent

# 模板引擎
_templates_dir = _BASE_DIR / "web_templates"
_templates_dir.mkdir(exist_ok=True)
_jinja_env = Environment(loader=FileSystemLoader(str(_templates_dir)), autoescape=True)

# 内存任务存储: thread_id → record
_task_store: Dict[str, Dict[str, Any]] = {}


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
    task: str, thread_id: str, event_queue: queue.Queue
) -> None:
    """在独立线程中执行 LangGraph 工作流，并将每个节点的输出和状态变化放入队列。"""
    from .config import get_settings
    from .agents import summarize_resource_events
    from .graph import build_graph
    from .state import create_initial_state

    settings = get_settings()
    if not settings.openai_api_key:
        event_queue.put(
            {"type": "error", "error": "请先在 .env 中设置 OPENAI_API_KEY"}
        )
        return

    graph = build_graph(settings)

    initial_state = create_initial_state(task, thread_id)

    config = {"configurable": {"thread_id": thread_id}}
    final_state: Dict[str, Any] = dict(initial_state)

    event_queue.put({"type": "status", "agent": "task_router", "state": "thinking"})

    try:
        for output in graph.stream(initial_state, config=config):
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
                final_state.update(state_update)

                event_queue.put(
                    {"type": "status", "agent": node_name, "state": "complete"}
                )

                if node_name == "task_router":
                    next_agent = (
                        "orchestrator"
                        if state_update.get("routing", {}).get("route") == "team"
                        else "workers"
                    )
                    event_queue.put(
                        {"type": "status", "agent": next_agent, "state": "thinking"}
                    )
                elif node_name == "prepare_batch":
                    event_queue.put(
                        {
                            "type": "status",
                            "agent": "workers",
                            "state": "thinking",
                            "batch": state_update.get("current_batch_ids", []),
                        }
                    )
                if node_name == "reviewer":
                    review_status = state_update.get("review_status", "")
                    attempts = state_update.get("attempts", 0)
                    if review_status == "revise" and attempts < 2:
                        event_queue.put(
                            {
                                "type": "status",
                                "agent": "revision",
                                "state": "retrying",
                                "targets": state_update.get("revision_targets", []),
                            }
                        )
                    else:
                        event_queue.put(
                            {"type": "status", "agent": "done", "state": "complete"}
                        )

        # 并行 Worker 的 reducer 合并结果以 checkpoint 中的最终状态为准。
        final_state = dict(graph.get_state(config).values)

        # 保存生成的代码片段
        code_snippets = final_state.get("code_snippets", [])
        saved_files = []
        if code_snippets:
            workspace_dir = _BASE_DIR / "workspace"
            workspace_dir.mkdir(exist_ok=True)
            for i, code in enumerate(code_snippets):
                file_path = workspace_dir / f"agent_generated_code_{i + 1}.py"
                file_path.write_text(code, encoding="utf-8")
                saved_files.append(str(file_path))

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
        _task_store[thread_id] = {
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
            "is_pass": final_state.get("is_pass", False),
            "attempts": final_state.get("attempts", 0),
            "error_logs": final_state.get("error_logs", []),
            "code_snippets": final_state.get("code_snippets", []),
            "resource_policy": final_state.get("resource_policy", {}),
            "resource_usage": summarize_resource_events(
                final_state.get("resource_events", [])
            ),
            "termination": termination,
            "saved_files": saved_files,
            "messages": messages_for_store,
            "metadata": final_state.get("metadata", {}),
            "created_at": _task_store.get(thread_id, {}).get("created_at", time.time()),
        }

        event_queue.put({"type": "done", "thread_id": thread_id})

    except Exception as exc:
        _task_store[thread_id] = {
            "task": task,
            "status": "failed",
            "error": str(exc),
            "metadata": {"thread_id": thread_id},
            "created_at": time.time(),
        }
        event_queue.put({"type": "error", "error": str(exc)})


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
    return {"status": "ok", "version": "1.0.0", "tasks_stored": len(_task_store)}


# ---- 任务 CRUD ----
@app.post("/api/tasks")
async def create_task(request: Request):
    """提交新任务，返回 thread_id。客户端随后应调用 /stream 端点开始执行。"""
    body = await request.json()
    task = body.get("task", "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="任务描述不能为空")

    thread_id = str(uuid.uuid4())[:8]
    _task_store[thread_id] = {
        "task": task,
        "status": "pending",
        "metadata": {"thread_id": thread_id},
        "created_at": time.time(),
    }

    return {"thread_id": thread_id, "task": task, "status": "pending"}


@app.get("/api/tasks")
async def list_tasks():
    """列出所有历史任务（按时间倒序）。"""
    items = []
    for tid, rec in sorted(
        _task_store.items(),
        key=lambda kv: kv[1].get("created_at", 0),
        reverse=True,
    ):
        items.append(
            {
                "thread_id": tid,
                "task": rec.get("task", ""),
                "status": rec.get("status", "unknown"),
                "is_pass": rec.get("is_pass"),
                "attempts": rec.get("attempts", 0),
                "created_at": rec.get("created_at", 0),
            }
        )
    return items


@app.get("/api/tasks/{thread_id}")
async def get_task(thread_id: str):
    """获取某个任务的完整详情。"""
    if thread_id not in _task_store:
        raise HTTPException(status_code=404, detail="任务不存在")

    rec = _task_store[thread_id]
    return {
        "thread_id": thread_id,
        **{k: v for k, v in rec.items() if k != "metadata"},
        "metadata": rec.get("metadata", {}),
    }


@app.delete("/api/tasks/{thread_id}")
async def delete_task(thread_id: str):
    """删除任务记录。"""
    if thread_id in _task_store:
        del _task_store[thread_id]
        return {"deleted": True}
    raise HTTPException(status_code=404, detail="任务不存在")


# ---- SSE 流式推送 ----
@app.get("/api/tasks/{thread_id}/stream")
async def stream_task(thread_id: str):
    """SSE 端点：实时推送工作流每个节点的执行结果。"""
    if thread_id not in _task_store:
        raise HTTPException(status_code=404, detail="任务不存在")

    record = _task_store[thread_id]
    if record["status"] != "pending":
        raise HTTPException(status_code=400, detail="任务已在运行或已完成")

    record["status"] = "running"
    event_queue: queue.Queue = queue.Queue()

    threading.Thread(
        target=_run_graph_stream,
        args=(record["task"], thread_id, event_queue),
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
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.multi_agent_system.web_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
