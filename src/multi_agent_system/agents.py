"""AgentG 的路由、编排、专业 Worker、审查节点与工具。"""

from __future__ import annotations

import io
import json
import sys
import time
import traceback
from enum import Enum
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

from .state import MultiAgentState, WorkerInputState


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
@tool
def python_repl(code: str) -> str:
    """执行 Python 代码并返回 stdout 或报错堆栈。

    适用于数据处理、计算、图表生成或文件读写。该工具直接使用进程内
    exec，仅应在可信环境使用。
    """
    old_stdout = sys.stdout
    redirected_output = sys.stdout = io.StringIO()
    try:
        exec(code, globals())
        output = redirected_output.getvalue()
        return output if output.strip() else "代码执行成功，无标准输出。"
    except Exception:
        return f"执行报错:\n{traceback.format_exc()}"
    finally:
        sys.stdout = old_stdout


def _create_ddgs_client(timeout: int = 15) -> Any:
    """创建新版 DDGS 客户端，并兼容旧依赖名称。"""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    return DDGS(timeout=timeout)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """使用 DDGS 元搜索，返回标题、URL 和摘要，最多返回 10 条。"""
    max_results = min(max(max_results, 1), 10)
    results: list[str] = []
    failures: list[str] = []

    try:
        for result in _create_ddgs_client(timeout=15).text(
            query, max_results=max_results
        ):
            results.append(
                f"标题: {result.get('title', 'N/A')}\n"
                f"URL: {result.get('href', 'N/A')}\n"
                f"摘要: {result.get('body', 'N/A')}"
            )
        if results:
            return "\n\n---\n\n".join(results)
    except Exception as exc:
        failures.append(f"ddgs:{type(exc).__name__}")

    try:
        import urllib.parse
        import urllib.request

        from bs4 import BeautifulSoup

        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            soup = BeautifulSoup(response.read(), "html.parser")
            for item in soup.select(".result")[:max_results]:
                title_element = item.select_one(".result__title")
                link_element = item.select_one(".result__url")
                snippet_element = item.select_one(".result__snippet")
                title = title_element.get_text(strip=True) if title_element else "N/A"
                link = (
                    link_element.get("href", link_element.get_text(strip=True))
                    if link_element
                    else "N/A"
                )
                snippet = (
                    snippet_element.get_text(strip=True) if snippet_element else "N/A"
                )
                results.append(f"标题: {title}\nURL: {link}\n摘要: {snippet}")
        if results:
            return "\n\n---\n\n".join(results)
    except Exception as exc:
        failures.append(f"html:{type(exc).__name__}")

    try:
        import urllib.parse
        import urllib.request

        from bs4 import BeautifulSoup

        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            soup = BeautifulSoup(response.read(), "html.parser")
            for row in soup.select("table tr")[1:][:max_results]:
                columns = row.select("td")
                if len(columns) < 2:
                    continue
                link_element = columns[0].select_one("a")
                title = link_element.get_text(strip=True) if link_element else "N/A"
                href = link_element.get("href", "N/A") if link_element else "N/A"
                snippet = columns[1].get_text(strip=True)
                results.append(f"标题: {title}\nURL: {href}\n摘要: {snippet}")
        if results:
            return "\n\n---\n\n".join(results)
    except Exception as exc:
        failures.append(f"lite:{type(exc).__name__}")

    diagnostic = ", ".join(failures) if failures else "empty_results"
    return (
        "所有搜索策略均未返回结果，请尝试更换关键词或稍后重试。"
        f"（诊断: {diagnostic}）"
    )


