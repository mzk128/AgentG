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
    from multi_agent_system.web_server import _task_store
    _task_store.clear()


@pytest.fixture(autouse=True)
def auto_clean():
    _reset_store()
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


class TestStreamEndpoint:
    def test_stream_nonexistent_task(self):
        resp = client.get("/api/tasks/ghost/stream")
        assert resp.status_code == 404

    def test_stream_already_running_task(self):
        create = client.post("/api/tasks", json={"task": "SSE test"})
        tid = create.json()["thread_id"]
        # 手动将状态改为 running
        from multi_agent_system.web_server import _task_store
        _task_store[tid]["status"] = "running"

        resp = client.get(f"/api/tasks/{tid}/stream")
        assert resp.status_code == 400
        assert "已" in resp.json()["detail"]
