"""Deterministic, offline context projection and budget management.

These helpers only prepare model input. They never mutate stored conversation
records or checkpoints, call an LLM, load configuration, or download a tokenizer.
The default token estimate is the UTF-8 byte count, intentionally conservative
for byte-based tokenizers. A caller may inject an already loaded local tokenizer.
This is an input-admission budget, separate from the graph's action budget.
"""

from __future__ import annotations

import json
import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


TokenCounter = Callable[[str], int]
SUMMARY_PREFIX = "历史摘要（原文抽取，可能省略细节）：\n"
TRUNCATION_MARKER = "\n[内容已截断]"
_UPSTREAM_FIELDS = ("task_id", "agent_type", "attempt", "status", "content")
_UPSTREAM_ARTIFACT_FIELDS = (
    "artifact_id",
    "artifact_version",
    "base_artifact_version",
    "changed_paths",
    "change_summary",
)


class ContextBudgetExceeded(ValueError):
    """Required instructions, provenance or protocol data cannot fit the budget."""

    reason = "context_budget_exceeded"


class InvalidToolHistory(ValueError):
    """A tool result is orphaned, duplicated, or missing from a call group."""


def estimate_tokens(text: str, *, token_counter: TokenCounter | None = None) -> int:
    """Count using a supplied local tokenizer, or conservative UTF-8 bytes."""
    if token_counter is None:
        return len(text.encode("utf-8"))
    value = token_counter(text)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("token_counter must return a nonnegative integer")
    return value


def _json_tokens(value: Any, token_counter: TokenCounter | None) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False), token_counter=token_counter)


def truncate_text(
    text: str,
    max_tokens: int,
    *,
    token_counter: TokenCounter | None = None,
    marker: str = TRUNCATION_MARKER,
) -> str:
    """Return a bounded Unicode prefix with an explicit, budgeted omission mark."""
    if max_tokens < 0:
        raise ValueError("max_tokens must be nonnegative")
    if estimate_tokens(text, token_counter=token_counter) <= max_tokens:
        return text
    if estimate_tokens(marker, token_counter=token_counter) > max_tokens:
        raise ContextBudgetExceeded("The budget cannot fit a truncation marker")
    # Repeated projection must not accumulate nested truncation suffixes.
    original = text[:-len(marker)] if marker and text.endswith(marker) else text
    low, high = 0, len(original)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = original[:middle] + marker
        if estimate_tokens(candidate, token_counter=token_counter) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    result = original[:low] + marker
    # An injected tokenizer need not have a strictly monotonic prefix count.
    while low and estimate_tokens(result, token_counter=token_counter) > max_tokens:
        low -= 1
        result = original[:low] + marker
    return result


def _bound_records(
    records: list[dict[str, Any]], max_tokens: int, token_counter: TokenCounter | None
) -> list[dict[str, Any]]:
    """Shrink content only, retaining every record's identity and other fields."""
    while _json_tokens(records, token_counter) > max_tokens:
        excess = _json_tokens(records, token_counter) - max_tokens
        candidates = sorted(
            (index for index, item in enumerate(records) if item.get("content")),
            key=lambda index: estimate_tokens(records[index]["content"], token_counter=token_counter),
            reverse=True,
        )
        changed = False
        minimum = estimate_tokens(TRUNCATION_MARKER, token_counter=token_counter)
        for index in candidates:
            current = records[index]["content"]
            count = estimate_tokens(current, token_counter=token_counter)
            if count <= minimum:
                continue
            target = max(minimum, count - max(1, excess))
            shortened = truncate_text(current, target, token_counter=token_counter)
            if shortened != current:
                records[index]["content"] = shortened
                changed = True
                break
        if not changed:
            raise ContextBudgetExceeded("The budget cannot fit required record metadata and omission markers")
    return records


