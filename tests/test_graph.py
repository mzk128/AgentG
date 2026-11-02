from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from multi_agent_system.artifact_runtime import RevisionInstruction, create_artifact
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


def test_build_graph_bounds_model_request_timeout_by_run_deadline(settings):
    configured = replace(
        settings,
        max_run_seconds=45.0,
        model_request_timeout_seconds=90.0,
    )
    with patch("multi_agent_system.graph.ChatOpenAI") as chat_model, patch(
        "multi_agent_system.graph.httpx.Client", return_value=MagicMock()
    ):
        build_graph(configured)

    assert chat_model.call_args.kwargs["timeout"] == 45.0


def test_disabled_memory_still_protects_configured_custom_database_parent(settings, tmp_path):
    private = tmp_path / "private"
    configured = replace(settings, long_term_memory_enabled=False, memory_db_path=str(private / "memory.sqlite3"))
    update = initialize_resources_node({}, configured)
    assert str(private.resolve()) in update["memory_policy"]["python_denied_paths"]
    assert not private.exists()  # Protection is path-only, not a database read/create.


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

    def test_revision_dispatch_includes_old_artifact_and_bound_instruction(self):
        state = _workflow_state()
        previous = create_artifact("research", "old result")
        state["current_batch_ids"] = ["research"]
        state["worker_attempts"] = {"research": 1}
        state["worker_results"] = [{
            "task_id": "research", "agent_type": "research", "attempt": 1,
            "status": "completed", "content": previous.content,
            "artifact": previous.model_dump(mode="json"),
        }]
        state["revision_instructions"] = [RevisionInstruction(
            task_id="research", mode="patch", allowed_paths=["/text"],
            requested_changes=["replace one sentence"],
        ).model_dump(mode="json")]

        send = dispatch_workers(state)[0]

        assert send.arg["previous_deliverable"]["content"] == "old result"
        assert send.arg["revision_instruction"]["mode"] == "patch"
        assert send.arg["revision_instruction"]["base_version"] == 1
        assert send.arg["revision_instruction"]["base_content_hash"] == previous.content_hash

    def test_revision_dispatch_without_old_artifact_uses_regenerate(self):
        state = _workflow_state()
        state["current_batch_ids"] = ["research"]
        state["worker_attempts"] = {"research": 1}
        state["revision_instructions"] = [RevisionInstruction(
            task_id="research",
            mode="patch",
            allowed_paths=["/text"],
            requested_changes=["补充缺失结果"],
            evidence_requirements=["重新执行检索"],
        ).model_dump(mode="json")]

        send = dispatch_workers(state)[0]

        assert send.arg["previous_deliverable"] == {}
        assert send.arg["revision_instruction"]["mode"] == "regenerate"
        assert send.arg["revision_instruction"]["requested_changes"] == ["补充缺失结果"]
        assert send.arg["revision_instruction"]["evidence_requirements"] == ["重新执行检索"]
        assert send.arg["revision_instruction"]["base_version"] is None

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
        assert policy["max_review_rounds"] == settings.max_review_rounds
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
                    "max_review_rounds": 3,
                    "started_at": 10.0,
                    "deadline_at": 20.0,
                }
            },
            next=("research_worker",),
        )

        with patch("multi_agent_system.graph.time.time", return_value=100.0):
            assert refresh_resume_deadline(
                graph, {"configurable": {}}, replace(settings, max_review_rounds=6)
            )

        policy = graph.update_state.call_args.args[1]["resource_policy"]
        assert policy["started_at"] == 10.0
        assert policy["resumed_at"] == 100.0
        assert policy["deadline_at"] == 100.0 + settings.max_run_seconds
        assert policy["max_tool_calls"] == 12
        assert policy["max_review_rounds"] == 3


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

    def test_review_limit_uses_persisted_run_policy(self, settings):
        state = {
            **initialize_resources_node({}, replace(settings, max_review_rounds=3)),
            "review_status": "revise",
            "attempts": 2,
            "revision_targets": ["research"],
        }
        assert route_after_review(state) == "prepare_revision"
        state["attempts"] = 3
        assert route_after_review(state) == "__end__"
        state["resource_policy"]["max_review_rounds"] = 1
        state["attempts"] = 1
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
