from __future__ import annotations

import re

from app.commands.contracts import CommandResult
from app.models import ChatReply
from app.text_utils import split_qq_text


MAX_COMMAND_REPLY_CHARS = 6000
_CQ_CODE_START_RE = re.compile(r"\[CQ:", re.IGNORECASE)


class QQCommandRenderer:
    """Render the uniform command result into bounded, plain QQ text."""

    def __init__(self, max_chars: int = MAX_COMMAND_REPLY_CHARS) -> None:
        if max_chars < 128:
            raise ValueError("command reply limit must be at least 128 characters")
        self.max_chars = max_chars

    def render(self, result: CommandResult) -> ChatReply:
        # Registry tools can return repository, web, or indexed document text.
        # Keep CQ-looking text literal before the dispatcher splits later chunks
        # into plain string messages without a structured reply segment.
        text = _CQ_CODE_START_RE.sub("[CQ：", str(result.text)).strip()
        if len(text) > self.max_chars:
            parts = split_qq_text(text, self.max_chars - 80)
            text = (parts[0] if parts else "命令已完成。") + "\n……结果过长，已截断。请缩小查询范围后重试。"
        return ChatReply(text=text or "命令已完成。")