def _history_pairs(history: Sequence[Mapping[str, Any]]) -> tuple[list[list[dict[str, str]]], str]:
    pairs: list[list[dict[str, str]]] = []
    pending: dict[str, str] | None = None
    summaries: list[str] = []
    for raw in history:
        if not isinstance(raw, Mapping):
            continue
        role, content = raw.get("role"), raw.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        if role == "assistant" and content.startswith(SUMMARY_PREFIX) and pending is None:
            summaries.append(content[len(SUMMARY_PREFIX):])
            continue
        clean = {"role": role, "content": content}
        if role == "user":
            pending = clean
        elif pending is not None:
            pairs.append([pending, clean])
            pending = None
    return pairs, "\n".join(summaries)


def _extract_summary(
    previous: str,
    older: list[list[dict[str, str]]],
    max_tokens: int,
    token_counter: TokenCounter | None,
) -> list[dict[str, str]]:
    if not previous and not older:
        return []
    # Reuse prior extracts verbatim: no recursive interpretation or LLM drift.
    lines = [previous] if previous else []
    for pair in older:
        excerpts = []
        for item in pair:
            text = item["content"].replace("\n", " ")
            excerpts.append(truncate_text(text, 160, token_counter=token_counter))
        lines.append(f"用户：{excerpts[0]}；结果：{excerpts[1]}")
    content = SUMMARY_PREFIX + "\n".join(lines)
    record = [{"role": "assistant", "content": content}]
    if _json_tokens(record, token_counter) <= max_tokens:
        return record
    framing = _json_tokens([{"role": "assistant", "content": SUMMARY_PREFIX}], token_counter)
    allowance = max_tokens - framing - 8
    if allowance < estimate_tokens(TRUNCATION_MARKER, token_counter=token_counter):
        raise ContextBudgetExceeded("The budget cannot fit the labeled conversation summary")
    # Preserve the oldest extract plus recent additions where space permits.
    # A prior summary remains literal source text, never an invented new claim.
    body = "\n".join(lines)
    if previous and older and allowance >= 120:
        old_budget = allowance // 2
        previous_part = truncate_text(previous, old_budget, token_counter=token_counter)
        recent_part = truncate_text("\n".join(lines[1:]), allowance - old_budget - 1, token_counter=token_counter)
        body = previous_part + "\n" + recent_part
    else:
        body = truncate_text(body, allowance, token_counter=token_counter)
    record[0]["content"] = SUMMARY_PREFIX + body
    # JSON escape overhead is included in the final check.
    while _json_tokens(record, token_counter) > max_tokens:
        excess = _json_tokens(record, token_counter) - max_tokens
        body = truncate_text(body, estimate_tokens(body, token_counter=token_counter) - excess, token_counter=token_counter)
        record[0]["content"] = SUMMARY_PREFIX + body
    return record


