import json
import os
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, ToolMessage

from multi_agent_system.artifact_runtime import (
    ArtifactPatch,
    RevisionInstruction,
    bind_revision_base,
    create_artifact,
)
from multi_agent_system.agents import (
    OrchestrationPlan,
    ReviewDecision,
    SubTaskSpec,
    TaskRoutingDecision,
    WorkerType,
    WORKER_TOOLBOX,
    _normalise_subtasks,
    _normalise_routing_for_task,
    code_worker_node,
    current_weather,
    data_worker_node,
    latest_worker_results,
    orchestrator_node,
    prepare_simple_task_node,
    python_repl,
    research_worker_node,
    reviewer_node,
    synthesis_worker_node,
    task_router_node,
    web_search,
)
from multi_agent_system.tool_runtime import ToolExecutionResult


def _structured_llm(output):
    llm = MagicMock()
    structured = MagicMock()
    structured.invoke.return_value = output
    llm.with_structured_output.return_value = structured
    return llm


def _worker_input(agent_type="synthesis", **overrides):
    value = {
        "task_id": "task-1",
        "agent_type": agent_type,
        "objective": "Complete the assigned task",
        "upstream_results": [],
        "allowed_tools": [],
        "output_format": "text",
        "acceptance_criteria": ["correct"],
        "revision_feedback": "",
        "attempt": 1,
    }
    value.update(overrides)
    return value


class TestPythonRepl:
    def test_executes_valid_code(self):
        assert "hello" in python_repl.invoke({"code": "print('hello')"}).data["stdout"]

    def test_catches_runtime_error(self):
        result = python_repl.invoke({"code": "1 / 0"})
        assert not result.ok
        assert result.error_code == "python_execution_error"
        assert "ZeroDivisionError" in result.data["stderr"]

    def test_catches_syntax_error(self):
        assert python_repl.invoke({"code": "if True print('bad')"}).error_code == "python_execution_error"

    def test_no_stdout_returns_success(self):
        assert "代码执行成功" in python_repl.invoke({"code": "x = 42"}).message

    def test_bound_workspace_controls_relative_working_directory(self, tmp_path):
        original_cwd = os.getcwd()
        result = python_repl.invoke(
            {"code": "import os; print(os.getcwd())", "workspace_path": str(tmp_path)}
        )

        assert result.ok
        assert str(tmp_path).lower() in result.data["stdout"].strip().lower()
        assert os.getcwd() == original_cwd


class TestWebSearch:
    def test_search_returns_results(self):
        with patch(
            "multi_agent_system.agents._bing_rss_search",
            return_value=[
                {"title": "Result", "url": "https://example.com", "snippet": "Body"}
            ],
        ):
            result = web_search.invoke({"query": "query", "max_results": 3})
        assert result.ok
        assert result.data["provider"] == "bing_rss"
        assert result.data["results"] == [
            {"title": "Result", "url": "https://example.com", "snippet": "Body"}
        ]
        assert result.provenance == {"provider": "bing_rss", "result_count": 1}

    def test_search_falls_back_after_unavailable_regional_sources(self):
        with (
            patch("multi_agent_system.agents._bing_rss_search", side_effect=OSError("blocked")),
            patch("multi_agent_system.agents._baidu_html_search", return_value=[]),
            patch(
                "multi_agent_system.agents._ddgs_search",
                return_value=[
                    {"title": "Title", "href": "https://example.com", "body": "Snippet"}
                ],
            ),
        ):
            result = web_search.invoke({"query": "query"})
        assert result.ok
        assert result.data["provider"] == "ddgs"
        assert result.data["results"][0]["title"] == "Title"
        assert result.data["results"][0]["snippet"] == "Snippet"

    def test_bing_rss_parser_returns_source_evidence(self):
        from multi_agent_system.agents import _bing_rss_search

        rss = b"""<?xml version='1.0' encoding='utf-8'?>
        <rss><channel><item><title>City population</title>
        <link>https://example.com/city</link>
        <description><![CDATA[Population <b>1,000,000</b> in 2025.]]></description>
        </item></channel></rss>"""
        response = MagicMock()
        response.read.return_value = rss
        context = MagicMock()
        context.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=context):
            results = _bing_rss_search("city population", 3)

        assert results == [
            {
                "title": "City population",
                "url": "https://example.com/city",
                "snippet": "Population 1,000,000 in 2025.",
            }
        ]

    def test_all_strategies_empty(self):
        with (
            patch("multi_agent_system.agents._bing_rss_search", return_value=[]),
            patch("multi_agent_system.agents._baidu_html_search", return_value=[]),
            patch("multi_agent_system.agents._ddgs_search", return_value=[]),
            patch("multi_agent_system.agents._duckduckgo_html_search", return_value=[]),
        ):
            result = web_search.invoke({"query": "nothing"})
        assert not result.ok
        assert "未返回结果" in result.message
        assert "bing_rss:empty" in result.message

    def test_max_results_is_capped(self):
        with patch(
            "multi_agent_system.agents._bing_rss_search", return_value=[]
        ) as bing, patch(
            "multi_agent_system.agents._baidu_html_search", return_value=[]
        ), patch(
            "multi_agent_system.agents._ddgs_search", return_value=[]
        ), patch(
            "multi_agent_system.agents._duckduckgo_html_search", return_value=[]
        ):
            web_search.invoke({"query": "query", "max_results": 100})
        bing.assert_called_once_with("query", 10)


class TestCurrentWeather:
    def test_returns_structured_current_conditions(self):
        geocoding = MagicMock()
        geocoding.read.return_value = json.dumps(
            {
                "results": [
                    {
                        "name": "南京",
                        "country": "中国",
                        "admin1": "江苏",
                        "latitude": 32.06,
                        "longitude": 118.78,
                        "population": 9000000,
                    }
                ]
            }
        ).encode()
        weather = MagicMock()
        weather.read.return_value = json.dumps(
            {
                "timezone": "Asia/Shanghai",
                "current": {
                    "time": "2026-09-05T12:00",
                    "temperature_2m": 28.5,
                    "relative_humidity_2m": 62,
                    "apparent_temperature": 30.1,
                    "weather_code": 2,
                    "wind_speed_10m": 8.2,
                    "wind_direction_10m": 120,
                },
                "current_units": {
                    "temperature_2m": "°C",
                    "relative_humidity_2m": "%",
                    "wind_speed_10m": "km/h",
                },
            }
        ).encode()
        geocoding_context = MagicMock()
        geocoding_context.__enter__.return_value = geocoding
        weather_context = MagicMock()
        weather_context.__enter__.return_value = weather

        with patch(
            "urllib.request.urlopen",
            side_effect=[geocoding_context, weather_context],
        ) as urlopen:
            outcome = current_weather.invoke({"city": "南京"})
            assert outcome.ok
            result = outcome.data

        assert result["city"] == "南京"
        assert result["condition"] == "局部多云"
        assert result["temperature"] == 28.5
        assert result["source"] == "Open-Meteo"
        assert urlopen.call_count == 2

    def test_missing_city_is_reported_as_failure(self):
        response = MagicMock()
        response.read.return_value = b'{"results": []}'
        context = MagicMock()
        context.__enter__.return_value = response
        with patch("urllib.request.urlopen", return_value=context):
            result = current_weather.invoke({"city": "不存在的城市"})
        assert not result.ok
        assert result.error_code == "city_not_found"


