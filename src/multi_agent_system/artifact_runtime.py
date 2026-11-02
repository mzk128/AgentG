"""Versioned deliverables and controlled, model-authored revision patches."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ArtifactRevisionError(ValueError):
    """A proposed artifact revision is stale, malformed or exceeds its scope."""


class RevisionInstruction(BaseModel):
    """Reviewer-owned contract for revising one existing deliverable."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    mode: Literal["patch", "replace", "regenerate"] = "replace"
    allowed_paths: list[str] = Field(default_factory=lambda: ["/"])
    requested_changes: list[str] = Field(default_factory=list)
    preserve_paths: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    base_version: int | None = Field(default=None, ge=1)
    base_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def normalise_boundary_aliases(cls, value: Any) -> Any:
        """Accept two common model aliases, then immediately restore canonical fields."""
        if not isinstance(value, dict):
            return value
        normalised = dict(value)
        for alias, canonical in (("target_id", "task_id"), ("action", "mode")):
            if alias not in normalised:
                continue
            if canonical in normalised and normalised[canonical] != normalised[alias]:
                raise ValueError(
                    f"conflicting revision fields: {canonical} and {alias}"
                )
            normalised[canonical] = normalised.pop(alias)
        return normalised

    @field_validator(
        "allowed_paths",
        "requested_changes",
        "preserve_paths",
        "evidence_requirements",
        mode="before",
    )
    @classmethod
    def normalise_string_lists(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("allowed_paths", "preserve_paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        normalised = list(dict.fromkeys(values))
        if any(not value.startswith("/") for value in normalised):
            raise ValueError("artifact paths must be JSON pointers beginning with /")
        return normalised

    @model_validator(mode="after")
    def validate_patch_scope(self) -> RevisionInstruction:
        if self.mode == "patch" and not self.allowed_paths:
            raise ValueError("patch revisions require at least one allowed path")
        if self.mode == "patch" and "/" in self.allowed_paths:
            raise ValueError("patch revisions must name narrower paths; use replace for the root")
        return self


class ArtifactPatchOperation(BaseModel):
    """Supported subset of JSON Patch plus an exact text replacement operation."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "replace", "remove", "replace_text"]
    path: str
    value: Any = None
    old_text: str | None = None
    new_text: str | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> ArtifactPatchOperation:
        if not self.path.startswith("/"):
            raise ValueError("patch path must be a JSON pointer beginning with /")
        if self.op != "replace_text" and self.path == "/":
            raise ValueError("structured patch operations cannot replace the root")
        if self.op == "replace_text":
            if self.path != "/text":
                raise ValueError("replace_text is only valid for /text")
            if not self.old_text or self.new_text is None:
                raise ValueError("replace_text requires nonempty old_text and new_text")
            if self.value is not None:
                raise ValueError("replace_text does not accept value")
        elif self.op in {"add", "replace"}:
            if "value" not in self.model_fields_set:
                raise ValueError(f"{self.op} requires an explicit value")
            if self.old_text is not None or self.new_text is not None:
                raise ValueError(f"{self.op} does not accept text replacement fields")
        elif self.value is not None or self.old_text is not None or self.new_text is not None:
            raise ValueError("remove does not accept value or text replacement fields")
        return self


class ArtifactPatch(BaseModel):
    """Patch envelope emitted by a Worker in patch revision mode."""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    base_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[ArtifactPatchOperation] = Field(min_length=1)
    change_summary: str = Field(min_length=1, max_length=1000)


class ArtifactRecord(BaseModel):
    """Immutable metadata for one version of a Worker deliverable."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    base_version: int | None = Field(default=None, ge=1)
    content_type: Literal["text", "json"]
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_paths: list[str] = Field(default_factory=list)
    change_summary: str = "Initial deliverable"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE)
    return match.group(1).strip() if match else stripped


def _parse_json_content(content: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(_strip_json_fence(content))
    except (TypeError, ValueError):
        return False, None


def create_artifact(
    artifact_id: str,
    content: str,
    *,
    version: int = 1,
    base_version: int | None = None,
    changed_paths: list[str] | None = None,
    change_summary: str = "Initial deliverable",
) -> ArtifactRecord:
    is_json, _ = _parse_json_content(content)
    return ArtifactRecord(
        artifact_id=artifact_id,
        version=version,
        base_version=base_version,
        content_type="json" if is_json else "text",
        content=content,
        content_hash=_content_hash(content),
        changed_paths=list(changed_paths or (["/"] if version > 1 else [])),
        change_summary=change_summary,
    )


def coerce_previous_artifact(deliverable: dict[str, Any]) -> ArtifactRecord:
    raw_artifact = deliverable.get("artifact")
    if isinstance(raw_artifact, dict):
        return ArtifactRecord.model_validate(raw_artifact)
    content = str(deliverable.get("content", ""))
    return create_artifact(
        str(deliverable.get("task_id") or "artifact"),
        content,
        version=max(1, int(deliverable.get("attempt", 1) or 1)),
    )


def bind_revision_base(
    instruction: RevisionInstruction, previous: ArtifactRecord
) -> RevisionInstruction:
    """Bind the model-authored instruction to the latest stored artifact version."""
    if instruction.task_id != previous.artifact_id:
        raise ArtifactRevisionError("revision instruction targets a different artifact")
    return instruction.model_copy(
        update={
            "base_version": previous.version,
            "base_content_hash": previous.content_hash,
        }
    )


def _decode_pointer(path: str) -> list[str]:
    if path == "/":
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _read_pointer(value: Any, path: str) -> Any:
    current = value
    for part in _decode_pointer(path):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ArtifactRevisionError(f"artifact path does not exist: {path}")
    return copy.deepcopy(current)


def _parent_pointer(value: Any, path: str) -> tuple[Any, str]:
    parts = _decode_pointer(path)
    if not parts:
        raise ArtifactRevisionError("the artifact root cannot be patched directly")
    current = value
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ArtifactRevisionError(f"artifact parent path does not exist: {path}")
    return current, parts[-1]


def _path_is_allowed(path: str, allowed_paths: list[str]) -> bool:
    return any(
        allowed == "/" or path == allowed or path.startswith(allowed.rstrip("/") + "/")
        for allowed in allowed_paths
    )


def _apply_json_operation(value: Any, operation: ArtifactPatchOperation) -> None:
    parent, key = _parent_pointer(value, operation.path)
    if isinstance(parent, dict):
        if operation.op in {"replace", "remove"} and key not in parent:
            raise ArtifactRevisionError(f"artifact path does not exist: {operation.path}")
        if operation.op == "remove":
            del parent[key]
        else:
            parent[key] = copy.deepcopy(operation.value)
        return
    if isinstance(parent, list):
        if operation.op == "add" and key == "-":
            parent.append(copy.deepcopy(operation.value))
            return
        if not key.isdigit():
            raise ArtifactRevisionError(f"invalid list index: {operation.path}")
        index = int(key)
        if operation.op == "add":
            if index > len(parent):
                raise ArtifactRevisionError(f"list index is out of range: {operation.path}")
            parent.insert(index, copy.deepcopy(operation.value))
        elif index >= len(parent):
            raise ArtifactRevisionError(f"list index is out of range: {operation.path}")
        elif operation.op == "remove":
            parent.pop(index)
        else:
            parent[index] = copy.deepcopy(operation.value)
        return
    raise ArtifactRevisionError(f"artifact path parent is not editable: {operation.path}")


def parse_artifact_patch(content: str) -> ArtifactPatch:
    try:
        payload = json.loads(_strip_json_fence(content))
    except (TypeError, ValueError) as exc:
        raise ArtifactRevisionError("patch response must be one JSON object") from exc
    try:
        return ArtifactPatch.model_validate(payload)
    except ValueError as exc:
        raise ArtifactRevisionError("patch response does not match ArtifactPatch") from exc


def apply_controlled_patch(
    previous: ArtifactRecord,
    patch: ArtifactPatch,
    instruction: RevisionInstruction,
) -> ArtifactRecord:
    """Apply a patch only when its base and every requested path are authorized."""
    if instruction.mode != "patch":
        raise ArtifactRevisionError("revision instruction is not in patch mode")
    if instruction.task_id != previous.artifact_id:
        raise ArtifactRevisionError("revision instruction targets a different artifact")
    if patch.base_version != previous.version or patch.base_content_hash != previous.content_hash:
        raise ArtifactRevisionError("patch base version or content hash is stale")
    if instruction.base_version not in {None, previous.version}:
        raise ArtifactRevisionError("revision instruction targets a stale version")
    if instruction.base_content_hash not in {None, previous.content_hash}:
        raise ArtifactRevisionError("revision instruction targets stale content")
    if any(not _path_is_allowed(item.path, instruction.allowed_paths) for item in patch.operations):
        raise ArtifactRevisionError("patch contains a path outside the reviewer-approved scope")

    is_json, original_value = _parse_json_content(previous.content)
    original_for_preserve = copy.deepcopy(original_value if is_json else previous.content)
    changed_paths: list[str] = []

    if is_json:
        revised_value = copy.deepcopy(original_value)
        for operation in patch.operations:
            if operation.op == "replace_text":
                raise ArtifactRevisionError("replace_text cannot edit a JSON artifact")
            _apply_json_operation(revised_value, operation)
            changed_paths.append(operation.path)
        for path in instruction.preserve_paths:
            if _read_pointer(original_for_preserve, path) != _read_pointer(revised_value, path):
                raise ArtifactRevisionError(f"patch changed a preserved path: {path}")
        revised_content = json.dumps(revised_value, ensure_ascii=False, indent=2)
    else:
        revised_content = previous.content
        for operation in patch.operations:
            if operation.op != "replace_text":
                raise ArtifactRevisionError("text artifacts only accept replace_text operations")
            count = revised_content.count(operation.old_text or "")
            if count != 1:
                raise ArtifactRevisionError("replace_text old_text must match exactly once")
            revised_content = revised_content.replace(
                operation.old_text or "", operation.new_text or "", 1
            )
            changed_paths.append(operation.path)
        if instruction.preserve_paths:
            raise ArtifactRevisionError("text artifacts cannot verify structured preserve_paths")

    if revised_content == previous.content:
        raise ArtifactRevisionError("patch did not change the artifact")
    return create_artifact(
        previous.artifact_id,
        revised_content,
        version=previous.version + 1,
        base_version=previous.version,
        changed_paths=list(dict.fromkeys(changed_paths)),
        change_summary=patch.change_summary,
    )


def create_replacement_artifact(
    previous: ArtifactRecord,
    content: str,
    instruction: RevisionInstruction,
) -> ArtifactRecord:
    """Create a new full version while enforcing declared JSON preserve paths."""
    if instruction.mode not in {"replace", "regenerate"}:
        raise ArtifactRevisionError("replacement requires replace or regenerate mode")
    if instruction.task_id != previous.artifact_id:
        raise ArtifactRevisionError("revision instruction targets a different artifact")
    if instruction.base_version not in {None, previous.version}:
        raise ArtifactRevisionError("revision instruction targets a stale version")
    if instruction.base_content_hash not in {None, previous.content_hash}:
        raise ArtifactRevisionError("revision instruction targets stale content")
    old_is_json, old_value = _parse_json_content(previous.content)
    new_is_json, new_value = _parse_json_content(content)
    if instruction.preserve_paths:
        if not old_is_json or not new_is_json:
            raise ArtifactRevisionError("preserve_paths require JSON deliverables")
        for path in instruction.preserve_paths:
            if _read_pointer(old_value, path) != _read_pointer(new_value, path):
                raise ArtifactRevisionError(f"replacement changed a preserved path: {path}")
    return create_artifact(
        previous.artifact_id,
        content,
        version=previous.version + 1,
        base_version=previous.version,
        changed_paths=["/"],
        change_summary=(instruction.requested_changes[0] if instruction.requested_changes else "Full replacement"),
    )