def compact_conversation_history(
    history: Sequence[Mapping[str, Any]],
    *,
    max_tokens: int = 4000,
    summary_tokens: int = 800,
    recent_turns: int = 6,
    token_counter: TokenCounter | None = None,
) -> list[dict[str, str]]:
    """Keep a recent paired window plus a labeled bounded extract of older turns.

    Only user/assistant role and string content are admitted. Incomplete turns,
    tools, system messages and diagnostic fields are excluded. The summary is a
    single assistant entry with ``SUMMARY_PREFIX``; passing the result back in
    without new turns preserves it. Stored source records are never changed.
    """
    if max_tokens <= 0 or summary_tokens < 0 or recent_turns < 1:
        raise ValueError("Context budget and recent_turns must be positive")
    pairs, previous = _history_pairs(history)
    flattened = [item for pair in pairs for item in pair]
    needs_summary = bool(previous or len(pairs) > recent_turns or _json_tokens(flattened, token_counter) > max_tokens)
    summary_allowance = min(summary_tokens, max_tokens // 2) if needs_summary else 0
    recent_allowance = max_tokens - summary_allowance
    recent: list[list[dict[str, str]]] = []
    for pair in reversed(pairs[-recent_turns:]):
        candidate = [pair] + recent
        if _json_tokens([item for turn in candidate for item in turn], token_counter) <= recent_allowance:
            recent = candidate
        elif not recent:
            bounded = _bound_records([dict(item) for item in pair], recent_allowance, token_counter)
            recent = [bounded]
            break
        else:
            break
    older = pairs[:len(pairs) - len(recent)]
    summary = _extract_summary(previous, older, summary_allowance, token_counter) if summary_allowance and (previous or older) else []
    result = summary + [item for pair in recent for item in pair]
    if _json_tokens(result, token_counter) > max_tokens:
        raise ContextBudgetExceeded("Conversation context exceeds its input budget")
    return result


def project_upstream_results(
    results: Sequence[Mapping[str, Any]],
    *,
    max_tokens: int = 3000,
    per_result_tokens: int = 1500,
    token_counter: TokenCounter | None = None,
) -> list[dict[str, Any]]:
    """Project only deliverables and provenance; never forward Worker diagnostics.

    Every dependency's identity is retained. If identities alone exceed the
    budget, fail explicitly rather than silently dropping a required dependency.
    """
    if max_tokens <= 0 or per_result_tokens <= 0:
        raise ValueError("Upstream budgets must be positive")
    projected: list[dict[str, Any]] = []
    for raw in results:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in _UPSTREAM_FIELDS:
            value = raw.get(key, 1 if key == "attempt" else "")
            if key == "attempt":
                item[key] = value if isinstance(value, int) and not isinstance(value, bool) else 1
            else:
                item[key] = value if isinstance(value, str) else ""
        for key in _UPSTREAM_ARTIFACT_FIELDS:
            if key in raw and isinstance(raw[key], (str, int, list, type(None))):
                item[key] = copy.deepcopy(raw[key])
        item["content"] = truncate_text(item["content"], per_result_tokens, token_counter=token_counter)
        projected.append(item)
    return _bound_records(projected, max_tokens, token_counter)


def _message_view(message: BaseMessage) -> dict[str, Any]:
    view: dict[str, Any] = {"role": message.type, "content": message.content}
    if message.name:
        view["name"] = message.name
    if isinstance(message, AIMessage) and message.tool_calls:
        view["tool_calls"] = message.tool_calls
    if isinstance(message, ToolMessage):
        view["tool_call_id"] = message.tool_call_id
    return view


def estimate_message_tokens(
    messages: Sequence[BaseMessage], *, token_counter: TokenCounter | None = None
) -> int:
    """Include call arguments/IDs, roles, names and conservative framing overhead."""
    return _json_tokens([_message_view(message) for message in messages], token_counter) + 8 * len(messages)


def _tool_groups(messages: Sequence[BaseMessage]) -> tuple[list[BaseMessage], list[list[BaseMessage]]]:
    prefix: list[BaseMessage] = []
    index = 0
    while index < len(messages) and isinstance(messages[index], (HumanMessage, SystemMessage)):
        prefix.append(messages[index])
        index += 1
    groups: list[list[BaseMessage]] = []
    seen_ids: set[str] = set()
    while index < len(messages):
        current = messages[index]
        if isinstance(current, ToolMessage):
            raise InvalidToolHistory("ToolMessage has no preceding matching AIMessage")
        group = [current]
        index += 1
        if isinstance(current, AIMessage) and current.tool_calls:
            ids = [call.get("id") for call in current.tool_calls]
            if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                raise InvalidToolHistory("Tool call IDs must be nonempty and unique")
            outstanding = set(ids)
            seen_ids.update(ids)
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                observation = messages[index]
                if observation.tool_call_id not in outstanding:
                    raise InvalidToolHistory("Unexpected or duplicate tool result ID")
                outstanding.remove(observation.tool_call_id)
                group.append(observation)
                index += 1
            if outstanding:
                raise InvalidToolHistory("An AIMessage tool group is missing tool results")
        groups.append(group)
    return prefix, groups


def _bounded_group(
    group: list[BaseMessage], tool_output_tokens: int, token_counter: TokenCounter | None
) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    for message in group:
        copy = message.model_copy(deep=True)
        content = copy.content if isinstance(copy.content, str) else json.dumps(copy.content, ensure_ascii=False)
        if estimate_tokens(content, token_counter=token_counter) > tool_output_tokens:
            copy = copy.model_copy(update={"content": _clip_content(copy, content, tool_output_tokens, token_counter)})
        result.append(copy)
    return result


def _clip_content(message: BaseMessage, content: str, budget: int, counter: TokenCounter | None) -> str:
    """Keep typed observations valid JSON and retain their success/error flags."""
    if not isinstance(message, ToolMessage):
        return truncate_text(content, budget, token_counter=counter)
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return truncate_text(content, budget, token_counter=counter)
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return truncate_text(content, budget, token_counter=counter)
    reduced = {"ok": payload["ok"], "truncated": True}
    if payload.get("error_code"):
        reduced["error_code"] = payload["error_code"]
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if estimate_tokens(encode(reduced), token_counter=counter) > budget:
        raise ContextBudgetExceeded("The budget cannot retain the tool error status")
    preview = payload.get("data") if payload.get("ok") else payload.get("message", "")
    preview = preview if isinstance(preview, str) else encode(preview)
    low, high = 0, len(preview)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(encode({**reduced, "preview": preview[:middle]}), token_counter=counter) <= budget:
            low = middle
        else:
            high = middle - 1
    if low:
        reduced["preview"] = preview[:low]
    return encode(reduced)


# 消息压缩算法
def compact_worker_messages(
    messages: Sequence[BaseMessage],
    *,
    max_tokens: int = 12000,
    tool_output_tokens: int = 2000,
    reserved_tokens: int = 0,
    token_counter: TokenCounter | None = None,
) -> list[BaseMessage]:
    """Copy a valid, bounded Worker transcript without breaking tool-call pairs.

    The initial human/system instructions are pinned unchanged. Oldest complete
    groups may be evicted; the latest complete tool group and the trailing group
    remain. Only message content may be shortened: call IDs, names and arguments
    are immutable. Budget overflow of required data is an explicit exception.
    ``reserved_tokens`` accounts for output reserve, tool schemas and other input
    outside these messages. The original transcript remains the audit source.
    """
    available = max_tokens - reserved_tokens
    if available <= 0 or tool_output_tokens <= 0 or reserved_tokens < 0:
        raise ContextBudgetExceeded("No usable Worker input budget remains")
    prefix, original_groups = _tool_groups(messages)
    prefix = [message.model_copy(deep=True) for message in prefix]
    if estimate_message_tokens(prefix, token_counter=token_counter) > available:
        raise ContextBudgetExceeded("The initial task and instructions exceed the input budget")
    groups = [_bounded_group(group, tool_output_tokens, token_counter) for group in original_groups]
    if not groups:
        return prefix
    mandatory = {len(groups) - 1}
    for index in range(len(groups) - 1, -1, -1):
        first = groups[index][0]
        if isinstance(first, AIMessage) and first.tool_calls:
            mandatory.add(index)
            break

    def flatten(indices: set[int]) -> list[BaseMessage]:
        return prefix + [message for index in sorted(indices) for message in groups[index]]

    # Reduce only required observation/content bodies to fit, never protocol IDs
    # or code arguments. All parallel results remain together in the same group.
    while estimate_message_tokens(flatten(mandatory), token_counter=token_counter) > available:
        current = flatten(mandatory)
        excess = estimate_message_tokens(current, token_counter=token_counter) - available
        candidates = [(index, pos, message) for index in mandatory for pos, message in enumerate(groups[index]) if isinstance(message.content, str) and message.content]
        candidates.sort(key=lambda entry: estimate_tokens(entry[2].content, token_counter=token_counter), reverse=True)
        minimum = estimate_tokens(TRUNCATION_MARKER, token_counter=token_counter)
        changed = False
        for index, pos, message in candidates:
            count = estimate_tokens(message.content, token_counter=token_counter)
            if count <= minimum:
                continue
            target = max(minimum, count - max(1, excess))
            clipped = _clip_content(message, message.content, target, token_counter)
            groups[index][pos] = message.model_copy(update={"content": clipped})
            changed = True
            break
        if not changed:
            raise ContextBudgetExceeded("The latest complete tool group cannot fit without losing call arguments or results")
    selected = set(mandatory)
    for index in range(len(groups) - 1, -1, -1):
        if index in selected:
            continue
        candidate = selected | {index}
        if estimate_message_tokens(flatten(candidate), token_counter=token_counter) <= available:
            selected = candidate
        else:
            break
    return flatten(selected)
