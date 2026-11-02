import json
from unittest.mock import MagicMock, patch

import pytest

from langchain_core.messages import AIMessage, ToolMessage

from multi_agent_system.agents import (
    OrchestrationPlan,
    ReviewDecision,
    SubTaskSpec,
    TaskRoutingDecision,
)
from multi_agent_system.artifact_runtime import ArtifactPatch, RevisionInstruction
from multi_agent_system.graph import build_graph
from multi_agent_system.state import create_initial_state
from multi_agent_system.tool_runtime import ToolExecutionResult


def _settings(**overrides):
    from multi_agent_system.config import Settings

    values = {
        "openai_api_key": "sk-integration-test",
        "openai_base_url": "https://api.test.com/v1",
        "model_name": "test-model",
        "use_redis": False,
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(overrides)
    return Settings(**values)


def _team_plan():
    return OrchestrationPlan(
        summary="Research and analyze in parallel, then synthesize",
        subtasks=[
            SubTaskSpec(
                id="research",
                agent_type="research",
                objective="Find source data",
                allowed_tools=["web_search"],
            ),
            SubTaskSpec(
                id="data",
                agent_type="data",
                objective="Calculate average sales",
                allowed_tools=["python_repl"],
            ),
            SubTaskSpec(
                id="synthesis",
                agent_type="synthesis",
                objective="Combine results",
                dependencies=["research", "data"],
            ),
        ],
    )


def _mock_llm(route="team", review_decisions=None):
    llm = MagicMock()
    reviews = iter(review_decisions or [ReviewDecision(status="pass", feedback="ok")])

    def structured_factory(schema, **_kwargs):
        runner = MagicMock()
        if schema is TaskRoutingDecision:
            runner.invoke.return_value = TaskRoutingDecision(
                route=route,
                worker_type="code" if route == "simple" else "synthesis",
                reason="integration route",
                allowed_tools=["python_repl"] if route == "simple" else [],
            )
        elif schema is OrchestrationPlan:
            runner.invoke.return_value = _team_plan()
        elif schema is ReviewDecision:
            runner.invoke.side_effect = lambda *_args, **_kw: next(reviews)
        else:
            raise AssertionError(f"Unexpected schema: {schema}")
        return runner

    llm.with_structured_output.side_effect = structured_factory

    def tool_worker_response(messages):
        prompt = messages[0].content
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="Worker final answer based on tool observation")
        response = AIMessage(content="")
        if "ResearchAgent" in prompt:
            response.tool_calls = [
                {"name": "web_search", "args": {"query": "sales source", "max_results": 2}}
            ]
        else:
            response.tool_calls = [
                {
                    "name": "python_repl",
                    "args": {"code": "print('Average sales: 200.0')"},
                }
            ]
        return response

    llm.bind_tools.return_value.invoke.side_effect = tool_worker_response

    def plain_worker_response(messages):
        prompt = messages[0].content
        if "本次定向返工反馈" in prompt:
            return AIMessage(content="Revised synthesis with verified data")
        return AIMessage(content="Final synthesis with research and average sales")

    llm.invoke.side_effect = plain_worker_response
    return llm


def _run_graph(llm, task="Analyze sales using research and data", thread="integration-1"):
    with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
        graph = build_graph(_settings())
        config = {"configurable": {"thread_id": thread}}
        with patch("multi_agent_system.agents._create_ddgs_client") as factory, patch(
            "multi_agent_system.agents._bing_rss_search", return_value=[]
        ), patch("multi_agent_system.agents._baidu_html_search", return_value=[]), patch(
            "multi_agent_system.agents._duckduckgo_html_search", return_value=[]
        ):
            context = MagicMock()
            context.text.return_value = [
                {"title": "Trusted", "href": "https://source.test", "body": "Trusted source"}
            ]
            factory.return_value = context
            result = graph.invoke(create_initial_state(task, thread), config=config)
    return result


