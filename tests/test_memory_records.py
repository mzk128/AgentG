"""Offline contract tests; no stores, model calls, user files or network access."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from multi_agent_system.memory_records import (
    LOCAL_USER_ID,
    MAX_MEMORY_CONTENT_CHARS,
    MAX_MEMORY_SOURCES,
    ArtifactIdentity,
    ArtifactReference,
    MemoryIdentity,
    MemoryKind,
    MemoryNamespace,
    MemoryRecord,
    MemoryScope,
    MemorySourceKind,
    MemorySourceRef,
    MemoryStatus,
    MemoryVersionRef,
    MemoryVerificationStatus,
    memory_content_hash,
    memory_idempotency_key,
    validate_memory_successor,
)


NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def artifact_ref(**changes):
    payload = {
        "thread_id": "run-1", "task_id": "task-1", "artifact_id": "task-1",
        "version": 1, "content_hash": memory_content_hash("artifact v1"),
    }
    return ArtifactReference.model_validate({**payload, **changes})


def identity(**changes):
    return MemoryIdentity.model_validate({
        "user_id": "user-1", "project_id": "project-1", "conversation_id": "chat-1",
        **changes,
    })


def user_source(**changes):
    return MemorySourceRef.model_validate({
        "kind": "user_message", "thread_id": "run-1", "message_id": "message-1",
        **changes,
    })


def tool_source(**changes):
    return MemorySourceRef.model_validate({
        "kind": "tool_result", "thread_id": "run-1", "task_id": "task-1",
        "tool_call_id": "call-1", "tool_name": "current_weather", **changes,
    })


def record_payload(**changes):
    content = changes.get("content", "用户偏好：以中文回答，先给结论。")
    return {
        "memory_id": "memory-1", "scope": "user", "kind": "preference",
        "namespace": identity().namespace_for("user"), "content": content,
        "source_refs": [user_source()], "write_basis": "explicit_request",
        "created_at": NOW, "updated_at": NOW,
        "content_hash": memory_content_hash(content), "idempotency_key": "event-1",
        **changes,
    }


def record(**changes):
    return MemoryRecord.model_validate(record_payload(**changes))


def next_record(previous, **changes):
    payload = previous.model_dump()
    content = changes.get("content", "新的公开结论。")
    payload.update({
        "version": previous.version + 1, "supersedes": previous.version_ref(),
        "content": content, "content_hash": memory_content_hash(content),
        "updated_at": previous.updated_at + timedelta(minutes=1),
        "idempotency_key": "event-2", **changes,
    })
    return MemoryRecord.model_validate(payload)


@pytest.mark.parametrize("scope", list(MemoryScope))
@pytest.mark.parametrize("kind", list(MemoryKind))
def test_all_scope_and_kind_combinations_round_trip_without_losing_types(scope, kind):
    reference = artifact_ref() if scope == MemoryScope.ARTIFACT else None
    namespace = identity().namespace_for(
        scope, artifact_identity=reference.identity() if reference else None,
    )
    sources = [user_source()]
    if reference:
        sources.append(MemorySourceRef(kind="artifact", artifact_ref=reference))
    original = record(
        scope=scope, kind=kind, namespace=namespace,
        source_refs=sources, artifact_ref=reference,
    )
    restored = MemoryRecord.model_validate_json(original.model_dump_json())
    assert restored == original
    assert isinstance(restored.scope, MemoryScope)
    assert isinstance(restored.kind, MemoryKind)
    assert isinstance(restored.source_refs, tuple)
    assert restored.created_at.tzinfo == timezone.utc
    assert json.loads(restored.model_dump_json())["source_refs"][0]["kind"] == "user_message"


def test_identity_default_and_keys_are_explicit_stable_and_run_qualified():
    assert MemoryIdentity().user_id == LOCAL_USER_ID
    first = identity().namespace_for("artifact", artifact_identity=artifact_ref().identity())
    restarted = MemoryIdentity.model_validate_json(identity().model_dump_json())
    assert first.key() == restarted.namespace_for(
        "artifact", artifact_identity=artifact_ref(version=2).identity(),
    ).key()
    other_run = identity().namespace_for(
        "artifact", artifact_identity=artifact_ref(thread_id="run-2").identity(),
    )
    assert first.key() != other_run.key()
    assert identity(project_id="project-2").namespace_for("project").key() != (
        identity().namespace_for("project").key()
    )
    assert identity(user_id="user-2").namespace_for("user").key() != (
        identity().namespace_for("user").key()
    )


@pytest.mark.parametrize("payload", [
    {"scope": "user"},
    {"scope": "project", "user_id": "user-1"},
    {"scope": "conversation", "user_id": "user-1"},
    {"scope": "artifact", "user_id": "user-1", "conversation_id": "chat-1"},
    {"scope": "artifact", "user_id": "user-1", "artifact_identity": artifact_ref().identity()},
    {"scope": "user", "user_id": "user-1", "project_id": "project-1"},
    {"scope": "project", "user_id": "user-1", "project_id": "project-1", "conversation_id": "chat-1"},
    {"scope": "conversation", "user_id": "user-1", "conversation_id": "chat-1",
     "artifact_identity": artifact_ref().identity()},
])
def test_namespace_requires_exact_scope_ownership(payload):
    with pytest.raises(ValidationError):
        MemoryNamespace.model_validate(payload)


@pytest.mark.parametrize("foreign", [
    identity(user_id="other-user").namespace_for("user"),
    identity(project_id="other-project").namespace_for("project"),
    identity(conversation_id="other-chat").namespace_for("conversation"),
    identity(project_id=None).namespace_for("conversation"),
])
def test_host_identity_rejects_foreign_or_rebound_namespace(foreign):
    with pytest.raises(ValueError, match="host identity"):
        identity().validate_namespace(foreign)


def test_identity_builders_do_not_guess_missing_project_or_conversation():
    assert identity().validate_namespace(identity().namespace_for("project"))
    with pytest.raises(ValidationError, match="project_id"):
        MemoryIdentity().namespace_for("project")
    with pytest.raises(ValidationError, match="conversation_id"):
        MemoryIdentity().namespace_for("conversation")
    with pytest.raises(ValueError, match="only valid"):
        identity().namespace_for("user", artifact_identity=artifact_ref().identity())


@pytest.mark.parametrize("value", [1, True, "", "  ", "user/other", "../data", "D:\\folder", "用户", "x" * 129])
def test_identifiers_are_strict_opaque_ids_not_paths_or_display_labels(value):
    with pytest.raises(ValidationError):
        MemoryIdentity(user_id=value)


@pytest.mark.parametrize("field,value", [
    ("scope", "USER"), ("kind", "unknown"), ("status", "running"),
    ("verification_status", "trusted"), ("write_basis", "model_says_so"),
    ("scope", 1), ("kind", True), ("kind", MemoryScope.USER),
    ("schema_version", True), ("schema_version", "1"), ("schema_version", 2),
    ("version", True), ("version", "1"), ("version", 0), ("version", 1.0),
    ("content", 42), ("idempotency_key", 1),
    ("source_refs", "one source"), ("source_refs", {}), ("source_refs", set()),
])
def test_invalid_enums_and_coercions_are_not_normalised(field, value):
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate({**record_payload(), field: value})


def test_unknown_fields_are_rejected_at_every_model_boundary():
    with pytest.raises(ValidationError, match="Extra inputs"):
        record(confidence=0.99)
    with pytest.raises(ValidationError, match="Extra inputs"):
        MemoryIdentity(user_id="user-1", workspace_path="D:\\data")
    with pytest.raises(ValidationError, match="Extra inputs"):
        MemoryNamespace(scope="user", user_id="user-1", title="project name")
    with pytest.raises(ValidationError, match="Extra inputs"):
        user_source(raw_log="not a source locator")
    with pytest.raises(ValidationError, match="Extra inputs"):
        artifact_ref(path="D:\\secret.txt")


@pytest.mark.parametrize("kind,fields", [
    ("user_message", {"thread_id": "run-1", "message_id": "message-1"}),
    ("tool_result", {"thread_id": "run-1", "task_id": "task-1", "tool_call_id": "call-1", "tool_name": "web_search"}),
    ("deliverable", {"thread_id": "run-1", "task_id": "task-1"}),
    ("artifact", {"artifact_ref": artifact_ref()}),
    ("external_url", {"url": "https://example.com/statistics?year=2025"}),
])
def test_sources_have_required_fields_and_no_irrelevant_locators(kind, fields):
    source = MemorySourceRef(kind=kind, **fields)
    assert source.kind == MemorySourceKind(kind)
    assert MemorySourceRef.model_validate_json(source.model_dump_json()) == source
    without_one = dict(fields)
    without_one.pop(next(iter(without_one)))
    with pytest.raises(ValidationError, match="exactly"):
        MemorySourceRef(kind=kind, **without_one)
    unrelated = "url" if kind != "external_url" else "task_id"
    with pytest.raises(ValidationError, match="exactly"):
        MemorySourceRef(kind=kind, **fields, **{
            unrelated: "https://example.com/" if unrelated == "url" else "task-1",
        })


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///D:/data", "/relative", "https:///missing-host",
    "https://user:secret@example.com/", "https://example.com:70000/",
    "https://example.com:wrong/", "https://exam ple.com/", "https://example.com/\nsecret",
    "https://example.com\\other/", "https://example.com/\x7fsecret", "https://example.com/" + "x" * 2048,
])
def test_source_url_is_bounded_absolute_and_without_embedded_credentials(url):
    with pytest.raises(ValidationError):
        MemorySourceRef(kind="external_url", url=url)


def test_source_lists_are_nonempty_bounded_unique_and_immutable():
    with pytest.raises(ValidationError):
        record(source_refs=[])
    with pytest.raises(ValidationError, match="duplicate"):
        record(source_refs=[user_source(), user_source()])
    with pytest.raises(ValidationError):
        record(source_refs=[user_source(message_id=f"message-{i}") for i in range(MAX_MEMORY_SOURCES + 1)])
    item = record()
    with pytest.raises(ValidationError, match="frozen"):
        item.source_refs[0].message_id = "changed"
    with pytest.raises(AttributeError):
        item.source_refs.append(user_source())
    with pytest.raises(ValidationError, match="frozen"):
        item.content = "changed"


@pytest.mark.parametrize(
    "content", ["", " \n\t", "文" * (MAX_MEMORY_CONTENT_CHARS + 1)],
    ids=["empty", "blank", "oversized"],
)
def test_content_is_nonblank_and_bounded(content):
    with pytest.raises(ValidationError):
        record(content=content)


def test_content_hash_binds_exact_unmodified_utf8_text():
    content = "报告\nParis: 18 °C\n"
    item = record(content=content)
    assert item.content == content
    assert memory_content_hash(content) != memory_content_hash(content.strip())
    assert memory_content_hash("é") != memory_content_hash("e\u0301")
    with pytest.raises(ValidationError, match="does not match"):
        record(content_hash="0" * 64)
    with pytest.raises(ValidationError):
        record(content_hash="x" * 64)


def test_times_are_aware_and_normalised_but_expired_records_can_be_stored():
    local = timezone(timedelta(hours=8))
    item = record(
        created_at=NOW.astimezone(local), updated_at=NOW.astimezone(local),
        observed_at=NOW - timedelta(days=1), expires_at=NOW - timedelta(hours=1),
        status="active", verification_status="user_confirmed",
    )
    assert item.created_at == NOW and item.created_at.tzinfo == timezone.utc
    assert not item.is_usable_at(NOW)
    assert MemoryRecord.model_validate_json(item.model_dump_json()) == item


@pytest.mark.parametrize("field,value", [
    ("created_at", NOW.replace(tzinfo=None)), ("updated_at", NOW.replace(tzinfo=None)),
    ("observed_at", NOW.replace(tzinfo=None)), ("expires_at", NOW.replace(tzinfo=None)),
    ("updated_at", NOW - timedelta(seconds=1)), ("observed_at", NOW + timedelta(seconds=1)),
    ("expires_at", NOW), ("created_at", 12345), ("updated_at", NOW.isoformat()),
])
def test_invalid_python_times_or_ordering_are_rejected(field, value):
    with pytest.raises(ValidationError):
        record(**{field: value})


def test_json_time_coercions_and_naive_values_are_rejected():
    payload = json.loads(record().model_dump_json())
    for value in (12345, True, "2026-10-07T02:00:00"):
        with pytest.raises(ValidationError):
            MemoryRecord.model_validate_json(json.dumps({**payload, "created_at": value}))


@pytest.mark.parametrize("status", ["candidate", "disabled", "superseded", "deleted"])
def test_nonactive_states_never_pass_local_usability_check(status):
    assert not record(status=status).is_usable_at(NOW)


def test_active_state_and_expiry_use_explicit_validation_not_model_confidence():
    with pytest.raises(ValidationError, match="unverified"):
        record(status="active")
    valid = record(status="active", verification_status="user_confirmed", expires_at=NOW + timedelta(hours=1))
    assert valid.is_usable_at(NOW)
    assert not valid.is_usable_at(NOW - timedelta(seconds=1))
    assert not valid.is_usable_at(valid.expires_at)
    with pytest.raises(ValueError, match="timezone-aware"):
        valid.is_usable_at(NOW.replace(tzinfo=None))


def test_write_basis_and_verification_require_the_appropriate_source_kinds():
    with pytest.raises(ValidationError, match="explicit_request"):
        record(source_refs=[tool_source()])
    with pytest.raises(ValidationError, match="user_confirmed"):
        record(write_basis="task_extraction", source_refs=[tool_source()], verification_status="user_confirmed")
    with pytest.raises(ValidationError, match="external evidence"):
        record(verification_status="source_supported")
    valid = record(
        kind="fact", source_refs=[tool_source()], write_basis="task_extraction",
        status="active", verification_status="source_supported",
    )
    assert valid.is_usable_at(NOW)
    with pytest.raises(ValidationError, match="confirmation"):
        record(
            source_refs=[tool_source()], write_basis="task_extraction",
            status="active", verification_status="source_supported",
        )


def test_scope_and_versioned_artifact_bindings_must_match():
    with pytest.raises(ValidationError, match="scope must match"):
        record(scope="project")
    reference = artifact_ref()
    namespace = identity().namespace_for("artifact", artifact_identity=reference.identity())
    with pytest.raises(ValidationError, match="versioned artifact_ref"):
        record(scope="artifact", namespace=namespace)
    with pytest.raises(ValidationError, match="namespace artifact identity"):
        record(scope="artifact", namespace=namespace, artifact_ref=artifact_ref(thread_id="other-run"))
    with pytest.raises(ValidationError, match="matching versioned artifact source"):
        record(scope="artifact", namespace=namespace, artifact_ref=reference)
    with pytest.raises(ValidationError, match="matching versioned artifact source"):
        record(
            scope="artifact", namespace=namespace, artifact_ref=reference,
            source_refs=[user_source(), MemorySourceRef(kind="artifact", artifact_ref=artifact_ref(version=2))],
        )


def test_version_chain_requires_actual_predecessor_and_preserves_old_record():
    previous = record()
    before = previous.model_dump_json()
    revised = next_record(previous)
    validate_memory_successor(previous, revised)
    assert previous.model_dump_json() == before
    assert revised.memory_id == previous.memory_id and revised.version == 2
    assert revised.supersedes == previous.version_ref()
    with pytest.raises(ValidationError, match="initial version"):
        record(supersedes=previous.version_ref())
    with pytest.raises(ValidationError, match="require a supersedes"):
        record(version=2)
    with pytest.raises(ValidationError, match="immediately preceding"):
        next_record(previous, version=3)
    with pytest.raises(ValidationError, match="immediately preceding"):
        next_record(previous, memory_id="other-memory")
    with pytest.raises(ValueError, match="stale predecessor"):
        validate_memory_successor(previous, next_record(previous, supersedes=MemoryVersionRef(
            memory_id=previous.memory_id, version=1, content_hash="0" * 64,
        )))


@pytest.mark.parametrize("changes", [
    {"namespace": identity(user_id="other-user").namespace_for("user")},
    {"scope": "project", "namespace": identity().namespace_for("project")},
    {"kind": "fact"},
])
def test_successor_cannot_rebind_the_logical_memory_identity(changes):
    previous = record()
    with pytest.raises(ValueError, match="cannot change identity"):
        validate_memory_successor(previous, next_record(previous, **changes))


def test_successor_cannot_reset_times_or_resurrect_a_deleted_current_version():
    previous = record(updated_at=NOW + timedelta(minutes=5))
    with pytest.raises(ValueError, match="reset creation time"):
        validate_memory_successor(previous, next_record(previous, created_at=NOW - timedelta(minutes=1)))
    with pytest.raises(ValueError, match="backwards"):
        validate_memory_successor(previous, next_record(previous, updated_at=NOW))
    deleted = record(status="deleted")
    with pytest.raises(ValueError, match="deleted"):
        validate_memory_successor(deleted, next_record(deleted))


def test_unvalidated_model_copy_is_rejected_when_reentering_contract_boundary():
    forged = record().model_copy(update={"content": "changed without updating hash"})
    with pytest.raises(ValidationError, match="content_hash"):
        MemoryRecord.model_validate(forged)
    forged_namespace = identity().namespace_for("user").model_copy(update={"project_id": "project-1"})
    with pytest.raises(ValidationError):
        record(namespace=forged_namespace)


def test_random_record_ids_and_event_keys_have_different_responsibilities():
    payload = record_payload()
    payload.pop("memory_id")
    assert MemoryRecord.model_validate(payload).memory_id != MemoryRecord.model_validate(payload).memory_id
    namespace = identity().namespace_for("user")
    kwargs = {"source_event_id": "run-1", "operation_version": 1, "candidate_key": "preference-1"}
    expected = memory_idempotency_key(namespace, **kwargs)
    assert expected == memory_idempotency_key(MemoryNamespace.model_validate_json(namespace.model_dump_json()), **kwargs)
    for changes in (
        {"source_event_id": "run-2"}, {"operation_version": 2}, {"candidate_key": "preference-2"},
    ):
        assert expected != memory_idempotency_key(namespace, **{**kwargs, **changes})
    assert expected != memory_idempotency_key(identity(user_id="other-user").namespace_for("user"), **kwargs)
    assert expected != memory_idempotency_key(identity().namespace_for("project"), **kwargs)
    with pytest.raises(ValidationError):
        memory_idempotency_key(namespace, **{**kwargs, "operation_version": True})


def test_generated_schema_exposes_strict_nested_contracts_and_bounded_sources():
    schema = MemoryRecord.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["source_refs"]["minItems"] == 1
    assert schema["properties"]["content"]["maxLength"] == MAX_MEMORY_CONTENT_CHARS
    for name in ("MemoryNamespace", "MemorySourceRef", "ArtifactReference", "MemoryVersionRef"):
        assert schema["$defs"][name]["additionalProperties"] is False


def test_validation_does_not_mutate_the_input_payload_or_source_list():
    source = {"kind": "user_message", "thread_id": "run-1", "message_id": "message-1"}
    payload = record_payload(source_refs=[source], namespace={"scope": "user", "user_id": "user-1"})
    original_namespace = dict(payload["namespace"])
    original_source = dict(source)
    item = MemoryRecord.model_validate(payload)
    assert payload["namespace"] == original_namespace
    assert source == original_source
    payload["source_refs"].append(user_source(message_id="message-2"))
    source["message_id"] = "changed"
    assert len(item.source_refs) == 1
    assert item.source_refs[0].message_id == "message-1"


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 0}, {"content_hash": "0" * 64 + "\n"},
    {"content_hash": "F" * 64}, {"thread_id": "run-1\n"}, {"task_id": 1},
])
def test_artifact_reference_fields_are_strict_and_canonical(changes):
    with pytest.raises(ValidationError):
        artifact_ref(**changes)


def test_claimed_deliverable_alone_is_not_source_supported_evidence():
    with pytest.raises(ValidationError, match="external evidence"):
        record(
            kind="experience", write_basis="task_extraction",
            verification_status="source_supported",
            source_refs=[MemorySourceRef(kind="deliverable", thread_id="run-1", task_id="task-1")],
        )
