"""Slash-command runtime, independent from the model-facing Tool Registry."""

from app.commands.contracts import (
    ArgumentSpec,
    CommandContext,
    CommandError,
    CommandErrorKind,
    CommandPolicy,
    CommandResult,
    CommandSpec,
    OptionSpec,
    ParsedCommand,
)
from app.commands.parser import CommandParser
from app.commands.registry import CommandRegistry
from app.commands.renderer import QQCommandRenderer
from app.commands.service import CommandService

__all__ = [
    "ArgumentSpec",
    "CommandContext",
    "CommandError",
    "CommandErrorKind",
    "CommandParser",
    "CommandPolicy",
    "CommandRegistry",
    "CommandResult",
    "CommandService",
    "CommandSpec",
    "QQCommandRenderer",
    "OptionSpec",
    "ParsedCommand",
]