class TestComplexTeamWorkflow:
    def test_three_city_weather_uses_required_tools_then_summarizes(self, tmp_path):
        import json

        cities = ["北京", "南京", "杭州"]
        plan = OrchestrationPlan(summary="先选取三个城市，并行查询当前天气，再汇总。", subtasks=[
            SubTaskSpec(id="cities", agent_type="code", objective="随机选择三个城市并输出名称数组",
                        allowed_tools=["python_repl"], required_tools=["python_repl"]),
            *[SubTaskSpec(id=f"weather-{index}", agent_type="research", objective=f"查询{city}当前天气",
                          dependencies=["cities"], allowed_tools=["current_weather"], required_tools=["current_weather"])
              for index, city in enumerate(cities)],
            SubTaskSpec(id="summary", agent_type="synthesis", objective="汇总三个城市当前天气，使用自然语言回答",
                        dependencies=[f"weather-{index}" for index in range(3)]),
        ])
        llm = MagicMock()

        def structured(schema, **_kwargs):
            runner = MagicMock()
            runner.invoke.return_value = {
                TaskRoutingDecision: TaskRoutingDecision(route="team", reason="多个城市查询"),
                OrchestrationPlan: plan,
                ReviewDecision: ReviewDecision(status="pass", feedback="三个城市均有查询证据"),
            }[schema]
            return runner

        def worker(messages):
            observed = [message for message in messages if isinstance(message, ToolMessage)]
            if observed:
                payload = json.loads(observed[-1].content)
                assert payload["ok"] is True
                return AIMessage(content=json.dumps(payload["data"], ensure_ascii=False))
            prompt = messages[0].content
            if "CodeAgent" in prompt:
                return AIMessage(content="", tool_calls=[{
                    "id": "choose-cities", "name": "python_repl",
                    "args": {"code": f"import random, json; print(json.dumps(random.sample({cities!r}, 3), ensure_ascii=False))"},
                }])
            city = next(city for city in cities if f"查询{city}当前天气" in prompt)
            return AIMessage(content="", tool_calls=[{
                "id": f"weather-{city}", "name": "current_weather", "args": {"city": city},
            }])

        llm.with_structured_output.side_effect = structured
        llm.bind_tools.return_value.invoke.side_effect = worker
        llm.invoke.return_value = AIMessage(content="北京、南京和杭州当前均为晴天，气温为 25°C。")
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm), patch(
            "multi_agent_system.agents.current_weather.func",
            side_effect=lambda city: ToolExecutionResult(ok=True, data={"city": city, "condition": "晴天", "temperature": 25}),
        ) as weather:
            graph = build_graph(_settings())
            result = graph.invoke(
                create_initial_state("请查询当前的天气状况，查询地点可以为随机生成三个城市。", "three-cities",
                                     workspace_path=str(tmp_path)),
                config={"configurable": {"thread_id": "three-cities"}},
            )
        assert result["is_pass"] is True
        assert not result["termination"]
        assert weather.call_count == 3
        assert len(result["worker_results"]) == 5
        assert sum(item["usage"]["tool_calls"] for item in result["worker_results"]) == 4
        assert "25°C" in result["execution_result"]

    def test_parallel_workers_then_synthesis(self):
        result = _run_graph(_mock_llm())
        latest = {}
        for item in result["worker_results"]:
            latest[item["task_id"]] = item
        assert set(latest) == {"research", "data", "synthesis"}
        assert latest["research"]["content"] == "Worker final answer based on tool observation"
        assert latest["data"]["content"] == "Worker final answer based on tool observation"
        assert "Trusted source" in latest["research"]["tool_observations"][0]["content"]
        assert "Average sales: 200.0" in latest["data"]["tool_observations"][0]["content"]
        assert result["execution_result"] == "Final synthesis with research and average sales"
        assert result["is_pass"] is True

    def test_stream_exposes_fan_out_before_synthesis(self):
        llm = _mock_llm()
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
            graph = build_graph(_settings())
            config = {"configurable": {"thread_id": "stream-test"}}
            with patch("multi_agent_system.agents._create_ddgs_client") as factory, patch(
                "multi_agent_system.agents._bing_rss_search", return_value=[]
            ), patch("multi_agent_system.agents._baidu_html_search", return_value=[]), patch(
                "multi_agent_system.agents._duckduckgo_html_search", return_value=[]
            ):
                context = MagicMock()
                context.text.return_value = [
                    {"title": "Source", "href": "https://source.test", "body": "source"}
                ]
                factory.return_value = context
                nodes = [
                    node
                    for output in graph.stream(
                        create_initial_state("complex task", "stream-test"), config=config
                    )
                    for node in output
                ]
        assert nodes.index("task_router") < nodes.index("orchestrator")
        assert nodes.index("research_worker") < nodes.index("synthesis_worker")
        assert nodes.index("data_worker") < nodes.index("synthesis_worker")
        assert nodes.index("synthesis_worker") < nodes.index("reviewer")


class TestSimpleDirectWorkflow:
    def test_simple_task_skips_orchestrator(self):
        llm = _mock_llm(route="simple")
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
            graph = build_graph(_settings())
            config = {"configurable": {"thread_id": "simple-test"}}
            nodes = [
                node
                for output in graph.stream(
                    create_initial_state("print an average", "simple-test"), config=config
                )
                for node in output
            ]
            result = dict(graph.get_state(config).values)
        assert "prepare_simple" in nodes
        assert "orchestrator" not in nodes
        assert "code_worker" in nodes
        assert result["is_pass"] is True


