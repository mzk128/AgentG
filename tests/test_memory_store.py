"""SQLite-only offline tests: temporary databases, no Redis/models/user data."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError

from multi_agent_system.config import Settings, get_settings
from multi_agent_system.memory_records import (
    ArtifactIdentity, ArtifactReference, MemoryIdentity, MemoryRecord,
    MemorySourceRef, memory_content_hash,
)
from multi_agent_system.memory_store import (
    APPLICATION_ID, APPLICATION_ROOT, SCHEMA_VERSION,
    MemoryConflict, MemorySchemaError, MemoryStoreBusy, MemoryStoreCorrupt,
    MemoryStoreUnavailable, SQLiteMemoryRepository, create_memory_repository,
    resolve_memory_db_path,
)


NOW = datetime(2026, 10, 7, 3, tzinfo=timezone.utc)


@pytest.fixture
def identity():
    return MemoryIdentity(user_id="user-1", project_id="project-1", conversation_id="chat-1")


@pytest.fixture
def repo(tmp_path):
    with SQLiteMemoryRepository(tmp_path / "memory.sqlite3") as repository:
        yield repository


def make_record(identity, **changes):
    scope = changes.get("scope", "user")
    reference = None
    sources = [MemorySourceRef(kind="user_message", thread_id="run-1", message_id="msg-1")]
    if scope == "artifact":
        reference = ArtifactReference(
            thread_id="run-1", task_id="task-1", artifact_id="artifact-1",
            version=1, content_hash=memory_content_hash("output"),
        )
        sources.append(MemorySourceRef(kind="artifact", artifact_ref=reference))
    content = changes.get("content", "用户偏好：中文，先给结论。")
    return MemoryRecord.model_validate({
        "memory_id": "memory-1", "scope": scope, "kind": "preference",
        "namespace": identity.namespace_for(
            scope, artifact_identity=reference.identity() if reference else None,
        ), "content": content, "content_hash": memory_content_hash(content),
        "source_refs": sources, "write_basis": "explicit_request",
        "verification_status": "user_confirmed", "status": "active",
        "created_at": NOW, "updated_at": NOW, "idempotency_key": "event-1",
        "artifact_ref": reference, **changes,
    })


def successor(previous, **changes):
    content = changes.get("content", "修订后的记忆。")
    return MemoryRecord.model_validate({
        **previous.model_dump(), "version": previous.version + 1,
        "supersedes": previous.version_ref(), "content": content,
        "content_hash": memory_content_hash(content),
        "updated_at": previous.updated_at + timedelta(minutes=1),
        "idempotency_key": f"event-{previous.version + 1}", **changes,
    })


def owned(identity, record):
    return {"identity": identity, "namespace": record.namespace}


def counts(path):
    with closing(sqlite3.connect(path)) as db:
        return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("memory_versions", "memory_heads", "memory_sources"))


@pytest.mark.parametrize("scope", ["user", "project", "conversation", "artifact"])
@pytest.mark.parametrize("kind", ["preference", "fact", "experience", "rule"])
def test_all_scopes_and_kinds_persist_and_reopen(tmp_path, identity, scope, kind):
    record = make_record(identity, scope=scope, kind=kind)
    path = tmp_path / "memory.sqlite3"
    with SQLiteMemoryRepository(path) as db:
        assert db.save(record, identity=identity) == record
    with SQLiteMemoryRepository(path) as db:
        assert db.get(record.memory_id, **owned(identity, record)) == record
        assert db.query_active(at=NOW, **owned(identity, record)) == [record]


def test_same_event_is_idempotent_even_after_a_later_version(repo, identity):
    record = make_record(identity)
    v2 = successor(record)
    repo.save(record, identity=identity)
    repo.save(v2, identity=identity)
    assert repo.save(record, identity=identity) == record
    assert repo.save(v2, identity=identity) == v2
    assert counts(repo.db_path) == (2, 1, 2)
    assert repo.get(record.memory_id, **owned(identity, record)) == v2
    assert repo.get(record.memory_id, version=1, **owned(identity, record)) == record
    assert repo.history(record.memory_id, **owned(identity, record)) == [record, v2]


@pytest.mark.parametrize("changes", [
    {"content": "不同正文"}, {"memory_id": "another"},
    {"updated_at": NOW + timedelta(seconds=1)}, {"status": "disabled"},
])
def test_same_event_with_different_payload_is_rejected(repo, identity, changes):
    record = make_record(identity)
    repo.save(record, identity=identity)
    with pytest.raises(MemoryConflict, match="idempotency"):
        repo.save(make_record(identity, **changes), identity=identity)
    assert counts(repo.db_path) == (1, 1, 1)


def test_same_text_does_not_merge_intentionally_distinct_events(repo, identity):
    record = make_record(identity)
    other = make_record(identity, memory_id="memory-2", idempotency_key="event-2")
    repo.save(record, identity=identity)
    repo.save(other, identity=identity)
    assert counts(repo.db_path) == (2, 2, 2)
    # Same event string can legitimately be scoped in another namespace.
    project = make_record(identity, memory_id="memory-project", scope="project")
    repo.save(project, identity=identity)
    assert counts(repo.db_path) == (3, 3, 3)


@pytest.mark.parametrize("case", ["missing", "stale_hash", "skip", "rebind", "kind", "backwards"])
def test_invalid_successors_are_rejected_atomically(repo, identity, case):
    previous = make_record(identity)
    repo.save(previous, identity=identity)
    changes = {}
    if case == "missing":
        changes = {"memory_id": "missing", "supersedes": {
            "memory_id": "missing", "version": 1, "content_hash": previous.content_hash}}
    elif case == "stale_hash":
        changes = {"supersedes": {**previous.version_ref().model_dump(), "content_hash": "0" * 64}}
    elif case == "skip":
        changes = {"version": 3, "supersedes": {
            "memory_id": previous.memory_id, "version": 2, "content_hash": previous.content_hash}}
    elif case == "rebind":
        changes = {"scope": "project", "namespace": identity.namespace_for("project")}
    elif case == "kind":
        changes = {"kind": "fact"}
    else:
        changes = {"created_at": NOW - timedelta(days=1), "updated_at": NOW - timedelta(seconds=1)}
    with pytest.raises(MemoryConflict):
        repo.save(successor(previous, **changes), identity=identity)
    assert counts(repo.db_path) == (1, 1, 1)
    assert repo.get(previous.memory_id, **owned(identity, previous)) == previous


def test_deleted_head_cannot_be_resurrected_or_retrieve_old_active(repo, identity):
    previous = make_record(identity)
    repo.save(previous, identity=identity)
    deleted = successor(previous, status="deleted")
    repo.save(deleted, identity=identity)
    assert repo.query_active(at=NOW + timedelta(days=1), **owned(identity, previous)) == []
    with pytest.raises(MemoryConflict):
        repo.save(successor(deleted, status="active"), identity=identity)
    assert repo.get(previous.memory_id, version=1, **owned(identity, previous)) == previous


def test_save_revalidates_unsafe_model_copy(repo, identity):
    record = make_record(identity)
    with pytest.raises(ValidationError):
        repo.save(record.model_copy(update={"content": "tampered"}), identity=identity)
    assert counts(repo.db_path) == (0, 0, 0)


@pytest.mark.parametrize("field", ["user_id", "project_id", "conversation_id"])
def test_foreign_host_context_is_rejected_for_read_and_write(repo, identity, field):
    record = make_record(identity, scope="conversation")
    repo.save(record, identity=identity)
    foreign = MemoryIdentity.model_validate({**identity.model_dump(), field: "foreign"})
    with pytest.raises(ValueError, match="host identity"):
        repo.save(record, identity=foreign)
    with pytest.raises(ValueError, match="host identity"):
        repo.get(record.memory_id, namespace=record.namespace, identity=foreign)
    with pytest.raises(ValueError, match="host identity"):
        repo.query_active(namespace=record.namespace, identity=foreign, at=NOW)


def test_exact_namespace_and_run_qualified_artifact_isolation(repo, identity):
    record = make_record(identity, scope="artifact")
    repo.save(record, identity=identity)
    namespace = identity.namespace_for("artifact", artifact_identity=ArtifactIdentity(
        thread_id="run-2", task_id="task-1", artifact_id="artifact-1"))
    assert repo.get(record.memory_id, namespace=namespace, identity=identity) is None
    assert repo.history(record.memory_id, namespace=namespace, identity=identity) == []
    assert repo.query_active(namespace=namespace, identity=identity, at=NOW) == []


def test_management_is_separate_from_active_time_filtered_query(repo, identity):
    variants = [
        {}, {"status": "candidate", "verification_status": "unverified"},
        {"status": "disabled"}, {"status": "deleted"}, {"status": "superseded"},
        {"expires_at": NOW + timedelta(hours=1)},
        {"observed_at": NOW + timedelta(days=2), "updated_at": NOW + timedelta(days=2)},
        {"created_at": NOW + timedelta(days=2), "updated_at": NOW + timedelta(days=2)},
    ]
    records = [make_record(identity, memory_id=f"m-{i}", idempotency_key=f"e-{i}", **changes)
               for i, changes in enumerate(variants)]
    for record in records:
        repo.save(record, identity=identity)
    args = owned(identity, records[0])
    assert repo.query_active(at=NOW + timedelta(days=1), **args) == [records[0]]
    assert len(repo.list_records(**args)) == 8
    assert repo.list_records(statuses=("candidate",), **args) == [records[1]]
    assert repo.list_records(kinds=("fact",), **args) == []
    assert len(repo.list_records(limit=2, offset=2, **args)) == 2
    assert records[5] not in repo.query_active(at=records[5].expires_at, **args)


@pytest.mark.parametrize("changes", [
    {"limit": True}, {"limit": 0}, {"limit": 1001}, {"offset": -1}, {"offset": "1"},
    {"statuses": []}, {"statuses": ()}, {"statuses": ("ACTIVE",)},
    {"statuses": ("active", "active")}, {"kinds": (3,)},
])
def test_invalid_query_arguments_are_rejected(repo, identity, changes):
    with pytest.raises(ValueError):
        repo.list_records(namespace=identity.namespace_for("user"), identity=identity, **changes)


@pytest.mark.parametrize("at", [None, "2026-10-07", NOW.replace(tzinfo=None)])
def test_naive_or_missing_query_time_cannot_bypass_expiry(repo, identity, at):
    with pytest.raises(ValueError, match="timezone"):
        repo.query_active(namespace=identity.namespace_for("user"), identity=identity, at=at)


def test_concurrent_successors_have_one_winner_and_no_lost_version(tmp_path, identity):
    path = tmp_path / "concurrent.sqlite3"
    repos = [SQLiteMemoryRepository(path), SQLiteMemoryRepository(path)]
    previous = make_record(identity)
    repos[0].save(previous, identity=identity)
    barrier = Barrier(2)

    def write(index):
        barrier.wait(timeout=5)
        try:
            return repos[index].save(successor(
                previous, content=f"proposal-{index}", idempotency_key=f"proposal-{index}",
            ), identity=identity)
        except MemoryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert sum(result is not None for result in results) == 1
    assert counts(path) == (2, 1, 2)
    assert repos[0].history(previous.memory_id, **owned(identity, previous))[0] == previous
    for repository in repos:
        repository.close()


def test_concurrent_replays_only_insert_one_record(repo, identity):
    record = make_record(identity)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repo.save(record, identity=identity), range(8)))
    assert results == [record] * 8
    assert counts(repo.db_path) == (1, 1, 1)


def test_sqlite_lock_wait_is_bounded_and_reported(tmp_path, identity):
    path = tmp_path / "locked.sqlite3"
    with SQLiteMemoryRepository(path, busy_timeout_ms=40) as repository:
        with closing(sqlite3.connect(path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            with pytest.raises(MemoryStoreBusy):
                repository.save(make_record(identity), identity=identity)
            assert time.monotonic() - start < 2
            assert counts(path) == (0, 0, 0)
            lock.rollback()


def test_insert_failure_rolls_back_versions_sources_and_heads(repo, identity):
    with closing(sqlite3.connect(repo.db_path)) as db:
        db.execute("CREATE TRIGGER fail_source BEFORE INSERT ON memory_sources "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        db.commit()
    with pytest.raises(MemoryConflict):
        repo.save(make_record(identity), identity=identity)
    assert counts(repo.db_path) == (0, 0, 0)


def test_empty_database_is_migrated_and_reopening_does_not_reset(repo, identity):
    with closing(sqlite3.connect(repo.db_path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    record = make_record(identity)
    repo.save(record, identity=identity)
    with SQLiteMemoryRepository(repo.db_path) as reopened:
        assert reopened.get(record.memory_id, **owned(identity, record)) == record


@pytest.mark.parametrize("case", ["future", "foreign", "foreign_table", "missing_index", "missing_table"])
def test_unsupported_schema_is_not_silently_reset(tmp_path, case):
    path = tmp_path / "schema.sqlite3"
    if case != "foreign_table":
        SQLiteMemoryRepository(path).close()
    with closing(sqlite3.connect(path)) as db:
        if case == "future":
            db.execute("PRAGMA user_version=99")
        elif case == "foreign":
            db.execute("PRAGMA application_id=123")
        elif case == "foreign_table":
            db.execute("CREATE TABLE user_data (content TEXT)")
        elif case == "missing_index":
            db.execute("DROP INDEX memory_filter_idx")
        else:
            db.execute("DROP TABLE memory_sources")
        db.commit()
    before = path.read_bytes()
    with pytest.raises(MemorySchemaError):
        SQLiteMemoryRepository(path)
    assert path.read_bytes() == before


def test_non_sqlite_file_is_not_reinitialized(tmp_path):
    path = tmp_path / "damaged.sqlite3"
    path.write_bytes(b"not a database")
    with pytest.raises(MemoryStoreCorrupt):
        SQLiteMemoryRepository(path)
    assert path.read_bytes() == b"not a database"


def test_migration_failure_rolls_back_partial_schema(monkeypatch, tmp_path):
    from multi_agent_system import memory_store
    path = tmp_path / "failed-migration.sqlite3"
    monkeypatch.setattr(memory_store, "_DDL", (*memory_store._DDL, "INVALID SQL"))
    with pytest.raises(MemoryStoreUnavailable):
        SQLiteMemoryRepository(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


@pytest.mark.parametrize("case", ["json", "index", "source"])
def test_corrupt_records_are_errors_not_empty_results(repo, identity, case):
    record = make_record(identity)
    repo.save(record, identity=identity)
    with closing(sqlite3.connect(repo.db_path)) as db:
        if case == "json":
            payload = json.loads(record.model_dump_json())
            payload["content"] = "wrong hash"
            db.execute("UPDATE memory_versions SET record_json=?", (json.dumps(payload),))
        elif case == "index":
            db.execute("UPDATE memory_versions SET kind='fact'")
        else:
            db.execute("DELETE FROM memory_sources")
        db.commit()
    with pytest.raises(MemoryStoreCorrupt):
        repo.get(record.memory_id, **owned(identity, record))


def test_backup_and_restore_include_committed_wal_and_version_history(repo, identity, tmp_path):
    record = make_record(identity)
    second = successor(record)
    repo.save(record, identity=identity)
    # Keep a WAL reader open so a main-file-only copy cannot be trusted.
    with closing(sqlite3.connect(repo.db_path)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM memory_heads").fetchall()
        repo.save(second, identity=identity)
        assert repo.db_path.with_name(repo.db_path.name + "-wal").exists()
        backup = repo.backup(tmp_path / "backup.sqlite3")
        assert counts(backup) == (2, 1, 2)
        before = backup.read_bytes()
        with SQLiteMemoryRepository.restore(backup, tmp_path / "restored.sqlite3") as restored:
            assert restored.history(record.memory_id, **owned(identity, record)) == [record, second]
            assert restored.query_active(at=NOW + timedelta(days=1), **owned(identity, record)) == [second]
        assert backup.read_bytes() == before


def test_backup_and_restore_never_overwrite_existing_destination(repo, tmp_path):
    target = tmp_path / "existing.sqlite3"
    target.write_bytes(b"existing file")
    with pytest.raises(ValueError, match="already exists"):
        repo.backup(target)
    backup = repo.backup(tmp_path / "backup.sqlite3")
    with pytest.raises(ValueError, match="already exists"):
        SQLiteMemoryRepository.restore(backup, target)
    with pytest.raises(ValueError, match="differ"):
        repo.backup(repo.db_path)
    assert target.read_bytes() == b"existing file"


def test_damaged_memory_payload_prevents_backup_and_restore(repo, identity, tmp_path):
    record = make_record(identity)
    repo.save(record, identity=identity)
    with closing(sqlite3.connect(repo.db_path)) as db:
        db.execute("UPDATE memory_versions SET record_json='{}'")
        db.commit()
    backup = tmp_path / "bad-backup.sqlite3"
    with pytest.raises(MemoryStoreCorrupt):
        repo.backup(backup)
    assert not backup.exists()  # Only the incomplete file allocated here was cleaned up.
    # Existing bad source is preserved, and no restore destination is created.
    with pytest.raises(MemoryStoreCorrupt):
        SQLiteMemoryRepository.restore(repo.db_path, tmp_path / "restored.sqlite3")
    assert repo.db_path.exists()
    assert not (tmp_path / "restored.sqlite3").exists()


def test_changed_schema_header_is_not_exported_as_a_valid_backup(repo, tmp_path):
    with closing(sqlite3.connect(repo.db_path)) as db:
        db.execute("PRAGMA user_version=99")
    backup = tmp_path / "unsupported.sqlite3"
    with pytest.raises(MemorySchemaError):
        repo.backup(backup)
    assert not backup.exists()


def test_corrupt_head_cannot_broaden_namespace_access(repo, identity):
    record = make_record(identity, scope="project")
    repo.save(record, identity=identity)
    namespace = identity.namespace_for("user")
    with closing(sqlite3.connect(repo.db_path)) as db:
        db.execute("UPDATE memory_heads SET namespace_key=?",
                   (json.dumps(namespace.key(), separators=(",", ":")),))
        db.commit()
    with pytest.raises(MemoryStoreCorrupt):
        repo.get(record.memory_id, namespace=namespace, identity=identity)
    with pytest.raises(MemoryStoreCorrupt):
        repo.query_active(namespace=namespace, identity=identity, at=NOW)
    with pytest.raises(MemoryStoreCorrupt):
        SQLiteMemoryRepository(repo.db_path)


def test_restore_rejects_missing_damaged_and_foreign_backups(tmp_path):
    backup = tmp_path / "backup.sqlite3"
    destination = tmp_path / "result.sqlite3"
    with pytest.raises(MemoryStoreUnavailable):
        SQLiteMemoryRepository.restore(backup, destination)
    backup.write_bytes(b"broken")
    with pytest.raises(MemoryStoreCorrupt):
        SQLiteMemoryRepository.restore(backup, destination)
    assert not destination.exists()
    foreign = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE foreign_data (id TEXT)")
    with pytest.raises(MemorySchemaError):
        SQLiteMemoryRepository.restore(foreign, destination)


def test_cross_process_reopen_reads_committed_contract(repo, identity):
    record = make_record(identity)
    repo.save(record, identity=identity)
    code = """
