"""Governed long-term memory: deterministic write plans and bounded retrieval.

Only trusted host code constructs evidence contexts. These checks do not add
authentication or prove semantic truth. No LLM, network or implicit retries.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .memory import estimate_tokens
from .memory_records import (
    ArtifactReference, ContentHash, Identifier, KindValue, MemoryIdentity,
    MemoryKind, MemoryNamespace, MemoryRecord, MemorySourceRef, MemoryStatus,
    MemoryVersionRef, ScopeValue, WriteBasisValue, memory_content_hash,
    memory_idempotency_key,
)
from .memory_store import MemoryConflict, MemoryRepository, MemoryStoreError


class MemoryPolicyError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)  # Never include rejected content/credentials.
        self.code = code


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              revalidate_instances="always", validate_default=True)

class MemoryEvidence(Contract):
    reference: MemorySourceRef
    namespace: MemoryNamespace
    verified: bool = False


class MemoryWriteContext(Contract):
    identity: MemoryIdentity
    thread_id: Identifier
    at: AwareDatetime
    user_text: str = ""
    evidence: tuple[MemoryEvidence, ...] = ()


class MemoryWriteIntent(Contract):
    key: Identifier
    scope: ScopeValue
    kind: KindValue
    content: Annotated[str, Field(min_length=1, max_length=8000)]
    source_refs: Annotated[tuple[MemorySourceRef, ...], Field(min_length=1, max_length=32)]
    write_basis: WriteBasisValue = "task_extraction"
    observed_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    artifact_ref: ArtifactReference | None = None


class MemoryWritePlan(Contract):
    key: Identifier
    status: Literal["ready", "rejected", "conflict"]
    reason: str = ""
    record: MemoryRecord | None = None


class MemoryWriteResult(Contract):
    key: Identifier
    status: Literal["saved", "candidate", "rejected", "conflict", "unavailable", "not_saved"]
    reason: str = ""
    reference: MemoryVersionRef | None = None


class MemoryExcerpt(Contract):
    memory_id: Identifier
    version: Annotated[int, Field(ge=1)]
    content_hash: ContentHash
    namespace: MemoryNamespace
    kind: KindValue
    content: str
    source_refs: tuple[MemorySourceRef, ...]
    verification_status: Literal["user_confirmed", "source_supported"]
    observed_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    artifact_ref: ArtifactReference | None
    required: bool = False


class MemorySnapshot(Contract):
    items: tuple[MemoryExcerpt, ...] = ()
    query_hash: ContentHash
    selected_at: AwareDatetime
    scanned: int = 0
    matched: int = 0
    omitted: int = 0
    invalidated: int = 0
    scan_limited: bool = False


def memory_payload(snapshot: MemorySnapshot | dict | None) -> list[dict]:
    if not snapshot:
        return []
    parsed = (MemorySnapshot.model_validate_json(json.dumps(snapshot, ensure_ascii=False))
              if isinstance(snapshot, dict) else MemorySnapshot.model_validate(snapshot))
    return [item.model_dump(mode="json") for item in parsed.items]


_SCOPES = {"用户": "user", "项目": "project", "会话": "conversation"}
_KINDS = {"偏好": "preference", "事实": "fact", "经验": "experience", "规则": "rule"}
_COMMAND = re.compile(r"^(?:请)?记住\[(用户|项目|会话)/(偏好|事实|经验|规则)/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\][：:]\s*([\s\S]+)$")
_CONFIRM = re.compile(r"^(?:请)?确认记忆\[(用户|项目|会话)/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\][：:]\s*([\s\S]+)$")


def explicit_intent(text: str, thread_id: str) -> MemoryWriteIntent | None:
    """Narrow opt-in grammar; ordinary conversation is never consent."""
    match = _COMMAND.fullmatch(text.strip())
    if not match:
        return None
    scope, kind, key, content = match.groups()
    return MemoryWriteIntent(
        key=key, scope=_SCOPES[scope], kind=_KINDS[kind], content=content,
        write_basis="explicit_request", source_refs=(MemorySourceRef(
            kind="user_message", thread_id=thread_id, message_id="user-task"),),
    )


def check_memory_content(text: str) -> None:
    # Deliberately conservative heuristics, not a complete DLP/semantic filter.
    patterns = (
        r"(?i)(?<![A-Za-z0-9_])(?:sk[-_]|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{8,}",
        r"(?i)(?:api[_ -]?key|password|secret|access[_ -]?token|authorization|密码|密钥)\s*[=:：]\s*\S+",
        r"-----BEGIN .*PRIVATE KEY-----", r"(?i)bearer\s+[A-Za-z0-9._-]+",
        r"(?i)\.env(?:\b|[./\\])", r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        r"(?<![A-Za-z0-9])1[3-9]\d{9}(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])\d{17}[\dXx](?![A-Za-z0-9])",
        r"(?i)(?:Thought\s*:|private chain.of.thought|ignore (?:all |previous |system )*instructions)",
        r"忽略.{0,8}(?:系统|安全|工具权限)|(?:输出|保存|展示).{0,8}私有思维链",
    )
    if any(re.search(pattern, text) for pattern in patterns):
        raise MemoryPolicyError("memory_sensitive_or_unsafe")


def _tokens(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text[:8000]):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def _excerpt(record: MemoryRecord, required: bool) -> MemoryExcerpt:
    return MemoryExcerpt.model_validate({
        key: getattr(record, key) for key in MemoryExcerpt.model_fields if key != "required"
    } | {"required": required})


class MemoryService:
    def __init__(self, repository: MemoryRepository, *, top_k: int = 5,
                 max_tokens: int = 4000, scan_limit: int = 200):
        if any(type(v) is not int for v in (top_k, max_tokens, scan_limit)):
            raise ValueError("memory budgets must be integers")
        if not 1 <= top_k <= 100 or not 2 <= max_tokens <= 100000 or not 1 <= scan_limit <= 999:
            raise ValueError("memory budgets out of range")
        self.repository = repository
        self.top_k, self.max_tokens, self.scan_limit = top_k, max_tokens, scan_limit

    def namespaces(self, identity: MemoryIdentity, artifacts: tuple[ArtifactReference, ...] = ()):
        identity = MemoryIdentity.model_validate(identity)
        if type(artifacts) is not tuple:
            raise MemoryPolicyError("memory_artifact_grants_invalid")
        artifacts = tuple(ArtifactReference.model_validate(ref) for ref in artifacts)
        if len(artifacts) > 16:
            raise MemoryPolicyError("memory_artifact_grants_exceeded")
        result = [identity.namespace_for("user")]
        if identity.project_id:
            result.append(identity.namespace_for("project"))
        if identity.conversation_id:
            result.append(identity.namespace_for("conversation"))
            result.extend(identity.namespace_for("artifact", artifact_identity=ref.identity())
                          for ref in artifacts)
        return list(dict.fromkeys(result))

    def stage_write(self, intent: MemoryWriteIntent, context: MemoryWriteContext) -> MemoryWritePlan:
        intent = MemoryWriteIntent.model_validate(intent)
        context = MemoryWriteContext.model_validate(context)
        try:
            check_memory_content(intent.content)
            check_memory_content(intent.key)
            for source in intent.source_refs:
                if source.url:
                    check_memory_content(source.url)
            try:
                namespace = context.identity.namespace_for(intent.scope, artifact_identity=(
                    intent.artifact_ref.identity() if intent.artifact_ref else None))
            except ValueError:
                raise MemoryPolicyError("memory_scope_unavailable") from None
            catalogue = {e.reference.model_dump_json(): e for e in context.evidence}
            evidence = []
            for source in intent.source_refs:
                item = catalogue.get(source.model_dump_json())
                if item is None:
                    raise MemoryPolicyError("memory_source_not_owned")
                try:
                    context.identity.validate_namespace(item.namespace)
                except ValueError:
                    raise MemoryPolicyError("memory_source_not_owned") from None
                # A source from another conversation cannot be promoted merely
                # by requesting user scope. Artifact grants stay host-owned.
                if (item.namespace.conversation_id is not None
                        and item.namespace.conversation_id != context.identity.conversation_id):
                    raise MemoryPolicyError("memory_source_not_owned")
                evidence.append(item)
            explicit = explicit_intent(context.user_text, context.thread_id)
            consent = (intent.write_basis == "explicit_request" and explicit is not None
                       and explicit == intent)
            if intent.write_basis == "explicit_request" and not consent:
                raise MemoryPolicyError("memory_explicit_consent_missing")
            active = consent and intent.kind in {MemoryKind.PREFERENCE, MemoryKind.RULE}
            verification = "user_confirmed" if active else "unverified"
            observed, expires = intent.observed_at, intent.expires_at
            if intent.kind == MemoryKind.FACT:
                observed = observed or context.at
                expires = expires or observed + timedelta(days=30)
                if re.search(r"天气|weather|实时|current", intent.content, re.I):
                    expires = min(expires, observed + timedelta(hours=1))
                if re.search(r"人口|population", intent.content, re.I):
                    if not re.search(r"(?:19|20)\d{2}", intent.content) or not re.search(
                            r"行政|都会|都市|市域|metro|municipal|city proper", intent.content, re.I):
                        raise MemoryPolicyError("memory_population_basis_missing")
                if observed > context.at or expires <= context.at:
                    raise MemoryPolicyError("memory_fact_time_invalid")
            memory_id = "mem-" + hashlib.sha256(json.dumps(
                [namespace.key(), intent.kind.value, intent.key], ensure_ascii=False,
            ).encode()).hexdigest()
            previous = self.repository.get(memory_id, namespace=namespace, identity=context.identity)
            if previous and previous.status in {MemoryStatus.DELETED, MemoryStatus.SUPERSEDED}:
                raise MemoryPolicyError("memory_terminal_record")
            # Unconfirmed extraction must not displace an active rule/fact.
            if previous and previous.status == MemoryStatus.ACTIVE and not active:
                return MemoryWritePlan(key=intent.key, status="conflict", reason="memory_confirmation_required")
            at = context.at.astimezone(timezone.utc)
            record = MemoryRecord(
                memory_id=memory_id, scope=intent.scope, kind=intent.kind, namespace=namespace,
                content=intent.content, source_refs=intent.source_refs, write_basis=intent.write_basis,
                verification_status=verification, status="active" if active else "candidate",
                observed_at=observed, expires_at=expires, created_at=previous.created_at if previous else at,
                updated_at=max(at, previous.updated_at) if previous else at,
                version=previous.version + 1 if previous else 1,
                supersedes=previous.version_ref() if previous else None,
                content_hash=memory_content_hash(intent.content), artifact_ref=intent.artifact_ref,
                idempotency_key=memory_idempotency_key(namespace, source_event_id=context.thread_id,
                                                      operation_version=1, candidate_key=intent.key),
            )
            return MemoryWritePlan(key=intent.key, status="ready", record=record)
        except MemoryPolicyError as error:
            return MemoryWritePlan(key=intent.key, status="rejected", reason=error.code)

    def stage_confirmation(self, context: MemoryWriteContext) -> MemoryWritePlan | None:
        """Confirm exactly displayed content, not a bare ID or model boolean."""
        match = _CONFIRM.fullmatch(context.user_text.strip())
        if not match:
            return None
        scope, memory_id, content = match.groups()
        check_memory_content(content)
        confirmation_source = MemorySourceRef(kind="user_message", thread_id=context.thread_id,
                                               message_id="user-task")
        owned_confirmation = next((e for e in context.evidence if e.reference == confirmation_source), None)
        if owned_confirmation is None:
            return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_source_not_owned")
        try:
            context.identity.validate_namespace(owned_confirmation.namespace)
        except ValueError:
            return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_source_not_owned")
        namespace = context.identity.namespace_for(_SCOPES[scope])
        previous = self.repository.get(memory_id, namespace=namespace, identity=context.identity)
        if (previous is None or previous.status != MemoryStatus.CANDIDATE
                or previous.content != content):
            return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_confirmation_mismatch")
        if previous.kind == MemoryKind.FACT:
            # Consent alone is not corroboration: host must independently
            # resolve a successful tool/artifact source before confirmation.
            supported = False
            for evidence in context.evidence:
                if (evidence.verified and evidence.reference in previous.source_refs
                        and evidence.reference.kind.value in {"tool_result", "artifact"}):
                    try:
                        context.identity.validate_namespace(evidence.namespace)
                    except ValueError:
                        return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_source_not_owned")
                    supported = True
            if not supported or previous.expires_at is None or previous.expires_at <= context.at:
                return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_fact_requires_verification")
        source = confirmation_source
        refs = tuple(dict.fromkeys((*previous.source_refs, source)))
        if len(refs) > 32:
            return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_source_capacity_exceeded")
        if context.at < previous.updated_at:
            return MemoryWritePlan(key="confirmation", status="rejected", reason="memory_confirmation_time_invalid")
        record = MemoryRecord.model_validate({
            **previous.model_dump(), "version": previous.version + 1,
            "supersedes": previous.version_ref(), "updated_at": context.at,
            "source_refs": refs, "status": "active", "verification_status": (
                "source_supported" if previous.kind == MemoryKind.FACT else "user_confirmed"),
            "write_basis": "explicit_request", "idempotency_key": memory_idempotency_key(
                namespace, source_event_id=context.thread_id, operation_version=1, candidate_key="confirmation"),
        })
        return MemoryWritePlan(key="confirmation", status="ready", record=record)

    def commit(self, plan: MemoryWritePlan, identity: MemoryIdentity) -> MemoryWriteResult:
        plan = MemoryWritePlan.model_validate(plan)
        if plan.status != "ready" or plan.record is None:
            return MemoryWriteResult(key=plan.key, status=plan.status if plan.status != "ready" else "rejected",
                                     reason=plan.reason or "memory_plan_invalid")
        try:
            check_memory_content(plan.record.content)
            for source in plan.record.source_refs:
                if source.url:
                    check_memory_content(source.url)
            record = self.repository.save(plan.record, identity=identity)
            return MemoryWriteResult(key=plan.key, status="saved" if record.status == MemoryStatus.ACTIVE else "candidate",
                                     reference=record.version_ref())
        except MemoryConflict:
            return MemoryWriteResult(key=plan.key, status="conflict", reason="memory_version_or_event_conflict")
        except MemoryStoreError:
            return MemoryWriteResult(key=plan.key, status="unavailable", reason="memory_store_unavailable")
        except MemoryPolicyError as error:
            return MemoryWriteResult(key=plan.key, status="rejected", reason=error.code)

    def retrieve(self, query: str, identity: MemoryIdentity, *, at: datetime,
                 required_ids: tuple[str, ...] = (), artifacts: tuple[ArtifactReference, ...] = ()) -> MemorySnapshot:
        if type(query) is not str or type(required_ids) is not tuple or len(required_ids) > self.top_k:
            raise MemoryPolicyError("memory_read_request_invalid")
        if len(set(required_ids)) != len(required_ids):
            raise MemoryPolicyError("memory_read_request_invalid")
        namespaces = self.namespaces(identity, artifacts)
        granted = lambda record: record.scope.value != "artifact" or record.artifact_ref in artifacts
        mandatory: dict[str, MemoryRecord] = {}
        for memory_id in required_ids:
            found = [self.repository.get(memory_id, namespace=namespace, identity=identity)
                     for namespace in namespaces]
            record = next((item for item in found if item is not None and item.is_usable_at(at) and granted(item)), None)
            if record is None:
                raise MemoryPolicyError("memory_required_unavailable")
            mandatory[memory_id] = record
        # All applicable rules are mandatory; never silently truncate constraints.
        for namespace in namespaces:
            rules = self.repository.query_active(namespace=namespace, identity=identity, at=at,
                                                 kinds=(MemoryKind.RULE,), limit=self.scan_limit + 1)
            if len(rules) > self.scan_limit:
                raise MemoryPolicyError("memory_rules_scan_exceeded")
            mandatory.update((item.memory_id, item) for item in rules if granted(item))
        items = [_excerpt(item, True) for item in mandatory.values()]
        payload_size = lambda values: estimate_tokens(json.dumps(
            [item.model_dump(mode="json") for item in values], ensure_ascii=False))
        if len(items) > self.top_k or payload_size(items) > self.max_tokens:
            raise MemoryPolicyError("memory_required_budget_exceeded")
        query_terms = _tokens(query[:8000])
        candidates, scanned, scan_limited = [], 0, False
        for namespace in namespaces:
            rows = self.repository.query_active(namespace=namespace, identity=identity, at=at,
                                                limit=self.scan_limit + 1)
            scan_limited |= len(rows) > self.scan_limit
            for record in rows[:self.scan_limit]:
                scanned += 1
                if record.memory_id in mandatory or not granted(record):
                    continue
                score = len(query_terms & _tokens(record.content))
                if record.kind == MemoryKind.PREFERENCE:
                    score += 1  # User/project presentation preferences are globally applicable.
                if score:
                    candidates.append((score, record))
        candidates.sort(key=lambda value: (-value[0], value[1].memory_id))
        seen = {(item.kind, item.content_hash) for item in items}
        omitted = 0
        for score, record in candidates:
            candidate = _excerpt(record, False)
            key = (candidate.kind, candidate.content_hash)
            if (key in seen or len(items) >= self.top_k
                    or payload_size([*items, candidate]) > self.max_tokens):
                omitted += 1
                continue
            items.append(candidate)
            seen.add(key)
        return MemorySnapshot(items=tuple(items), query_hash=memory_content_hash(query), selected_at=at,
                              scanned=scanned, matched=len(candidates) + len(mandatory), omitted=omitted,
                              scan_limited=scan_limited)

    def revalidate(self, snapshot: MemorySnapshot, identity: MemoryIdentity, *, at: datetime,
                   artifacts: tuple[ArtifactReference, ...] = ()) -> MemorySnapshot:
        snapshot = MemorySnapshot.model_validate(snapshot)
        allowed = self.namespaces(identity, artifacts)
        items, invalidated = [], snapshot.invalidated
        for item in snapshot.items:
            if item.namespace not in allowed:
                raise MemoryPolicyError("memory_snapshot_not_owned")
            record = self.repository.get(item.memory_id, namespace=item.namespace, identity=identity)
            valid = (record is not None and record.is_usable_at(at)
                     and (record.scope.value != "artifact" or record.artifact_ref in artifacts)
                     and _excerpt(record, item.required) == item)
            if not valid:
                if item.required:
                    raise MemoryPolicyError("memory_required_snapshot_invalid")
                invalidated += 1
            else:
                items.append(item)
        if len(items) > self.top_k or estimate_tokens(json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False)) > self.max_tokens:
            raise MemoryPolicyError("memory_snapshot_budget_exceeded")
        return MemorySnapshot.model_validate({**snapshot.model_dump(), "items": tuple(items), "invalidated": invalidated})
