"""Host-only LangGraph integration. No database handles enter Send payloads."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from langchain_core.messages import AIMessage

from .memory_records import ArtifactReference, MemoryIdentity, MemorySourceRef
from .memory_service import (
    MemoryEvidence, MemoryPolicyError, MemoryService, MemorySnapshot,
    MemoryWriteContext, MemoryWriteIntent, MemoryWritePlan, explicit_intent,
)
from .memory_store import MemoryStoreError, create_memory_repository
from .memory_paths import configured_memory_roots, protected_memory_roots
from .memory_guard import worker_memory_guard


def parse_contract(cls, value):
    return cls.model_validate_json(json.dumps(value, ensure_ascii=False))


def memory_blocked(source: str, reason: str) -> dict:
    feedback = f"长期记忆无法安全使用：{reason}；请核查记忆状态或配置。"
    return {
        "termination": {"source": source, "reason": reason},
        "review_status": "blocked", "review": feedback, "is_pass": False,
        "review_decision": {"status": "blocked", "feedback": feedback, "failed_criteria": [reason],
                            "revision_targets": [], "revision_instructions": [], "confidence": 1.0},
        "revision_targets": [], "revision_instructions": [],
        "messages": [AIMessage(content=f"[Memory] {feedback}")],
    }


class GraphMemoryRuntime:
    def __init__(self, settings, service: MemoryService | None = None):
        self.settings = settings
        self.service = service
        self.store_error = False
        # Validate host configuration even if SQLite will be unavailable.
        self.host = (MemoryIdentity(user_id=settings.memory_user_id,
                                    project_id=settings.memory_project_id or None)
                     if settings.long_term_memory_enabled or service is not None else MemoryIdentity())
        if self.service is None and settings.long_term_memory_enabled:
            try:
                repository = create_memory_repository(settings)
                self.service = MemoryService(repository, top_k=settings.memory_top_k,
                                             max_tokens=settings.memory_max_tokens,
                                             scan_limit=settings.memory_scan_limit)
            except MemoryStoreError:
                self.store_error = True

    def identity(self, state: dict) -> MemoryIdentity:
        identity = MemoryIdentity.model_validate(state["memory_binding"])
        if (identity.user_id, identity.project_id) != (self.host.user_id, self.host.project_id):
            raise MemoryPolicyError("memory_host_binding_changed")
        return identity

    def policy_service(self, state: dict) -> MemoryService:
        """Recovery may tighten current limits, never expand the original policy."""
        policy = state.get("memory_policy", {})
        return MemoryService(self.service.repository,
            top_k=min(self.service.top_k, int(policy.get("long_term_top_k", self.service.top_k))),
            max_tokens=min(self.service.max_tokens, int(policy.get("long_term_max_tokens", self.service.max_tokens))),
            scan_limit=min(self.service.scan_limit, int(policy.get("long_term_scan_limit", self.service.scan_limit))))

    @staticmethod
    def context(state: dict, identity: MemoryIdentity) -> MemoryWriteContext:
        source = MemorySourceRef(kind="user_message", thread_id=state["metadata"]["thread_id"],
                                 message_id="user-task")
        return MemoryWriteContext(
            identity=identity, thread_id=state["metadata"]["thread_id"],
            at=datetime.fromisoformat(state["memory_event_at"]), user_text=state["task"],
            evidence=(MemoryEvidence(reference=source, namespace=identity.namespace_for("conversation")),),
        )

    def prepare(self, state: dict) -> dict:
        now = datetime.now(timezone.utc)
        metadata = state.get("metadata", {})
        identity = MemoryIdentity(user_id=self.host.user_id, project_id=self.host.project_id,
                                  conversation_id=metadata.get("conversation_id") or metadata["thread_id"])
        update = {"memory_binding": identity.model_dump(mode="json"), "memory_event_at": now.isoformat(),
                  "memory_notices": [], "memory_write_plans": [], "memory_write_results": []}
        if self.service and getattr(self.service.repository, "db_path", None):
            update["memory_policy"] = {**state.get("memory_policy", {}), "python_denied_paths": protected_memory_roots(
                self.service.repository.db_path)}
        local = {**state, **update}
        if state.get("termination"):
            return update
        required = tuple(state.get("memory_required_ids", []))
        if self.service is None:
            if required:
                update.update(memory_blocked("memory_read", "memory_store_unavailable"))
            else:
                update["memory_notices"] = ["memory_store_unavailable"]
            return update
        try:
            snapshot = self.policy_service(state).retrieve(state["task"], identity, at=now, required_ids=required)
            update["retrieved_memories"] = snapshot.model_dump(mode="json")
            context = self.context(local, identity)
            intent = explicit_intent(state["task"], metadata["thread_id"])
            confirmation = self.service.stage_confirmation(context)
            plans = [self.service.stage_write(intent, context)] if intent else []
            if confirmation:
                plans.append(confirmation)
            update["memory_write_plans"] = [plan.model_dump(mode="json") for plan in plans]
            update["messages"] = [AIMessage(content=f"[Memory] 已选取 {len(snapshot.items)} 条历史记忆；写入请求 {len(plans)} 条。")]
        except MemoryPolicyError as error:
            update.update(memory_blocked("memory_read", error.code))
        except MemoryStoreError:
            if required:
                update.update(memory_blocked("memory_read", "memory_store_unavailable"))
            else:
                update["memory_notices"] = ["memory_store_unavailable"]
        except ValueError:
            update.update(memory_blocked("memory_write", "memory_write_request_invalid"))
        return update

    def _protected_state(self, state: dict) -> dict:
        local = dict(state)
        policy = dict(state.get("memory_policy", {}))
        roots = [*policy.get("python_denied_paths", []), *configured_memory_roots(self.settings)]
        if self.service and getattr(self.service.repository, "db_path", None):
            roots.extend(protected_memory_roots(self.service.repository.db_path))
        policy["python_denied_paths"] = list(dict.fromkeys(roots))
        local["memory_policy"] = policy  # Also protect old pending Send payloads.
        return local

    def _validate(self, state: dict) -> dict:
        local = self._protected_state(state)
        if not state.get("memory_binding"):
            # Old checkpoints are not silently opted into new cross-task memory.
            local["retrieved_memories"] = {}
            return local
        if not state.get("retrieved_memories"):
            if self.service is not None:
                self.identity(state)
            return local
        snapshot = parse_contract(MemorySnapshot, state["retrieved_memories"])
        if self.service is None:
            if any(item.required for item in snapshot.items):
                raise MemoryPolicyError("memory_store_unavailable")
            local["retrieved_memories"] = {}
            local["memory_notices"] = ["memory_store_unavailable"]
        else:
            identity = self.identity(state)
            grants = tuple(ArtifactReference.model_validate(ref) for ref in state.get("memory_artifact_grants", []))
            checked = self.policy_service(state).revalidate(snapshot, identity, at=datetime.now(timezone.utc), artifacts=grants)
            local["retrieved_memories"] = checked.model_dump(mode="json")
        return local

    def wrap(self, function, stage: str, *, worker: bool = False):
        def node(state):
            if state.get("termination") and not worker:
                return {}
            try:
                local = self._validate(state)
            except MemoryStoreError:
                snapshot = parse_contract(MemorySnapshot, state["retrieved_memories"]) if state.get("retrieved_memories") else None
                if snapshot and any(item.required for item in snapshot.items):
                    return memory_blocked(stage, "memory_store_unavailable") if not worker else {
                        "worker_results": [{"task_id": state["task_id"], "agent_type": state["agent_type"],
                        "attempt": state.get("attempt", 1), "status": "terminated", "content": "",
                        "termination_reason": "memory_store_unavailable", "tool_observations": []}]}
                local = self._protected_state({**state, "retrieved_memories": {}, "memory_notices": ["memory_store_unavailable"]})
            except (MemoryPolicyError, ValueError) as error:
                reason = error.code if isinstance(error, MemoryPolicyError) else "memory_validation_unavailable"
                if worker:
                    return {"worker_results": [{"task_id": state["task_id"], "agent_type": state["agent_type"],
                        "attempt": state.get("attempt", 1), "status": "terminated", "content": "",
                        "termination_reason": reason, "tool_observations": []}],
                        "worker_attempts": {state["task_id"]: state.get("attempt", 1)}}
                return memory_blocked(stage, reason)
            def refresh_worker(payload):
                try:
                    checked = self._validate(payload)
                except MemoryStoreError:
                    snapshot = payload.get("retrieved_memories", {})
                    if any(item.get("required") for item in snapshot.get("items", [])):
                        raise MemoryPolicyError("memory_store_unavailable") from None
                    checked = {"retrieved_memories": {}, "memory_notices": ["memory_store_unavailable"]}
                payload["retrieved_memories"] = checked.get("retrieved_memories", {})
                if checked.get("memory_notices"):
                    payload["memory_notices"] = checked["memory_notices"]

            with worker_memory_guard(refresh_worker if worker else None):
                result = function(local)
            if worker:
                for item in result.get("worker_results", []):
                    item["memory_snapshot"] = local.get("retrieved_memories", {})
                    if local.get("memory_notices"):
                        item["memory_notices"] = local["memory_notices"]
            if not worker:
                result["memory_policy"] = local["memory_policy"]
                result["retrieved_memories"] = local.get("retrieved_memories", {})
                if local.get("memory_notices"):
                    result["memory_notices"] = local["memory_notices"]
            return result
        return node

    def prepare_batch(self, function, state: dict) -> dict:
        result = function(state)
        if result.get("termination") or not state.get("memory_binding") or self.service is None:
            return result
        snapshots = dict(state.get("worker_memories", {}))
        grant_map = dict(state.get("worker_memory_grants", {}))
        latest = {item["task_id"]: item for item in sorted(state.get("worker_results", []), key=lambda item: item.get("attempt", 0))}
        try:
            identity = self.identity(state)
            service = self.policy_service(state)
            for spec in state.get("subtasks", []):
                if spec["id"] not in result.get("current_batch_ids", []):
                    continue
                key = f"{spec['id']}:{int(state.get('worker_attempts', {}).get(spec['id'], 0)) + 1}"
                # Grant only own previous artifact and direct upstream artifacts,
                # never every result on the global blackboard.
                grants = []
                for task_id in [spec["id"], *spec.get("dependencies", [])]:
                    result_item = latest.get(task_id, {})
                    artifact = result_item.get("artifact", {})
                    if artifact:
                        try:
                            grants.append(ArtifactReference(
                                thread_id=state["metadata"]["thread_id"], task_id=task_id,
                                artifact_id=artifact["artifact_id"], version=artifact["version"],
                                content_hash=artifact["content_hash"]))
                        except (KeyError, ValueError):
                            result["memory_notices"] = ["memory_artifact_reference_invalid"]
                grant_map[key] = [ref.model_dump(mode="json") for ref in grants]
                if key in snapshots:
                    snapshot = service.revalidate(parse_contract(MemorySnapshot, snapshots[key]), identity,
                                                       at=datetime.now(timezone.utc), artifacts=tuple(grants))
                else:
                    snapshot = service.retrieve(
                        spec["objective"], identity, at=datetime.now(timezone.utc),
                        required_ids=tuple(state.get("memory_required_ids", [])), artifacts=tuple(grants))
                snapshots[key] = snapshot.model_dump(mode="json")
            result["worker_memories"] = snapshots
            result["worker_memory_grants"] = grant_map
        except MemoryStoreError:
            if state.get("memory_required_ids") or any(item.get("required") for item in state.get("retrieved_memories", {}).get("items", [])):
                result.update(memory_blocked("memory_scheduler", "memory_store_unavailable"))
                result["current_batch_ids"] = []
            else:
                result["memory_notices"] = ["memory_store_unavailable"]
                result["worker_memories"] = {}  # No unvalidated stale projection in a Send.
        except ValueError as error:
            reason = error.code if isinstance(error, MemoryPolicyError) else "memory_binding_invalid"
            result.update(memory_blocked("memory_scheduler", reason))
            result["current_batch_ids"] = []
        return result

    def stage_final(self, state: dict) -> dict:
        from .agents import resource_stop_reason
        plans = list(state.get("memory_write_plans", []))
        if (not self.service or not state.get("memory_binding")
                or not state.get("memory_policy", {}).get("long_term_auto_extract", False)
                or not self.settings.memory_auto_extract
                or not state.get("execution_result")):
            return {"memory_write_plans": plans}
        if resource_stop_reason(state):
            return {"memory_write_plans": plans, "memory_notices": ["memory_candidate_not_staged_budget"]}
        try:
            identity = self.identity(state)
            context = self.context(state, identity)
            results = state.get("worker_results", [])
            latest = {item["task_id"]: item for item in sorted(results, key=lambda item: item.get("attempt", 0))}
            # One bounded candidate from final public delivery, never raw logs,
            # tools, exception text, or all intermediate Worker answers.
            winner = next((item for item in reversed(list(latest.values()))
                           if item.get("content") == state["execution_result"]), None)
            if winner:
                source = MemorySourceRef(kind="deliverable", thread_id=context.thread_id, task_id=winner["task_id"])
                context = MemoryWriteContext.model_validate({**context.model_dump(), "evidence": (
                    MemoryEvidence(reference=source, namespace=identity.namespace_for("conversation")),)})
                content = f"本轮审查状态：{state.get('review_status', 'unknown')}。公开交付：\n{state['execution_result']}"
                if len(content) <= 8000:
                    # Even a passed Reviewer only produces an unverified candidate.
                    intent = MemoryWriteIntent(key="final-experience", scope="conversation", kind="experience",
                                               content=content, source_refs=(source,))
                    plan = self.service.stage_write(intent, context)
                    plans = [p for p in plans if p.get("key") != plan.key] + [plan.model_dump(mode="json")]
            return {"memory_write_plans": plans}
        except (MemoryPolicyError, MemoryStoreError, ValueError):
            return {"memory_write_plans": plans, "memory_notices": ["memory_candidate_not_staged"]}

    def commit_final(self, state: dict) -> dict:
        from .agents import resource_stop_reason
        plans = state.get("memory_write_plans", [])
        if not plans:
            return {}
        results = []
        for raw in plans:
            if (raw.get("record") and raw["record"].get("write_basis") == "task_extraction"
                    and not self.settings.memory_auto_extract):
                results.append({"key": raw["key"], "status": "not_saved", "reason": "memory_auto_extract_disabled"})
                continue
            if not self.service or resource_stop_reason(state):
                results.append({"key": raw["key"], "status": "not_saved", "reason": "memory_write_budget_or_store_unavailable"})
                continue
            try:
                result = self.service.commit(parse_contract(MemoryWritePlan, raw), self.identity(state))
                results.append(result.model_dump(mode="json"))
            except ValueError:
                results.append({"key": raw["key"], "status": "rejected", "reason": "memory_write_plan_invalid"})
        text = "; ".join(f"{item['key']}: {item['status']}" for item in results)
        return {"memory_write_results": results, "messages": [AIMessage(content=f"[Memory] 记忆写入结果：{text}")]}