class TestStructuredModels:
    def test_routing_model(self):
        decision = TaskRoutingDecision(
            route="simple", worker_type="data", reason="single data task"
        )
        assert decision.worker_type == WorkerType.DATA

    def test_routing_model_accepts_type_alias_and_missing_reason(self):
        decision = TaskRoutingDecision.model_validate({"type": "team"})
        assert decision.route == "team"
        assert decision.worker_type == WorkerType.SYNTHESIS
        assert decision.reason == "模型未提供路由原因"

    def test_subtask_defaults(self):
        spec = SubTaskSpec(id="x", agent_type="research", objective="find facts")
        assert spec.dependencies == []
        assert spec.output_format

    def test_subtask_wraps_single_string_list_fields(self):
        spec = SubTaskSpec.model_validate(
            {
                "id": "weather-1",
                "agent_type": "research",
                "objective": "query weather",
                "dependencies": "city-selection",
                "allowed_tools": "web_search",
                "acceptance_criteria": "包含温度和天气状况",
            }
        )
        assert spec.dependencies == ["city-selection"]
        assert spec.allowed_tools == ["web_search"]
        assert spec.acceptance_criteria == ["包含温度和天气状况"]

    def test_subtask_unwraps_single_item_output_format_array(self):
        spec = SubTaskSpec.model_validate(
            {
                "id": "weather-1",
                "agent_type": "research",
                "objective": "query weather",
                "output_format": [
                    "JSON 对象，包含 city、temperature、condition 字段"
                ],
            }
        )
        assert spec.output_format == "JSON 对象，包含 city、temperature、condition 字段"

    def test_subtask_rejects_multi_item_output_format_array(self):
        with pytest.raises(Exception):
            SubTaskSpec.model_validate(
                {
                    "id": "weather-1",
                    "agent_type": "research",
                    "objective": "query weather",
                    "output_format": ["JSON 对象", "纯文本"],
                }
            )

    def test_review_confidence_range(self):
        with pytest.raises(Exception):
            ReviewDecision(status="pass", feedback="ok", confidence=1.5)

    def test_review_pass_defaults_feedback_when_json_mode_omits_it(self):
        decision = ReviewDecision.model_validate(
            {
                "status": "pass",
                "revision_targets": [],
                "revision_instructions": [],
            }
        )

        assert decision.feedback == "结果满足验收标准。"

    def test_review_pass_normalises_empty_object_list_fields(self):
        decision = ReviewDecision.model_validate(
            {
                "status": "pass",
                "feedback": "18 × 27 = 486，结果正确。",
                "failed_criteria": {},
                "revision_targets": {},
                "revision_instructions": {},
            }
        )

        assert decision.failed_criteria == []
        assert decision.revision_targets == []
        assert decision.revision_instructions == []

    @pytest.mark.parametrize("task_id", ["4", "6", "simple-1"])
    def test_review_revise_normalises_one_to_one_string_instruction(self, task_id):
        decision = ReviewDecision.model_validate(
            {
                "status": "revise",
                "failed_criteria": ["交付未满足验收标准"],
                "revision_targets": [task_id],
                "revision_instructions": ["仅修改该目标并重新生成完整交付"],
                "feedback": "请按要求定向返工",
            }
        )

        instruction = decision.revision_instructions[0]
        assert instruction.task_id == task_id
        assert instruction.mode == "replace"
        assert instruction.allowed_paths == ["/"]
        assert instruction.requested_changes == ["仅修改该目标并重新生成完整交付"]

    @pytest.mark.parametrize(
        ("targets", "instructions"),
        [
            (["a", "b"], ["only one"]),
            (["a", "a"], ["first", "second"]),
            (["a"], ["  "]),
            (["a", "b"], ["first", {"task_id": "b", "mode": "replace"}]),
        ],
    )
    def test_review_revise_rejects_ambiguous_string_instructions(
        self, targets, instructions
    ):
        with pytest.raises(Exception):
            ReviewDecision.model_validate(
                {
                    "status": "revise",
                    "failed_criteria": ["invalid revision contract"],
                    "revision_targets": targets,
                    "revision_instructions": instructions,
                    "feedback": "repair",
                }
            )

    @pytest.mark.parametrize("status", ["revise", "blocked"])
    def test_non_pass_review_rejects_empty_object_revision_instructions(self, status):
        with pytest.raises(Exception):
            ReviewDecision.model_validate(
                {
                    "status": status,
                    "feedback": "需要明确处理原因",
                    "revision_instructions": {},
                }
            )

    @pytest.mark.parametrize("status", ["revise", "blocked"])
    def test_non_pass_review_still_requires_feedback(self, status):
        with pytest.raises(Exception):
            ReviewDecision.model_validate({"status": status})

    def test_review_rejects_explicitly_empty_feedback(self):
        with pytest.raises(Exception):
            ReviewDecision.model_validate({"status": "pass", "feedback": "  "})

    def test_worker_tool_permissions(self):
        assert [tool.name for tool in WORKER_TOOLBOX[WorkerType.RESEARCH]] == [
            "web_search",
            "current_weather",
        ]
        assert [tool.name for tool in WORKER_TOOLBOX[WorkerType.CODE]] == [
            "python_repl"
        ]
        assert WORKER_TOOLBOX[WorkerType.SYNTHESIS] == []


