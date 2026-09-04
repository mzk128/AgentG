import os
from dataclasses import dataclass

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_base_url: str
    model_name: str
    use_redis: bool
    redis_url: str
    max_tool_rounds: int = 4
    max_tool_calls: int = 12
    max_consecutive_errors: int = 2
    max_run_seconds: float = 120.0
    total_action_budget: int = 40


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def get_settings() -> Settings:
    use_redis = os.getenv("USE_REDIS", "false").lower() == "true"
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model_name=os.getenv("MODEL_NAME", "gpt-4o-mini"),
        use_redis=use_redis,
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        max_tool_rounds=_positive_int("MAX_TOOL_ROUNDS", 4),
        max_tool_calls=_positive_int("MAX_TOOL_CALLS", 12),
        max_consecutive_errors=_positive_int("MAX_CONSECUTIVE_ERRORS", 2),
        max_run_seconds=_positive_float("MAX_RUN_SECONDS", 120.0),
        total_action_budget=_positive_int("TOTAL_ACTION_BUDGET", 40),
    )
