"""Web API 测试 — 使用 FastAPI TestClient 验证所有 REST 端点和 SSE 流。"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Workflow 耗时较长，避免真的执行 LLM 调用
with patch("httpx.Client", return_value=MagicMock()):
    from multi_agent_system.web_server import app

client = TestClient(app)


def _reset_store():
    """清空内存任务存储，确保测试隔离。"""
    from multi_agent_system.web_server import _active_threads, _task_store
    _task_store.clear()
    _active_threads.clear()


@pytest.fixture(autouse=True)
def auto_clean():
    _reset_store()
    with patch(
        "multi_agent_system.web_server._get_task_repository", return_value=None
    ):
        yield
    _reset_store()


class TestHealthCheck:
    def test_health_ok(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "1.0.0"

    def test_health_counts_tasks(self):
        client.post("/api/tasks", json={"task": "test"})
        resp = client.get("/api/health")
        assert resp.json()["tasks_stored"] == 1


class TestTaskCRUD:
    def test_create_task_returns_thread_id(self):
        resp = client.post("/api/tasks", json={"task": "Analyze data"})
        assert resp.status_code == 200
        data = resp.json()
        assert "thread_id" in data
        assert data["task"] == "Analyze data"
        assert data["status"] == "pending"

    def test_create_task_empty_rejected(self):
        resp = client.post("/api/tasks", json={"task": ""})
        assert resp.status_code == 400
        assert "不能为空" in resp.json()["detail"]

    def test_create_task_missing_field(self):
        resp = client.post("/api/tasks", json={})
        assert resp.status_code == 400

    def test_list_tasks_empty(self):
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_tasks_returns_created(self):
        client.post("/api/tasks", json={"task": "Task A"})
        client.post("/api/tasks", json={"task": "Task B"})
        resp = client.get("/api/tasks")
        assert len(resp.json()) == 2

    def test_get_task_detail(self):
        create = client.post("/api/tasks", json={"task": "Detail test"})
        tid = create.json()["thread_id"]

        resp = client.get(f"/api/tasks/{tid}")
        assert resp.status_code == 200
        assert resp.json()["task"] == "Detail test"
        assert resp.json()["status"] == "pending"

    def test_get_nonexistent_task(self):
        resp = client.get("/api/tasks/nonexist")
        assert resp.status_code == 404

    def test_delete_task(self):
        create = client.post("/api/tasks", json={"task": "To delete"})
        tid = create.json()["thread_id"]

        resp = client.delete(f"/api/tasks/{tid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # 确认删除后查不到
        assert client.get(f"/api/tasks/{tid}").status_code == 404

    def test_delete_nonexistent(self):
        resp = client.delete("/api/tasks/ghost")
        assert resp.status_code == 404

    def test_task_list_sorted_by_time(self):
        client.post("/api/tasks", json={"task": "Old"})
        time.sleep(0.1)
        client.post("/api/tasks", json={"task": "New"})

        tasks = client.get("/api/tasks").json()
        # 最新创建排前面
        assert tasks[0]["task"] == "New"
        assert tasks[1]["task"] == "Old"

    def test_get_task_loads_record_from_persistent_repository(self):
        from multi_agent_system.web_server import _task_store

        repository = MagicMock()
        repository.load_task.return_value = {
            "task": "Recovered",
            "status": "running",
            "metadata": {"thread_id": "persisted"},
            "created_at": 1.0,
        }
        _task_store.clear()

        with patch(
            "multi_agent_system.web_server._get_task_repository",
            return_value=repository,
        ):
            resp = client.get("/api/tasks/persisted")

        assert resp.status_code == 200
        assert resp.json()["task"] == "Recovered"
        repository.load_task.assert_called_once_with("persisted")

    def test_list_exposes_run_success_and_failure_outcomes(self):
        from multi_agent_system.web_server import _task_store

        _task_store.update(
            {
                "running": {"task": "Run", "status": "running", "created_at": 3},
                "success": {
                    "task": "Success",
                    "status": "completed",
                    "is_pass": True,
                    "created_at": 2,
                },
                "failure": {
                    "task": "Failure",
                    "status": "terminated",
                    "created_at": 1,
                },
            }
        )

        outcomes = {
            item["thread_id"]: item["outcome"]
            for item in client.get("/api/tasks").json()
        }

        assert outcomes == {
            "running": "run",
            "success": "success",
            "failure": "failure",
        }

    def test_terminal_task_exposes_user_facing_success_summary(self):
        from multi_agent_system.web_server import _task_store

        _task_store["summary"] = {
            "task": "Summarize the data",
            "status": "completed",
            "is_pass": True,
            "subtasks": [{"id": "one"}, {"id": "two"}],
            "execution_result": (
                "```json\n"
                + json.dumps(
                    {
                        "summary": "天气查询完成",
                        "details": [
                            {"city": "北京", "condition": "晴", "temperature": "24°C"},
                            {"city": "上海", "condition": "多云", "temperature": "26°C"},
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n```"
            ),
            "created_at": 1,
        }

        detail = client.get("/api/tasks/summary").json()

        assert detail["user_summary"] == {
            "status": "success",
            "title": "任务已完成",
            "overview": "系统完成了 2 个子任务，并通过结果审查。",
            "content": (
                "天气查询完成。\n\n详细结果：\n"
                "1. 北京：天气为晴，气温为24°C。\n"
                "2. 上海：天气为多云，气温为26°C。"
            ),
            "step_count": 2,
        }
        assert "{" not in detail["user_summary"]["content"]
        assert detail["conversation_turns"][0]["user_summary"]["status"] == "success"

    @pytest.mark.parametrize(
        ("record", "title", "expected_text"),
        [
            (
                {
                    "status": "terminated",
                    "termination": {"reason": "max_tool_calls_exceeded"},
                },
                "任务因资源限制停止",
                "工具调用次数",
            ),
            (
                {"status": "failed", "error": "sensitive technical traceback"},
                "任务执行失败",
                "执行过程中发生异常",
            ),
            (
                {
                    "status": "completed",
                    "is_pass": False,
                    "review_decision": {"feedback": "缺少一个城市的结果。"},
                },
                "任务未通过结果审查",
                "尚未满足验收标准",
            ),
        ],
    )
    def test_failed_task_summary_is_plain_and_actionable(
        self, record, title, expected_text
    ):
        from multi_agent_system.web_server import _task_store

        _task_store["failed-summary"] = {
            "task": "A task",
            "created_at": 1,
            **record,
        }

        summary = client.get("/api/tasks/failed-summary").json()["user_summary"]

        assert summary["title"] == title
        assert expected_text in summary["overview"]
        assert "sensitive technical traceback" not in str(summary)

    def test_task_detail_reports_current_process_activity(self):
        from multi_agent_system.web_server import _active_threads, _task_store

        _task_store["active"] = {
            "task": "Working",
            "status": "running",
            "created_at": 1,
        }
        _active_threads.add("active")

        detail = client.get("/api/tasks/active").json()

        assert detail["outcome"] == "run"
        assert detail["is_active"] is True

    def test_running_progress_is_available_before_completion(self):
        from multi_agent_system.web_server import _save_running_progress, _task_store

        _task_store["progress"] = {
            "task": "Track me",
            "status": "running",
            "messages": [],
            "created_at": 1,
        }

        _save_running_progress(
            "progress",
            "Track me",
            "orchestrator",
            {"plan": "Research then synthesize"},
            "[Orchestrator] plan ready",
        )

        detail = client.get("/api/tasks/progress").json()
        assert detail["status"] == "running"
        assert detail["current_agent"] == "orchestrator"
        assert detail["plan"] == "Research then synthesize"
        assert detail["messages"][0]["content"] == "[Orchestrator] plan ready"

    def test_completed_task_can_continue_in_same_conversation(self):
        from multi_agent_system.web_server import _task_store

        first = client.post("/api/tasks", json={"task": "First question"}).json()
        first_id = first["thread_id"]
        _task_store[first_id].update(
            {
                "status": "completed",
                "is_pass": True,
                "execution_result": "First answer",
                "messages": [],
            }
        )

        response = client.post(
            "/api/tasks",
            json={"task": "Follow-up question", "parent_thread_id": first_id},
        )

        assert response.status_code == 200
        second = response.json()
        assert second["conversation_id"] == first_id
        assert second["turn_index"] == 2
        detail = client.get(f"/api/tasks/{second['thread_id']}").json()
        assert [turn["task"] for turn in detail["conversation_turns"]] == [
            "First question",
            "Follow-up question",
        ]
        assert detail["conversation_context"] == [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
        ]

    def test_running_task_cannot_accept_follow_up(self):
        first = client.post("/api/tasks", json={"task": "Still running"}).json()
        response = client.post(
            "/api/tasks",
            json={"task": "Too soon", "parent_thread_id": first["thread_id"]},
        )

        assert response.status_code == 409
        assert "运行结束" in response.json()["detail"]

    def test_workspace_is_validated_and_inherited(self, tmp_path):
        from multi_agent_system.web_server import _task_store

        first = client.post(
            "/api/tasks", json={"task": "Inspect project", "workspace_path": str(tmp_path)}
        ).json()
        assert first["workspace_path"] == str(tmp_path.resolve())
        _task_store[first["thread_id"]].update(
            {"status": "completed", "is_pass": True, "execution_result": "Done"}
        )

        second = client.post(
            "/api/tasks",
            json={"task": "Continue", "parent_thread_id": first["thread_id"]},
        ).json()

        assert second["workspace_path"] == str(tmp_path.resolve())

    def test_invalid_workspace_is_rejected(self, tmp_path):
        response = client.post(
            "/api/tasks",
            json={"task": "Inspect", "workspace_path": str(tmp_path / "missing")},
        )

        assert response.status_code == 400
        assert "不存在" in response.json()["detail"]

    def test_bound_conversation_cannot_switch_workspace(self, tmp_path):
        from multi_agent_system.web_server import _task_store

        first_workspace = tmp_path / "first"
        second_workspace = tmp_path / "second"
        first_workspace.mkdir()
        second_workspace.mkdir()
        first = client.post(
            "/api/tasks",
            json={"task": "First", "workspace_path": str(first_workspace)},
        ).json()
        _task_store[first["thread_id"]].update(
            {"status": "completed", "is_pass": True, "execution_result": "Done"}
        )

        response = client.post(
            "/api/tasks",
            json={
                "task": "Follow-up",
                "parent_thread_id": first["thread_id"],
                "workspace_path": str(second_workspace),
            },
        )

        assert response.status_code == 409
        assert "不能切换" in response.json()["detail"]

    def test_conversation_can_be_renamed_and_pinned(self):
        from multi_agent_system.web_server import _task_store

        first = client.post("/api/tasks", json={"task": "Original title"}).json()
        first_id = first["thread_id"]
        _task_store[first_id].update(
            {"status": "completed", "is_pass": True, "execution_result": "Done"}
        )
        second = client.post(
            "/api/tasks",
            json={"task": "Follow-up", "parent_thread_id": first_id},
        ).json()

        response = client.patch(
            f"/api/conversations/{first_id}",
            json={"title": "Renamed conversation", "is_pinned": True},
        )

        assert response.status_code == 200
        assert response.json()["updated_runs"] == 2
        assert response.json()["conversation_title"] == "Renamed conversation"
        assert response.json()["is_pinned"] is True
        assert all(
            record["conversation_title"] == "Renamed conversation"
            and record["is_pinned"] is True
            for record in (_task_store[first_id], _task_store[second["thread_id"]])
        )
        listed = client.get("/api/tasks").json()
        assert all(item["is_pinned"] is True for item in listed)

    def test_conversation_rename_rejects_blank_title(self):
        created = client.post("/api/tasks", json={"task": "Original"}).json()

        response = client.patch(
            f"/api/conversations/{created['conversation_id']}", json={"title": "   "}
        )

        assert response.status_code == 400
        assert "不能为空" in response.json()["detail"]

    def test_delete_conversation_removes_all_turns(self):
        from multi_agent_system.web_server import _task_store

        first = client.post("/api/tasks", json={"task": "First"}).json()
        _task_store[first["thread_id"]].update(
            {"status": "completed", "is_pass": True, "execution_result": "Done"}
        )
        second = client.post(
            "/api/tasks",
            json={"task": "Second", "parent_thread_id": first["thread_id"]},
        ).json()

        response = client.delete(f"/api/conversations/{first['conversation_id']}")

        assert response.status_code == 200
        assert response.json()["deleted_runs"] == 2
        assert client.get(f"/api/tasks/{first['thread_id']}").status_code == 404
        assert client.get(f"/api/tasks/{second['thread_id']}").status_code == 404

    def test_running_conversation_cannot_be_deleted(self):
        from multi_agent_system.web_server import _active_threads

        created = client.post("/api/tasks", json={"task": "Running"}).json()
        _active_threads.add(created["thread_id"])

        response = client.delete(
            f"/api/conversations/{created['conversation_id']}"
        )

        assert response.status_code == 409
        assert "运行中" in response.json()["detail"]


class TestHomePage:
    def test_index_returns_html(self):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "AgentG" in resp.text
        assert "orchestrator" in resp.text.lower()
        assert "researchagent" in resp.text.lower()

    def test_index_has_form_elements(self):
        resp = client.get("/")
        assert "taskInput" in resp.text
        assert "submitBtn" in resp.text
        assert "historyList" in resp.text

    def test_index_uses_selected_researcher_core_logo(self):
        html = client.get("/").text

        assert 'aria-label="AgentG Researcher Core Logo"' in html
        assert '<symbol id="researcher-core-mark" viewBox="0 0 100 100">' in html
        assert '<svg viewBox="0 0 100 100"' in html
        assert 'stroke="currentColor"' in html
        assert 'fill="currentColor"' in html
        assert ".brand-mark svg { width:27px; height:27px" in html
        assert ".empty-icon svg { width:42px; height:42px" in html
        assert html.count('href="#researcher-core-mark"') == 3
        assert '<div class="brand-mark">G</div>' not in html
        assert '<div class="empty-icon">⌁</div>' not in html

    def test_agent_workspace_layout_places_composer_after_execution_area(self):
        html = client.get("/").text
        assert html.index('id="historyList"') < html.index('id="outputArea"')
        assert html.index('id="outputArea"') < html.index('id="taskForm"')
        assert "Multi-Agent Workspace" in html

    def test_frontend_restores_selected_task_and_uses_outcome_labels(self):
        html = client.get("/").text
        assert "searchParams.get('task')" in html
        assert "followActiveTask" in html
        assert "Success" in html
        assert "Failure" in html

    def test_frontend_supports_scroll_conversation_and_workspace_binding(self):
        html = client.get("/").text

        assert 'id="workspaceInput"' in html
        assert "parent_thread_id" in html
        assert "conversation_turns" in html
        assert "position:relative; min-height:0; height:0; flex:1 1 auto" in html
        assert "max-height:440px" not in html

    def test_frontend_exposes_theme_and_conversation_context_menu(self):
        html = client.get("/").text

        assert 'id="themeToggle"' in html
        assert 'id="conversationMenu"' in html
        assert "contextmenu" in html
        assert "/api/conversations/" in html

    def test_frontend_supports_collapsible_steps_turn_navigation_and_summary(self):
        html = client.get("/").text

        assert 'id="turnNav"' in html
        assert 'id="turnNavRail"' in html
        assert "turn-nav-panel" in html
        assert "turn-nav-rail-marker" in html
        assert "top:50%; right:15px" in html
        assert "flex-direction:column; justify-content:center" in html
        assert "width:100%; height:100%" in html
        assert ".output::-webkit-scrollbar" in html
        assert "data-turn-anchor" in html
        assert "setCardCollapsed" in html
        assert ".card.collapsed .card-body" in html
        assert "user_summary" in html
        assert "结果总结 · 用户视图" in html


class TestWebServerRunner:
    def test_reload_mode_watches_only_source_and_templates(self):
        from multi_agent_system.web_server import run_web_server

        with patch("uvicorn.run") as run:
            run_web_server(reload_override=True)

        kwargs = run.call_args.kwargs
        assert kwargs["reload"] is True
        assert kwargs["reload_dirs"]
        assert "*.html" in kwargs["reload_includes"]
        assert "*/workspace/*" in kwargs["reload_excludes"]

    def test_stable_mode_keeps_reload_disabled(self):
        from multi_agent_system.web_server import run_web_server

        with patch("uvicorn.run") as run:
            run_web_server(reload_override=False)

        assert run.call_args.kwargs == {
            "host": "0.0.0.0",
            "port": 8000,
            "reload": False,
        }


class TestStreamEndpoint:
    def test_stream_nonexistent_task(self):
        resp = client.get("/api/tasks/ghost/stream")
        assert resp.status_code == 404

    def test_stream_already_running_task(self):
        create = client.post("/api/tasks", json={"task": "SSE test"})
        tid = create.json()["thread_id"]
        # 手动将状态改为 running
        from multi_agent_system.web_server import _active_threads, _task_store
        _task_store[tid]["status"] = "running"
        _active_threads.add(tid)

        resp = client.get(f"/api/tasks/{tid}/stream")
        assert resp.status_code == 400
        assert "已" in resp.json()["detail"]
