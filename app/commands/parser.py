from __future__ import annotations

import shlex
from typing import Any, Iterable

from app.commands.contracts import (
    ArgumentSpec,
    CommandError,
    CommandErrorKind,
    CommandSpec,
    OptionSpec,
    ParsedCommand,
)


MAX_COMMAND_CHARS = 70_000
MAX_COMMAND_TOKENS = 64


class CommandParser:
    """Small shell-like parser for slash commands; it never interprets shell syntax."""

    def parse(self, text: str, specs: Iterable[CommandSpec]) -> ParsedCommand:
        if not isinstance(text, str) or len(text) > MAX_COMMAND_CHARS:
            raise CommandError(
                CommandErrorKind.INVALID_ARGUMENT,
                f"命令超过长度上限（{MAX_COMMAND_CHARS} 字符）。",
            )
        cleaned = text.strip()
        if not cleaned.startswith("/"):
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "命令必须以 / 开头。")
        first_line, separator, multiline = cleaned.partition("\n")
        try:
            tokens = shlex.split(first_line, posix=True)
        except ValueError:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "引号或转义格式不完整。") from None
        if not tokens or not tokens[0].startswith("/"):
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "命令格式无效。")
        if len(tokens) > MAX_COMMAND_TOKENS:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "命令参数过多。")

        root = tokens[0][1:].casefold()
        if not root or any(char.isspace() for char in root):
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "命令名称无效。")
        candidates = tuple(specs)
        possible = [spec for spec in candidates if spec.path[0] == root]
        if not possible:
            raise CommandError(CommandErrorKind.UNKNOWN_COMMAND, _unknown_message(root, candidates))

        matched: CommandSpec | None = None
        consumed = 1
        remaining = tokens[1:]
        for spec in sorted(possible, key=lambda item: len(item.path), reverse=True):
            tail = spec.path[1:]
            if len(remaining) >= len(tail) and tuple(item.casefold() for item in remaining[:len(tail)]) == tail:
                matched = spec
                consumed = 1 + len(tail)
                break
        if (
            matched is not None
            and len(matched.path) == 1
            and matched.handler is None
            and any(len(item.path) > 1 for item in possible)
            and remaining
            and not any(item.casefold() == "--help" for item in remaining)
        ):
            requested = remaining[0].casefold()
            child_names = sorted({item.path[1] for item in possible if len(item.path) > 1})
            import difflib

            suggestion = difflib.get_close_matches(requested, child_names, n=1, cutoff=0.55)
            detail = f"你是否想使用 /{root} {suggestion[0]}？" if suggestion else f"发送 /help {root} 查看子命令。"
            raise CommandError(CommandErrorKind.UNKNOWN_COMMAND, f"未知子命令：/{root} {requested}。{detail}")
        if matched is None:
            matched = next((item for item in possible if len(item.path) == 1), None)
            if matched is None:
                raise CommandError(CommandErrorKind.UNKNOWN_COMMAND, f"未知命令：/{root}。发送 /help 查看可用命令。")
        if separator and multiline.strip() and not matched.allow_multiline:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, "此命令不接受多行参数。")
        trailing = tokens[consumed:]
        if any(item.casefold() == "--help" for item in trailing):
            return ParsedCommand(matched, {}, {}, True, cleaned)
        args, options = self._parse_tail(matched, trailing, multiline if separator else None)
        return ParsedCommand(matched, args, options, False, cleaned)

    def _parse_tail(
        self,
        spec: CommandSpec,
        tokens: list[str],
        multiline: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        positional: list[str] = []
        option_values: dict[str, Any] = {}
        option_specs = {item.name: item for item in spec.options}
        index = 0
        options_enabled = True
        while index < len(tokens):
            token = tokens[index]
            if options_enabled and token == "--":
                options_enabled = False
                index += 1
                continue
            if options_enabled and token.startswith("--"):
                name_value = token[2:].split("=", 1)
                name = name_value[0].casefold()
                option = option_specs.get(name)
                if option is None:
                    raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"不支持选项 --{name}。用法：{spec.usage}")
                if name in option_values:
                    raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"选项 --{name} 不能重复指定。")
                if option.flag:
                    if len(name_value) != 1:
                        raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"选项 --{name} 不接受值。")
                    value: Any = True
                else:
                    if len(name_value) == 2:
                        raw = name_value[1]
                    else:
                        index += 1
                        if index >= len(tokens) or tokens[index].startswith("--"):
                            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"选项 --{name} 缺少值。")
                        raw = tokens[index]
                    value = _convert(raw, option, spec.usage)
                option_values[name.replace("-", "_")] = value
            else:
                positional.append(token)
            index += 1

        if spec.allow_multiline and multiline is not None:
            if positional:
                positional[-1] = f"{positional[-1]}\n{multiline}".strip("\n")
            else:
                positional.append(multiline.strip("\n"))

        arguments = _bind_arguments(spec.arguments, positional, spec.usage)
        for option in spec.options:
            key = option.name.replace("-", "_")
            if option.required and key not in option_values:
                raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"缺少选项 --{option.name}。用法：{spec.usage}")
        return arguments, option_values


def _bind_arguments(
    specifications: tuple[ArgumentSpec, ...], values: list[str], usage: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    cursor = 0
    for spec in specifications:
        if spec.variadic:
            raw = " ".join(values[cursor:])
            cursor = len(values)
            if raw or spec.required:
                result[spec.name] = _convert(raw, spec, usage)
            continue
        if cursor >= len(values):
            if spec.required:
                raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"缺少参数 <{spec.name}>。用法：{usage}")
            continue
        result[spec.name] = _convert(values[cursor], spec, usage)
        cursor += 1
    if cursor < len(values):
        raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"参数过多。用法：{usage}")
    return result


def _convert(raw: str, spec: ArgumentSpec | OptionSpec, usage: str) -> Any:
    if len(raw) > spec.max_length:
        raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"参数 <{spec.name}> 超过长度限制。用法：{usage}")
    if spec.value_type == "int":
        try:
            value: Any = int(raw, 10)
        except ValueError:
            raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"参数 <{spec.name}> 必须是整数。用法：{usage}") from None
    else:
        value = raw.strip()
    if isinstance(value, str) and not value:
        raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"参数 <{spec.name}> 不能为空。用法：{usage}")
    if spec.choices and value not in spec.choices:
        choices = "、".join(spec.choices)
        raise CommandError(CommandErrorKind.INVALID_ARGUMENT, f"参数 <{spec.name}> 只能是：{choices}。用法：{usage}")
    return value


def _unknown_message(root: str, specs: tuple[CommandSpec, ...]) -> str:
    import difflib

    roots = sorted({item.path[0] for item in specs})
    suggestion = difflib.get_close_matches(root, roots, n=1, cutoff=0.55)
    if suggestion:
        return f"未知命令：/{root}。你是否想使用 /{suggestion[0]}？"
    return f"未知命令：/{root}。发送 /help 查看可用命令。"
