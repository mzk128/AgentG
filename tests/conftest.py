import os
import sys
from unittest.mock import MagicMock

import pytest

# 测试必须与用户专属的 .env 完全隔离，只使用 monkeypatch 注入的环境变量。
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from multi_agent_system.config import Settings
from multi_agent_system.state import create_initial_state


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
