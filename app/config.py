from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re

from app.context.budget import ContextWindowProfile


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_int(value: str | None, default: int = 0) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, _as_int(value, default)))


def _optional_positive_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("configured budget limits must be positive integers") from exc
    if parsed < 1:
        raise ValueError("configured budget limits must be positive integers")
    return parsed


def _as_float(value: str | None, default: float = 0.0) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def _parse_local_git_repositories(value: str | None) -> tuple[tuple[str, Path], ...]:
    """Parse the administrator-only ``repo_id=/absolute/root;...`` allowlist."""
    if not value or not value.strip():
        return ()
    parsed: dict[str, Path] = {}
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            raise ValueError("LOCAL_GIT_REPOSITORIES must contain repo_id=/absolute/path entries")
        repo_id, raw_path = (part.strip() for part in entry.split("=", 1))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", repo_id) or repo_id in {".", ".."}:
            raise ValueError("LOCAL_GIT_REPOSITORIES contains an invalid repository id")
        if repo_id in parsed:
            raise ValueError("LOCAL_GIT_REPOSITORIES contains a duplicate repository id")
        root = Path(raw_path).expanduser()
        if not raw_path or not root.is_absolute():
            raise ValueError("LOCAL_GIT_REPOSITORIES roots must be absolute paths")
        parsed[repo_id] = root
    return tuple(sorted(parsed.items()))


def _parse_context_window_profiles(value: str | None) -> tuple[ContextWindowProfile, ...]:
    if not value or not value.strip():
        return ()
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM_CONTEXT_WINDOW_PROFILES must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM_CONTEXT_WINDOW_PROFILES must be a JSON object")
    profiles = []
    for key, raw_profile in data.items():
        if not isinstance(key, str) or ":" not in key or not isinstance(raw_profile, dict):
            raise ValueError("context profiles must use provider:model keys and object values")
        provider, model = key.split(":", 1)
        window = raw_profile.get("context_window_tokens")
        status = raw_profile.get("status", "operator_declared")
        max_output = raw_profile.get("max_output_tokens")
        preferred_output = raw_profile.get("preferred_output_tokens")
        if isinstance(window, bool) or not isinstance(window, int):
            raise ValueError("context profile context_window_tokens must be an integer")
        if not isinstance(status, str):
            raise ValueError("context profile status must be a string")
        for name, output_limit in (
            ("max_output_tokens", max_output),
            ("preferred_output_tokens", preferred_output),
        ):
            if output_limit is not None and (isinstance(output_limit, bool) or not isinstance(output_limit, int)):
                raise ValueError(f"context profile {name} must be an integer")
        profiles.append(
            ContextWindowProfile(provider, model, window, status, max_output, preferred_output)
        )
    return tuple(profiles)


