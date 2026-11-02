import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from multi_agent_system.config import Settings
from multi_agent_system.state import create_initial_state


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    """Tests may explicitly mock providers, never fall through to the Internet.

    Keep loopback sockets usable for ASGI TestClient/asyncio; blocking socket
    globally would break the local event loop rather than test application IO.
    """
    def denied(*args, **kwargs):
        raise AssertionError("external HTTP must be explicitly mocked")
    async def denied_async(*args, **kwargs):
        raise AssertionError("external HTTP must be explicitly mocked")
    monkeypatch.setattr("urllib.request.urlopen", denied)
    monkeypatch.setattr("requests.sessions.Session.request", denied)
    # TestClient uses httpx's ASGI transport, not a real network transport.
    monkeypatch.setattr("httpx.HTTPTransport.handle_request", denied)
    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", denied_async)
    monkeypatch.setattr("multi_agent_system.agents._create_ddgs_client", denied)


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.model = "test-model"
    llm.temperature = 0.2
    return llm


@pytest.fixture
def base_state():
    return create_initial_state(
        "Analyze sales data and generate a summary report", "test-thread-001"
    )


@pytest.fixture
def settings():
    return Settings(
        openai_api_key="sk-test-key",
        openai_base_url="https://api.test.com/v1",
        model_name="test-model",
        use_redis=False,
        redis_url="redis://localhost:6379/0",
    )