class TestTaskRouter:
    def test_simple_route(self, base_state):
        llm = _structured_llm(
            TaskRoutingDecision(
                route="simple", worker_type="data", reason="one operation"
            )
        )
        result = task_router_node(base_state, llm)
        assert result["routing"]["route"] == "simple"
        assert result["routing"]["worker_type"] == "data"
        assert "TaskRouter" in result["messages"][0].content

    def test_router_prompt_contains_task(self, base_state):
        llm = _structured_llm(
            TaskRoutingDecision(route="team", reason="complex")
        )
        task_router_node(base_state, llm)
        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert base_state["task"] in prompt.content
        assert "json" in prompt.content.lower()
        assert "必须使用 route，禁止用 type 代替" in prompt.content
        assert "多个城市的天气查询" in prompt.content

    def test_multi_city_weather_is_corrected_to_team(self, base_state):
        base_state["task"] = "请查询当前的天气状况，查询地点可以为随机生成三个城市。"
        llm = _structured_llm(
            TaskRoutingDecision(
                route="simple", worker_type="data", reason="single domain"
            )
        )

        result = task_router_node(base_state, llm)

        assert result["routing"]["route"] == "team"
        assert result["routing"]["worker_type"] == "synthesis"
        assert "规则校正" in result["routing"]["reason"]

    def test_single_city_weather_is_corrected_to_research(self, base_state):
        decision = TaskRoutingDecision(
            route="simple", worker_type="data", reason="single query"
        )

        routing = _normalise_routing_for_task("查询南京当前天气", decision)

        assert routing["route"] == "simple"
        assert routing["worker_type"] == "research"

    def test_historical_weather_analysis_keeps_data_worker(self):
        decision = TaskRoutingDecision(
            route="simple", worker_type="data", reason="historical analysis"
        )

        routing = _normalise_routing_for_task("分析南京历史天气数据", decision)

        assert routing["route"] == "simple"
        assert routing["worker_type"] == "data"

    def test_simple_task_builder_limits_context(self, base_state):
        base_state["routing"] = {"route": "simple", "worker_type": "code", "allowed_tools": ["python_repl"]}
        result = prepare_simple_task_node(base_state)
        assert len(result["subtasks"]) == 1
        assert result["subtasks"][0]["allowed_tools"] == ["python_repl"]
        assert result["pending_task_ids"] == ["simple-1"]

    def test_simple_follow_up_receives_compact_conversation_context(self, base_state):
        base_state["routing"] = {"route": "simple", "worker_type": "synthesis"}
        base_state["conversation_context"] = [
            {"role": "user", "content": "Original question"},
            {"role": "assistant", "content": "Original answer"},
        ]

        result = prepare_simple_task_node(base_state)

        objective = result["subtasks"][0]["objective"]
        assert "Original question" in objective
        assert "Original answer" in objective


class TestOrchestrator:
    def test_creates_structured_plan(self, base_state):
        output = OrchestrationPlan(
            summary="Research then summarize",
            subtasks=[
                SubTaskSpec(id="r1", agent_type="research", objective="Find data")
            ],
        )
        result = orchestrator_node(base_state, _structured_llm(output))
        assert result["subtasks"][0]["id"] == "r1"
        assert result["subtasks"][-1]["agent_type"] == "synthesis"
        assert result["subtasks"][-1]["dependencies"] == ["r1"]

    def test_json_mode_is_explicit_in_prompt(self, base_state):
        output = OrchestrationPlan(
            summary="Direct",
            subtasks=[SubTaskSpec(id="s", agent_type="synthesis", objective="Answer")],
        )
        llm = _structured_llm(output)
        orchestrator_node(base_state, llm)
        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert "json" in prompt.content.lower()
        assert "即使只有一项也不得输出为字符串" in prompt.content
        assert "output_format 必须是 json 字符串" in prompt.content

    def test_independent_tasks_remain_parallelizable(self):
        plan = OrchestrationPlan(
            summary="parallel",
            subtasks=[
                SubTaskSpec(id="r", agent_type="research", objective="research"),
                SubTaskSpec(id="c", agent_type="code", objective="code"),
            ],
        )
        tasks, warning = _normalise_subtasks(plan)
        assert tasks[0]["dependencies"] == []
        assert tasks[1]["dependencies"] == []
        assert set(tasks[-1]["dependencies"]) == {"r", "c"}
        assert warning == ""

    def test_current_weather_task_uses_dedicated_tool(self):
        plan = OrchestrationPlan(
            summary="weather",
            subtasks=[
                SubTaskSpec(
                    id="weather",
                    agent_type="research",
                    objective="查询南京的当前天气",
                    allowed_tools=["web_search"],
                )
            ],
        )
        tasks, _ = _normalise_subtasks(plan)
        assert tasks[0]["allowed_tools"] == ["current_weather"]

    def test_invalid_dependencies_are_removed(self):
        plan = OrchestrationPlan(
            summary="invalid dependency",
            subtasks=[
                SubTaskSpec(
                    id="r", agent_type="research", objective="research", dependencies=["x"]
                )
            ],
        )
        tasks, _ = _normalise_subtasks(plan)
        assert tasks[0]["dependencies"] == []

    def test_cycle_falls_back_to_serial(self):
        plan = OrchestrationPlan(
            summary="cycle",
            subtasks=[
                SubTaskSpec(id="a", agent_type="research", objective="a", dependencies=["b"]),
                SubTaskSpec(id="b", agent_type="code", objective="b", dependencies=["a"]),
            ],
        )
        tasks, warning = _normalise_subtasks(plan)
        assert tasks[0]["dependencies"] == []
        assert tasks[1]["dependencies"] == [tasks[0]["id"]]
        assert "循环依赖" in warning

    def test_duplicate_ids_are_made_unique(self):
        plan = OrchestrationPlan(
            summary="duplicate",
            subtasks=[
                SubTaskSpec(id="a", agent_type="research", objective="a"),
                SubTaskSpec(id="a", agent_type="data", objective="b"),
            ],
        )
        tasks, _ = _normalise_subtasks(plan)
        ids = [task["id"] for task in tasks]
        assert len(ids) == len(set(ids))

    def test_trailing_non_synthesis_gets_final_aggregator(self):
        plan = OrchestrationPlan(
            summary="bad order",
            subtasks=[
                SubTaskSpec(id="s", agent_type="synthesis", objective="partial"),
                SubTaskSpec(id="c", agent_type="code", objective="follow-up", dependencies=["s"]),
            ],
        )
        tasks, warning = _normalise_subtasks(plan)
        assert tasks[-1]["agent_type"] == "synthesis"
        assert tasks[-1]["id"] not in {"s", "c"}
        assert warning == ""


