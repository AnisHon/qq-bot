from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(slots=True)
class MessageAttachment:
    type: str
    name: str = ""
    url: str = ""


@dataclass(frozen=True, slots=True)
class QQMention:
    user_id: str
    display: str = ""


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    message_id: str
    user_id: str = ""
    nickname: str = ""
    text: str = ""
    attachments: tuple[MessageAttachment, ...] = ()
    is_from_bot: bool = False


@dataclass(slots=True)
class GroupMessageEvent:
    message_id: str
    group_id: str
    user_id: str
    nickname: str
    text: str
    is_at_bot: bool
    timestamp: int
    raw_event: Mapping[str, Any]
    attachments: tuple[MessageAttachment, ...] = ()
    bot_names: tuple[str, ...] = ()
    mentions: tuple[QQMention, ...] = ()
    reply_message_id: str = ""
    quote: QuotedMessage | None = None


@dataclass(slots=True)
class SearchResult:
    title: str
    summary: str
    url: str
    source: str


@dataclass(slots=True)
class ChatReply:
    text: str
    should_send: bool = True
    context_epoch: int | None = None
    context_persisted: bool | None = None
    task_id: str | None = None
