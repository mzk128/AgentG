from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from multi_agent_system.graph import (
    MAX_REVIEW_ROUNDS,
    _affected_task_ids,
    build_graph,
    dispatch_workers,
    fan_in_node,
    initialize_resources_node,
    prepare_batch_node,
    prepare_revision_node,
    refresh_resume_deadline,
    route_after_fan_in,
    route_after_review,
    route_task,
    resource_terminated_node,
)
from multi_agent_system.storage import RedisCheckpointSaver


def _workflow_state():
    return {
        "routing": {"route": "team"},
        "subtasks": [
            {
                "id": "research",
                "agent_type": "research",
                "objective": "find facts",
                "dependencies": [],
                "allowed_tools": ["web_search"],
                "output_format": "facts",
                "acceptance_criteria": ["sources"],
            },
            {
                "id": "data",
                "agent_type": "data",
                "objective": "analyze",
                "dependencies": [],
                "allowed_tools": ["python_repl"],
                "output_format": "metrics",
                "acceptance_criteria": ["correct"],
            },
            {
                "id": "synthesis",
                "agent_type": "synthesis",
                "objective": "combine",
                "dependencies": ["research", "data"],
                "allowed_tools": [],
                "output_format": "answer",
                "acceptance_criteria": ["complete"],
            },
        ],
        "pending_task_ids": ["research", "data", "synthesis"],
        "current_batch_ids": [],
        "worker_results": [],
        "worker_attempts": {},
        "revision_feedback": "",
        "attempts": 0,
    }


class TestTaskRouting:
    def test_simple_route(self):
        assert route_task({"routing": {"route": "simple"}}) == "prepare_simple"

    def test_team_route(self):
        assert route_task({"routing": {"route": "team"}}) == "orchestrator"

    def test_missing_route_defaults_to_team(self):
        assert route_task({}) == "orchestrator"

    def test_termination_routes_to_terminal_node(self):
        assert route_task({"termination": {"reason": "limit"}}) == "resource_terminated"


class TestDependencyScheduler:
    def test_independent_tasks_share_first_batch(self):
        state = _workflow_state()
        result = prepare_batch_node(state)
        assert result["current_batch_ids"] == ["research", "data"]

    def test_dependent_task_waits(self):
        state = _workflow_state()
        state["pending_task_ids"] = ["data", "synthesis"]
        result = prepare_batch_node(state)
        assert result["current_batch_ids"] == ["data"]

    def test_next_batch_after_fan_in(self):
        state = _workflow_state()
        state["current_batch_ids"] = ["research", "data"]
        state["worker_results"] = [
            {"task_id": "research", "attempt": 1, "content": "facts"},
            {"task_id": "data", "attempt": 1, "content": "metrics"},
        ]
        result = fan_in_node(state)
        assert result["pending_task_ids"] == ["synthesis"]
        assert route_after_fan_in(result) == "prepare_batch"

    def test_final_synthesis_becomes_execution_result(self):
        state = _workflow_state()
        state["pending_task_ids"] = ["synthesis"]
        state["current_batch_ids"] = ["synthesis"]
        state["worker_results"] = [
            {"task_id": "synthesis", "attempt": 1, "content": "final answer"}
        ]
        result = fan_in_node(state)
        assert result["execution_result"] == "final answer"
        assert route_after_fan_in(result) == "reviewer"

    def test_dispatch_uses_worker_specific_node(self):
        state = _workflow_state()
        state["current_batch_ids"] = ["research", "data"]
        sends = dispatch_workers(state)
        assert [send.node for send in sends] == ["research_worker", "data_worker"]

    def test_dispatch_includes_only_needed_upstream_results(self):
        state = _workflow_state()
        state["pending_task_ids"] = ["synthesis"]
        state["current_batch_ids"] = ["synthesis"]
        state["worker_results"] = [
            {"task_id": "research", "attempt": 1, "content": "facts"},
            {"task_id": "data", "attempt": 1, "content": "metrics"},
            {"task_id": "unrelated", "attempt": 1, "content": "ignore"},
        ]
        sends = dispatch_workers(state)
        upstream_ids = {item["task_id"] for item in sends[0].arg["upstream_results"]}
        assert upstream_ids == {"research", "data"}

    def test_dispatch_increments_per_task_attempt(self):
        state = _workflow_state()
        state["current_batch_ids"] = ["research"]
        state["worker_attempts"] = {"research": 2}
        sends = dispatch_workers(state)
        assert sends[0].arg["attempt"] == 3

    def test_dispatch_passes_only_bound_workspace_to_worker(self):
        state = _workflow_state()
        state["current_batch_ids"] = ["data"]
        state["workspace_path"] = r"D:\projects\demo"

        sends = dispatch_workers(state)

        assert sends[0].arg["workspace_path"] == r"D:\projects\demo"

    def test_parallel_dispatch_uses_non_overlapping_quotas(self):
        state = _workflow_state()
        state["current_batch_ids"] = ["research", "data"]
        state["resource_policy"] = {
            "total_action_budget": 7,
            "max_tool_calls": 3,
            "max_tool_rounds": 2,
            "max_consecutive_errors": 1,
            "deadline_at": 9999999999.0,
        }
        sends = dispatch_workers(state)
        assert sum(send.arg["budget_quota"] for send in sends) == 7
        assert sum(send.arg["tool_call_quota"] for send in sends) == 3
        assert all(send.arg["budget_quota"] >= 1 for send in sends)

    def test_exhausted_budget_stops_batch(self):
        state = _workflow_state()
        state["resource_policy"] = {
            "total_action_budget": 1,
            "deadline_at": 9999999999.0,
        }
        state["resource_events"] = [{"budget_used": 1}]
        result = prepare_batch_node(state)
        assert result["current_batch_ids"] == []
        assert result["termination"]["reason"] == "total_action_budget_exhausted"


