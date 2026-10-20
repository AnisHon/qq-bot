from __future__ import annotations

import asyncio
import logging

from app.commands.contracts import (
    CommandContext,
    CommandError,
    CommandErrorKind,
    CommandResult,
    CommandSpec,
)
from app.commands.parser import CommandParser


LOGGER = logging.getLogger(__name__)


class CommandRegistry:
    """Application command registry; intentionally separate from ToolRegistry."""

    def __init__(self, parser: CommandParser | None = None) -> None:
        self.parser = parser or CommandParser()
        self._specs: dict[tuple[str, ...], CommandSpec] = {}

    @property
    def specs(self) -> tuple[CommandSpec, ...]:
        return tuple(self._specs.values())

    def register(self, spec: CommandSpec) -> None:
        if spec.path in self._specs:
            raise ValueError(f"command path collision: /{' '.join(spec.path)}")
        self._specs[spec.path] = spec

    async def dispatch(self, text: str, context: CommandContext) -> CommandResult:
        try:
            parsed = self.parser.parse(text, self.specs)
            if not parsed.spec.policy.allows(context):
                raise CommandError(
                    CommandErrorKind.PERMISSION_DENIED,
                    "你没有权限执行此命令。",
                )
            if parsed.help_requested:
                return CommandResult.success(self.render_help(parsed.spec.path, context))
            if parsed.spec.handler is None:
                return CommandResult(
                    CommandErrorKind.INVALID_ARGUMENT,
                    f"该命令需要子命令。用法：{self.render_help(parsed.spec.path, context)}",
                )
            async with asyncio.timeout(parsed.spec.timeout_seconds):
                return await parsed.spec.handler(context, parsed)
        except CommandError as exc:
            return CommandResult(exc.kind, exc.message)
        except TimeoutError:
            return CommandResult(
                CommandErrorKind.TIMEOUT,
                "命令执行超时，结果可能尚未确认。请先检查状态，再决定是否重试。",
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("slash command failed (%s)", type(exc).__name__)
            return CommandResult(
                CommandErrorKind.INTERNAL_ERROR,
                "命令执行失败，请稍后重试；管理员可检查服务日志。",
            )

    def render_help(self, path: tuple[str, ...] = (), context: CommandContext | None = None) -> str:
        visible = [spec for spec in self.specs if context is None or spec.policy.allows(context)]
        target = tuple(item.casefold().lstrip("/") for item in path)
        if target:
            visible = [spec for spec in visible if spec.path[:len(target)] == target]
            if not visible:
                raise CommandError(CommandErrorKind.NOT_FOUND, f"没有可用的命令：/{' '.join(target)}。")
            exact = self._specs.get(target)
            if exact is not None and (context is None or exact.policy.allows(context)):
                if exact.handler is not None:
                    return f"{exact.usage}\n{exact.description}"
            lines = [f"/{' '.join(target)} 可用子命令："]
        else:
            lines = ["可用命令（发送 /help <命令> 查看子命令）："]
            visible_paths = {spec.path for spec in visible}
            roots: dict[tuple[str, ...], CommandSpec] = {}
            for spec in self.specs:
                if len(spec.path) != 1 or (context is not None and not spec.policy.allows(context)):
                    continue
                if spec.handler is not None or any(
                    candidate[:1] == spec.path for candidate in visible_paths if len(candidate) > 1
                ):
                    roots[spec.path] = spec
            paths = sorted(roots)
            for command_path in paths:
                spec = roots[command_path]
                lines.append(f"{spec.usage} — {spec.description}")
            return "\n".join(lines)

        paths = sorted(spec.path for spec in visible if len(spec.path) == len(target) + 1)
        for command_path in paths:
            spec = self._specs.get(command_path)
            if spec is None:
                continue
            lines.append(f"{spec.usage} — {spec.description}")
        if len(lines) == 1:
            raise CommandError(CommandErrorKind.NOT_FOUND, f"没有可用的命令：/{' '.join(target)}。")
        return "\n".join(lines)
