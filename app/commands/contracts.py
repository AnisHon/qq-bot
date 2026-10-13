from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import re
from typing import Any, Awaitable, Callable, Mapping

from app.models import GroupMessageEvent


class CommandErrorKind(StrEnum):
    SUCCESS = "SUCCESS"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class CommandResult:
    kind: CommandErrorKind
    text: str

    @classmethod
    def success(cls, text: str) -> CommandResult:
        return cls(CommandErrorKind.SUCCESS, text)


class CommandError(Exception):
    def __init__(self, kind: CommandErrorKind, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    admin_only: bool = False

    def allows(self, context: CommandContext) -> bool:
        return not self.admin_only or context.is_admin


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    name: str
    value_type: str = "string"
    required: bool = True
    variadic: bool = False
    max_length: int = 4096
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OptionSpec:
    name: str
    value_type: str = "string"
    required: bool = False
    flag: bool = False
    max_length: int = 2048
    choices: tuple[str, ...] = ()


CommandCallable = Callable[["CommandContext", "ParsedCommand"], Awaitable["CommandResult"]]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    path: tuple[str, ...]
    description: str
    usage: str
    handler: CommandCallable | None = field(default=None, repr=False, compare=False)
    arguments: tuple[ArgumentSpec, ...] = ()
    options: tuple[OptionSpec, ...] = ()
    policy: CommandPolicy = field(default_factory=CommandPolicy)
    allow_multiline: bool = False
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        path = tuple(item.casefold() for item in self.path)
        if not path or any(not item or item.startswith("/") for item in path):
            raise ValueError("command path must contain plain command tokens")
        if any(not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", item) for item in path):
            raise ValueError("command path tokens must be kebab-case words")
        if len({item.name for item in self.arguments}) != len(self.arguments):
            raise ValueError("command argument names must be unique")
        if len({item.name for item in self.options}) != len(self.options):
            raise ValueError("command option names must be unique")
        if any(not item.name or item.name.startswith("-") for item in self.options):
            raise ValueError("option names must not include leading dashes")
        if any(not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", item.name) for item in self.options):
            raise ValueError("option names must be kebab-case words")
        if any(item.value_type not in {"string", "int"} for item in (*self.arguments, *self.options)):
            raise ValueError("command value types must be string or int")
        if any(item.variadic for item in self.arguments[:-1]):
            raise ValueError("only the final positional argument can be variadic")
        if any(item.max_length < 1 for item in (*self.arguments, *self.options)):
            raise ValueError("argument limits must be positive")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 120
        ):
            raise ValueError("command timeout must be greater than zero and at most 120 seconds")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    spec: CommandSpec
    arguments: Mapping[str, Any]
    options: Mapping[str, Any]
    help_requested: bool = False
    raw_text: str = ""


@dataclass(slots=True)
class CommandContext:
    event: GroupMessageEvent
    settings: Any
    memory: Any
    llm_handler: Any
    storage_executor: Any = None
    request_id: str = ""
    is_admin: bool = False
    services: Mapping[str, Any] = field(default_factory=dict)

    @property
    def group_id(self) -> str:
        return str(self.event.group_id)

    @property
    def user_id(self) -> str:
        return str(self.event.user_id)
