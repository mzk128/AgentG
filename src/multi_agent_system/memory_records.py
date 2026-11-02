"""Strict, storage-independent contracts for governed long-term memory.

No database, model, filesystem or configuration is accessed here. Namespace
construction is a host responsibility: these contracts check consistency, not
authentication, source ownership, factual truth or permission to read an artifact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


LOCAL_USER_ID = "local-user"
MAX_MEMORY_CONTENT_CHARS = 8_000
MAX_MEMORY_SOURCES = 32

Identifier = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=128,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
)]
ContentHash = Annotated[str, StringConstraints(
    strict=True, pattern=r"^[0-9a-f]{64}$",
)]
PositiveVersion = Annotated[int, Field(strict=True, ge=1)]


class MemoryScope(str, Enum):
    USER = "user"
    PROJECT = "project"
    CONVERSATION = "conversation"
    ARTIFACT = "artifact"


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    EXPERIENCE = "experience"
    RULE = "rule"


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemoryWriteBasis(str, Enum):
    EXPLICIT_REQUEST = "explicit_request"
    TASK_EXTRACTION = "task_extraction"


class MemoryVerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    USER_CONFIRMED = "user_confirmed"
    SOURCE_SUPPORTED = "source_supported"


class MemorySourceKind(str, Enum):
    USER_MESSAGE = "user_message"
    TOOL_RESULT = "tool_result"
    DELIVERABLE = "deliverable"
    ARTIFACT = "artifact"
    EXTERNAL_URL = "external_url"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_value(enum_type: type[_EnumT], value: Any) -> _EnumT:
    """Accept only enum members or their exact canonical strings, never aliases."""
    if isinstance(value, enum_type):
        return value
    if type(value) is str:
        return enum_type(value)
    raise ValueError(f"expected a canonical {enum_type.__name__} value")


ScopeValue = Annotated[MemoryScope, BeforeValidator(lambda v: _enum_value(MemoryScope, v))]
KindValue = Annotated[MemoryKind, BeforeValidator(lambda v: _enum_value(MemoryKind, v))]
StatusValue = Annotated[MemoryStatus, BeforeValidator(lambda v: _enum_value(MemoryStatus, v))]
WriteBasisValue = Annotated[MemoryWriteBasis, BeforeValidator(
    lambda v: _enum_value(MemoryWriteBasis, v)
)]
VerificationValue = Annotated[MemoryVerificationStatus, BeforeValidator(
    lambda v: _enum_value(MemoryVerificationStatus, v)
)]
SourceKindValue = Annotated[MemorySourceKind, BeforeValidator(
    lambda v: _enum_value(MemorySourceKind, v)
)]


class _MemoryContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, validate_default=True,
        revalidate_instances="always",
    )


class ArtifactIdentity(_MemoryContract):
    """An artifact's run-qualified identity, stable across content versions."""

    thread_id: Identifier
    task_id: Identifier
    artifact_id: Identifier


class ArtifactReference(ArtifactIdentity):
    version: PositiveVersion
    content_hash: ContentHash

    def identity(self) -> ArtifactIdentity:
        return ArtifactIdentity(
            thread_id=self.thread_id, task_id=self.task_id,
            artifact_id=self.artifact_id,
        )