class TestWorkers:
    def test_synthesis_worker_plain_response(self):
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="Final answer")
        result = synthesis_worker_node(_worker_input(), llm)
        assert result["worker_results"][0]["content"] == "Final answer"
        assert result["worker_results"][0]["agent_type"] == "synthesis"

    def test_code_worker_executes_python(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "python_repl", "args": {"code": "print('worked')"}}
        ]
        llm.bind_tools.return_value.invoke.side_effect = [
            response,
            AIMessage(content="final code result"),
        ]
        result = code_worker_node(
            _worker_input("code", allowed_tools=["python_repl"]), llm
        )
        assert result["worker_results"][0]["content"] == "final code result"
        assert "worked" in result["messages"][0].content
        assert result["code_snippets"] == ["print('worked')"]

    def test_data_worker_records_python_error(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [{"name": "python_repl", "args": {"code": "1/0"}}]
        llm.bind_tools.return_value.invoke.side_effect = [
            response,
            AIMessage(content="cannot complete"),
        ]
        result = data_worker_node(
            _worker_input("data", allowed_tools=["python_repl"]), llm
        )
        assert result["worker_results"][0]["status"] == "error"
        assert "ZeroDivisionError" in result["error_logs"][0]

    def test_research_worker_uses_search_only(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "web_search", "args": {"query": "facts", "max_results": 2}}
        ]
        llm.bind_tools.return_value.invoke.side_effect = [
            response,
            AIMessage(content="final research result"),
        ]
        with patch(
            "multi_agent_system.agents._bing_rss_search",
            return_value=[
                {"title": "Source", "url": "https://source.test", "snippet": "source result"}
            ],
        ):
            result = research_worker_node(
                _worker_input("research", allowed_tools=["web_search"]), llm
            )
        assert result["worker_results"][0]["content"] == "final research result"
        assert "source result" in result["messages"][0].content
        bound_tools = llm.bind_tools.call_args[0][0]
        assert [tool.name for tool in bound_tools] == ["web_search"]

    def test_research_worker_executes_five_population_searches_without_false_termination(self):
        llm = MagicMock()
        cities = ["上海", "大连", "海口", "昆明", "天津"]
        response = AIMessage(content="")
        response.tool_calls = [
            {
                "name": "web_search",
                "args": {"query": f"{city} 最近人口 统计年份 来源", "max_results": 3},
                "id": f"population-{index}",
            }
            for index, city in enumerate(cities, start=1)
        ]
        llm.bind_tools.return_value.invoke.side_effect = [
            response,
            AIMessage(content="五个城市人口资料已检索并整理。"),
        ]

        def search_result(query, _max_results):
            city = next(city for city in cities if city in query)
            return [{
                "title": f"{city}统计公报",
                "url": f"https://example.test/{city}",
                "snippet": f"{city} 2025 年常住人口统计资料。",
            }]

        with patch(
            "multi_agent_system.agents._bing_rss_search", side_effect=search_result
        ):
            result = research_worker_node(
                _worker_input(
                    "research",
                    allowed_tools=["web_search"],
                    required_tools=["web_search"],
                    tool_call_quota=5,
                    max_consecutive_errors=2,
                ),
                llm,
            )

        worker = result["worker_results"][0]
        assert worker["status"] == "completed"
        assert worker["termination_reason"] is None
        assert worker["usage"]["tool_calls"] == 5
        assert len(worker["tool_observations"]) == 5
        assert all(not item["is_error"] for item in worker["tool_observations"])

    def test_research_worker_uses_current_weather_tool(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "current_weather", "args": {"city": "南京"}, "id": "weather-1"}
        ]
        llm.bind_tools.return_value.invoke.side_effect = [
            response,
            AIMessage(content="南京当前局部多云，28.5°C。"),
        ]
        with patch.object(
            type(current_weather),
            "invoke",
            return_value='{"city":"南京","condition":"局部多云","temperature":28.5}',
        ):
            result = research_worker_node(
                _worker_input("research", allowed_tools=["current_weather"]), llm
            )
        worker = result["worker_results"][0]
        assert worker["status"] == "completed"
        assert "南京当前局部多云" in worker["content"]
        assert worker["usage"]["tool_calls"] == 1

    def test_worker_rejects_unapproved_tool(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "python_repl", "args": {"code": "print('no')"}}
        ]
        llm.invoke.side_effect = [response, AIMessage(content="continued safely")]
        result = code_worker_node(_worker_input("code", allowed_tools=[]), llm)
        assert result["worker_results"][0]["content"] == "continued safely"
        assert "拒绝未授权" in result["messages"][0].content

    def test_tool_observation_is_returned_to_model(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {
                "name": "python_repl",
                "args": {"code": "print(6 * 7)"},
                "id": "call-42",
            }
        ]
        runner = llm.bind_tools.return_value
        runner.invoke.side_effect = [response, AIMessage(content="The answer is 42")]

        result = code_worker_node(
            _worker_input("code", allowed_tools=["python_repl"]), llm
        )

        second_messages = runner.invoke.call_args_list[1].args[0]
        observation = next(
            message for message in second_messages if isinstance(message, ToolMessage)
        )
        assert isinstance(observation, ToolMessage)
        assert observation.tool_call_id == "call-42"
        assert "42" in observation.content
        assert result["worker_results"][0]["content"].endswith("The answer is 42")
        assert result["worker_results"][0]["usage"]["tool_calls"] == 1
        tool_traces = [
            item for item in result["decision_traces"]
            if item["stage"] in {"tool_selection", "tool_observation"}
        ]
        assert [item["stage"] for item in tool_traces] == [
            "tool_selection", "tool_observation",
        ]
        assert all(item["status"] == "success" for item in tool_traces)
        assert tool_traces[1]["evidence_refs"] == ["tool_call:call-42"]

    def test_max_tool_rounds_terminates_loop(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "python_repl", "args": {"code": "print('once')"}}
        ]
        llm.bind_tools.return_value.invoke.return_value = response

        result = code_worker_node(
            _worker_input(
                "code",
                allowed_tools=["python_repl"],
                max_tool_rounds=1,
            ),
            llm,
        )
        worker = result["worker_results"][0]
        assert worker["status"] == "terminated"
        assert worker["termination_reason"] == "max_tool_rounds_exceeded"
        assert worker["usage"]["tool_rounds"] == 1
        assert worker["usage"]["tool_calls"] == 1

    def test_tool_call_quota_prevents_execution(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "python_repl", "args": {"code": "print('blocked')"}}
        ]
        llm.bind_tools.return_value.invoke.return_value = response

        result = code_worker_node(
            _worker_input(
                "code",
                allowed_tools=["python_repl"],
                tool_call_quota=0,
            ),
            llm,
        )
        worker = result["worker_results"][0]
        assert worker["termination_reason"] == "max_tool_calls_exceeded"
        assert worker["usage"]["tool_calls"] == 0
        assert result["code_snippets"] == []

    def test_consecutive_tool_errors_terminate(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [{"name": "python_repl", "args": {"code": "1/0"}}]
        llm.bind_tools.return_value.invoke.return_value = response

        result = data_worker_node(
            _worker_input(
                "data",
                allowed_tools=["python_repl"],
                max_consecutive_errors=1,
            ),
            llm,
        )
        worker = result["worker_results"][0]
        assert worker["status"] == "terminated"
        assert worker["termination_reason"] == "max_consecutive_errors_exceeded"
        assert worker["usage"]["errors"] == 1

    def test_action_budget_includes_model_and_tool_calls(self):
        llm = MagicMock()
        response = AIMessage(content="")
        response.tool_calls = [
            {"name": "python_repl", "args": {"code": "print('blocked')"}}
        ]
        llm.bind_tools.return_value.invoke.return_value = response

        result = code_worker_node(
            _worker_input(
                "code",
                allowed_tools=["python_repl"],
                budget_quota=1,
            ),
            llm,
        )
        worker = result["worker_results"][0]
        assert worker["termination_reason"] == "total_action_budget_exhausted"
        assert worker["usage"]["model_calls"] == 1
        assert worker["usage"]["tool_calls"] == 0
        assert worker["usage"]["budget_used"] == 1

    def test_expired_deadline_stops_before_model_call(self):
        llm = MagicMock()
        with patch("multi_agent_system.agents.time.time", return_value=100.0):
            result = synthesis_worker_node(
                _worker_input(deadline_at=99.0), llm
            )
        worker = result["worker_results"][0]
        assert worker["termination_reason"] == "max_run_seconds_exceeded"
        assert worker["usage"]["model_calls"] == 0
        llm.invoke.assert_not_called()

    def test_worker_receives_only_scoped_context(self):
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="done")
        synthesis_worker_node(
            _worker_input(
                upstream_results=[{"task_id": "upstream", "content": "needed"}],
                revision_feedback="fix citation",
            ),
            llm,
        )
        prompt = llm.invoke.call_args[0][0][0].content
        assert "needed" in prompt
        assert "fix citation" in prompt
        assert "完整会话" in prompt

    def test_worker_emits_public_decision_traces_and_versioned_artifact(self):
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="A concise deliverable")

        result = synthesis_worker_node(_worker_input(), llm)

        worker = result["worker_results"][0]
        assert worker["artifact"]["version"] == 1
        assert worker["artifact"]["content"] == "A concise deliverable"
        assert [item["stage"] for item in result["decision_traces"]] == [
            "input", "delivery",
        ]
        assert "私有思维链" in llm.invoke.call_args.args[0][0].content

    def test_patch_revision_applies_to_previous_artifact_only(self):
        previous = create_artifact(
            "task-1", "Paris is 18 C. Tokyo is 26 C."
        )
        instruction = bind_revision_base(
            RevisionInstruction(
                task_id="task-1",
                mode="patch",
                allowed_paths=["/text"],
                requested_changes=["Update Paris only"],
            ),
            previous,
        )
        patch = ArtifactPatch(
            base_version=1,
            base_content_hash=previous.content_hash,
            operations=[{
                "op": "replace_text", "path": "/text",
                "old_text": "Paris is 18 C.", "new_text": "Paris is 19 C.",
            }],
            change_summary="Updated Paris temperature",
        )
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(
            content=json.dumps(patch.model_dump(mode="json"))
        )

        result = synthesis_worker_node(
            _worker_input(
                attempt=2,
                previous_deliverable={
                    "task_id": "task-1", "attempt": 1,
                    "content": previous.content,
                    "artifact": previous.model_dump(mode="json"),
                },
                revision_instruction=instruction.model_dump(mode="json"),
                revision_feedback="Update Paris only",
            ),
            llm,
        )

        worker = result["worker_results"][0]
        assert worker["content"] == "Paris is 19 C. Tokyo is 26 C."
        assert worker["artifact_version"] == 2
        assert worker["base_artifact_version"] == 1
        assert worker["changed_paths"] == ["/text"]
        assert worker["revision_application"]["mode"] == "patch"
        assert any(
            item["stage"] == "revision" and item["status"] == "success"
            for item in result["decision_traces"]
        )

    def test_regenerate_revision_without_previous_artifact_creates_v1(self):
        instruction = RevisionInstruction(
            task_id="task-1",
            mode="regenerate",
            requested_changes=["重新执行并生成完整产物"],
            evidence_requirements=["包含新的完整结果"],
        )
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="Fresh complete deliverable")

        result = synthesis_worker_node(
            _worker_input(
                attempt=2,
                revision_instruction=instruction.model_dump(mode="json"),
                revision_feedback="The first attempt produced no artifact",
            ),
            llm,
        )

        worker = result["worker_results"][0]
        assert worker["content"] == "Fresh complete deliverable"
        assert worker["artifact_version"] == 1
        assert worker["base_artifact_version"] is None
        assert worker["revision_application"]["mode"] == "regenerate"
        assert worker["revision_application"]["base_version"] is None
        assert worker["status"] == "completed"
        assert not result["error_logs"]
        revision_trace = next(
            item for item in result["decision_traces"]
            if item["stage"] == "revision"
        )
        assert "重新生成" in revision_trace["summary"]
        assert revision_trace["evidence_refs"] == []

    def test_latest_worker_result_uses_highest_attempt(self):
        results = [
            {"task_id": "a", "attempt": 1, "content": "old"},
            {"task_id": "a", "attempt": 2, "content": "new"},
        ]
        assert latest_worker_results(results)["a"]["content"] == "new"


