"""Local single-user management API; namespace checks do not add authentication."""
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from .config import get_settings
from .memory_management import MemoryManagementCommand, MemoryManager, RETENTION_NOTICE
from .memory_records import ArtifactReference, MemoryIdentity
from .memory_service import MemoryPolicyError
from .memory_store import MemoryConflict, MemoryStoreError, create_memory_repository

router = APIRouter()
Scope = Literal["user", "project", "conversation", "artifact"]


def host_context(conversation_id: str | None):
    from .web_server import _conversation_records
    settings = get_settings()
    host = MemoryIdentity(user_id=settings.memory_user_id, project_id=settings.memory_project_id or None)
    records = []
    if conversation_id:
        records = _conversation_records(conversation_id)
        if not records:
            raise HTTPException(404, "会话不存在")
        host = MemoryIdentity(user_id=host.user_id, project_id=host.project_id, conversation_id=conversation_id)
        for _, record in records:
            if record.get("memory_binding"):
                binding = MemoryIdentity.model_validate(record["memory_binding"])
                if binding != host:
                    raise HTTPException(403, "memory_host_binding_changed")
    grants = []
    for thread_id, record in records:
        for item in record.get("worker_results", []):
            artifact = item.get("artifact")
            if not artifact:
                continue
            try:
                grants.append(ArtifactReference(thread_id=thread_id, task_id=item["task_id"],
                    artifact_id=artifact["artifact_id"], version=artifact["version"], content_hash=artifact["content_hash"]))
            except (ValueError, KeyError):
                continue  # Invalid stored artifacts never become grants.
    return host, tuple(dict.fromkeys(grants)), tuple(thread_id for thread_id, _ in records)


@contextmanager
def memory_access(conversation_id=None):
    repository = None
    try:
        settings = get_settings()
        if not settings.long_term_memory_enabled:
            raise HTTPException(409, "memory_disabled")
        identity, grants, threads = host_context(conversation_id)
        repository = create_memory_repository(settings)
        yield MemoryManager(repository), identity, grants, threads
    except MemoryConflict:
        raise HTTPException(409, "memory_version_conflict：条目已变化，请刷新后重试") from None
    except MemoryPolicyError as error:
        raise HTTPException(404 if error.code == "memory_not_found" else 409, error.code) from None
    except MemoryStoreError:
        raise HTTPException(503, "memory_store_unavailable") from None
    except (ValidationError, ValueError):
        raise HTTPException(400, "memory_management_request_invalid") from None
    finally:
        if repository is not None:
            repository.close()


def check_local_mutation(request: Request):
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "跨站管理请求已拒绝")
    if "application/json" not in request.headers.get("content-type", "").lower():
        raise HTTPException(415, "管理操作必须使用 application/json")


@router.get("/api/memories/settings")
def memory_settings():
    settings = get_settings()
    return {"enabled": settings.long_term_memory_enabled, "auto_extract": settings.memory_auto_extract,
            "user_id": settings.memory_user_id, "project_id": settings.memory_project_id or None,
            "retention_notice": RETENTION_NOTICE}


@router.get("/api/memories")
def list_memories(scope: Scope = "user", conversation_id: str | None = None,
                  status: Literal["candidate", "active", "disabled", "superseded", "deleted"] | None = None,
                  kind: Literal["preference", "fact", "experience", "rule"] | None = None,
                  limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000)):
    with memory_access(conversation_id) as (manager, identity, grants, _):
        records, more = manager.listing(identity, scope, artifacts=grants, limit=limit, offset=offset,
                                        status=status, kind=kind)
        now = datetime.now(timezone.utc)
        return {"items": [{**record.model_dump(mode="json"), "usable": record.is_usable_at(now)} for record in records],
                "has_more": more, "offset": offset, "retention_notice": RETENTION_NOTICE}


@router.get("/api/memories/{memory_id}")
def memory_detail(memory_id: str, scope: Scope = "user", conversation_id: str | None = None,
                  limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000)):
    with memory_access(conversation_id) as (manager, identity, grants, _):
        record = manager.find(memory_id, identity, scope, grants)
        history = manager.repository.history(memory_id, namespace=record.namespace, identity=identity,
                                              limit=limit, offset=offset)
        has_more = offset + len(history) < record.version
        if scope == "artifact":
            history = [item for item in history if item.artifact_ref in grants]
        return {"record": record.model_dump(mode="json"),
                "history": [item.model_dump(mode="json") for item in history],
                "has_more": has_more, "retention_notice": RETENTION_NOTICE}


@router.post("/api/memories/{memory_id}/actions")
async def manage_memory(memory_id: str, request: Request, scope: Scope = "user", conversation_id: str | None = None):
    check_local_mutation(request)
    # Preserve strict JSON semantics (no arbitrary Python before-coercion).
    with memory_access(conversation_id) as (manager, identity, grants, _):
        command = MemoryManagementCommand.model_validate_json(await request.body())
        if command.expected.memory_id != memory_id:
            raise HTTPException(400, "memory_target_mismatch")
        record = manager.apply(command, identity, scope, artifacts=grants)
        return {"record": record.model_dump(mode="json"), "retention_notice": RETENTION_NOTICE}


@router.get("/api/conversations/{conversation_id}/memory-impact")
def conversation_memory_impact(conversation_id: str):
    if not get_settings().long_term_memory_enabled:
        return {"enabled": False, "private": 0, "promoted": 0, "retention_notice": RETENTION_NOTICE}
    with memory_access(conversation_id) as (manager, identity, _, threads):
        private = manager.related(identity, threads)
        all_related = manager.related(identity, threads, True)
        return {"enabled": True, "private": len(private), "promoted": len(all_related) - len(private),
                "retention_notice": RETENTION_NOTICE}


def cleanup_conversation_memory(conversation_id: str, selection: str):
    if not get_settings().long_term_memory_enabled:
        return 0
    with memory_access(conversation_id) as (manager, identity, _, threads):
        return len(manager.delete_related(identity, threads, selection == "all_related"))
