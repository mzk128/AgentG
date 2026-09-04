from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, ToolMessage

from multi_agent_system.agents import (
    OrchestrationPlan,
    ReviewDecision,
    SubTaskSpec,
    TaskRoutingDecision,
)
from multi_agent_system.graph import build_graph
from multi_agent_system.state import create_initial_state


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
        with patch("multi_agent_system.agents._create_ddgs_client") as factory:
            context = MagicMock()
            context.text.return_value = [
                {"title": "Trusted", "href": "https://source.test", "body": "Trusted source"}
            ]
            factory.return_value = context
            result = graph.invoke(create_initial_state(task, thread), config=config)
    return result


class TestComplexTeamWorkflow:
    def test_parallel_workers_then_synthesis(self):
        result = _run_graph(_mock_llm())
        latest = {}
        for item in result["worker_results"]:
            latest[item["task_id"]] = item
        assert set(latest) == {"research", "data", "synthesis"}
        assert "Trusted source" in latest["research"]["content"]
        assert "Average sales: 200.0" in latest["data"]["content"]
        assert result["execution_result"] == "Final synthesis with research and average sales"
        assert result["is_pass"] is True

    def test_stream_exposes_fan_out_before_synthesis(self):
        llm = _mock_llm()
        with patch("multi_agent_system.graph.ChatOpenAI", return_value=llm):
            graph = build_graph(_settings())
            config = {"configurable": {"thread_id": "stream-test"}}
            with patch("multi_agent_system.agents._create_ddgs_client") as factory:
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
