import json

import pytest

from multi_agent_system.artifact_runtime import (
    ArtifactPatch,
    ArtifactRevisionError,
    RevisionInstruction,
    apply_controlled_patch,
    bind_revision_base,
    create_artifact,
    create_replacement_artifact,
)


def test_json_patch_creates_a_new_version_and_preserves_protected_fields():
    previous = create_artifact(
        "weather",
        json.dumps({"city": "Paris", "temperature": 18, "humidity": None}),
    )
    instruction = bind_revision_base(
        RevisionInstruction(
            task_id="weather",
            mode="patch",
            allowed_paths=["/humidity"],
            preserve_paths=["/city", "/temperature"],
            requested_changes=["fill humidity"],
        ),
        previous,
    )
    patch = ArtifactPatch(
        base_version=previous.version,
        base_content_hash=previous.content_hash,
        operations=[{"op": "replace", "path": "/humidity", "value": 72}],
        change_summary="Added verified humidity",
    )

    revised = apply_controlled_patch(previous, patch, instruction)

    assert revised.version == 2
    assert revised.base_version == 1
    assert revised.changed_paths == ["/humidity"]
    assert json.loads(revised.content) == {
        "city": "Paris", "temperature": 18, "humidity": 72,
    }
    assert json.loads(previous.content)["humidity"] is None


def test_patch_rejects_stale_base_and_paths_outside_scope():
    previous = create_artifact("weather", '{"city":"Paris","temperature":18}')
    instruction = bind_revision_base(
        RevisionInstruction(
            task_id="weather", mode="patch", allowed_paths=["/temperature"]
        ),
        previous,
    )
    outside = ArtifactPatch(
        base_version=1,
        base_content_hash=previous.content_hash,
        operations=[{"op": "replace", "path": "/city", "value": "Lyon"}],
        change_summary="Wrong field",
    )
    stale = outside.model_copy(
        update={
            "base_content_hash": "0" * 64,
            "operations": [
                outside.operations[0].model_copy(
                    update={"path": "/temperature", "value": 20}
                )
            ],
        }
    )

    with pytest.raises(ArtifactRevisionError, match="outside"):
        apply_controlled_patch(previous, outside, instruction)
    with pytest.raises(ArtifactRevisionError, match="stale"):
        apply_controlled_patch(previous, stale, instruction)


def test_text_patch_requires_one_exact_match():
    previous = create_artifact("report", "Paris is 18 C. Tokyo is 26 C.")
    instruction = bind_revision_base(
        RevisionInstruction(
            task_id="report", mode="patch", allowed_paths=["/text"]
        ),
        previous,
    )
    patch = ArtifactPatch(
        base_version=1,
        base_content_hash=previous.content_hash,
        operations=[{
            "op": "replace_text",
            "path": "/text",
            "old_text": "Paris is 18 C.",
            "new_text": "Paris is 19 C.",
        }],
        change_summary="Updated only Paris",
    )

    revised = apply_controlled_patch(previous, patch, instruction)

    assert revised.content == "Paris is 19 C. Tokyo is 26 C."
    assert previous.content == "Paris is 18 C. Tokyo is 26 C."


def test_full_replacement_enforces_json_preserve_paths():
    previous = create_artifact("data", '{"id":"fixed","value":1}')
    instruction = bind_revision_base(
        RevisionInstruction(
            task_id="data", mode="replace", preserve_paths=["/id"]
        ),
        previous,
    )

    revised = create_replacement_artifact(
        previous, '{"id":"fixed","value":2}', instruction
    )
    assert revised.version == 2

    with pytest.raises(ArtifactRevisionError, match="preserved"):
        create_replacement_artifact(
            previous, '{"id":"changed","value":2}', instruction
        )


def test_full_replacement_rejects_stale_or_different_artifact_contracts():
    previous = create_artifact("data", '{"value":1}')
    stale = RevisionInstruction(
        task_id="data",
        mode="replace",
        base_version=2,
        base_content_hash=previous.content_hash,
    )
    wrong_task = RevisionInstruction(task_id="other", mode="replace")

    with pytest.raises(ArtifactRevisionError, match="stale version"):
        create_replacement_artifact(previous, '{"value":2}', stale)
    with pytest.raises(ArtifactRevisionError, match="different artifact"):
        create_replacement_artifact(previous, '{"value":2}', wrong_task)


def test_structured_patch_add_and_replace_require_explicit_values():
    with pytest.raises(ValueError, match="explicit value"):
        ArtifactPatch(
            base_version=1,
            base_content_hash="0" * 64,
            operations=[{"op": "replace", "path": "/value"}],
            change_summary="Missing value",
        )

    patch = ArtifactPatch(
        base_version=1,
        base_content_hash="0" * 64,
        operations=[{"op": "replace", "path": "/value", "value": None}],
        change_summary="Explicitly clear value",
    )
    assert patch.operations[0].value is None


def test_patch_mode_cannot_authorize_the_entire_root():
    with pytest.raises(ValueError, match="narrower paths"):
        RevisionInstruction(task_id="data", mode="patch", allowed_paths=["/"])


def test_revision_instruction_normalises_exact_boundary_aliases():
    instruction = RevisionInstruction.model_validate(
        {
            "target_id": "weather-2",
            "action": "regenerate",
            "requested_changes": "重新查询完整天气",
        }
    )

    assert instruction.task_id == "weather-2"
    assert instruction.mode == "regenerate"
    assert instruction.requested_changes == ["重新查询完整天气"]
    assert "target_id" not in instruction.model_dump()
    assert "action" not in instruction.model_dump()


def test_revision_instruction_rejects_conflicting_boundary_aliases():
    with pytest.raises(ValueError, match="conflicting revision fields"):
        RevisionInstruction.model_validate(
            {
                "task_id": "weather-2",
                "target_id": "weather-3",
                "mode": "replace",
            }
        )