_WMO_WEATHER_CODES = {
    0: "晴朗",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴天",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


@tool
def current_weather(city: str) -> str:
    """查询一个城市的当前天气，返回 Open-Meteo 的结构化实时观测结果。"""
    import urllib.parse
    import urllib.request

    try:
        geocoding_query = urllib.parse.urlencode(
            {"name": city, "count": 10, "language": "zh", "format": "json"}
        )
        geocoding_url = (
            "https://geocoding-api.open-meteo.com/v1/search?" + geocoding_query
        )
        geocoding_request = urllib.request.Request(
            geocoding_url, headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(geocoding_request, timeout=15) as response:
            locations = json.loads(response.read().decode("utf-8")).get("results", [])
        if not locations:
            return f"天气查询失败：找不到城市 {city}。"

        # 同名地点优先选择人口最多者，降低小型同名地点误匹配概率。
        location = max(locations, key=lambda item: int(item.get("population", 0) or 0))
        weather_query = urllib.parse.urlencode(
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "weather_code,wind_speed_10m,wind_direction_10m"
                ),
                "timezone": "auto",
            }
        )
        weather_url = "https://api.open-meteo.com/v1/forecast?" + weather_query
        weather_request = urllib.request.Request(
            weather_url, headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(weather_request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        observed = payload.get("current", {})
        units = payload.get("current_units", {})
        weather_code = int(observed.get("weather_code", -1))
        result = {
            "requested_city": city,
            "city": location.get("name", city),
            "admin1": location.get("admin1", ""),
            "country": location.get("country", ""),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "timezone": payload.get("timezone", location.get("timezone", "")),
            "observed_at": observed.get("time"),
            "condition": _WMO_WEATHER_CODES.get(weather_code, f"未知代码 {weather_code}"),
            "temperature": observed.get("temperature_2m"),
            "temperature_unit": units.get("temperature_2m", "°C"),
            "apparent_temperature": observed.get("apparent_temperature"),
            "humidity": observed.get("relative_humidity_2m"),
            "humidity_unit": units.get("relative_humidity_2m", "%"),
            "wind_speed": observed.get("wind_speed_10m"),
            "wind_speed_unit": units.get("wind_speed_10m", "km/h"),
            "wind_direction": observed.get("wind_direction_10m"),
            "source": "Open-Meteo",
        }
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return f"天气查询失败（{type(exc).__name__}），请稍后重试。"


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 结构化契约
# ---------------------------------------------------------------------------
class WorkerType(str, Enum):
    RESEARCH = "research"
    CODE = "code"
    DATA = "data"
    SYNTHESIS = "synthesis"


class TaskRoutingDecision(BaseModel):
    route: Literal["simple", "team"] = Field(
        description="simple 表示单个专业 Worker 可完成，team 表示需要拆分协作"
    )
    worker_type: WorkerType = Field(
        default=WorkerType.SYNTHESIS,
        description="simple 路径使用的 Worker；team 路径可忽略",
    )
    reason: str = Field(default="模型未提供路由原因", description="路由原因")

    @model_validator(mode="before")
    @classmethod
    def normalise_route_alias(cls, value: Any) -> Any:
        """兼容部分 JSON Mode 模型把 route 输出为 type。"""
        if not isinstance(value, dict):
            return value
        normalised = dict(value)
        route_alias = normalised.get("type")
        if "route" not in normalised and route_alias in {"simple", "team"}:
            normalised["route"] = route_alias
        return normalised


class SubTaskSpec(BaseModel):
    id: str = Field(description="子任务唯一 ID")
    agent_type: WorkerType = Field(description="负责执行的专业 Worker")
    objective: str = Field(description="不依赖完整对话即可理解的任务目标")
    dependencies: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    output_format: str = Field(default="清晰、可复用的文本结果")
    acceptance_criteria: list[str] = Field(default_factory=list)

    @field_validator(
        "dependencies", "allowed_tools", "acceptance_criteria", mode="before"
    )
    @classmethod
    def normalise_string_list(cls, value: Any) -> Any:
        """兼容 JSON Mode 模型把单项数组字段输出成字符串。"""
        if value is None:
            return []
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        return value


class OrchestrationPlan(BaseModel):
    summary: str = Field(description="总体执行策略")
    subtasks: list[SubTaskSpec] = Field(description="具有依赖关系的子任务列表")


class ReviewDecision(BaseModel):
    status: Literal["pass", "revise", "blocked"]
    failed_criteria: list[str] = Field(default_factory=list)
    revision_targets: list[str] = Field(
        default_factory=list,
        description="需要返工的子任务 ID；不得填写 Agent 名称",
    )
    feedback: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


WORKER_TOOLBOX: dict[WorkerType, list[BaseTool]] = {
    WorkerType.RESEARCH: [web_search, current_weather],
    WorkerType.CODE: [python_repl],
    WorkerType.DATA: [python_repl],
    WorkerType.SYNTHESIS: [],
}

WORKER_DESCRIPTIONS: dict[WorkerType, str] = {
    WorkerType.RESEARCH: "搜索、实时天气查询、来源梳理和事实核验",
    WorkerType.CODE: "Python 代码设计、执行和技术实现",
    WorkerType.DATA: "数据读取、清洗、统计分析和表格处理",
    WorkerType.SYNTHESIS: "整合上游产物并形成最终答案",
}


def _worker_type(value: Any) -> WorkerType:
    if isinstance(value, WorkerType):
        return value
    try:
        return WorkerType(str(value))
    except ValueError:
        return WorkerType.SYNTHESIS


def _default_tool_names(worker_type: WorkerType) -> list[str]:
    return [item.name for item in WORKER_TOOLBOX[worker_type]]


def _normalise_subtasks(plan: OrchestrationPlan) -> tuple[list[dict[str, Any]], str]:
    """清理模型计划，补充最终综合任务，并对循环依赖安全降级。"""
    normalised: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for index, raw_spec in enumerate(plan.subtasks, start=1):
        data = raw_spec.model_dump(mode="json")
        task_id = str(data.get("id") or f"task-{index}").strip() or f"task-{index}"
        if task_id in used_ids:
            task_id = f"{task_id}-{index}"
        used_ids.add(task_id)

        agent_type = _worker_type(data.get("agent_type"))
        objective = str(data.get("objective") or "完成分配的子任务")
        allowed_tools = list(data.get("allowed_tools") or _default_tool_names(agent_type))
        valid_tool_names = set(_default_tool_names(agent_type))
        allowed_tools = [name for name in allowed_tools if name in valid_tool_names]
        current_weather_terms = ("当前天气", "实时天气", "current weather")
        if agent_type == WorkerType.RESEARCH and any(
            term in objective.lower() for term in current_weather_terms
        ):
            # 当前天气使用结构化专用 API，避免依赖搜索引擎摘要与可用性。
            allowed_tools = [current_weather.name]

        normalised.append(
            {
                "id": task_id,
                "agent_type": agent_type.value,
                "objective": objective,
                "dependencies": list(dict.fromkeys(data.get("dependencies") or [])),
                "allowed_tools": allowed_tools,
                "output_format": str(data.get("output_format") or "清晰、可复用的文本结果"),
                "acceptance_criteria": list(
                    data.get("acceptance_criteria") or ["结果直接满足子任务目标"]
                ),
            }
        )

    if not normalised:
        normalised.append(
            {
                "id": "synthesis-final",
                "agent_type": WorkerType.SYNTHESIS.value,
                "objective": "直接完成用户任务并给出最终答案",
                "dependencies": [],
                "allowed_tools": [],
                "output_format": "面向用户的完整最终答案",
                "acceptance_criteria": ["完整回答用户任务"],
            }
        )

    synthesis_tasks = [
        item for item in normalised if item["agent_type"] == WorkerType.SYNTHESIS.value
    ]
    if not synthesis_tasks or normalised[-1]["agent_type"] != WorkerType.SYNTHESIS.value:
        dependencies = [item["id"] for item in normalised]
        synthesis_id = "synthesis-final"
        suffix = 1
        while synthesis_id in used_ids:
            suffix += 1
            synthesis_id = f"synthesis-final-{suffix}"
        synthesis_task = {
            "id": synthesis_id,
            "agent_type": WorkerType.SYNTHESIS.value,
            "objective": "整合所有专业 Worker 的结果，形成面向用户的最终答案",
            "dependencies": dependencies,
            "allowed_tools": [],
            "output_format": "结构清晰、结论明确的最终答案",
            "acceptance_criteria": ["覆盖所有有效 Worker 结果", "直接回应原始任务"],
        }
        normalised.append(synthesis_task)
        used_ids.add(synthesis_id)
        synthesis_tasks = [synthesis_task]

    final_synthesis = synthesis_tasks[-1]
    final_synthesis["dependencies"] = list(
        dict.fromkeys(
            list(final_synthesis["dependencies"])
            + [item["id"] for item in normalised if item["id"] != final_synthesis["id"]]
        )
    )

    known_ids = {item["id"] for item in normalised}
    for item in normalised:
        item["dependencies"] = [
            dependency
            for dependency in item["dependencies"]
            if dependency in known_ids and dependency != item["id"]
        ]

    if _has_dependency_cycle(normalised):
        previous_id: str | None = None
        for item in normalised:
            item["dependencies"] = [previous_id] if previous_id else []
            previous_id = item["id"]
        warning = "检测到循环依赖，已按计划顺序安全降级为串行执行。"
    else:
        warning = ""

    return normalised, warning


def _has_dependency_cycle(subtasks: list[dict[str, Any]]) -> bool:
    dependencies = {item["id"]: set(item["dependencies"]) for item in subtasks}
    remaining = set(dependencies)
    while remaining:
        ready = {task_id for task_id in remaining if not (dependencies[task_id] & remaining)}
        if not ready:
            return True
        remaining -= ready
    return False


def latest_worker_results(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 task_id 选取 attempt 最大的最新 Worker 结果。"""
    latest: dict[str, dict[str, Any]] = {}
    for result in results:
        task_id = str(result.get("task_id", ""))
        if not task_id:
            continue
        if task_id not in latest or int(result.get("attempt", 0)) >= int(
            latest[task_id].get("attempt", 0)
        ):
            latest[task_id] = result
    return latest


def summarize_resource_events(events: list[dict[str, Any]]) -> dict[str, int]:
    """汇总动作预算；Token 明确不参与当前资源策略。"""
    fields = ("model_calls", "tool_calls", "errors", "budget_used")
    return {
        field: sum(max(0, int(event.get(field, 0))) for event in events)
        for field in fields
    }


def resource_stop_reason(
    state: MultiAgentState, required_budget: int = 1
) -> str | None:
    """在模型节点执行前检查全局时间和动作预算。"""
    policy = state.get("resource_policy", {})
    deadline_at = float(policy.get("deadline_at", 0) or 0)
    if deadline_at and time.time() >= deadline_at:
        return "max_run_seconds_exceeded"
    total_budget = int(policy.get("total_action_budget", 0) or 0)
    if total_budget:
        used = summarize_resource_events(state.get("resource_events", []))["budget_used"]
        if used + required_budget > total_budget:
            return "total_action_budget_exhausted"
    return None


def _model_event(source: str, *, errors: int = 0) -> dict[str, Any]:
    return {
        "source": source,
        "model_calls": 1,
        "tool_calls": 0,
        "errors": errors,
        "budget_used": 1,
    }


def _terminated_update(source: str, reason: str) -> MultiAgentState:
    return {
        "termination": {"source": source, "reason": reason},
        "messages": [AIMessage(content=f"[{source}] 资源终止：{reason}")],
    }


# ---------------------------------------------------------------------------
# 路由与编排
# ---------------------------------------------------------------------------
def task_router_node(state: MultiAgentState, llm: ChatOpenAI) -> MultiAgentState:
    stop_reason = resource_stop_reason(state)
    if stop_reason:
        return _terminated_update("TaskRouter", stop_reason)
    task = state.get("task", "")
    prompt = (
        "你是任务分流 Agent。判断任务是否可由一个专业 Worker 独立完成。"
        "请严格返回符合指定结构的 json 对象，不要输出对象之外的内容。"
        "顶层字段必须是 route、worker_type、reason；必须使用 route，禁止用 type 代替。"
        "合法示例：{\"route\":\"team\",\"worker_type\":\"synthesis\","
        "\"reason\":\"任务包含多个可并行子问题\"}。"
        "单一步骤、单一领域且无独立并行子问题时选择 simple；"
        "需要多个专业领域、多个可并行子问题或显式依赖链时选择 team。"
        "simple 时从 research、code、data、synthesis 中选择 worker_type。\n"
        f"任务：{task}"
    )
    structured_llm = llm.with_structured_output(TaskRoutingDecision, method="json_mode")
    decision = structured_llm.invoke([HumanMessage(content=prompt)])
    routing = decision.model_dump(mode="json")
    return {
        "routing": routing,
        "resource_events": [_model_event("task_router")],
        "messages": [
            AIMessage(
                content=(
                    f"[TaskRouter] {routing['route'].upper()}"
                    f" → {routing['worker_type']}\n原因: {routing['reason']}"
                )
            )
        ],
    }


def prepare_simple_task_node(state: MultiAgentState) -> MultiAgentState:
    routing = state.get("routing", {})
    worker_type = _worker_type(routing.get("worker_type"))
    task = state.get("task", "")
    subtask = {
        "id": "simple-1",
        "agent_type": worker_type.value,
        "objective": task,
        "dependencies": [],
        "allowed_tools": _default_tool_names(worker_type),
        "output_format": "直接面向用户的完整答案",
        "acceptance_criteria": ["准确、完整地回应原始任务"],
    }
    return {
        "plan": f"简单任务直通 {worker_type.value} Worker",
        "subtasks": [subtask],
        "pending_task_ids": [subtask["id"]],
    }


def orchestrator_node(state: MultiAgentState, llm: ChatOpenAI) -> MultiAgentState:
    stop_reason = resource_stop_reason(state)
    if stop_reason:
        return _terminated_update("Orchestrator", stop_reason)
    task = state.get("task", "")
    prompt = (
        "你是 Orchestrator。把复杂任务拆成可调度的结构化子任务。"
        "请严格返回符合指定结构的 json 对象，不要输出对象之外的内容。"
        "顶层字段必须是 summary 和 subtasks；subtasks 中每个对象必须包含 "
        "id、agent_type、objective、dependencies、allowed_tools、output_format、"
        "acceptance_criteria。dependencies、allowed_tools、acceptance_criteria 必须是 "
        "json 数组，即使只有一项也不得输出为字符串。"
        "专业 Agent 只有 research、code、data、synthesis。"
        "无依赖的任务应保持彼此独立以便并行；有前置结果时用 dependencies 指定任务 ID。"
        "每个任务必须给出自足 objective、最小 allowed_tools、output_format 和 acceptance_criteria。"
        "research 可使用 web_search 和 current_weather；查询当前或实时天气时必须使用 "
        "current_weather，不要使用 web_search；code/data 只能使用 python_repl；"
        "synthesis 不使用工具。"
        "安排 synthesis 汇总必要上游结果。\n"
        f"原始任务：{task}"
    )
    structured_llm = llm.with_structured_output(OrchestrationPlan, method="json_mode")
    plan_output = structured_llm.invoke([HumanMessage(content=prompt)])
    subtasks, warning = _normalise_subtasks(plan_output)
    lines = [plan_output.summary]
    for item in subtasks:
        dependency_text = ", ".join(item["dependencies"]) or "无"
        lines.append(
            f"- {item['id']} [{item['agent_type']}] {item['objective']}"
            f"（依赖: {dependency_text}）"
        )
    if warning:
        lines.append(f"- 注意：{warning}")
    plan_text = "\n".join(lines)
    return {
        "plan": plan_text,
        "subtasks": subtasks,
        "pending_task_ids": [item["id"] for item in subtasks],
        "resource_events": [_model_event("orchestrator")],
        "messages": [AIMessage(content=f"[Orchestrator]\n{plan_text}")],
    }


# ---------------------------------------------------------------------------
# 专业 Worker
# ---------------------------------------------------------------------------
def _run_worker(
    state: WorkerInputState, llm: ChatOpenAI, worker_type: WorkerType
) -> MultiAgentState:
    task_id = state.get("task_id", "unknown")
    objective = state.get("objective", "")
    upstream_results = state.get("upstream_results", [])
    allowed_tool_names = set(state.get("allowed_tools", []))
    output_format = state.get("output_format", "清晰文本")
    acceptance_criteria = state.get("acceptance_criteria", [])
    revision_feedback = state.get("revision_feedback", "")
    attempt = int(state.get("attempt", 1))
    max_tool_rounds = max(1, int(state.get("max_tool_rounds", 4)))
    tool_call_quota = max(0, int(state.get("tool_call_quota", 12)))
    max_consecutive_errors = max(
        1, int(state.get("max_consecutive_errors", 2))
    )
    deadline_at = float(state.get("deadline_at", 0) or 0)
    budget_quota = max(1, int(state.get("budget_quota", 40)))

    available_tools = [
        item for item in WORKER_TOOLBOX[worker_type] if item.name in allowed_tool_names
    ]
    prompt_parts = [
        f"你是 {worker_type.value.title()}Agent，职责是{WORKER_DESCRIPTIONS[worker_type]}。",
        "你只能处理当前子任务，不得假设自己看到了完整会话或其他无关状态。",
        f"任务目标：{objective}",
        f"必要上游结果：{json.dumps(upstream_results, ensure_ascii=False)}",
        f"允许工具：{sorted(allowed_tool_names) if allowed_tool_names else '无'}",
        f"输出格式：{output_format}",
        f"验收标准：{json.dumps(acceptance_criteria, ensure_ascii=False)}",
    ]
    if revision_feedback:
        prompt_parts.append(f"本次定向返工反馈：{revision_feedback}")
    prompt = "\n".join(prompt_parts)

    result_parts: list[str] = []
    code_snippets: list[str] = []
    error_logs: list[str] = []
    observations: list[dict[str, Any]] = []
    conversation: list[Any] = [HumanMessage(content=prompt)]
    runnable = llm.bind_tools(available_tools) if available_tools else llm
    model_calls = 0
    actual_tool_calls = 0
    tool_rounds = 0
    total_errors = 0
    consecutive_errors = 0
    budget_used = 0
    termination_reason: str | None = None
    final_content = ""
    started_at = time.time()

    def limit_reason(cost: int, *, for_tool: bool = False) -> str | None:
        if deadline_at and time.time() >= deadline_at:
            return "max_run_seconds_exceeded"
        if budget_used + cost > budget_quota:
            return "total_action_budget_exhausted"
        if for_tool and actual_tool_calls >= tool_call_quota:
            return "max_tool_calls_exceeded"
        return None

    while not termination_reason:
        termination_reason = limit_reason(1)
        if termination_reason:
            break

        model_calls += 1
        budget_used += 1
        try:
            response = runnable.invoke(conversation)
        except Exception as exc:
            total_errors += 1
            consecutive_errors += 1
            error_text = f"模型调用失败: {exc}"
            error_logs.append(f"[{task_id}] {error_text}")
            final_content = error_text
            termination_reason = (
                "max_consecutive_errors_exceeded"
                if consecutive_errors >= max_consecutive_errors
                else "model_call_failed"
            )
            break

        conversation.append(response)
        tool_calls = getattr(response, "tool_calls", []) or []
        if not tool_calls:
            final_content = (
                response.content
                if isinstance(response.content, str)
                else str(response.content)
            )
            break

        if tool_rounds >= max_tool_rounds:
            termination_reason = "max_tool_rounds_exceeded"
        else:
            tool_rounds += 1

        for index, tool_call in enumerate(tool_calls, start=1):
            tool_name = str(tool_call.get("name", ""))
            tool_args = tool_call.get("args", {}) or {}
            tool_call_id = str(
                tool_call.get("id") or f"{task_id}-r{tool_rounds}-c{index}"
            )
            call_error = False

            if termination_reason:
                tool_output = f"工具调用未执行：{termination_reason}"
                call_error = True
            elif tool_name not in allowed_tool_names or tool_name not in {
                item.name for item in available_tools
            }:
                tool_output = f"已拒绝未授权工具调用: {tool_name}"
                call_error = True
            else:
                termination_reason = limit_reason(1, for_tool=True)
                if termination_reason:
                    tool_output = f"工具调用未执行：{termination_reason}"
                    call_error = True
                else:
                    try:
                        if tool_name == python_repl.name:
                            code = str(tool_args.get("code", ""))
                            code_snippets.append(code)
                            tool_output = python_repl.invoke({"code": code})
                            result_parts.append(
                                f"🔧 [执行代码]\n{code}\n💻 [运行结果]\n{tool_output}"
                            )
                            call_error = "执行报错" in tool_output
                        elif tool_name == web_search.name:
                            query = str(tool_args.get("query", ""))
                            max_results = int(tool_args.get("max_results", 5))
                            tool_output = web_search.invoke(
                                {"query": query, "max_results": max_results}
                            )
                            result_parts.append(
                                f"🌐 [网络搜索: {query}]\n{tool_output}"
                            )
                            call_error = "未返回结果" in tool_output
                        elif tool_name == current_weather.name:
                            city = str(tool_args.get("city", ""))
                            tool_output = current_weather.invoke({"city": city})
                            result_parts.append(
                                f"🌤️ [当前天气: {city}]\n{tool_output}"
                            )
                            call_error = "天气查询失败" in tool_output
                        else:
                            tool_output = f"未实现工具: {tool_name}"
                            call_error = True
                    except Exception as exc:
                        tool_output = f"工具执行异常: {exc}"
                        call_error = True
                    actual_tool_calls += 1
                    budget_used += 1

            if call_error:
                total_errors += 1
                consecutive_errors += 1
                error_logs.append(f"[{task_id}] {tool_output}")
                result_parts.append(f"⚠️ [工具观察: {tool_name}]\n{tool_output}")
            else:
                consecutive_errors = 0

            observation = {
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": tool_output,
                "is_error": call_error,
            }
            observations.append(observation)
            conversation.append(
                ToolMessage(
                    content=tool_output,
                    tool_call_id=tool_call_id,
                    name=tool_name or "unknown_tool",
                )
            )
            if (
                not termination_reason
                and consecutive_errors >= max_consecutive_errors
            ):
                termination_reason = "max_consecutive_errors_exceeded"

    if final_content.strip():
        result_parts.append(final_content)
    if termination_reason:
        result_parts.append(f"资源终止：{termination_reason}")

    result_text = "\n\n".join(part for part in result_parts if part).strip()
    status = "terminated" if termination_reason else ("error" if error_logs else "completed")
    usage = {
        "model_calls": model_calls,
        "tool_calls": actual_tool_calls,
        "tool_rounds": tool_rounds,
        "errors": total_errors,
        "budget_used": budget_used,
        "elapsed_seconds": round(time.time() - started_at, 6),
    }
    worker_result = {
        "task_id": task_id,
        "agent_type": worker_type.value,
        "attempt": attempt,
        "status": status,
        "content": result_text,
        "tool_observations": observations,
        "usage": usage,
        "termination_reason": termination_reason,
    }
    return {
        "worker_results": [worker_result],
        "worker_attempts": {task_id: attempt},
        "code_snippets": code_snippets,
        "error_logs": error_logs,
        "resource_events": [
            {
                "source": f"{worker_type.value}_worker:{task_id}",
                "model_calls": model_calls,
                "tool_calls": actual_tool_calls,
                "errors": total_errors,
                "budget_used": budget_used,
            }
        ],
        "messages": [
            AIMessage(
                content=(
                    f"[{worker_type.value.title()}Agent:{task_id}] attempt={attempt}\n"
                    f"{result_text}"
                )
            )
        ],
    }


def research_worker_node(state: WorkerInputState, llm: ChatOpenAI) -> MultiAgentState:
    return _run_worker(state, llm, WorkerType.RESEARCH)


def code_worker_node(state: WorkerInputState, llm: ChatOpenAI) -> MultiAgentState:
    return _run_worker(state, llm, WorkerType.CODE)


def data_worker_node(state: WorkerInputState, llm: ChatOpenAI) -> MultiAgentState:
    return _run_worker(state, llm, WorkerType.DATA)


def synthesis_worker_node(state: WorkerInputState, llm: ChatOpenAI) -> MultiAgentState:
    return _run_worker(state, llm, WorkerType.SYNTHESIS)


# ---------------------------------------------------------------------------
# 结构化审查
# ---------------------------------------------------------------------------
def reviewer_node(state: MultiAgentState, llm: ChatOpenAI) -> MultiAgentState:
    stop_reason = resource_stop_reason(state)
    if stop_reason:
        update = _terminated_update("Reviewer", stop_reason)
        update.update(
            {
                "review": f"资源限制触发：{stop_reason}",
                "review_status": "blocked",
                "review_decision": {
                    "status": "blocked",
                    "failed_criteria": ["resource_policy"],
                    "revision_targets": [],
                    "feedback": f"资源限制触发：{stop_reason}",
                    "confidence": 1.0,
                },
                "is_pass": False,
            }
        )
        return update
    subtasks = state.get("subtasks", [])
    latest_results = latest_worker_results(state.get("worker_results", []))
    prompt = (
        "你是严格的 Reviewer。根据原始任务、结构化子任务、各 Worker 最新结果和最终输出审查。"
        "请严格返回符合指定结构的 json 对象，不要输出对象之外的内容。"
        "顶层字段必须是 status、failed_criteria、revision_targets、feedback、confidence。"
        "必须返回 status=pass、revise 或 blocked。"
        "revise 时 revision_targets 只能填写需要返工的子任务 ID；"
        "不要让无关 Worker 重做。若上游变更会影响综合结果，应同时包含相关 synthesis 任务。\n"
        f"原始任务：{state.get('task', '')}\n"
        f"执行计划：{state.get('plan', '')}\n"
        f"子任务：{json.dumps(subtasks, ensure_ascii=False)}\n"
        f"最新 Worker 结果：{json.dumps(latest_results, ensure_ascii=False)}\n"
        f"最终输出：{state.get('execution_result', '')}\n"
        f"错误记录：{json.dumps(state.get('error_logs', []), ensure_ascii=False)}"
    )
    structured_llm = llm.with_structured_output(ReviewDecision, method="json_mode")
    decision = structured_llm.invoke([HumanMessage(content=prompt)])
    decision_data = decision.model_dump(mode="json")

    known_ids = {item["id"] for item in subtasks}
    targets = [
        task_id
        for task_id in decision_data.get("revision_targets", [])
        if task_id in known_ids
    ]
    if decision_data["status"] == "revise" and not targets:
        synthesis_ids = [
            item["id"]
            for item in subtasks
            if item.get("agent_type") == WorkerType.SYNTHESIS.value
        ]
        targets = synthesis_ids[-1:] or list(known_ids)[-1:]
    decision_data["revision_targets"] = targets

    review_round = int(state.get("attempts", 0)) + 1
    is_pass = decision_data["status"] == "pass"
    return {
        "review": decision_data["feedback"],
        "review_status": decision_data["status"],
        "review_decision": decision_data,
        "revision_targets": targets,
        "revision_feedback": decision_data["feedback"],
        "is_pass": is_pass,
        "attempts": review_round,
        "resource_events": [_model_event("reviewer")],
        "messages": [
            AIMessage(
                content=(
                    f"[Reviewer] {decision_data['status'].upper()}"
                    f" · confidence={decision_data['confidence']:.2f}\n"
                    f"定向返工: {targets or '无'}\n"
                    f"反馈: {decision_data['feedback']}"
                )
            )
        ],
    }