class TestResourceTerminationWorkflow:
    def test_global_action_budget_reaches_blocked_terminal_state(self):
        llm = _mock_llm(route="team")
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
            graph = build_graph(_settings(total_action_budget=1))
            result = graph.invoke(
                create_initial_state("complex task", "budget-test"),
                config={"configurable": {"thread_id": "budget-test"}},
            )
        assert result["termination"]["reason"] == "total_action_budget_exhausted"
        assert result["review_status"] == "blocked"
        assert result["is_pass"] is False


class TestTargetedRevisionWorkflow:
    @pytest.mark.parametrize("rounds", [3, 4])
    def test_configured_three_reviews_rework_only_affected_branch(self, rounds):
        decisions = [
            *[ReviewDecision(status="revise", revision_targets=["research"], feedback="Improve source")
              for _ in range(rounds - 1)],
            ReviewDecision(status="pass", feedback="fixed"),
        ]
        llm = _mock_llm(review_decisions=decisions)
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
            graph = build_graph(_settings(max_review_rounds=rounds))
            with patch("multi_agent_system.agents.web_search.func", return_value=ToolExecutionResult(
                ok=True, data=[{"title": "Source", "url": "https://source.test", "snippet": "Verified"}],
                executed=True,
            )):
                result = graph.invoke(
                    create_initial_state("Analyze data", "three-reviews"),
                    config={"configurable": {"thread_id": "three-reviews"}},
                )
        assert result["is_pass"] is True
        assert result["attempts"] == rounds
        assert result["worker_attempts"] == {"research": rounds, "data": 1, "synthesis": rounds}
        assert result["resource_policy"]["max_review_rounds"] == rounds

    def test_only_target_and_downstream_are_repeated(self):
        decisions = [
            ReviewDecision(
                status="revise",
                revision_targets=["research"],
                feedback="Use a stronger source",
            ),
            ReviewDecision(status="pass", feedback="fixed"),
        ]
        result = _run_graph(_mock_llm(review_decisions=decisions), thread="revision-test")
        assert result["worker_attempts"]["research"] == 2
        assert result["worker_attempts"]["synthesis"] == 2
        assert result["worker_attempts"]["data"] == 1
        assert result["attempts"] == 2
        assert result["is_pass"] is True
        assert result["execution_result"] == "Revised synthesis with verified data"

    def test_patch_revision_preserves_old_version_and_applies_exact_text_change(self):
        decisions = iter([
            ReviewDecision(
                status="revise",
                revision_targets=["simple-1"],
                revision_instructions=[RevisionInstruction(
                    task_id="simple-1",
                    mode="patch",
                    allowed_paths=["/text"],
                    requested_changes=["Update only Paris"],
                )],
                feedback="Paris temperature changed",
            ),
            ReviewDecision(status="pass", feedback="Exact change verified"),
        ])
        llm = MagicMock()

        def structured(schema, **_kwargs):
            runner = MagicMock()
            if schema is TaskRoutingDecision:
                runner.invoke.return_value = TaskRoutingDecision(
                    route="simple", worker_type="synthesis", reason="one report"
                )
            elif schema is ReviewDecision:
                runner.invoke.side_effect = lambda *_args, **_kw: next(decisions)
            else:
                raise AssertionError(f"Unexpected schema: {schema}")
            return runner

        def worker(messages):
            prompt = messages[0].content
            if "上一版可交付产物：" not in prompt:
                return AIMessage(content="Paris is 18 C. Tokyo is 26 C.")
            artifact_text = prompt.split("上一版可交付产物：", 1)[1].split(
                "\n类型化修订指令：", 1
            )[0]
            artifact = json.loads(artifact_text)
            revision = ArtifactPatch(
                base_version=artifact["version"],
                base_content_hash=artifact["content_hash"],
                operations=[{
                    "op": "replace_text", "path": "/text",
                    "old_text": "Paris is 18 C.",
                    "new_text": "Paris is 19 C.",
                }],
                change_summary="Updated only Paris",
            )
            return AIMessage(content=json.dumps(revision.model_dump(mode="json")))

        llm.with_structured_output.side_effect = structured
        llm.invoke.side_effect = worker
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
            graph = build_graph(_settings())
            result = graph.invoke(
                create_initial_state("Update a city report", "patch-revision"),
                config={"configurable": {"thread_id": "patch-revision"}},
            )

        versions = [
            item for item in result["worker_results"]
            if item["task_id"] == "simple-1"
        ]
        assert [item["artifact_version"] for item in versions] == [1, 2]
        assert versions[0]["content"] == "Paris is 18 C. Tokyo is 26 C."
        assert versions[1]["content"] == "Paris is 19 C. Tokyo is 26 C."
        assert versions[1]["changed_paths"] == ["/text"]
        assert result["execution_result"] == "Paris is 19 C. Tokyo is 26 C."
        assert result["is_pass"] is True
