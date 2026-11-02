"""Trusted-host memory management. No model/tool receives this capability.

Deletion is a terminal immutable version, NOT erasure of historical plaintext.
SQLite CAS protects edits; SQLite and Redis still have no distributed transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .memory_records import (
    ArtifactReference, Identifier, MemoryIdentity, MemoryRecord, MemorySourceRef,
    MemoryVersionRef, memory_content_hash, memory_idempotency_key,
)
from .memory_service import Contract, MemoryPolicyError, check_memory_content
from .memory_store import MemoryConflict, MemoryRepository


RETENTION_NOTICE = (
    "停用、纠错和逻辑删除会使旧投影在下次模型调用边界重校验时失效；"
    "已经发出的调用无法撤回。旧版本、历史页面、Redis checkpoint 和现有备份仍可能含原文。"
    "当前没有自动物理擦除、到期清理或删除清单导入；旧备份恢复到新路径后可能恢复旧状态，"
    "须人工对照现库删除记录重做清理并核验，无法核验则不能作为在线库。备份应放在受保护的专用目录，"
    "由用户管理 OS 访问权限及保留期，不可公开或提交 Git。"
)


class MemoryManagementCommand(Contract):
    action: Literal["confirm", "correct", "disable", "enable", "delete"]
    expected: MemoryVersionRef
    # Exact displayed body is part of consent, not a model-supplied boolean.
    confirmed_content: Annotated[str, Field(min_length=1, max_length=8000)]
    content: Annotated[str, Field(min_length=1, max_length=8000)] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def shape(self):
        if not self.reason.strip() or not self.confirmed_content.strip():
            raise ValueError("blank confirmation or reason")
        if (self.action == "correct") != (self.content is not None):
            raise ValueError("only correction requires replacement content")
        return self


class MemoryManager:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    @staticmethod
    def namespaces(identity: MemoryIdentity, scope: str, artifacts: tuple[ArtifactReference, ...]):
        if scope != "artifact":
            return [identity.namespace_for(scope)]
        if len(artifacts) > 1000:
            raise MemoryPolicyError("memory_management_grants_exceeded")
        return list(dict.fromkeys(identity.namespace_for(
            "artifact", artifact_identity=ref.identity()) for ref in artifacts))

    def find(self, memory_id: str, identity: MemoryIdentity, scope: str,
             artifacts: tuple[ArtifactReference, ...] = ()) -> MemoryRecord:
        for namespace in self.namespaces(identity, scope, artifacts):
            record = self.repository.get(memory_id, namespace=namespace, identity=identity)
            if record is not None and (scope != "artifact" or record.artifact_ref in artifacts):
                return record
        raise MemoryPolicyError("memory_not_found")

    def listing(self, identity: MemoryIdentity, scope: str, *, artifacts=(), limit=50, offset=0,
                status=None, kind=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise MemoryPolicyError("memory_management_page_invalid")
        namespaces = self.namespaces(identity, scope, artifacts)
        if scope != "artifact":
            # SQL offset applies to the actual namespace, not a truncated
            # in-memory first page. Large private lists remain manageable.
            page = self.repository.list_records(namespace=namespaces[0], identity=identity,
                limit=limit + 1, offset=offset, statuses=(status,) if status else None,
                kinds=(kind,) if kind else None)
            return page[:limit], len(page) > limit
        if offset + limit + 1 > 10000:
            raise MemoryPolicyError("memory_management_scan_exceeded")
        records, scanned = [], 0
        for namespace in namespaces:
            window = []
            wanted = offset + limit + 1
            while len(window) < wanted:
                count = min(1000, wanted - len(window))
                chunk = self.repository.list_records(namespace=namespace, identity=identity,
                    limit=count, offset=len(window), statuses=(status,) if status else None,
                    kinds=(kind,) if kind else None)
                window.extend(chunk)
                if len(chunk) < count:
                    break
            scanned += len(window)
            records.extend(item for item in window
                if scope != "artifact" or item.artifact_ref in artifacts)
            if scanned > 10000:
                raise MemoryPolicyError("memory_management_scan_exceeded")
        records.sort(key=lambda item: (item.updated_at, item.memory_id), reverse=True)
        return records[offset:offset + limit], len(records) > offset + limit

    @staticmethod
    def successor(previous: MemoryRecord, command: MemoryManagementCommand, *, at=None) -> MemoryRecord:
        command = MemoryManagementCommand.model_validate(command)
        if previous.version_ref() != command.expected or previous.content != command.confirmed_content:
            raise MemoryConflict("memory changed; refresh before editing")
        if previous.status.value in {"deleted", "superseded"}:
            raise MemoryPolicyError("memory_terminal_record")
        check_memory_content(command.reason)
        at = max(at or datetime.now(timezone.utc), previous.updated_at)
        content, status, verification = previous.content, previous.status.value, previous.verification_status.value
        if command.action == "correct":
            check_memory_content(command.content)
            if command.content == previous.content:
                raise MemoryPolicyError("memory_correction_unchanged")
            content = command.content
            # Human correction is consent, not independent corroboration.
            status = "active" if previous.kind.value in {"preference", "rule"} else "candidate"
            verification = "user_confirmed" if status == "active" else "unverified"
            if previous.kind.value == "fact" and (previous.expires_at is None or previous.expires_at <= at):
                raise MemoryPolicyError("memory_fact_time_invalid")
        elif command.action == "confirm":
            if status != "candidate":
                raise MemoryPolicyError("memory_not_candidate")
            # No client boolean/URL can promote an external fact to evidence.
            # Existing host stage_confirmation remains the evidence-aware path.
            if previous.kind.value == "fact":
                raise MemoryPolicyError("memory_fact_requires_verification")
            status, verification = "active", "user_confirmed"
        elif command.action == "enable":
            if status != "disabled" or verification == "unverified":
                raise MemoryPolicyError("memory_enable_requires_verification")
            if previous.expires_at is not None and previous.expires_at <= at:
                raise MemoryPolicyError("memory_expired")
            status = "active"
        elif command.action == "disable":
            if status == "disabled":
                raise MemoryPolicyError("memory_already_disabled")
            status = "disabled"
        else:
            status = "deleted"
        event = "manage-" + uuid4().hex
        source = MemorySourceRef(kind="user_message", thread_id="memory-management", message_id=event)
        edits = command.action in {"confirm", "correct"}
        sources = tuple(dict.fromkeys((*previous.source_refs, source))) if edits else previous.source_refs
        if len(sources) > 32:
            # Do not discard lineage just to fit the schema.
            raise MemoryPolicyError("memory_source_capacity_exceeded")
        return MemoryRecord.model_validate({
            **previous.model_dump(), "version": previous.version + 1, "supersedes": previous.version_ref(),
            "updated_at": at, "content": content, "content_hash": memory_content_hash(content),
            "status": status, "verification_status": verification, "source_refs": sources,
            "write_basis": "explicit_request" if edits else previous.write_basis, "revision_note": command.reason,
            "idempotency_key": memory_idempotency_key(previous.namespace, source_event_id=event,
                                                       operation_version=1, candidate_key=command.action),
        })

    def apply(self, command: MemoryManagementCommand, identity: MemoryIdentity, scope: str, *, artifacts=()):
        record = self.find(command.expected.memory_id, identity, scope, artifacts)
        return self.repository.save(self.successor(record, command), identity=identity)

    def related(self, identity, thread_ids, include_promoted=False):
        return self.repository.conversation_records(identity=identity, thread_ids=thread_ids,
                                                     include_promoted=include_promoted)

    def delete_related(self, identity, thread_ids, include_promoted=False):
        records = self.related(identity, thread_ids, include_promoted)
        replacements = tuple(self.successor(record, MemoryManagementCommand(
            action="delete", expected=record.version_ref(), confirmed_content=record.content,
            reason="用户删除会话时选择关联清理")) for record in records)
        return self.repository.save_batch(replacements, identity=identity)