class TestResourceLifecycle:
    def test_initialize_resources_sets_deadline(self, settings):
        result = initialize_resources_node({}, settings)
        policy = result["resource_policy"]
        assert policy["deadline_at"] > policy["started_at"]
        assert policy["max_tool_calls"] == settings.max_tool_calls
        assert policy["token_budget_enabled"] is False

    def test_resource_termination_is_blocked(self):
        result = resource_terminated_node(
            {"termination": {"source": "worker:x", "reason": "max_tool_calls_exceeded"}}
        )
        assert result["review_status"] == "blocked"
        assert result["is_pass"] is False
        assert "max_tool_calls_exceeded" in result["review"]

    def test_resume_refreshes_only_wall_clock_deadline(self, settings):
        graph = MagicMock()
        graph.get_state.return_value = SimpleNamespace(
            values={
                "resource_policy": {
                    "max_tool_calls": 12,
                    "started_at": 10.0,
                    "deadline_at": 20.0,
                }
            },
            next=("research_worker",),
        )

        with patch("multi_agent_system.graph.time.time", return_value=100.0):
            assert refresh_resume_deadline(graph, {"configurable": {}}, settings)

        policy = graph.update_state.call_args.args[1]["resource_policy"]
        assert policy["started_at"] == 10.0
        assert policy["resumed_at"] == 100.0
        assert policy["deadline_at"] == 100.0 + settings.max_run_seconds
        assert policy["max_tool_calls"] == 12


class TestTargetedRevision:
    def test_affected_tasks_include_downstream_only(self):
        state = _workflow_state()
        affected = _affected_task_ids(state["subtasks"], ["research"])
        assert affected == ["research", "synthesis"]
        assert "data" not in affected

    def test_synthesis_only_revision_does_not_rerun_upstream(self):
        state = _workflow_state()
        affected = _affected_task_ids(state["subtasks"], ["synthesis"])
        assert affected == ["synthesis"]

    def test_prepare_revision_uses_targets_and_dependents(self):
        state = _workflow_state()
        state["revision_targets"] = ["data"]
        result = prepare_revision_node(state)
        assert result["pending_task_ids"] == ["data", "synthesis"]

    def test_revise_under_limit_routes_to_revision(self):
        state = {
            "review_status": "revise",
            "attempts": MAX_REVIEW_ROUNDS - 1,
            "revision_targets": ["research"],
        }
        assert route_after_review(state) == "prepare_revision"

    def test_revise_at_limit_stops(self):
        state = {
            "review_status": "revise",
            "attempts": MAX_REVIEW_ROUNDS,
            "revision_targets": ["research"],
        }
        assert route_after_review(state) == "__end__"

    def test_pass_stops(self):
        assert route_after_review({"review_status": "pass", "attempts": 1}) == "__end__"

    def test_blocked_stops(self):
        assert route_after_review({"review_status": "blocked", "attempts": 1}) == "__end__"


class TestBuildGraph:
    def test_returns_compiled_graph(self, settings):
        graph = build_graph(settings)
        assert hasattr(graph, "invoke")
        assert hasattr(graph, "stream")

    def test_graph_has_new_agent_nodes(self, settings):
        nodes = set(build_graph(settings).get_graph().nodes)
        assert {
            "task_router",
            "orchestrator",
            "research_worker",
            "code_worker",
            "data_worker",
            "synthesis_worker",
            "fan_in",
            "reviewer",
            "prepare_revision",
            "initialize_resources",
            "resource_terminated",
        } <= nodes

    def test_legacy_nodes_are_removed(self, settings):
        nodes = set(build_graph(settings).get_graph().nodes)
        assert "planner" not in nodes
        assert "executor" not in nodes

    def test_redis_settings_select_persistent_checkpointer(self, settings):
        graph = build_graph(replace(settings, use_redis=True))
        assert isinstance(graph.checkpointer, RedisCheckpointSaver)
