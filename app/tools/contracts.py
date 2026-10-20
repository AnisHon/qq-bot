from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Awaitable, Callable, Literal, Mapping, TYPE_CHECKING

from app.models import SearchResult
from app.progress.events import EventSink, TaskIdentity, ToolCategory
from app.usage_budget import BudgetScope, ToolUsage

if TYPE_CHECKING:
    from app.usage_budget import BudgetController


class ToolStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    NO_RESULTS = "no_results"
    DATA_UNAVAILABLE = "data_unavailable"
    NOT_INDEXED = "not_indexed"
    VERSION_MISMATCH = "version_mismatch"
    INVALID_ARGUMENT = "invalid_argument"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


class ToolAccess(str, Enum):
    READ_ONLY = "read_only"
    NETWORK_READ = "network_read"
    LOCAL_READ = "local_read"
    VALIDATION_PROCESS = "validation_process"


class ToolOutputKind(str, Enum):
    TEXT = "text"
    EVIDENCE = "evidence"
    STRUCTURED = "structured"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    source_id: str
    title: str
    locator: str
    revision: str | None = None
    pack_id: str | None = None
    pack_version: str | None = None
    confidence: str | None = None
    completeness: Literal["complete", "partial", "reference_only", "unavailable"] = "reference_only"
    content_hash: str | None = None
    query_conditions: tuple[tuple[str, str], ...] = ()
    provider: str | None = None
    retrieved_at: str | None = None
    published_at: str | None = None


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False
    field: str | None = None


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Immutable authorization facts captured for one tool execution."""

    permissions: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    access: ToolAccess = ToolAccess.READ_ONLY
    allowed_roles: frozenset[str] | None = None
    required_permissions: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] | None = None
    allowed_users: frozenset[str] | None = None

    def denial_reason(self, name: str, context: ToolContext) -> str | None:
        snapshot = context.policy_snapshot
        if snapshot.allowed_tools is not None and name not in snapshot.allowed_tools:
            return "tool is not allowed by the request policy"
        if self.allowed_roles is not None and not self.allowed_roles.intersection(snapshot.roles):
            return "caller role is not allowed"
        if not self.required_permissions.issubset(snapshot.permissions):
            return "required permission is missing"
        if self.allowed_groups is not None and context.group_id not in self.allowed_groups:
            return "group is not allowed"
        if self.allowed_users is not None and context.user_id not in self.allowed_users:
            return "user is not allowed"
        return None


@dataclass(frozen=True, slots=True)
class ToolContext:
    request_id: str
    group_id: str = ""
    user_id: str = ""
    session_id: str | None = None
    context_epoch: int | None = None
    deadline_monotonic: float | None = None
    cancel_scope: object | None = field(default=None, repr=False, compare=False)
    policy_snapshot: PolicySnapshot = field(default_factory=PolicySnapshot)
    budget_scope: BudgetScope | None = None
    budget_controller: BudgetController | None = field(default=None, repr=False, compare=False)
    evidence_limit: int = 8
    tool_timeout_seconds: float | None = None
    result_max_chars: int | None = None
    task_identity: TaskIdentity | None = None
    event_sink: EventSink | None = field(default=None, repr=False, compare=False)
    tool_call_id: str | None = field(default=None, repr=False, compare=False)
    agent_round: int | None = field(default=None, repr=False, compare=False)
    extensions: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


AsyncToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable["ToolResult"]]
ArgumentValidator = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: AsyncToolHandler
    policy: ToolPolicy
    argument_validator: ArgumentValidator | None = None
    timeout_seconds: float = 30.0
    max_concurrency: int = 4
    max_result_chars: int = 16_000
    output_kind: ToolOutputKind = ToolOutputKind.STRUCTURED
    data_requirements: tuple[str, ...] = ()
    progress_category: ToolCategory = ToolCategory.OTHER

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("tool name must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if self.max_concurrency < 1:
            raise ValueError("tool max_concurrency must be positive")
        if self.max_result_chars < 1:
            raise ValueError("tool max_result_chars must be positive")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object JSON Schema")
        if not (
            self.policy.allowed_roles
            or self.policy.required_permissions
            or self.policy.allowed_groups
            or self.policy.allowed_users
        ):
            raise ValueError("tool policy must declare an allowed role, permission, group, or user")
        object.__setattr__(self, "parameters", deepcopy(self.parameters))


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: ToolStatus
    content: str | dict[str, Any]
    evidence: tuple[Evidence, ...] = ()
    usage: ToolUsage = field(default_factory=ToolUsage)
    error: ToolError | None = None
    completeness: Literal["complete", "partial", "reference_only", "unavailable"] = "complete"


def search_results_to_tool_result(results: list[SearchResult]) -> ToolResult:
    """Bridge the existing SearchResult DTO into the structured tool contract."""
    evidence: list[Evidence] = []
    encoded: list[dict[str, str]] = []
    for item in results:
        encoded.append(
            {
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "source": item.source,
            }
        )
        if item.url and not item.title.endswith("失败"):
            if item.source in {"GitHub仓库文件", "GitHub文件"}:
                kind = "repository_file"
            elif item.source == "网页读取":
                kind = "web_page"
            elif item.source == "GitHub仓库":
                kind = "repository"
            else:
                kind = "web"
            evidence.append(
                Evidence(
                    kind=kind,
                    source_id=item.source,
                    title=item.title,
                    locator=item.url,
                )
            )
    status = ToolStatus.NO_RESULTS if not results else ToolStatus.OK
    return ToolResult(status=status, content={"results": encoded}, evidence=tuple(evidence))


def format_search_results(results: list[SearchResult], max_chars: int) -> str:
    """Keep the existing compact tool observation format during migration."""
    if not results:
        return "没有找到结果。"
    lines: list[str] = []
    for index, item in enumerate(results[:6], start=1):
        summary = re.sub(r"\s+", " ", item.summary).strip()
        if len(summary) > 2200:
            summary = summary[:2200].rstrip() + "..."
        url_part = f"\n   URL: {item.url}" if item.url else ""
        lines.append(f"{index}. {item.title}\n   {summary}{url_part}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def tool_result_to_search_results(result: ToolResult) -> list[SearchResult]:
    """Compatibility view used while legacy tool handlers are being migrated."""
    if not isinstance(result.content, dict):
        return []
    rows = result.content.get("results")
    if not isinstance(rows, list):
        return []
    converted: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        converted.append(
            SearchResult(
                title=str(row.get("title") or ""),
                summary=str(row.get("summary") or ""),
                url=str(row.get("url") or ""),
                source=str(row.get("source") or ""),
            )
        )
    return converted
