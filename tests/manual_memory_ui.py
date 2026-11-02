"""Disposable UI fixture: python -m tests.manual_memory_ui (127.0.0.1:8766).

Never loads dotenv, uses Redis or calls a model. SQLite exists only in a fresh
TemporaryDirectory, removed on normal shutdown. Not an acceptance result.
"""
import os
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["USE_REDIS"] = "false"
os.environ["LONG_TERM_MEMORY_ENABLED"] = "true"
os.environ["MEMORY_USER_ID"] = "ui-test-user"
os.environ["MEMORY_PROJECT_ID"] = "ui-test-project"
os.environ["MEMORY_AUTO_EXTRACT"] = "false"


def main():
    import uvicorn
    from fastapi.responses import JSONResponse
    with TemporaryDirectory(prefix="agentg-memory-ui-") as directory:
        os.environ["MEMORY_DB_PATH"] = str(Path(directory) / "private" / "memory.sqlite3")
        from src.multi_agent_system import web_server as web
        from src.multi_agent_system.memory_records import MemoryIdentity
        from src.multi_agent_system.memory_service import MemoryService, MemoryWriteContext, MemoryEvidence, explicit_intent
        from src.multi_agent_system.memory_store import SQLiteMemoryRepository
        now = datetime.now(timezone.utc)
        host = MemoryIdentity(user_id="ui-test-user",project_id="ui-test-project",conversation_id="ui-chat")
        with SQLiteMemoryRepository(os.environ["MEMORY_DB_PATH"]) as repo:
            service = MemoryService(repo)
            for text in ("记住[用户/偏好/style]：使用中文，先给结论。", "记住[用户/经验/check]：交付前检查单位。",
                         "记住[项目/规则/layout]：保持分层模块。", "记住[会话/偏好/private]：本轮使用表格。",
                         "记住[用户/偏好/html]：<script>alert(1)</script> 只是普通文本。"):
                intent = explicit_intent(text,"ui-run")
                ctx = MemoryWriteContext(identity=host,thread_id="ui-run",at=now,user_text=text,
                    evidence=(MemoryEvidence(reference=intent.source_refs[0],namespace=host.namespace_for("conversation")),))
                service.commit(service.stage_write(intent,ctx),host)
        web._get_task_repository = lambda: None
        web._task_store["ui-run"] = {"thread_id":"ui-run","conversation_id":"ui-chat","conversation_title":"记忆管理演示（临时数据）",
            "task":"请总结已完成的测试。","status":"completed","is_pass":True,"created_at":now.timestamp(),
            "execution_result":"测试已完成，可打开记忆管理查看条目。","messages":[
                {"role":"user","content":"请总结已完成的测试。"},
                {"role":"assistant","content":"[SynthesisAgent:simple-1] 测试交付已完成。"}],
            "memory_binding":host.model_dump(mode="json")}

        @web.app.middleware("http")
        async def fixture_only(request, call_next):
            if request.url.path.startswith("/api/tasks/") and request.url.path.endswith("/stream") or (
                    request.method == "POST" and request.url.path == "/api/tasks"):
                return JSONResponse({"detail":"仅用于 UI 验证，不执行任务或模型"},status_code=405)
            return await call_next(request)

        uvicorn.run(web.app,host="127.0.0.1",port=8766,log_level="warning")


if __name__ == "__main__":
    main()