class MemoryNamespace(_MemoryContract):
    """Canonical ownership coordinates; labels and filesystem paths are not IDs."""

    scope: ScopeValue
    user_id: Identifier
    project_id: Identifier | None = None
    conversation_id: Identifier | None = None
    artifact_identity: ArtifactIdentity | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> MemoryNamespace:
        if self.scope == MemoryScope.USER:
            if any(value is not None for value in (
                self.project_id, self.conversation_id, self.artifact_identity,
            )):
                raise ValueError("user namespace must contain only user_id")
        elif self.scope == MemoryScope.PROJECT:
            if self.project_id is None:
                raise ValueError("project namespace requires project_id")
            if self.conversation_id is not None or self.artifact_identity is not None:
                raise ValueError("project namespace cannot contain conversation or artifact IDs")
        elif self.scope == MemoryScope.CONVERSATION:
            if self.conversation_id is None:
                raise ValueError("conversation namespace requires conversation_id")
            if self.artifact_identity is not None:
                raise ValueError("conversation namespace cannot contain an artifact identity")
        elif self.conversation_id is None or self.artifact_identity is None:
            raise ValueError("artifact namespace requires conversation_id and artifact_identity")
        return self

    def key(self) -> tuple[str, ...]:
        """Unambiguous, deterministic components suitable for later store indexes."""
        parts = ["agentg-memory", "v1", self.scope.value, "user", self.user_id]
        if self.project_id is not None:
            parts.extend(["project", self.project_id])
        if self.conversation_id is not None:
            parts.extend(["conversation", self.conversation_id])
        if self.artifact_identity is not None:
            artifact = self.artifact_identity
            parts.extend([
                "thread", artifact.thread_id, "task", artifact.task_id,
                "artifact", artifact.artifact_id,
            ])
        return tuple(parts)


class MemoryIdentity(_MemoryContract):
    """Host-supplied binding context, not an authentication/registration service.

    The explicit local default is for trusted single-user development only.
    A registered project ID is supplied by the host, never derived from a title
    or arbitrary workspace path. No existing CLI/Web identity is changed here.
    """

    user_id: Identifier = LOCAL_USER_ID
    project_id: Identifier | None = None
    conversation_id: Identifier | None = None

    def namespace_for(
        self, scope: MemoryScope | str, *, artifact_identity: ArtifactIdentity | None = None,
    ) -> MemoryNamespace:
        scope = _enum_value(MemoryScope, scope)
        if scope != MemoryScope.ARTIFACT and artifact_identity is not None:
            raise ValueError("artifact identity is only valid for artifact scope")
        return MemoryNamespace(
            scope=scope,
            user_id=self.user_id,
            project_id=self.project_id if scope != MemoryScope.USER else None,
            conversation_id=(
                self.conversation_id
                if scope in {MemoryScope.CONVERSATION, MemoryScope.ARTIFACT} else None
            ),
            artifact_identity=artifact_identity,
        )

    def validate_namespace(self, namespace: MemoryNamespace) -> MemoryNamespace:
        """Reject foreign supplied coordinates; artifact ACLs remain host-owned."""
        namespace = MemoryNamespace.model_validate(namespace)
        expected = self.namespace_for(
            namespace.scope, artifact_identity=namespace.artifact_identity,
        )
        if namespace != expected:
            raise ValueError("memory namespace does not match the host identity")
        return namespace


class MemorySourceRef(_MemoryContract):
    """One evidence locator; different kinds have exact required field sets."""

    kind: SourceKindValue
    thread_id: Identifier | None = None
    message_id: Identifier | None = None
    task_id: Identifier | None = None
    tool_call_id: Identifier | None = None
    tool_name: Identifier | None = None
    artifact_ref: ArtifactReference | None = None

    url: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)] | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source URL cannot contain whitespace or control characters")
        if "\\" in value:
            raise ValueError("source URL cannot contain backslashes")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source URL must be an absolute http/https URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source URL cannot contain credentials")
        _ = parsed.port  # Reject malformed/non-numeric/out-of-range ports.
        return value

    @model_validator(mode="after")
    def validate_source_shape(self) -> MemorySourceRef:
        required = {
            MemorySourceKind.USER_MESSAGE: {"thread_id", "message_id"},
            MemorySourceKind.TOOL_RESULT: {"thread_id", "task_id", "tool_call_id", "tool_name"},
            MemorySourceKind.DELIVERABLE: {"thread_id", "task_id"},
            MemorySourceKind.ARTIFACT: {"artifact_ref"},
            MemorySourceKind.EXTERNAL_URL: {"url"},
        }[self.kind]
        present = {
            name for name in type(self).model_fields
            if name != "kind" and getattr(self, name) is not None
        }
        if present != required:
            raise ValueError(f"{self.kind.value} source requires exactly {sorted(required)}")
        return self