class TestReviewer:
    def test_review_decision_accepts_nested_revision_aliases(self):
        decision = ReviewDecision.model_validate(
            {
                "status": "revise",
                "failed_criteria": ["weather evidence incomplete"],
                "revision_targets": ["2"],
                "revision_instructions": [
                    {
                        "target_id": "2",
                        "action": "regenerate",
                        "requested_changes": "重新查询完整天气",
                        "preserve_paths": ["/city"],
                        "evidence_requirements": ["完整 current_weather JSON"],
                    }
                ],
                "feedback": "retry weather",
                "confidence": 0.95,
            }
        )

        instruction = decision.revision_instructions[0]
        assert instruction.task_id == "2"
        assert instruction.mode == "regenerate"
        assert instruction.requested_changes == ["重新查询完整天气"]

    def test_pass_is_structured(self, base_state):
        base_state["subtasks"] = [
            {"id": "simple-1", "agent_type": "synthesis", "dependencies": []}
        ]
        decision = ReviewDecision(status="pass", feedback="All good", confidence=0.9)
        result = reviewer_node(base_state, _structured_llm(decision))
        assert result["review_status"] == "pass"
        assert result["is_pass"] is True
        assert result["attempts"] == 1

    def test_json_mode_is_explicit_in_prompt(self, base_state):
        base_state["subtasks"] = [
            {"id": "s", "agent_type": "synthesis", "dependencies": []}
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))
        reviewer_node(base_state, llm)
        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert "json" in prompt.content.lower()
        assert "revision_instructions 的每个元素必须是 json 对象" in prompt.content
        assert '"task_id":"task-1"' in prompt.content

    def test_invalid_reviewer_output_becomes_structured_blocked(self, base_state):
        base_state["subtasks"] = [
            {"id": "simple-1", "agent_type": "synthesis", "dependencies": []}
        ]
        llm = MagicMock()
        llm.with_structured_output.return_value.invoke.side_effect = OutputParserException(
            "revision_instructions.0 must be an object",
            llm_output='{"status":"revise","revision_instructions":["repair"]}',
        )

        result = reviewer_node(base_state, llm)

        assert result["review_status"] == "blocked"
        assert result["review_decision"]["status"] == "blocked"
        assert result["review_decision"]["failed_criteria"] == [
            "review_output_invalid"
        ]
        assert result["revision_targets"] == []
        assert result["revision_instructions"] == []
        assert result["is_pass"] is False
        assert result["attempts"] == 1
        assert "termination" not in result
        assert result["resource_events"][0]["model_calls"] == 1
        assert result["resource_events"][0]["errors"] == 1
        assert "revision_instructions.0" in result["error_logs"][0]

    def test_revision_targets_are_validated(self, base_state):
        base_state["subtasks"] = [
            {"id": "r", "agent_type": "research", "dependencies": []},
            {"id": "s", "agent_type": "synthesis", "dependencies": ["r"]},
        ]
        decision = ReviewDecision(
            status="revise",
            revision_targets=["missing", "r"],
            feedback="Improve sources",
        )
        result = reviewer_node(base_state, _structured_llm(decision))
        assert result["revision_targets"] == ["r"]
        assert result["is_pass"] is False

    def test_reviewer_binds_typed_patch_instruction_to_latest_artifact(self, base_state):
        previous = create_artifact(
            "r", '{"city":"Paris","temperature":18,"humidity":null}'
        )
        base_state["subtasks"] = [
            {"id": "r", "agent_type": "research", "dependencies": []}
        ]
        base_state["worker_results"] = [{
            "task_id": "r", "agent_type": "research", "attempt": 1,
            "status": "completed", "content": previous.content,
            "artifact": previous.model_dump(mode="json"),
            "artifact_version": 1,
        }]
        decision = ReviewDecision(
            status="revise",
            revision_targets=["r"],
            revision_instructions=[RevisionInstruction(
                task_id="r", mode="patch", allowed_paths=["/humidity"],
                requested_changes=["Fill humidity"], preserve_paths=["/city"],
            )],
            feedback="Humidity is missing",
        )

        result = reviewer_node(base_state, _structured_llm(decision))

        instruction = result["revision_instructions"][0]
        assert instruction["task_id"] == "r"
        assert instruction["mode"] == "patch"
        assert instruction["base_version"] == 1
        assert instruction["base_content_hash"] == previous.content_hash

    def test_empty_revision_targets_fall_back_to_synthesis(self, base_state):
        base_state["subtasks"] = [
            {"id": "r", "agent_type": "research", "dependencies": []},
            {"id": "s", "agent_type": "synthesis", "dependencies": ["r"]},
        ]
        decision = ReviewDecision(status="revise", feedback="Rewrite final answer")
        result = reviewer_node(base_state, _structured_llm(decision))
        assert result["revision_targets"] == ["s"]

    def test_blocked_stops_without_pass(self, base_state):
        base_state["subtasks"] = [
            {"id": "s", "agent_type": "synthesis", "dependencies": []}
        ]
        decision = ReviewDecision(status="blocked", feedback="Missing user input")
        result = reviewer_node(base_state, _structured_llm(decision))
        assert result["review_status"] == "blocked"
        assert result["is_pass"] is False

    def test_reviewer_prompt_contains_worker_results(self, base_state):
        base_state["subtasks"] = [
            {"id": "s", "agent_type": "synthesis", "dependencies": []}
        ]
        base_state["worker_results"] = [
            {"task_id": "s", "attempt": 1, "content": "answer"}
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))
        reviewer_node(base_state, llm)
        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert "answer" in prompt.content
        assert "可交付内容" in prompt.content
        assert "工具诊断" in prompt.content

    def test_reviewer_keeps_complete_small_structured_weather_evidence(self, base_state):
        weather_result = ToolExecutionResult(
            ok=True,
            executed=True,
            data={
                "city": "Paris",
                "condition": "晴朗",
                "temperature": 19.5,
                "humidity": 61,
                "wind_speed": 8.4,
                "source": "Open-Meteo",
            },
        )
        base_state["subtasks"] = [
            {
                "id": "2",
                "agent_type": "research",
                "dependencies": [],
                "required_tools": ["current_weather"],
            }
        ]
        base_state["worker_results"] = [
            {
                "task_id": "2",
                "agent_type": "research",
                "attempt": 1,
                "status": "completed",
                "content": "巴黎当前晴朗，19.5°C，湿度61%，风速8.4km/h。",
                "tool_observations": [
                    {
                        "name": "current_weather",
                        "tool_call_id": "weather-call-2",
                        "is_error": False,
                        "content": weather_result.to_message(),
                        "result": weather_result.model_dump(mode="json"),
                    }
                ],
            }
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        reviewer_node(base_state, llm)

        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert '"temperature": 19.5' in prompt.content
        assert '"humidity": 61' in prompt.content
        assert '"wind_speed": 8.4' in prompt.content
        assert '"projection_truncated": false' in prompt.content
        assert '"source_truncated": false' in prompt.content

    def test_reviewer_distinguishes_source_truncation_from_projection(self, base_state):
        weather_result = ToolExecutionResult(
            ok=True,
            executed=True,
            data={"city": "Paris", "preview": "partial upstream result"},
            truncated=True,
        )
        base_state["subtasks"] = [
            {
                "id": "weather",
                "agent_type": "research",
                "dependencies": [],
                "required_tools": ["current_weather"],
            }
        ]
        base_state["worker_results"] = [
            {
                "task_id": "weather",
                "agent_type": "research",
                "attempt": 1,
                "status": "completed",
                "content": "partial",
                "tool_observations": [
                    {
                        "name": "current_weather",
                        "tool_call_id": "weather-source-truncated",
                        "is_error": False,
                        "content": weather_result.to_message(),
                        "result": weather_result.model_dump(mode="json"),
                    }
                ],
            }
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        reviewer_node(base_state, llm)

        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert '"source_truncated": true' in prompt.content
        assert '"projection_truncated": false' in prompt.content

    def test_reviewer_marks_large_projection_without_claiming_source_truncation(self, base_state):
        large_result = ToolExecutionResult(
            ok=True,
            executed=True,
            data={"rows": ["x" * 4000]},
        )
        base_state["subtasks"] = [
            {
                "id": "large",
                "agent_type": "research",
                "dependencies": [],
                "required_tools": ["web_search"],
            }
        ]
        base_state["worker_results"] = [
            {
                "task_id": "large",
                "agent_type": "research",
                "attempt": 1,
                "status": "completed",
                "content": "bounded summary",
                "tool_observations": [
                    {
                        "name": "web_search",
                        "tool_call_id": "large-review-projection",
                        "is_error": False,
                        "content": large_result.to_message(),
                        "result": large_result.model_dump(mode="json"),
                    }
                ],
            }
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        reviewer_node(base_state, llm)

        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert '"source_truncated": false' in prompt.content
        assert '"projection_truncated": true' in prompt.content
        assert "[Reviewer 投影已截断]" in prompt.content

    def test_reviewer_many_large_observations_fit_minimum_projection_share(self, base_state):
        large_result = ToolExecutionResult(
            ok=True,
            executed=True,
            data={"rows": ["x" * 4000]},
            message="A verbose diagnostic message that is not required for review status.",
        )
        observations = [
            {
                "name": "web_search",
                "tool_call_id": f"population-call-{index}",
                "is_error": False,
                "content": large_result.to_message(),
                "result": large_result.model_dump(mode="json"),
            }
            for index in range(16)
        ]
        base_state["subtasks"] = [
            {
                "id": "population",
                "agent_type": "research",
                "dependencies": [],
                "required_tools": ["web_search"],
            }
        ]
        base_state["worker_results"] = [
            {
                "task_id": "population",
                "agent_type": "research",
                "attempt": 1,
                "status": "completed",
                "content": "Five-city population summary.",
                "tool_observations": observations,
            }
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        reviewer_node(base_state, llm)

        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert prompt.content.count('"projection_truncated": true') == 1
        assert prompt.content.count('"source_truncated": false') == 1
        assert '"tool_call_id": "population-call-15"' in prompt.content
        assert '"omitted_redundant_or_superseded": 15' in prompt.content

    def test_reviewer_keeps_distinct_successes_and_latest_failure_per_tool(
        self, base_state
    ):
        success_a = ToolExecutionResult(ok=True, executed=True, data={"city": "A"})
        success_b = ToolExecutionResult(ok=True, executed=True, data={"city": "B"})
        old_failure = ToolExecutionResult(
            ok=False, executed=True, error_code="timeout", message="old timeout"
        )
        latest_failure = ToolExecutionResult(
            ok=False, executed=True, error_code="timeout", message="latest timeout"
        )
        base_state["subtasks"] = [
            {
                "id": "population",
                "agent_type": "research",
                "dependencies": [],
                "required_tools": ["web_search"],
            }
        ]
        base_state["worker_results"] = [
            {
                "task_id": "population",
                "agent_type": "research",
                "attempt": 1,
                "status": "completed",
                "content": "A and B population evidence",
                "tool_observations": [
                    {
                        "name": "web_search",
                        "tool_call_id": "success-a",
                        "is_error": False,
                        "content": success_a.to_message(),
                        "result": success_a.model_dump(mode="json"),
                    },
                    {
                        "name": "web_search",
                        "tool_call_id": "failure-old",
                        "is_error": True,
                        "content": old_failure.to_message(),
                        "result": old_failure.model_dump(mode="json"),
                    },
                    {
                        "name": "web_search",
                        "tool_call_id": "success-b",
                        "is_error": False,
                        "content": success_b.to_message(),
                        "result": success_b.model_dump(mode="json"),
                    },
                    {
                        "name": "web_search",
                        "tool_call_id": "failure-latest",
                        "is_error": True,
                        "content": latest_failure.to_message(),
                        "result": latest_failure.model_dump(mode="json"),
                    },
                ],
            }
        ]
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        reviewer_node(base_state, llm)

        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        assert '"tool_call_id": "success-a"' in prompt.content
        assert '"tool_call_id": "success-b"' in prompt.content
        assert '"tool_call_id": "failure-latest"' in prompt.content
        assert '"tool_call_id": "failure-old"' not in prompt.content

    def test_reviewer_adapts_total_budget_and_deduplicates_current_run_evidence(
        self, base_state
    ):
        cities = ["shanghai", "dalian", "haikou", "kunming", "tianjin"]
        subtasks = []
        worker_results = []
        synthesis_ids = []
        for index, city in enumerate(cities, start=1):
            task_id = f"population-{index}"
            synthesis_ids.append(task_id)
            subtasks.append(
                {
                    "id": task_id,
                    "agent_type": "research",
                    "objective": f"Find a sourced current population figure for {city}.",
                    "dependencies": [],
                    "allowed_tools": ["web_search"],
                    "required_tools": ["web_search"],
                    "output_format": "JSON city and population",
                    "acceptance_criteria": ["City and population are present", "Evidence is sourced"],
                }
            )
            tool_result = ToolExecutionResult(
                ok=True,
                executed=True,
                data={
                    "city": city,
                    "population": index * 1_000_000,
                    "source_excerpt": "x" * 4000,
                },
            )
            worker_results.append(
                {
                    "task_id": task_id,
                    "agent_type": "research",
                    "attempt": 1,
                    "status": "completed",
                    "content": f"{city}: {index * 1_000_000}; " + "evidence " * 80,
                    "tool_observations": [
                        {
                            "name": "web_search",
                            "tool_call_id": f"{task_id}-call-{attempt}",
                            "is_error": False,
                            "content": tool_result.to_message(),
                            "result": tool_result.model_dump(mode="json"),
                        }
                        for attempt in range(3)
                    ],
                }
            )

        final_content = json.dumps(
            {
                "cities": [
                    {"city": city, "population": index * 1_000_000}
                    for index, city in enumerate(cities, start=1)
                ]
            }
        )
        subtasks.append(
            {
                "id": "population-summary",
                "agent_type": "synthesis",
                "objective": "Combine all five city population results.",
                "dependencies": synthesis_ids,
                "allowed_tools": [],
                "required_tools": [],
                "output_format": "JSON cities array",
                "acceptance_criteria": ["All five cities are present"],
            }
        )
        worker_results.append(
            {
                "task_id": "population-summary",
                "agent_type": "synthesis",
                "attempt": 1,
                "status": "completed",
                "content": final_content,
                "tool_observations": [],
            }
        )
        base_state.update(
            {
                "task": (
                    "请随机选择世界上的五个不同城市，查询并统计它们最近可获得的人口数据。"
                    "请为每个城市给出人口数、统计年份和来源，最后按人口从高到低排序并用自然语言总结。"
                ),
                "subtasks": subtasks,
                "worker_results": worker_results,
                "execution_result": final_content,
                "plan": "Search, verify, and synthesize five independent population results. " * 30,
            }
        )
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        result = reviewer_node(base_state, llm)

        assert result["review_status"] == "pass"
        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        payload = json.loads(prompt.content.split("\n审查输入：\n", 1)[1])
        assert json.loads(
            payload["可交付内容"]["population-summary"]["content"]
        ) == json.loads(final_content)
        assert payload["最终输出"] == {
            "deduplicated_with_task_id": "population-summary"
        }
        for task_id in synthesis_ids:
            assert f'"tool_call_id": "{task_id}-call-2"' in prompt.content
            assert f'"tool_call_id": "{task_id}-call-0"' not in prompt.content

    def test_reviewer_fits_eight_stage_population_workflow(self, base_state):
        """Match the real L2-03 topology: generate, five searches, sort, synthesize."""
        cities = ["上海", "东京", "巴黎", "多伦多", "悉尼"]
        subtasks = [
            {
                "id": "generate-cities",
                "agent_type": "code",
                "objective": "随机选择五个不同国家或地区的城市并输出结构化城市列表。" * 3,
                "dependencies": [],
                "allowed_tools": ["python_repl"],
                "required_tools": ["python_repl"],
                "output_format": "JSON 城市数组，保留城市和国家或地区。" * 2,
                "acceptance_criteria": ["恰好五个城市", "城市不得重复", "覆盖不同国家或地区"],
            }
        ]
        worker_results = []
        python_result = ToolExecutionResult(
            ok=True, executed=True, data={"stdout": json.dumps(cities, ensure_ascii=False)}
        )
        worker_results.append(
            {
                "task_id": "generate-cities",
                "agent_type": "code",
                "attempt": 1,
                "status": "completed",
                "content": json.dumps({"cities": cities}, ensure_ascii=False),
                "tool_observations": [
                    {
                        "name": "python_repl",
                        "tool_call_id": "generate-call",
                        "is_error": False,
                        "content": python_result.to_message(),
                        "result": python_result.model_dump(mode="json"),
                    }
                ],
            }
        )

        research_ids = []
        for index, city in enumerate(cities, start=1):
            task_id = f"population-{index}"
            research_ids.append(task_id)
            subtasks.append(
                {
                    "id": task_id,
                    "agent_type": "research",
                    "objective": f"查询{city}最近可获得的人口、统计年份、统计口径和可靠来源。" * 3,
                    "dependencies": ["generate-cities"],
                    "allowed_tools": ["web_search"],
                    "required_tools": ["web_search"],
                    "output_format": "JSON，包含 city、population、year、scope、source_url。" * 2,
                    "acceptance_criteria": [
                        "人口为正整数",
                        "统计年份明确",
                        "来源 URL 可追溯",
                        "行政区统计口径明确",
                    ],
                }
            )
            search_result = ToolExecutionResult(
                ok=True,
                executed=True,
                data={
                    "query": f"{city} population official statistics latest",
                    "provider": "bing_rss",
                    "results": [
                        {
                            "title": f"{city} official population source {rank}",
                            "url": f"https://example.com/{index}/{rank}",
                            "snippet": (
                                f"Official population evidence for {city}, statistical year 2025. "
                                + "source context " * 30
                            ),
                        }
                        for rank in range(1, 6)
                    ],
                },
                provenance={"provider": "bing_rss", "result_count": 5},
            )
            worker_results.append(
                {
                    "task_id": task_id,
                    "agent_type": "research",
                    "attempt": 1,
                    "status": "completed",
                    "content": json.dumps(
                        {
                            "city": city,
                            "population": index * 2_000_000,
                            "year": 2025,
                            "scope": "city proper",
                            "source_url": f"https://example.com/{index}/1",
                        },
                        ensure_ascii=False,
                    ),
                    "tool_observations": [
                        {
                            "name": "web_search",
                            "tool_call_id": f"search-call-{index}",
                            "is_error": False,
                            "content": search_result.to_message(),
                            "result": search_result.model_dump(mode="json"),
                        }
                    ],
                }
            )

        subtasks.extend(
            [
                {
                    "id": "sort-populations",
                    "agent_type": "code",
                    "objective": "读取五个研究产物，按人口从高到低排序并保留年份、口径和来源。" * 3,
                    "dependencies": research_ids,
                    "allowed_tools": ["python_repl"],
                    "required_tools": ["python_repl"],
                    "output_format": "JSON 排序数组。" * 2,
                    "acceptance_criteria": ["包含全部五个城市", "降序正确", "来源字段不得丢失"],
                },
                {
                    "id": "population-summary",
                    "agent_type": "synthesis",
                    "objective": "根据排序结果形成自然语言总结并提示统计年份和行政区口径差异。" * 3,
                    "dependencies": ["sort-populations", *research_ids],
                    "allowed_tools": [],
                    "required_tools": [],
                    "output_format": "Markdown 表格和自然语言总结。" * 2,
                    "acceptance_criteria": ["五城市完整", "排序一致", "年份与口径差异明确", "来源可读"],
                },
            ]
        )
        ordered = [
            {
                "city": city,
                "population": index * 2_000_000,
                "year": 2025,
                "scope": "city proper",
                "source_url": f"https://example.com/{index}/1",
            }
            for index, city in reversed(list(enumerate(cities, start=1)))
        ]
        sort_content = json.dumps({"cities": ordered}, ensure_ascii=False)
        worker_results.extend(
            [
                {
                    "task_id": "sort-populations",
                    "agent_type": "code",
                    "attempt": 1,
                    "status": "completed",
                    "content": sort_content,
                    "tool_observations": [
                        {
                            "name": "python_repl",
                            "tool_call_id": "sort-call",
                            "is_error": False,
                            "content": python_result.to_message(),
                            "result": python_result.model_dump(mode="json"),
                        }
                    ],
                },
                {
                    "task_id": "population-summary",
                    "agent_type": "synthesis",
                    "attempt": 1,
                    "status": "completed",
                    "content": sort_content,
                    "tool_observations": [],
                },
            ]
        )
        base_state.update(
            {
                "task": (
                    "请随机选择世界上的五个不同城市，查询并统计它们最近可获得的人口数据。"
                    "请为每个城市给出人口数、统计年份和来源，最后按人口从高到低排序并用自然语言总结。"
                ),
                "subtasks": subtasks,
                "worker_results": worker_results,
                "execution_result": sort_content,
                "plan": "生成城市，分别检索可靠人口来源，排序并汇总。" * 40,
                "memory_policy": {
                    "context_max_tokens": 12000,
                    "context_output_reserve": 2000,
                },
            }
        )
        llm = _structured_llm(ReviewDecision(status="pass", feedback="ok"))

        result = reviewer_node(base_state, llm)

        assert result["review_status"] == "pass"
        prompt = llm.with_structured_output.return_value.invoke.call_args[0][0][0]
        payload = json.loads(prompt.content.split("\n审查输入：\n", 1)[1])
        assert len(payload["子任务"]) == 8
        assert len(payload["工具诊断"]) == 7
        assert all(item["review_requirements"].startswith("验收：") for item in payload["子任务"])
        assert all("observation_summary" not in item for item in payload["工具诊断"].values())
        assert all(
            payload["工具诊断"][task_id]["tool_observations"][0][
                "projection_truncated"
            ]
            is True
            for task_id in research_ids
        )
        assert payload["最终输出"] == {
            "deduplicated_with_task_id": "population-summary"
        }
