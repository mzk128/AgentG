import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv, find_dotenv

if os.getenv("PYTHON_DOTENV_DISABLED", "").lower() not in {"1", "true", "yes"}:
    load_dotenv(find_dotenv())


# Stable application root, deliberately independent of shell/task cwd.
APPLICATION_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MEMORY_DB_PATH = str(APPLICATION_ROOT / ".agentg" / "memory" / "long_term.sqlite3")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_base_url: str
    model_name: str
    use_redis: bool
    redis_url: str
    web_reload: bool = False
    max_tool_rounds: int = 4
    max_tool_calls: int = 12
    max_consecutive_errors: int = 2
    max_run_seconds: float = 120.0
    total_action_budget: int = 40
    # memory.py currently accounts with conservative UTF-8 bytes. 320k units
    # is therefore roughly an 80k-100k-token mixed-language admission window,
    # while the provider model must still support the resulting request.
    context_max_tokens: int = 320000
    context_output_reserve: int = 16000
    conversation_max_tokens: int = 4000
    summary_max_tokens: int = 800
    recent_turns: int = 6
    upstream_max_tokens: int = 3000
    tool_output_max_chars: int = 12000
    python_timeout_seconds: float = 15.0
    model_request_timeout_seconds: float = 180.0
    # Total Reviewer rounds, including the initial review (not only rework).
    max_review_rounds: int = 100
    # Long-term memory is opt-in; stable IDs are host configuration, not prompts.
    long_term_memory_enabled: bool = False
    memory_db_path: str = DEFAULT_MEMORY_DB_PATH
    memory_db_busy_timeout_ms: int = 5000
    memory_user_id: str = "local-user"
    memory_project_id: str = ""
    memory_top_k: int = 5
    memory_max_tokens: int = 4000
    memory_scan_limit: int = 200
    memory_auto_extract: bool = False


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
    web_reload = os.getenv("WEB_RELOAD", "false").lower() == "true"
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model_name=os.getenv("MODEL_NAME", "gpt-4o-mini"),
        use_redis=use_redis,
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        web_reload=web_reload,
        max_tool_rounds=_positive_int("MAX_TOOL_ROUNDS", 4),
        max_tool_calls=_positive_int("MAX_TOOL_CALLS", 12),
        max_consecutive_errors=_positive_int("MAX_CONSECUTIVE_ERRORS", 2),
        max_run_seconds=_positive_float("MAX_RUN_SECONDS", 120.0),
        total_action_budget=_positive_int("TOTAL_ACTION_BUDGET", 40),
        context_max_tokens=_positive_int("CONTEXT_MAX_TOKENS", 320000),
        context_output_reserve=_positive_int("CONTEXT_OUTPUT_RESERVE", 16000),
        conversation_max_tokens=_positive_int("CONVERSATION_MAX_TOKENS", 4000),
        summary_max_tokens=_positive_int("SUMMARY_MAX_TOKENS", 800),
        recent_turns=_positive_int("MEMORY_RECENT_TURNS", 6),
        upstream_max_tokens=_positive_int("UPSTREAM_MAX_TOKENS", 3000),
        tool_output_max_chars=_positive_int("TOOL_OUTPUT_MAX_CHARS", 12000),
        python_timeout_seconds=_positive_float("PYTHON_TIMEOUT_SECONDS", 15.0),
        model_request_timeout_seconds=_positive_float(
            "MODEL_REQUEST_TIMEOUT_SECONDS", 180.0
        ),
        max_review_rounds=_positive_int("MAX_REVIEW_ROUNDS", 2),
        long_term_memory_enabled=os.getenv("LONG_TERM_MEMORY_ENABLED", "false").lower() == "true",
        memory_db_path=os.getenv("MEMORY_DB_PATH", DEFAULT_MEMORY_DB_PATH),
        memory_db_busy_timeout_ms=_positive_int("MEMORY_DB_BUSY_TIMEOUT_MS", 5000),
        memory_user_id=os.getenv("MEMORY_USER_ID", "local-user"),
        memory_project_id=os.getenv("MEMORY_PROJECT_ID", ""),
        memory_top_k=_positive_int("MEMORY_TOP_K", 5),
        memory_max_tokens=_positive_int("MEMORY_MAX_TOKENS", 4000),
        memory_scan_limit=_positive_int("MEMORY_SCAN_LIMIT", 200),
        memory_auto_extract=os.getenv("MEMORY_AUTO_EXTRACT", "false").lower() == "true",
    )