import sys
from multi_agent_system.memory_store import SQLiteMemoryRepository
from multi_agent_system.memory_records import MemoryIdentity
i = MemoryIdentity(user_id='user-1', project_id='project-1', conversation_id='chat-1')
with SQLiteMemoryRepository(sys.argv[1]) as r:
    result = r.get('memory-1', identity=i, namespace=i.namespace_for('user'))
    print(result.model_dump_json())
"""
    environment = {**os.environ, "PYTHONPATH": str(APPLICATION_ROOT / "src"),
                   "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-c", code, str(repo.db_path)],
                            env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert MemoryRecord.model_validate_json(result.stdout.strip()) == record


@pytest.mark.parametrize("path", ["", " ", ":memory:", "file:test.sqlite3", ".env", ".ENV", ".env.backup"])
def test_invalid_or_protected_paths_are_rejected_without_io(path):
    with pytest.raises(ValueError):
        resolve_memory_db_path(path)


@pytest.mark.parametrize("timeout", [0, -1, True, 60001, "5000"])
def test_lock_timeout_bounds(tmp_path, timeout):
    with pytest.raises(ValueError):
        SQLiteMemoryRepository(tmp_path / "never.sqlite3", busy_timeout_ms=timeout)
    assert not (tmp_path / "never.sqlite3").exists()


def test_relative_path_resolution_is_independent_of_cwd(monkeypatch, tmp_path):
    first = resolve_memory_db_path(".agentg/memory/long_term.sqlite3")
    monkeypatch.chdir(tmp_path)
    assert resolve_memory_db_path(".agentg/memory/long_term.sqlite3") == first
    assert first == APPLICATION_ROOT / ".agentg" / "memory" / "long_term.sqlite3"


def test_disabled_factory_has_no_io_and_is_independent_of_redis(tmp_path):
    path = tmp_path / "not-created" / "memory.sqlite3"
    for use_redis in (False, True):
        settings = Settings(openai_api_key="", openai_base_url="", model_name="",
                            use_redis=use_redis, redis_url="", memory_db_path=str(path))
        assert create_memory_repository(settings) is None
    assert not path.parent.exists()
    enabled = Settings(openai_api_key="", openai_base_url="", model_name="",
                       use_redis=True, redis_url="", long_term_memory_enabled=True,
                       memory_db_path=str(path), memory_db_busy_timeout_ms=200)
    with create_memory_repository(enabled) as db:
        assert db.db_path == path


def test_config_env_and_default_memory_fields(monkeypatch, tmp_path):
    for name in ("LONG_TERM_MEMORY_ENABLED", "MEMORY_DB_PATH", "MEMORY_DB_BUSY_TIMEOUT_MS"):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert settings.long_term_memory_enabled is False
    assert resolve_memory_db_path(settings.memory_db_path) == APPLICATION_ROOT / ".agentg/memory/long_term.sqlite3"
    assert settings.memory_db_busy_timeout_ms == 5000
    monkeypatch.setenv("LONG_TERM_MEMORY_ENABLED", "TRUE")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "explicit.sqlite3"))
    monkeypatch.setenv("MEMORY_DB_BUSY_TIMEOUT_MS", "250")
    settings = get_settings()
    assert settings.long_term_memory_enabled is True
    assert settings.memory_db_busy_timeout_ms == 250
    assert settings.memory_db_path == str(tmp_path / "explicit.sqlite3")


def test_closed_and_removed_stores_are_unavailable_not_empty(repo, identity):
    namespace = identity.namespace_for("user")
    path = repo.db_path
    path.unlink()
    with pytest.raises(MemoryStoreUnavailable):
        repo.query_active(namespace=namespace, identity=identity, at=NOW)
    assert not path.exists()  # Ordinary read must not silently create a new empty DB.
    repo.close()
    with pytest.raises(MemoryStoreUnavailable, match="closed"):
        repo.list_records(namespace=namespace, identity=identity)
    with pytest.raises(MemoryStoreUnavailable, match="closed"):
        repo.__enter__()