@dataclass(frozen=True, slots=True)
class Settings:
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "INFO"

    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = field(default="https://api.deepseek.com", repr=False)
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout: float = 60.0
    nvidia_api_key: str = field(default="", repr=False)
    nvidia_base_url: str = field(default="https://integrate.api.nvidia.com/v1", repr=False)
    nvidia_model: str = "openai/gpt-oss-120b"
    nvidia_timeout: float = 120.0
    qwen_api_key: str = field(default="", repr=False)
    qwen_base_url: str = field(default="https://dashscope.aliyuncs.com/compatible-mode/v1", repr=False)
    qwen_model: str = "qwen3.7-flash"
    qwen_timeout: float = 120.0
    newapi_api_key: str = field(default="", repr=False)
    newapi_base_url: str = field(default="https://api.shirosora.cn/v1", repr=False)
    newapi_model: str = "gpt-5.4-mini"
    newapi_timeout: float = 180.0
    cctq_api_key: str = field(default="", repr=False)
    cctq_base_url: str = field(default="https://www.cctq.ai/v1", repr=False)
    cctq_model: str = "gpt-6-luna"
    cctq_timeout: float = 180.0
    default_model_provider: str = "nvidia"
    default_temperature: float = 0.65
    default_presence_penalty: float = 0.3
    default_frequency_penalty: float = 0.2

    bot_trigger_mode: str = "at"
    bot_prefix: str = "/bot"
    bot_qq_id: str = ""
    bot_admin_qq_list: tuple[str, ...] = ()
    bot_default_name: str = "胖虎"
    bot_name_cache_seconds: int = 3600

    sqlite_path: Path = Path("data/bot.db")
    message_history_limit: int = 80
    context_history_max_messages_per_group: int = 20_000
    context_history_max_bytes_per_group: int = 512 * 1024 * 1024
    context_history_retention_days: int = 30
    context_history_auto_prune: bool = False
    context_recent_message_limit: int = 24
    context_recent_candidate_limit: int = 120
    context_history_fts_limit: int = 24
    context_history_final_limit: int = 12
    context_history_neighbor_limit: int = 32
    context_history_max_reply_depth: int = 3
    context_history_retrieval_enabled: bool = True
    context_max_user_input_bytes: int = 65_536
    summary_trigger_message_count: int = 18
    summary_trigger_token_count: int = 3000
    summary_max_input_bytes: int = 262_144
    summary_max_batch_rows: int = 100
    summary_max_chars: int = 1200
    memory_query_limit: int = 10
    rag_enabled: bool = True
    rag_document_name: str = "sfm"
    rag_query_limit: int = 4
    qq_message_max_chars: int = 850
    qq_message_workers: int = 4
    qq_message_queue_capacity: int = 64
    shutdown_drain_timeout: float = 15.0
    storage_sqlite_workers: int = 1
    storage_vector_workers: int = 1
    storage_queue_capacity: int = 16
    llm_max_concurrent_requests: int = 4
    llm_budget_global_requests_per_day: int | None = None
    llm_budget_group_requests_per_day: int | None = None
    llm_budget_user_requests_per_day: int | None = None
    llm_budget_global_tokens_per_day: int | None = None
    llm_budget_group_tokens_per_day: int | None = None
    llm_budget_user_tokens_per_day: int | None = None
    llm_budget_default_max_completion_tokens: int | None = None
    llm_budget_reservation_lease_seconds: int = 300
    llm_context_budget_mode: str = "off"
    llm_context_window_profiles: tuple[ContextWindowProfile, ...] = ()
    llm_context_default_output_tokens: int = 2048
    reply_notice_delay: float = 3.0
    network_tool_timeout: float = 10.0
    network_proxy_url: str = field(default="", repr=False)
    agent_tools_enabled: bool = True
    agent_tool_max_rounds: int = 8
    agent_tool_max_calls_per_round: int = 10
    agent_tool_max_total_calls: int = 36
    agent_total_timeout: float = 110.0
    agent_tool_results_max_chars: int = 6000
    scheduler_poll_interval: float = 10.0
    local_git_repositories: tuple[tuple[str, Path], ...] = field(default=(), repr=False)

    natcapqq_api_base_url: str = field(default="http://127.0.0.1:3000", repr=False)
    natcapqq_api_token: str = field(default="", repr=False)
    # OneBot callbacks remain unavailable until this secret is configured.
    natcapqq_callback_token: str = field(default="", repr=False)

    http_timeout: float = 30.0
    minecraft_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "Settings":
        agent_timeout = max(10.0, _as_float(os.getenv("AGENT_TOTAL_TIMEOUT"), 110.0))
        shutdown_drain_timeout = max(1.0, _as_float(os.getenv("SHUTDOWN_DRAIN_TIMEOUT"), 15.0))
        provider_timeouts = (
            _as_float(os.getenv("DEEPSEEK_TIMEOUT"), 60.0),
            _as_float(os.getenv("NVIDIA_TIMEOUT"), 120.0),
            _as_float(os.getenv("QWEN_TIMEOUT"), 120.0),
            _as_float(os.getenv("NEWAPI_TIMEOUT"), 180.0),
            _as_float(os.getenv("CCTQ_TIMEOUT"), 180.0),
        )
        reservation_lease_minimum = int(
            max(agent_timeout, *provider_timeouts) + shutdown_drain_timeout + 30
        )
        context_budget_mode = os.getenv("LLM_CONTEXT_BUDGET_MODE", "off").strip().casefold()
        if context_budget_mode not in {"off", "shadow", "enforce"}:
            raise ValueError("LLM_CONTEXT_BUDGET_MODE must be off, shadow, or enforce")
        return cls(
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=_as_int(os.getenv("APP_PORT"), 8080),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            deepseek_timeout=_as_float(os.getenv("DEEPSEEK_TIMEOUT"), 60.0),
            nvidia_api_key=os.getenv("NVIDIA_API_KEY", "").strip(),
            nvidia_base_url=os.getenv(
                "NVIDIA_BASE_URL",
                "https://integrate.api.nvidia.com/v1",
            ).rstrip("/"),
            nvidia_model=os.getenv("NVIDIA_MODEL", "openai/gpt-oss-120b").strip()
            or "openai/gpt-oss-120b",
            nvidia_timeout=_as_float(os.getenv("NVIDIA_TIMEOUT"), 120.0),
            qwen_api_key=os.getenv("QWEN_API_KEY", "").strip(),
            qwen_base_url=os.getenv(
                "QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ).rstrip("/"),
            qwen_model=os.getenv("QWEN_MODEL", "qwen3.7-flash").strip() or "qwen3.7-flash",
            qwen_timeout=_as_float(os.getenv("QWEN_TIMEOUT"), 120.0),
            newapi_api_key=os.getenv("NEWAPI_API_KEY", "").strip(),
            newapi_base_url=os.getenv("NEWAPI_BASE_URL", "https://api.shirosora.cn/v1").rstrip("/"),
            newapi_model=os.getenv("NEWAPI_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini",
            newapi_timeout=_as_float(os.getenv("NEWAPI_TIMEOUT"), 180.0),
            cctq_api_key=os.getenv("CCTQ_API_KEY", "").strip(),
            cctq_base_url=os.getenv("CCTQ_BASE_URL", "https://www.cctq.ai/v1").rstrip("/"),
            cctq_model=os.getenv("CCTQ_MODEL", "gpt-6-luna").strip() or "gpt-6-luna",
            cctq_timeout=_as_float(os.getenv("CCTQ_TIMEOUT"), 180.0),
            default_model_provider=os.getenv("DEFAULT_MODEL_PROVIDER", "nvidia").strip().lower() or "nvidia",
            default_temperature=_as_float(os.getenv("DEFAULT_TEMPERATURE"), 0.65),
            default_presence_penalty=_as_float(os.getenv("DEFAULT_PRESENCE_PENALTY"), 0.3),
            default_frequency_penalty=_as_float(os.getenv("DEFAULT_FREQUENCY_PENALTY"), 0.2),
            bot_trigger_mode=os.getenv("BOT_TRIGGER_MODE", "at").strip().lower(),
            bot_prefix=os.getenv("BOT_PREFIX", "/bot"),
            bot_qq_id=os.getenv("BOT_QQ_ID", "").strip(),
            bot_admin_qq_list=tuple(_split_csv(os.getenv("BOT_ADMIN_QQ_LIST"))),
            bot_default_name=os.getenv("BOT_DEFAULT_NAME", "胖虎").strip() or "胖虎",
            bot_name_cache_seconds=max(60, _as_int(os.getenv("BOT_NAME_CACHE_SECONDS"), 3600)),
            sqlite_path=Path(os.getenv("SQLITE_PATH", "data/bot.db")),
            message_history_limit=max(20, _as_int(os.getenv("MESSAGE_HISTORY_LIMIT"), 80)),
            context_history_max_messages_per_group=_bounded_int(
                os.getenv("CONTEXT_HISTORY_MAX_MESSAGES_PER_GROUP"), 20_000, 1, 10_000_000
            ),
            context_history_max_bytes_per_group=_bounded_int(
                os.getenv("CONTEXT_HISTORY_MAX_BYTES_PER_GROUP"), 512 * 1024 * 1024, 65_536,
                16 * 1024 * 1024 * 1024,
            ),
            context_history_retention_days=_bounded_int(
                os.getenv("CONTEXT_HISTORY_RETENTION_DAYS"), 30, 1, 3650
            ),
            context_history_auto_prune=_as_bool(os.getenv("CONTEXT_HISTORY_AUTO_PRUNE"), False),
            context_recent_message_limit=_bounded_int(
                os.getenv("CONTEXT_RECENT_MESSAGE_LIMIT"), 24, 2, 120
            ),
            context_recent_candidate_limit=_bounded_int(
                os.getenv("CONTEXT_RECENT_CANDIDATE_LIMIT"), 120, 24, 500
            ),
            context_history_fts_limit=_bounded_int(
                os.getenv("CONTEXT_HISTORY_FTS_LIMIT"), 24, 1, 100
            ),
            context_history_final_limit=_bounded_int(
                os.getenv("CONTEXT_HISTORY_FINAL_LIMIT"), 12, 1, 48
            ),
            context_history_neighbor_limit=_bounded_int(
                os.getenv("CONTEXT_HISTORY_NEIGHBOR_LIMIT"), 32, 1, 100
            ),
            context_history_max_reply_depth=_bounded_int(
                os.getenv("CONTEXT_HISTORY_MAX_REPLY_DEPTH"), 3, 0, 3
            ),
            context_history_retrieval_enabled=_as_bool(
                os.getenv("CONTEXT_HISTORY_RETRIEVAL_ENABLED"), True
            ),
            context_max_user_input_bytes=_bounded_int(
                os.getenv("CONTEXT_MAX_USER_INPUT_BYTES"), 65_536, 1_024, 1_048_576
            ),
            summary_trigger_message_count=max(6, _as_int(os.getenv("SUMMARY_TRIGGER_MESSAGE_COUNT"), 18)),
            summary_trigger_token_count=_bounded_int(
                os.getenv("SUMMARY_TRIGGER_TOKEN_COUNT"), 3000, 256, 100_000
            ),
            summary_max_input_bytes=_bounded_int(
                os.getenv("SUMMARY_MAX_INPUT_BYTES"), 262_144, 4096, 2_000_000
            ),
            summary_max_batch_rows=_bounded_int(
                os.getenv("SUMMARY_MAX_BATCH_ROWS"), 100, 1, 500
            ),
            summary_max_chars=max(300, _as_int(os.getenv("SUMMARY_MAX_CHARS"), 1200)),
            memory_query_limit=max(1, _as_int(os.getenv("MEMORY_QUERY_LIMIT"), 10)),
            rag_enabled=_as_bool(os.getenv("RAG_ENABLED"), True),
            rag_document_name=os.getenv("RAG_DOCUMENT_NAME", "sfm"),
            rag_query_limit=max(1, _as_int(os.getenv("RAG_QUERY_LIMIT"), 4)),
            qq_message_max_chars=max(200, _as_int(os.getenv("QQ_MESSAGE_MAX_CHARS"), 850)),
            qq_message_workers=max(1, _as_int(os.getenv("QQ_MESSAGE_WORKERS"), 4)),
            qq_message_queue_capacity=max(1, _as_int(os.getenv("QQ_MESSAGE_QUEUE_CAPACITY"), 64)),
            shutdown_drain_timeout=shutdown_drain_timeout,
            storage_sqlite_workers=_bounded_int(os.getenv("STORAGE_SQLITE_WORKERS"), 1, 1, 4),
            storage_vector_workers=_bounded_int(os.getenv("STORAGE_VECTOR_WORKERS"), 1, 1, 1),
            storage_queue_capacity=_bounded_int(os.getenv("STORAGE_QUEUE_CAPACITY"), 16, 1, 128),
            llm_max_concurrent_requests=_bounded_int(os.getenv("LLM_MAX_CONCURRENT_REQUESTS"), 4, 1, 32),
            llm_budget_global_requests_per_day=_optional_positive_int(
                os.getenv("LLM_BUDGET_GLOBAL_REQUESTS_PER_DAY")
            ),
            llm_budget_group_requests_per_day=_optional_positive_int(
                os.getenv("LLM_BUDGET_GROUP_REQUESTS_PER_DAY")
            ),
            llm_budget_user_requests_per_day=_optional_positive_int(
                os.getenv("LLM_BUDGET_USER_REQUESTS_PER_DAY")
            ),
            llm_budget_global_tokens_per_day=_optional_positive_int(
                os.getenv("LLM_BUDGET_GLOBAL_TOKENS_PER_DAY")
            ),
            llm_budget_group_tokens_per_day=_optional_positive_int(
                os.getenv("LLM_BUDGET_GROUP_TOKENS_PER_DAY")
            ),
            llm_budget_user_tokens_per_day=_optional_positive_int(
                os.getenv("LLM_BUDGET_USER_TOKENS_PER_DAY")
            ),
            llm_budget_default_max_completion_tokens=_optional_positive_int(
                os.getenv("LLM_BUDGET_DEFAULT_MAX_COMPLETION_TOKENS")
            ),
            llm_context_budget_mode=context_budget_mode,
            llm_context_window_profiles=_parse_context_window_profiles(
                os.getenv("LLM_CONTEXT_WINDOW_PROFILES")
            ),
            llm_context_default_output_tokens=_bounded_int(
                os.getenv("LLM_CONTEXT_DEFAULT_OUTPUT_TOKENS"), 2048, 128, 65536
            ),
            llm_budget_reservation_lease_seconds=max(
                300,
                _as_int(os.getenv("LLM_BUDGET_RESERVATION_LEASE_SECONDS"), 300),
                reservation_lease_minimum,
            ),
            reply_notice_delay=max(0.5, _as_float(os.getenv("REPLY_NOTICE_DELAY"), 3.0)),
            network_tool_timeout=max(3.0, _as_float(os.getenv("NETWORK_TOOL_TIMEOUT"), 10.0)),
            network_proxy_url=os.getenv("NETWORK_PROXY_URL", "").strip(),
            agent_tools_enabled=_as_bool(os.getenv("AGENT_TOOLS_ENABLED"), True),
            agent_tool_max_rounds=max(0, _as_int(os.getenv("AGENT_TOOL_MAX_ROUNDS"), 8)),
            agent_tool_max_calls_per_round=max(1, _as_int(os.getenv("AGENT_TOOL_MAX_CALLS_PER_ROUND"), 10)),
            agent_tool_max_total_calls=max(1, _as_int(os.getenv("AGENT_TOOL_MAX_TOTAL_CALLS"), 36)),
            agent_total_timeout=agent_timeout,
            agent_tool_results_max_chars=max(500, _as_int(os.getenv("AGENT_TOOL_RESULTS_MAX_CHARS"), 2500)),
            scheduler_poll_interval=max(1.0, _as_float(os.getenv("SCHEDULER_POLL_INTERVAL"), 10.0)),
            local_git_repositories=_parse_local_git_repositories(
                os.getenv("LOCAL_GIT_REPOSITORIES")
            ),
            natcapqq_api_base_url=os.getenv("NATCAPQQ_API_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
            natcapqq_api_token=os.getenv("NATCAPQQ_API_TOKEN", ""),
            natcapqq_callback_token=os.getenv("NATCAPQQ_CALLBACK_TOKEN", "").strip(),
            http_timeout=_as_float(os.getenv("HTTP_TIMEOUT"), 30.0),
            minecraft_timeout=_as_float(os.getenv("MINECRAFT_TIMEOUT"), 15.0),
        )

    def is_admin(self, qq_id: str) -> bool:
        return str(qq_id) in self.bot_admin_qq_list

    def sqlite_dir(self) -> Path:
        return self.sqlite_path.expanduser().resolve().parent