class MemoryVersionRef(_MemoryContract):
    memory_id: Identifier
    version: PositiveVersion
    content_hash: ContentHash


def memory_content_hash(content: str) -> str:
    """Hash exact UTF-8 content without whitespace or Unicode normalization."""
    if type(content) is not str:
        raise ValueError("memory content must be a string")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def memory_idempotency_key(
    namespace: MemoryNamespace, *, source_event_id: str,
    operation_version: int, candidate_key: str,
) -> str:
    """Identify a host write event, not all equal content or tool parameters.

    The future repository must enforce uniqueness and detect conflicting
    payloads for the same key. Merely generating this key performs no write.
    """
    namespace = MemoryNamespace.model_validate(namespace)
    event = _WriteEvent(
        source_event_id=source_event_id, operation_version=operation_version,
        candidate_key=candidate_key,
    )
    payload = json.dumps(
        [namespace.key(), event.model_dump()], sort_keys=True,
        ensure_ascii=False, separators=(",", ":"),
    )
    return "memop_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _WriteEvent(_MemoryContract):
    source_event_id: Identifier
    operation_version: PositiveVersion
    candidate_key: Identifier


class MemoryRecord(_MemoryContract):
    """One immutable version of a memory, independent of SQLite/checkpoints.

    Canonical strings are accepted for enums; other coercions are disabled.
    Python callers supply aware datetimes; JSON callers use model_validate_json.
    Source locators and verification labels are claims the host must verify.
    """

    memory_id: Identifier = Field(default_factory=lambda: str(uuid4()))
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    scope: ScopeValue
    kind: KindValue
    namespace: MemoryNamespace
    content: Annotated[str, StringConstraints(
        strict=True, min_length=1, max_length=MAX_MEMORY_CONTENT_CHARS,
    )]
    source_refs: Annotated[tuple[MemorySourceRef, ...], Field(
        min_length=1, max_length=MAX_MEMORY_SOURCES,
    )]
    write_basis: WriteBasisValue
    verification_status: VerificationValue = MemoryVerificationStatus.UNVERIFIED
    created_at: AwareDatetime
    updated_at: AwareDatetime
    observed_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    version: PositiveVersion = 1
    supersedes: MemoryVersionRef | None = None
    status: StatusValue = MemoryStatus.CANDIDATE
    content_hash: ContentHash
    idempotency_key: Identifier
    artifact_ref: ArtifactReference | None = None
    # Additive metadata: old v1 JSON remains readable. Public audit note only.
    revision_note: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)] | None = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory content cannot be blank")
        return value

    @field_validator("revision_note")
    @classmethod
    def validate_revision_note(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("revision note cannot be blank")
        return value

    @field_validator("source_refs", mode="before")
    @classmethod
    def freeze_sources(cls, value: Any) -> Any:
        # Exact JSON arrays / Python lists become immutable tuples, not a
        # string-to-list or arbitrary iterable coercion.
        return tuple(value) if type(value) is list else value

    @field_validator("created_at", "updated_at", "observed_at", "expires_at")
    @classmethod
    def normalise_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_record(self) -> MemoryRecord:
        if self.scope != self.namespace.scope:
            raise ValueError("record scope must match namespace scope")
        if self.content_hash != memory_content_hash(self.content):
            raise ValueError("content_hash does not match exact memory content")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.observed_at is not None and self.observed_at > self.updated_at:
            raise ValueError("observed_at cannot follow updated_at")
        if self.expires_at is not None and self.expires_at <= (self.observed_at or self.created_at):
            raise ValueError("expires_at must follow observed_at or created_at")
        if self.version == 1 and self.supersedes is not None:
            raise ValueError("initial version cannot supersede another version")
        if self.version > 1:
            if self.supersedes is None:
                raise ValueError("later versions require a supersedes reference")
            if (self.supersedes.memory_id != self.memory_id
                    or self.supersedes.version != self.version - 1):
                raise ValueError("supersedes must identify the immediately preceding memory version")
        if len({source.model_dump_json() for source in self.source_refs}) != len(self.source_refs):
            raise ValueError("duplicate source references are not allowed")

        source_kinds = {source.kind for source in self.source_refs}
        if self.write_basis == MemoryWriteBasis.EXPLICIT_REQUEST:
            if MemorySourceKind.USER_MESSAGE not in source_kinds:
                raise ValueError("explicit_request requires a user_message source")
        if self.verification_status == MemoryVerificationStatus.USER_CONFIRMED:
            if MemorySourceKind.USER_MESSAGE not in source_kinds:
                raise ValueError("user_confirmed requires a user_message source")
        if self.verification_status == MemoryVerificationStatus.SOURCE_SUPPORTED:
            if not source_kinds & {
                MemorySourceKind.TOOL_RESULT, MemorySourceKind.ARTIFACT,
                MemorySourceKind.EXTERNAL_URL,
            }:
                raise ValueError("source_supported requires tool, artifact or external evidence")
        if self.status == MemoryStatus.ACTIVE:
            if self.verification_status == MemoryVerificationStatus.UNVERIFIED:
                raise ValueError("active memory cannot be unverified")
            if self.kind in {MemoryKind.PREFERENCE, MemoryKind.RULE}:
                if self.verification_status != MemoryVerificationStatus.USER_CONFIRMED:
                    raise ValueError("active preferences and rules require user confirmation")

        if self.scope == MemoryScope.ARTIFACT:
            if self.artifact_ref is None:
                raise ValueError("artifact memory requires a versioned artifact_ref")
            if self.artifact_ref.identity() != self.namespace.artifact_identity:
                raise ValueError("artifact_ref does not match namespace artifact identity")
        if self.artifact_ref is not None and not any(
            source.kind == MemorySourceKind.ARTIFACT
            and source.artifact_ref == self.artifact_ref for source in self.source_refs
        ):
            raise ValueError("artifact_ref requires a matching versioned artifact source")
        return self

    def version_ref(self) -> MemoryVersionRef:
        return MemoryVersionRef(
            memory_id=self.memory_id, version=self.version,
            content_hash=self.content_hash,
        )

    def is_usable_at(self, now: datetime) -> bool:
        """Local lifecycle/time predicate only; does not grant read permission."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
        return (
            self.status == MemoryStatus.ACTIVE
            and self.verification_status != MemoryVerificationStatus.UNVERIFIED
            and self.created_at <= now
            and (self.observed_at is None or self.observed_at <= now)
            and (self.expires_at is None or now < self.expires_at)
        )


def validate_memory_successor(previous: MemoryRecord, successor: MemoryRecord) -> None:
    """Check a revision against its actual predecessor before a future store write.

    This checks pairwise integrity, not database existence, uniqueness or a
    transactional version race. Those checks belong to the future repository.
    """
    previous = MemoryRecord.model_validate(previous)
    successor = MemoryRecord.model_validate(successor)
    if (
        previous.memory_id != successor.memory_id
        or previous.namespace != successor.namespace
        or previous.scope != successor.scope
        or previous.kind != successor.kind
    ):
        raise ValueError("memory revision cannot change identity, namespace, scope or kind")
    if successor.supersedes != previous.version_ref() or successor.version != previous.version + 1:
        raise ValueError("memory revision has a stale predecessor version or hash")
    if successor.created_at != previous.created_at or successor.updated_at < previous.updated_at:
        raise ValueError("memory revision cannot reset creation time or move update time backwards")
    if previous.status in {MemoryStatus.SUPERSEDED, MemoryStatus.DELETED}:
        raise ValueError("superseded or deleted memory cannot be revised as the current version")
