"""L1.5 graph/checkpoint tests. Models and Redis are offline mocks."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from multi_agent_system.agents import OrchestrationPlan, ReviewDecision, SubTaskSpec, TaskRoutingDecision
from multi_agent_system.config import Settings
from multi_agent_system.graph import build_graph, refresh_resume_deadline
from multi_agent_system.memory_records import MemoryIdentity, MemoryRecord
from multi_agent_system.memory_runtime import GraphMemoryRuntime
from multi_agent_system.memory_service import MemoryService, explicit_intent
from multi_agent_system.memory_store import SQLiteMemoryRepository
from multi_agent_system.state import create_initial_state
from multi_agent_system.storage import RedisCheckpointSaver
from tests.test_storage import FakeRedis


@pytest.fixture
def configured(tmp_path):
    return Settings(openai_api_key="synthetic", openai_base_url="https://model.invalid/v1", model_name="test",
                    use_redis=False, redis_url="redis://unused", long_term_memory_enabled=True,
                    memory_db_path=str(tmp_path / "private" / "memory.sqlite3"), memory_project_id="project-1")


@pytest.fixture
def service(configured):
    with SQLiteMemoryRepository(configured.memory_db_path) as repository:
        yield MemoryService(repository)


def llm_mock(*, team=False, revise=False):
    llm = MagicMock()
    reviews = iter(([ReviewDecision(status="revise", revision_targets=["research"], feedback="补充来源")]
                    if revise else []) + [ReviewDecision(status="pass", feedback="ok")])

    def structured(schema, **kwargs):
        runner = MagicMock()
        if schema is TaskRoutingDecision:
            runner.invoke.return_value = TaskRoutingDecision(route="team" if team else "simple", worker_type="synthesis", reason="test")
        elif schema is OrchestrationPlan:
            runner.invoke.return_value = OrchestrationPlan(summary="并行研究和统计后汇总", subtasks=[
                SubTaskSpec(id="research", agent_type="research", objective="杭州天气研究"),
                SubTaskSpec(id="data", agent_type="data", objective="销售数据分析"),
                SubTaskSpec(id="summary", agent_type="synthesis", objective="汇总报告", dependencies=["research", "data"]),
            ])
        elif schema is ReviewDecision:
            runner.invoke.side_effect = lambda *_a, **_kw: next(reviews)
        return runner

    llm.with_structured_output.side_effect = structured
    llm.invoke.side_effect = lambda _messages: AIMessage(content="公开答复，任务已完成。")
    return llm


def graph(configured, service, llm=None, checkpointer=None):
    with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm or llm_mock()), patch(
        "multi_agent_system.graph.httpx.Client", return_value=MagicMock()):
        return build_graph(configured, memory_service=service, checkpointer=checkpointer)


def first_write(configured, service, command="记住[用户/偏好/style]：使用中文。", thread="write-1"):
    app = graph(configured, service)
    return app.invoke(create_initial_state(command, thread), {"configurable": {"thread_id": thread}})


def mutate(service, record, **changes):
    record = MemoryRecord.model_validate({**record.model_dump(), "version": record.version + 1,
        "supersedes": record.version_ref(), "updated_at": record.updated_at + timedelta(seconds=1),
        "idempotency_key": f"update-{record.version}", **changes})
    service.repository.save(record, identity=MemoryIdentity(user_id="local-user", project_id="project-1",
                                                          conversation_id="write-1"))
    return record


def test_simple_graph_explicit_save_and_cross_conversation_read(configured, service):
    written = first_write(configured, service)
    assert written["is_pass"]
    assert written["memory_write_results"][0]["status"] == "saved"
    assert written["memory_binding"]["conversation_id"] == "write-1"
    llm = llm_mock()
    app = graph(configured, service, llm)
    final = app.invoke(create_initial_state("介绍系统", "read-1"), {"configurable": {"thread_id": "read-1"}})
    assert final["is_pass"] and len(final["retrieved_memories"]["items"]) == 1
    assert "使用中文" in llm.invoke.call_args.args[0][0].content
    assert final["worker_results"][0]["memory_snapshot"]["items"]
    assert not final["memory_write_results"]  # No auto indexing of normal tasks.


def test_auto_extraction_is_opt_in_candidate_never_activated_by_review(configured, service):
    app = graph(replace(configured, memory_auto_extract=True), service)
    final = app.invoke(create_initial_state("介绍系统", "auto-1"), {"configurable": {"thread_id": "auto-1"}})
    assert final["is_pass"]
    assert final["memory_write_results"][0]["status"] == "candidate"
    host = MemoryIdentity(project_id="project-1", conversation_id="auto-1")
    candidates = service.repository.list_records(namespace=host.namespace_for("conversation"), identity=host)
    assert len(candidates) == 1 and "审查状态：pass" in candidates[0].content
    assert service.retrieve("公开答复", host, at=datetime.now(timezone.utc)).items == ()


def test_team_worker_memories_are_objective_scoped_and_not_global_blackboard(configured, service):
    # Candidate APIs are tested separately. Seed verified host-approved facts.
    host = MemoryIdentity(project_id="project-1", conversation_id="team-1")
    from multi_agent_system.memory_records import MemorySourceRef, memory_content_hash
    now = datetime.now(timezone.utc) - timedelta(seconds=1)
    for key, content in (("weather", "杭州天气研究资料"), ("sales", "销售数据分析经验")):
        record = MemoryRecord(memory_id=key, scope="project", kind="experience",
            namespace=host.namespace_for("project"), content=content,
            source_refs=(MemorySourceRef(kind="user_message", thread_id="seed", message_id=key),),
            write_basis="explicit_request", verification_status="user_confirmed", status="active",
            created_at=now, updated_at=now, content_hash=memory_content_hash(content), idempotency_key=key)
        service.repository.save(record, identity=host)
    llm = llm_mock(team=True, revise=True)
    app = graph(configured, service, llm)
    result = app.invoke(create_initial_state("研究并生成报告", "team-1"), {"configurable": {"thread_id": "team-1"}})
    assert result["is_pass"]
    research_prompts = [call.args[0][0].content for call in llm.invoke.call_args_list if "ResearchAgent" in call.args[0][0].content]
    data_prompts = [call.args[0][0].content for call in llm.invoke.call_args_list if "DataAgent" in call.args[0][0].content]
    assert len(research_prompts) == 2 and len(data_prompts) == 1
    assert all("杭州天气研究资料" in prompt and "销售数据分析经验" not in prompt for prompt in research_prompts)
    assert "销售数据分析经验" in data_prompts[0] and "杭州天气研究资料" not in data_prompts[0]
    assert result["worker_attempts"] == {"research": 2, "data": 1, "summary": 2}


def test_sqlite_commit_then_process_failure_replays_one_version_with_new_redis_instance(configured, service):
    client = FakeRedis()
    config = {"configurable": {"thread_id": "crash-1"}}
    app = graph(configured, service, checkpointer=RedisCheckpointSaver("redis://unused", client=client))
    original_save = service.repository.save
    fail_once = {"fail": True}

    def save_then_crash(*args, **kwargs):
        value = original_save(*args, **kwargs)
        if fail_once["fail"]:
            fail_once["fail"] = False
            raise RuntimeError("simulated crash after SQLite commit")
        return value

    with patch.object(service.repository, "save", side_effect=save_then_crash):
        with pytest.raises(RuntimeError, match="simulated crash"):
            app.invoke(create_initial_state("记住[用户/偏好/style]：使用中文。", "crash-1"), config)
    before = app.get_state(config)
    assert before.next == ("commit_memory_writes",)
    with SQLiteMemoryRepository(configured.memory_db_path) as reopened:
        restored_service = MemoryService(reopened)
        llm = llm_mock()
        resumed = graph(configured, restored_service, llm,
                        RedisCheckpointSaver("redis://unused", client=client))
        refresh_resume_deadline(resumed, config, configured)
        result = resumed.invoke(None, config)
        assert result["memory_write_results"][0]["status"] == "saved"
        assert result["resource_events"] == before.values["resource_events"]
        assert result["worker_attempts"] == before.values["worker_attempts"]
        llm.invoke.assert_not_called()
        plan = result["memory_write_plans"][0]["record"]
        identity = MemoryIdentity.model_validate(result["memory_binding"])
        assert len(reopened.history(plan["memory_id"], namespace=identity.namespace_for("user"), identity=identity)) == 1


@pytest.mark.parametrize("mandatory", [False, True])
def test_deleted_memory_rechecked_in_pending_send_on_recreated_graph(configured, service, mandatory):
    written = first_write(configured, service)
    record = MemoryRecord.model_validate_json(json.dumps(written["memory_write_plans"][0]["record"]))
    saver, llm = MemorySaver(), llm_mock()
    app = graph(configured, service, llm, saver)
    config = {"configurable": {"thread_id": "pending-1"}}
    initial = create_initial_state("介绍系统", "pending-1", memory_required_ids=[record.memory_id] if mandatory else [])
    app.invoke(initial, config, interrupt_before=["synthesis_worker"])
    assert app.get_state(config).next == ("synthesis_worker",)
    mutate(service, record, status="deleted")
    new_llm = llm_mock()
    resumed = graph(configured, service, new_llm, saver)
    result = resumed.invoke(None, config)
    if mandatory:
        assert result["review_status"] == "blocked"
        assert result["termination"]["reason"] == "memory_required_snapshot_invalid"
        new_llm.invoke.assert_not_called()
    else:
        prompt = new_llm.invoke.call_args.args[0][0].content
        assert "使用中文" not in prompt
        assert result["worker_results"][-1]["memory_snapshot"]["items"] == []


def test_host_identity_change_blocks_old_checkpoint_memory(configured, service):
    first_write(configured, service)
    saver = MemorySaver()
    config = {"configurable": {"thread_id": "old-identity"}}
    app = graph(configured, service, checkpointer=saver)
    app.invoke(create_initial_state("介绍系统", "old-identity"), config, interrupt_before=["task_router"])
    llm = llm_mock()
    resumed = graph(replace(configured, memory_project_id="other-project"), service, llm, saver)
    result = resumed.invoke(None, config)
    assert result["termination"]["reason"] == "memory_host_binding_changed"
    llm.invoke.assert_not_called()


def test_unavailable_optional_store_is_explained_but_required_memory_blocks(configured, service):
    first_write(configured, service)
    runtime = GraphMemoryRuntime(configured, service)
    state = create_initial_state("解释系统", "degrade")
    state.update(runtime.prepare(state))
    state["memory_policy"]["python_denied_paths"] = []  # Legacy pending payload.
    service.repository.close()
    guarded = runtime.wrap(lambda _state: {"is_pass": True}, "test")(state)
    assert str(__import__("pathlib").Path(configured.memory_db_path).parent) in guarded["memory_policy"]["python_denied_paths"]
    assert guarded["retrieved_memories"] == {}
    app = graph(configured, service)
    result = app.invoke(create_initial_state("解释系统", "optional"), {"configurable": {"thread_id": "optional"}})
    assert result["is_pass"] and "memory_store_unavailable" in result["memory_notices"]
    app = graph(configured, service)
    result = app.invoke(create_initial_state("解释系统", "required", memory_required_ids=["required-id"]),
                        {"configurable": {"thread_id": "required"}})
    assert result["review_status"] == "blocked" and result["termination"]["reason"] == "memory_store_unavailable"


def test_memory_input_still_participates_in_whole_request_admission(configured, service):
    first_write(configured, service, "记住[用户/规则/style]：" + "中文输出要求" * 50)
    app = graph(replace(configured, context_max_tokens=1000, context_output_reserve=100), service)
    result = app.invoke(create_initial_state("介绍系统", "budget"), {"configurable": {"thread_id": "budget"}})
    assert result["termination"]["reason"] == "context_budget_exceeded"


def test_disabled_feature_does_not_build_repository_or_execute_memory_nodes(configured):
    disabled = replace(configured, long_term_memory_enabled=False)
    with patch("multi_agent_system.memory_runtime.create_memory_repository") as factory:
        app = graph(disabled, None)
    factory.assert_not_called()
    old_payload = GraphMemoryRuntime(disabled)._validate({"memory_policy": {}})
    assert str(__import__("pathlib").Path(configured.memory_db_path).parent) in old_payload["memory_policy"]["python_denied_paths"]
    nodes = [node for update in app.stream(create_initial_state("介绍系统", "disabled"),
             {"configurable": {"thread_id": "disabled"}}) for node in update]
    assert not {"prepare_memory", "stage_memory_writes", "commit_memory_writes"} & set(nodes)
    assert not __import__("pathlib").Path(configured.memory_db_path).exists()


def test_disabling_feature_can_resume_a_pending_memory_commit_without_sqlite_io(configured, service):
    saver = MemorySaver()
    app = graph(configured, service, checkpointer=saver)
    config = {"configurable": {"thread_id": "turn-off"}}
    app.invoke(create_initial_state("记住[用户/偏好/style]：使用中文。", "turn-off"), config,
               interrupt_before=["commit_memory_writes"])
    with patch("multi_agent_system.memory_runtime.create_memory_repository") as factory:
        resumed = graph(replace(configured, long_term_memory_enabled=False), None, checkpointer=saver)
        result = resumed.invoke(None, config)
    factory.assert_not_called()
    assert result["memory_write_results"][0]["status"] == "not_saved"
    assert result["is_pass"]


@pytest.mark.parametrize("before,after", [(False, True), (True, False)])
def test_automatic_candidate_consent_and_policy_are_frozen_before_resume(configured, service, before, after):
    saver = MemorySaver()
    app = graph(replace(configured, memory_auto_extract=before), service, checkpointer=saver)
    config = {"configurable": {"thread_id": "no-auto"}}
    app.invoke(create_initial_state("介绍系统", "no-auto"), config, interrupt_before=["stage_memory_writes"])
    resumed = graph(replace(configured, memory_auto_extract=after, memory_max_tokens=9000), service, checkpointer=saver)
    result = resumed.invoke(None, config)
    assert result["memory_write_results"] == []
    assert result["memory_policy"]["long_term_max_tokens"] == configured.memory_max_tokens


def test_memory_write_budget_failure_does_not_undo_passed_delivery(configured, service):
    runtime = GraphMemoryRuntime(configured, service)
    state = create_initial_state("记住[用户/偏好/style]：使用中文。", "budget-write")
    state.update(runtime.prepare(state))
    state.update({"is_pass": True, "review_status": "pass", "execution_result": "已交付",
                  "resource_policy": {"total_action_budget": 1},
                  "resource_events": [{"budget_used": 1, "tool_calls": 0, "errors": 0}]})
    result = runtime.commit_final(state)
    assert result["memory_write_results"][0]["status"] == "not_saved"
    assert state["is_pass"] and state["execution_result"] == "已交付"
    assert service.repository.list_records(namespace=runtime.identity(state).namespace_for("user"),
                                           identity=runtime.identity(state)) == []
