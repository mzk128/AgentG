"""L1.6: temporary SQLite, mocked model/Redis, no user memory or HTTP."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from multi_agent_system.memory_management import MemoryManagementCommand, MemoryManager
from multi_agent_system.memory_records import ArtifactReference, MemoryIdentity, MemoryRecord, MemorySourceRef, memory_content_hash
from multi_agent_system.memory_service import MemoryPolicyError, MemoryService
from multi_agent_system.memory_store import MemoryConflict, SQLiteMemoryRepository
from tests.test_memory_service import write, insert, context

NOW = datetime.now(timezone.utc) - timedelta(minutes=5)


@pytest.fixture
def host():
    return MemoryIdentity(user_id="user-1", project_id="project-1", conversation_id="chat-1")


@pytest.fixture
def service(tmp_path):
    with SQLiteMemoryRepository(tmp_path / "private" / "memory.sqlite3") as repo:
        yield MemoryService(repo)


def seed(service, host, scope="用户", key="style", text="使用中文。", thread="run-1", kind="偏好"):
    plan, result = write(service, host, f"记住[{scope}/{kind}/{key}]：{text}", thread=thread, at=NOW)
    assert result.status in {"saved", "candidate"}
    return plan.record


def command(record, action, **kwargs):
    return MemoryManagementCommand(action=action, expected=record.version_ref(),
        confirmed_content=record.content, reason="用户调整测试偏好", **kwargs)


def test_correction_history_and_stale_predecessor(service, host):
    manager = MemoryManager(service.repository)
    old = seed(service, host)
    new = manager.apply(command(old, "correct", content="自然语言回答。"), host, "user")
    assert new.version == 2 and new.status == "active" and new.revision_note
    assert new.content_hash == memory_content_hash(new.content) and new.supersedes == old.version_ref()
    assert service.repository.history(old.memory_id, namespace=old.namespace, identity=host) == [old, new]
    with pytest.raises(MemoryConflict):
        manager.apply(command(old, "delete"), host, "user")
    assert service.retrieve("回答", host, at=datetime.now(timezone.utc)).items[0].content == new.content


@pytest.mark.parametrize("action", ["disable", "correct", "delete"])
def test_management_invalidates_snapshots(service, host, action):
    old = seed(service, host)
    optional = service.retrieve("中文", host, at=NOW)
    required = service.retrieve("中文", host, at=NOW, required_ids=(old.memory_id,))
    MemoryManager(service.repository).apply(command(old, action, **({"content":"使用英文。"} if action == "correct" else {})), host, "user")
    assert service.revalidate(optional, host, at=datetime.now(timezone.utc)).items == ()
    with pytest.raises(MemoryPolicyError, match="memory_required_snapshot_invalid"):
        service.revalidate(required, host, at=datetime.now(timezone.utc))


def test_disable_enable_and_terminal_deletion(service, host):
    manager = MemoryManager(service.repository)
    old = seed(service, host)
    disabled = manager.apply(command(old, "disable"), host, "user")
    assert not service.retrieve("中文", host, at=datetime.now(timezone.utc)).items
    enabled = manager.apply(command(disabled, "enable"), host, "user")
    deleted = manager.apply(command(enabled, "delete"), host, "user")
    assert deleted.version == 4 and deleted.status == "deleted"
    for action in ("enable", "correct", "delete", "confirm"):
        with pytest.raises(MemoryPolicyError, match="memory_terminal_record"):
            manager.apply(command(deleted, action, **({"content":"英文"} if action == "correct" else {})), host, "user")
    # Replaying an old staged write gives its old receipt but never moves head.
    assert service.repository.save(old, identity=host) == old
    assert manager.find(old.memory_id, host, "user") == deleted


def test_candidate_confirmation_and_fact_safety(service, host):
    manager = MemoryManager(service.repository)
    experience = seed(service, host, kind="经验", text="统计后应检查单位。")
    assert manager.apply(command(experience, "confirm"), host, "user").status == "active"
    fact = seed(service, host, key="fact", kind="事实", text="本项目采用 SQLite。")
    with pytest.raises(MemoryPolicyError, match="memory_fact_requires_verification"):
        manager.apply(command(fact, "confirm"), host, "user")
    corrected = manager.apply(command(fact, "correct", content="本项目采用 Redis 与 SQLite。"), host, "user")
    assert corrected.status == "candidate" and corrected.verification_status == "unverified"
    assert corrected.expires_at == fact.expires_at


@pytest.mark.parametrize("change", [
    {"confirmed_content":"另一个正文"}, {"content":"不能出现在删除请求"},
    {"reason":" "}, {"verified":True}, {"expected":{"memory_id":"id","version":"1","content_hash":"a"*64}},
])
def test_management_strict_contract_and_exact_consent(service, host, change):
    old = seed(service, host)
    payload = command(old, "delete").model_dump() | change
    with pytest.raises((ValidationError, MemoryConflict)):
        cmd = MemoryManagementCommand.model_validate(payload)
        MemoryManager(service.repository).apply(cmd, host, "user")


def test_sensitive_correction_and_reason_rejected(service, host):
    old = seed(service, host)
    manager = MemoryManager(service.repository)
    with pytest.raises(MemoryPolicyError, match="memory_sensitive_or_unsafe"):
        manager.apply(command(old, "correct", content="password=synthetic-only"), host, "user")
    unsafe = MemoryManagementCommand.model_validate({**command(old,"delete").model_dump(), "reason":"password=synthetic-only"})
    with pytest.raises(MemoryPolicyError):
        manager.apply(unsafe, host, "user")


def test_scope_isolation_and_artifact_exact_grant(service, host):
    old = seed(service, host, scope="会话")
    manager = MemoryManager(service.repository)
    other = MemoryIdentity(user_id=host.user_id, project_id=host.project_id, conversation_id="other")
    with pytest.raises(MemoryPolicyError, match="memory_not_found"):
        manager.apply(command(old,"delete"), other, "conversation")
    ref = ArtifactReference(thread_id="run-1", task_id="task-1", artifact_id="artifact-1", version=1, content_hash="a"*64)
    record = insert(service, host, memory_id="artifact-memory", scope="artifact", kind="experience", content="测试产物说明",
        namespace=host.namespace_for("artifact", artifact_identity=ref.identity()), artifact_ref=ref,
        source_refs=(MemorySourceRef(kind="artifact", artifact_ref=ref),),
        created_at=NOW,updated_at=NOW,observed_at=NOW,expires_at=NOW+timedelta(days=1))
    with pytest.raises(MemoryPolicyError, match="memory_not_found"):
        manager.find(record.memory_id,host,"artifact")
    assert manager.find(record.memory_id,host,"artifact",(ref,)) == record
    assert manager.listing(host,"artifact", artifacts=(ref,))[0] == [record]


def test_batch_rolls_back_all_changes_on_stale_version(service, host):
    manager = MemoryManager(service.repository)
    a, b = seed(service,host,key="a"), seed(service,host,key="b")
    av2, bv2 = manager.successor(a,command(a,"delete")), manager.successor(b,command(b,"delete"))
    manager.apply(command(b,"disable"),host,"user")
    with pytest.raises(MemoryConflict):
        service.repository.save_batch((av2,bv2),identity=host)
    assert manager.find(a.memory_id,host,"user") == a


def test_conversation_cleanup_private_promoted_foreign_and_retry(service, host):
    manager = MemoryManager(service.repository)
    private = seed(service,host,scope="会话",key="private")
    promoted = seed(service,host,key="promoted")
    unrelated = seed(service,host,key="unrelated",thread="other-run")
    foreign = MemoryIdentity(user_id="other",project_id=host.project_id,conversation_id=host.conversation_id)
    seed(service,foreign,scope="会话",key="foreign")
    assert {r.memory_id for r in manager.related(host,("run-1",))} == {private.memory_id}
    assert {r.memory_id for r in manager.related(host,("run-1",),True)} == {private.memory_id,promoted.memory_id}
    assert len(manager.delete_related(host,("run-1",))) == 1
    assert manager.find(promoted.memory_id,host,"user").status == "active"
    assert len(manager.delete_related(host,("run-1",),True)) == 1
    assert manager.delete_related(host,("run-1",),True) == []
    assert manager.find(unrelated.memory_id,host,"user").status == "active"


def test_backup_after_deletion_retains_tombstone_and_old_backup_is_historical(service,host,tmp_path):
    old = seed(service,host)
    before = service.repository.backup(tmp_path/"before.sqlite3")
    MemoryManager(service.repository).apply(command(old,"delete"),host,"user")
    after = service.repository.backup(tmp_path/"after.sqlite3")
    for source,name,status in ((before,"old","active"),(after,"new","deleted")):
        with SQLiteMemoryRepository.restore(source,tmp_path/f"{name}.sqlite3") as repo:
            assert repo.get(old.memory_id,namespace=old.namespace,identity=host).status == status


@pytest.fixture
def api(service,host,settings):
    from multi_agent_system import web_server as web
    configured = replace(settings,long_term_memory_enabled=True,memory_user_id=host.user_id,memory_project_id=host.project_id)
    web._task_store.clear(); web._active_threads.clear()
    web._task_store["run-1"] = {"thread_id":"run-1","conversation_id":"chat-1","status":"completed","task":"test"}
    with patch.object(web,"_get_task_repository",return_value=None), patch("multi_agent_system.memory_api.get_settings",return_value=configured), patch("multi_agent_system.memory_api.create_memory_repository",side_effect=lambda _:SQLiteMemoryRepository(service.repository.db_path)):
        with TestClient(web.app) as client:
            yield client
    web._task_store.clear(); web._active_threads.clear()


def test_api_list_edit_history_conflict_and_cross_site(api,service,host):
    old = seed(service,host)
    assert len(api.get("/api/memories").json()["items"]) == 1
    url = f"/api/memories/{old.memory_id}/actions"
    body = command(old,"correct",content="自然语言输出。").model_dump(mode="json")
    assert api.post(url,json=body,headers={"Origin":"https://untrusted.invalid"}).status_code == 403
    assert api.post(url,json=body).status_code == 200
    assert api.post(url,json=body).status_code == 409
    data = api.get(f"/api/memories/{old.memory_id}").json()
    assert len(data["history"]) == 2 and "备份" in data["retention_notice"]
    assert api.post(url,json={**body,"verified":True}).status_code == 400
    assert api.get("/api/memories?scope=conversation&conversation_id=foreign").status_code == 404


@pytest.mark.parametrize("selection,private_status,promoted_status", [
    ("conversation","deleted","active"),("all_related","deleted","deleted")])
def test_api_conversation_delete_choice(api,service,host,selection,private_status,promoted_status):
    private = seed(service,host,scope="会话")
    promoted = seed(service,host)
    assert api.get("/api/conversations/chat-1/memory-impact").json()["promoted"] == 1
    response = api.delete(f"/api/conversations/chat-1?memory_cleanup={selection}")
    assert response.status_code == 200
    for record,status in ((private,private_status),(promoted,promoted_status)):
        assert service.repository.get(record.memory_id,namespace=record.namespace,identity=host).status == status


def test_api_active_conversation_delete_denied_before_memory_changes(api,service,host):
    from multi_agent_system import web_server as web
    old = seed(service,host,scope="会话")
    web._active_threads.add("run-1")
    assert api.delete("/api/conversations/chat-1").status_code == 409
    assert service.repository.get(old.memory_id,namespace=old.namespace,identity=host) == old


def test_management_disabled_does_not_open_database(api):
    from multi_agent_system.memory_api import get_settings
    with patch("multi_agent_system.memory_api.get_settings",return_value=replace(get_settings(),long_term_memory_enabled=False)), patch("multi_agent_system.memory_api.create_memory_repository") as factory:
        assert not api.get("/api/memories/settings").json()["enabled"]
        assert api.get("/api/memories").status_code == 409
        factory.assert_not_called()


@pytest.mark.parametrize("required", [False,True])
def test_revocation_inside_worker_tool_loop_stops_before_next_model_call(service,host,settings,required):
    from unittest.mock import MagicMock
    from langchain_core.messages import AIMessage
    from multi_agent_system.agents import code_worker_node
    from multi_agent_system.memory_runtime import GraphMemoryRuntime
    from multi_agent_system.tool_runtime import ToolExecutionResult
    record = seed(service,host)
    snapshot = service.retrieve("中文",host,at=NOW,required_ids=(record.memory_id,) if required else ())
    configured = replace(settings,long_term_memory_enabled=True,memory_user_id=host.user_id,memory_project_id=host.project_id)
    runtime = GraphMemoryRuntime(configured,service)
    llm = MagicMock()
    runner = llm.bind_tools.return_value
    runner.invoke.return_value = AIMessage(content="",tool_calls=[{"name":"python_repl","args":{"code":"print(1)"},"id":"call-1"}])
    state = {"task_id":"code-1","agent_type":"code","objective":"计算测试","allowed_tools":["python_repl"],
             "memory_binding":host.model_dump(),"retrieved_memories":snapshot.model_dump(mode="json"),"memory_policy":{}}

    def dispatch(*args,**kwargs):
        MemoryManager(service.repository).apply(command(record,"delete"),host,"user")
        return ToolExecutionResult(ok=True,data={"stdout":"1"},executed=True)

    with patch("multi_agent_system.agents.dispatch_tool",side_effect=dispatch):
        result = runtime.wrap(lambda local:code_worker_node(local,llm),"code_worker",worker=True)(state)
    assert runner.invoke.call_count == 1
    worker = result["worker_results"][0]
    assert worker["status"] == "terminated"
    assert worker["termination_reason"] == ("memory_required_snapshot_invalid" if required else "memory_snapshot_changed_during_worker")


def test_api_storage_failure_does_not_delete_conversation(api,service,host):
    from multi_agent_system import web_server as web
    from multi_agent_system.memory_store import MemoryStoreUnavailable
    seed(service,host,scope="会话")
    with patch("multi_agent_system.memory_api.create_memory_repository",side_effect=MemoryStoreUnavailable("synthetic-private-path")):
        response = api.delete("/api/conversations/chat-1")
    assert response.status_code == 503 and "synthetic-private-path" not in response.text
    assert "run-1" in web._task_store


def test_ui_memory_contract_is_plain_text_and_version_checked():
    from pathlib import Path
    text = (Path(__file__).parents[1]/"src/multi_agent_system/web_templates/index.html").read_text(encoding="utf-8")
    assert 'id="memoryToggle"' in text and 'id="memoryHeaderToggle"' in text
    assert 'memoryRecord.content_hash' in text or 'record.content_hash' in text
    assert "confirmed_content:record.content" in text
    assert "document.getElementById('memoryHistory').textContent" in text
    assert "document.getElementById('memoryList')" in text and 'memory_cleanup=' in text


def test_redis_delete_failure_can_retry_without_reviving_memory(api,service,host):
    from multi_agent_system import web_server as web
    from redis.exceptions import RedisError
    old = seed(service,host,scope="会话")
    with patch.object(web,"_delete_task_record",side_effect=RedisError("synthetic")):
        assert api.delete("/api/conversations/chat-1").status_code == 503
    deleted = service.repository.get(old.memory_id,namespace=old.namespace,identity=host)
    assert deleted.status == "deleted" and deleted.version == 2
    assert "run-1" in web._task_store
    response = api.delete("/api/conversations/chat-1")
    assert response.status_code == 200 and response.json()["deleted_memories"] == 0
    assert service.repository.get(old.memory_id,namespace=old.namespace,identity=host) == deleted


def test_last_task_deletion_cleans_private_memory(api,service,host):
    old = seed(service,host,scope="会话")
    assert api.delete("/api/tasks/run-1").status_code == 200
    assert service.repository.get(old.memory_id,namespace=old.namespace,identity=host).status == "deleted"


def test_promoted_cleanup_uses_historical_sources(service,host):
    old = seed(service,host)
    new = seed(service,host,thread="other-run",text="先解释输出。")
    assert old.source_refs[0] not in new.source_refs
    assert MemoryManager(service.repository).related(host,("run-1",),True) == [new]


def test_management_pagination_crosses_repository_page_boundary(service,host):
    from unittest.mock import MagicMock
    old = seed(service,host)
    repository = MagicMock()
    repository.list_records.return_value = [old]*51
    records, more = MemoryManager(repository).listing(host,"user",limit=50,offset=1000)
    assert len(records) == 50 and more
    assert repository.list_records.call_args.kwargs["offset"] == 1000
    assert repository.list_records.call_args.kwargs["limit"] == 51


def test_delete_at_source_capacity_and_superseded_cleanup(service,host):
    old = seed(service,host,scope="会话")
    payload = old.model_dump() | {"memory_id":"capacity", "idempotency_key":"capacity", "source_refs":tuple(
        MemorySourceRef(kind="user_message",thread_id="run-1",message_id=f"m-{i}") for i in range(32))}
    record = MemoryRecord.model_validate(payload)
    service.repository.save(record,identity=host)
    deleted = MemoryManager(service.repository).apply(command(record,"delete"),host,"conversation")
    assert deleted.status == "deleted" and len(deleted.source_refs) == 32
    superseded = MemoryRecord.model_validate({**old.model_dump(),"memory_id":"terminal-old",
        "idempotency_key":"terminal-old","status":"superseded"})
    service.repository.save(superseded,identity=host)
    related = MemoryManager(service.repository).related(host,("run-1",))
    assert all(item.memory_id != "terminal-old" for item in related)


def test_artifact_history_does_not_expand_version_grants(api,service,host):
    from multi_agent_system import web_server as web
    first = ArtifactReference(thread_id="run-1",task_id="task-1",artifact_id="artifact-1",version=1,content_hash="a"*64)
    second = ArtifactReference(thread_id="run-1",task_id="task-1",artifact_id="artifact-1",version=2,content_hash="b"*64)
    source1, source2 = (MemorySourceRef(kind="artifact",artifact_ref=ref) for ref in (first,second))
    record = insert(service,host,memory_id="artifact-history",scope="artifact",kind="experience",
        namespace=host.namespace_for("artifact",artifact_identity=first.identity()),artifact_ref=first,
        source_refs=(source1,),created_at=NOW,updated_at=NOW,observed_at=NOW,expires_at=NOW+timedelta(days=1))
    newer = MemoryRecord.model_validate({**record.model_dump(),"version":2,"supersedes":record.version_ref(),
        "idempotency_key":"artifact-v2","artifact_ref":second,"source_refs":(source2,)})
    service.repository.save(newer,identity=host)
    web._task_store["run-1"]["worker_results"] = [{"task_id":"task-1","artifact":second.model_dump()}]
    detail = api.get("/api/memories/artifact-history?scope=artifact&conversation_id=chat-1").json()
    assert [item["version"] for item in detail["history"]] == [2]
