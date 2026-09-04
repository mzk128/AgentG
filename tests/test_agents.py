import json
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from multi_agent_system.agents import (
    OrchestrationPlan,
    ReviewDecision,
    SubTaskSpec,
    TaskRoutingDecision,
    WorkerType,
    WORKER_TOOLBOX,
    _normalise_subtasks,
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
        assert "hello" in python_repl.invoke({"code": "print('hello')"})

    def test_catches_runtime_error(self):
        result = python_repl.invoke({"code": "1 / 0"})
        assert "执行报错" in result
        assert "ZeroDivisionError" in result

    def test_catches_syntax_error(self):
        assert "执行报错" in python_repl.invoke({"code": "if True print('bad')"})

    def test_no_stdout_returns_success(self):
        assert "代码执行成功" in python_repl.invoke({"code": "x = 42"})


class TestWebSearch:
    def test_search_returns_results(self):
        mock_results = [
            {"title": "Result", "href": "https://example.com", "body": "Body"}
        ]
        with patch("multi_agent_system.agents._create_ddgs_client") as factory:
            factory.return_value.text.return_value = mock_results
            result = web_search.invoke({"query": "query", "max_results": 3})
        assert "Result" in result
        assert "https://example.com" in result

    def test_search_falls_back_to_html(self):
        html = b"""<div class='result'><a class='result__title'>Title</a>
        <a class='result__url' href='https://example.com'>URL</a>
        <span class='result__snippet'>Snippet</span></div>"""
        with patch(
            "multi_agent_system.agents._create_ddgs_client",
            side_effect=Exception("fail"),
        ):
            with patch("urllib.request.urlopen") as urlopen:
                response = MagicMock()
                response.read.return_value = html
                urlopen.return_value.__enter__.return_value = response
                result = web_search.invoke({"query": "query"})
        assert "Title" in result
        assert "Snippet" in result

    def test_all_strategies_empty(self):
        with patch(
            "multi_agent_system.agents._create_ddgs_client",
            side_effect=Exception("fail"),
        ):
            with patch("urllib.request.urlopen") as urlopen:
                response = MagicMock()
                response.read.return_value = b"<html></html>"
                urlopen.return_value.__enter__.return_value = response
                result = web_search.invoke({"query": "nothing"})
        assert "未返回结果" in result

    def test_max_results_is_capped(self):
        with patch("multi_agent_system.agents._create_ddgs_client") as factory:
            factory.return_value.text.return_value = []
            with patch("urllib.request.urlopen") as urlopen:
                response = MagicMock()
                response.read.return_value = b"<html></html>"
                urlopen.return_value.__enter__.return_value = response
                web_search.invoke({"query": "query", "max_results": 100})
        factory.return_value.text.assert_called_once_with("query", max_results=10)


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
            result = json.loads(current_weather.invoke({"city": "南京"}))

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
        assert "天气查询失败" in result


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

    def test_review_confidence_range(self):
        with pytest.raises(Exception):
            ReviewDecision(status="pass", feedback="ok", confidence=1.5)

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

    def test_simple_task_builder_limits_context(self, base_state):
        base_state["routing"] = {"route": "simple", "worker_type": "code"}
        result = prepare_simple_task_node(base_state)
        assert len(result["subtasks"]) == 1
        assert result["subtasks"][0]["allowed_tools"] == ["python_repl"]
        assert result["pending_task_ids"] == ["simple-1"]


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
        assert "worked" in result["worker_results"][0]["content"]
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
        with patch("multi_agent_system.agents._create_ddgs_client") as factory:
            factory.return_value.text.return_value = [
                {"title": "Source", "href": "https://source.test", "body": "source result"}
            ]
            result = research_worker_node(
                _worker_input("research", allowed_tools=["web_search"]), llm
            )
        assert "source result" in result["worker_results"][0]["content"]
        bound_tools = llm.bind_tools.call_args[0][0]
        assert [tool.name for tool in bound_tools] == ["web_search"]

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
        assert "拒绝未授权" in result["worker_results"][0]["content"]

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

    def test_latest_worker_result_uses_highest_attempt(self):
        results = [
            {"task_id": "a", "attempt": 1, "content": "old"},
            {"task_id": "a", "attempt": 2, "content": "new"},
        ]
        assert latest_worker_results(results)["a"]["content"] == "new"


class TestReviewer:
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
