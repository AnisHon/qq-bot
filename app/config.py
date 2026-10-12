from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


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


@dataclass(frozen=True, slots=True)
class Settings:
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "INFO"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout: float = 60.0
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "openai/gpt-oss-120b"
    nvidia_timeout: float = 120.0
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.7-flash"
    qwen_timeout: float = 120.0
    newapi_api_key: str = ""
    newapi_base_url: str = "https://api.shirosora.cn/v1"
    newapi_model: str = "gpt-5.4-mini"
    newapi_timeout: float = 180.0
    cctq_api_key: str = ""
    cctq_base_url: str = "https://www.cctq.ai/v1"
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
    context_recent_message_limit: int = 8
    summary_trigger_message_count: int = 18
    summary_max_chars: int = 1200
    memory_query_limit: int = 10
    rag_enabled: bool = True
    rag_document_name: str = "sfm"
    rag_query_limit: int = 4
    qq_message_max_chars: int = 850
    reply_notice_delay: float = 3.0
    network_tool_timeout: float = 10.0
    network_proxy_url: str = ""
    agent_tools_enabled: bool = True
    agent_tool_max_rounds: int = 8
    agent_tool_max_calls_per_round: int = 10
    agent_tool_max_total_calls: int = 36
    agent_total_timeout: float = 110.0
    agent_tool_results_max_chars: int = 6000
    scheduler_poll_interval: float = 10.0

    natcapqq_api_base_url: str = "http://127.0.0.1:3000"
    natcapqq_api_token: str = ""
    natcapqq_callback_token: str = ""

    http_timeout: float = 30.0
    minecraft_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "Settings":
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
            context_recent_message_limit=max(2, _as_int(os.getenv("CONTEXT_RECENT_MESSAGE_LIMIT"), 8)),
            summary_trigger_message_count=max(6, _as_int(os.getenv("SUMMARY_TRIGGER_MESSAGE_COUNT"), 18)),
            summary_max_chars=max(300, _as_int(os.getenv("SUMMARY_MAX_CHARS"), 1200)),
            memory_query_limit=max(1, _as_int(os.getenv("MEMORY_QUERY_LIMIT"), 10)),
            rag_enabled=_as_bool(os.getenv("RAG_ENABLED"), True),
            rag_document_name=os.getenv("RAG_DOCUMENT_NAME", "sfm"),
            rag_query_limit=max(1, _as_int(os.getenv("RAG_QUERY_LIMIT"), 4)),
            qq_message_max_chars=max(200, _as_int(os.getenv("QQ_MESSAGE_MAX_CHARS"), 850)),
            reply_notice_delay=max(0.5, _as_float(os.getenv("REPLY_NOTICE_DELAY"), 3.0)),
            network_tool_timeout=max(3.0, _as_float(os.getenv("NETWORK_TOOL_TIMEOUT"), 10.0)),
            network_proxy_url=os.getenv("NETWORK_PROXY_URL", "").strip(),
            agent_tools_enabled=_as_bool(os.getenv("AGENT_TOOLS_ENABLED"), True),
            agent_tool_max_rounds=max(0, _as_int(os.getenv("AGENT_TOOL_MAX_ROUNDS"), 8)),
            agent_tool_max_calls_per_round=max(1, _as_int(os.getenv("AGENT_TOOL_MAX_CALLS_PER_ROUND"), 10)),
            agent_tool_max_total_calls=max(1, _as_int(os.getenv("AGENT_TOOL_MAX_TOTAL_CALLS"), 36)),
            agent_total_timeout=max(10.0, _as_float(os.getenv("AGENT_TOTAL_TIMEOUT"), 110.0)),
            agent_tool_results_max_chars=max(500, _as_int(os.getenv("AGENT_TOOL_RESULTS_MAX_CHARS"), 2500)),
            scheduler_poll_interval=max(1.0, _as_float(os.getenv("SCHEDULER_POLL_INTERVAL"), 10.0)),
            natcapqq_api_base_url=os.getenv("NATCAPQQ_API_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
            natcapqq_api_token=os.getenv("NATCAPQQ_API_TOKEN", ""),
            natcapqq_callback_token=os.getenv("NATCAPQQ_CALLBACK_TOKEN", ""),
            http_timeout=_as_float(os.getenv("HTTP_TIMEOUT"), 30.0),
            minecraft_timeout=_as_float(os.getenv("MINECRAFT_TIMEOUT"), 15.0),
        )

    def is_admin(self, qq_id: str) -> bool:
        return str(qq_id) in self.bot_admin_qq_list

    def sqlite_dir(self) -> Path:
        return self.sqlite_path.expanduser().resolve().parent
