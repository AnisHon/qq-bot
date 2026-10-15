from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from app.models import GroupMessageEvent, MessageAttachment, QQMention, QuotedMessage


CQ_AT_PATTERN = re.compile(r"\[CQ:at,qq=(?P<qq>[^\],]+)(?:,[^\]]*)?\]")
CQ_SEGMENT_PATTERN = re.compile(r"\[CQ:(?P<type>[^,\]]+)(?P<data>[^\]]*)\]")
CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]")
ATTACHMENT_TYPES = {"image", "file", "video", "record", "audio"}


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


@dataclass(slots=True)
class NatcapQQAdapter:
    _client: httpx.AsyncClient = field(init=False, repr=False)
    _bot_name_cache: dict[str, tuple[tuple[str, ...], float]] = field(default_factory=dict, init=False, repr=False)
    api_base_url: str = field(repr=False)
    api_token: str = field(default="", repr=False)
    bot_qq_id: str = ""
    callback_token: str = field(default="", repr=False)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.api_base_url.rstrip("/"),
            timeout=self.timeout,
            headers=self._auth_headers(self.api_token),
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    @property
    def callback_auth_configured(self) -> bool:
        return bool(self.callback_token.strip())

    def verify_callback_token(self, headers: Mapping[str, str], body: bytes | None = None) -> bool:
        expected = self.callback_token.strip()
        if not expected:
            return False

        signature = next(
            (
                value
                for name, value in headers.items()
                if str(name).casefold() == "x-signature"
            ),
            "",
        )
        if signature:
            if body is None or not isinstance(signature, str):
                return False
            # NapCat HTTP Client uses crypto.createHmac("sha1", token).update(body).
            # Keep the legacy Bearer/raw-token path for existing OneBot clients.
            digest = hmac.new(expected.encode("utf-8"), body, hashlib.sha1).hexdigest()
            expected_signature = "sha1=" + digest
            return hmac.compare_digest(signature.strip(), expected_signature)

        auth_value = next(
            (
                value
                for name, value in headers.items()
                if str(name).casefold() == "authorization"
            ),
            "",
        )
        if not isinstance(auth_value, str):
            return False

        auth = auth_value.strip()
        scheme, separator, credential = auth.partition(" ")
        presented = credential.strip() if separator and scheme.casefold() == "bearer" else auth
        return hmac.compare_digest(
            presented.encode("utf-8"),
            expected.encode("utf-8"),
        )

    def parse_group_event(self, payload: Mapping[str, Any]) -> GroupMessageEvent | None:
        if payload.get("post_type") != "message":
            return None
        if payload.get("message_type") != "group":
            return None

        sender = payload.get("sender") or {}
        if self.bot_qq_id and str(payload.get("user_id") or "") == self.bot_qq_id:
            return None
        message = payload.get("message")
        text, is_at_bot, attachments, mentions, reply_message_id = self._extract_message(message)
        if not text:
            raw_text = _coerce_text(payload.get("raw_message") or "")
            if raw_text:
                extracted_text, raw_is_at_bot, raw_attachments, raw_mentions, raw_reply_id = self._extract_message(raw_text)
                text = extracted_text
                is_at_bot = is_at_bot or raw_is_at_bot
                attachments = attachments or raw_attachments
                mentions = mentions or raw_mentions
                reply_message_id = reply_message_id or raw_reply_id
        text = self._normalize_text(text)
        if not text and not is_at_bot and not attachments and not reply_message_id:
            return None

        return GroupMessageEvent(
            message_id=str(payload.get("message_id", "")),
            group_id=str(payload.get("group_id", "")),
            user_id=str(payload.get("user_id", "")),
            nickname=str(sender.get("card") or sender.get("nickname") or ""),
            text=text,
            is_at_bot=is_at_bot,
            timestamp=int(payload.get("time") or payload.get("timestamp") or 0),
            raw_event=payload,
            attachments=tuple(attachments),
            mentions=tuple(mentions),
            reply_message_id=reply_message_id,
        )

    def _extract_message(
        self,
        message: Any,
    ) -> tuple[str, bool, list[MessageAttachment], list[QQMention], str]:
        if isinstance(message, list):
            parts: list[str] = []
            is_at_bot = False
            attachments: list[MessageAttachment] = []
            mentions: list[QQMention] = []
            reply_message_id = ""
            for segment in message:
                if not isinstance(segment, Mapping):
                    continue
                seg_type = str(segment.get("type") or "").lower()
                data = segment.get("data") or {}
                if seg_type == "text":
                    parts.append(_coerce_text(data.get("text")))
                elif seg_type == "at":
                    qq = str(data.get("qq") or "")
                    mentions.append(QQMention(user_id=qq, display=_coerce_text(data.get("name"))))
                    if self.bot_qq_id and qq == self.bot_qq_id:
                        is_at_bot = True
                elif seg_type == "reply":
                    reply_message_id = str(data.get("id") or data.get("message_id") or "")
                elif seg_type == "face":
                    continue
                elif seg_type in ATTACHMENT_TYPES:
                    attachments.append(self._attachment_from_segment(seg_type, data))
            return "".join(parts), is_at_bot, attachments, mentions, reply_message_id

        raw = _coerce_text(message)
        is_at_bot = False
        mentions: list[QQMention] = []
        for match in CQ_AT_PATTERN.finditer(raw):
            qq = match.group("qq")
            mentions.append(QQMention(user_id=qq))
        if self.bot_qq_id:
            for match in CQ_AT_PATTERN.finditer(raw):
                if match.group("qq") == self.bot_qq_id:
                    is_at_bot = True
                    break
            if re.search(rf"@{re.escape(self.bot_qq_id)}\b", raw):
                is_at_bot = True
        attachments = self._extract_cq_attachments(raw)
        reply_message_id = ""
        for match in CQ_SEGMENT_PATTERN.finditer(raw):
            if match.group("type").lower() == "reply":
                data = self._parse_cq_data(match.group("data"))
                reply_message_id = str(data.get("id") or data.get("message_id") or "")
                break
        raw = CQ_AT_PATTERN.sub("", raw)
        raw = CQ_CODE_PATTERN.sub("", raw)
        return raw, is_at_bot, attachments, mentions, reply_message_id

    async def hydrate_quote(self, event: GroupMessageEvent) -> GroupMessageEvent:
        if not event.reply_message_id or event.quote is not None:
            return event
        try:
            resp = await self._client.post("/get_msg", json={"message_id": int(event.reply_message_id)})
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping):
                return event
            sender = data.get("sender") or {}
            text, _, attachments, _, _ = self._extract_message(data.get("message") or data.get("raw_message") or "")
            quoted_user_id = str(data.get("user_id") or (sender.get("user_id") if isinstance(sender, Mapping) else "") or "")
            nickname = ""
            if isinstance(sender, Mapping):
                nickname = str(sender.get("card") or sender.get("nickname") or "")
            event.quote = QuotedMessage(
                message_id=event.reply_message_id,
                user_id=quoted_user_id,
                nickname=nickname,
                text=self._normalize_text(text),
                attachments=tuple(attachments),
                is_from_bot=bool(self.bot_qq_id and quoted_user_id == self.bot_qq_id),
            )
        except Exception:  # noqa: BLE001
            return event
        return event

    def _extract_cq_attachments(self, raw: str) -> list[MessageAttachment]:
        attachments: list[MessageAttachment] = []
        for match in CQ_SEGMENT_PATTERN.finditer(raw):
            seg_type = match.group("type").lower()
            if seg_type not in ATTACHMENT_TYPES:
                continue
            data = self._parse_cq_data(match.group("data"))
            attachments.append(self._attachment_from_segment(seg_type, data))
        return attachments

    @staticmethod
    def _parse_cq_data(raw: str) -> dict[str, str]:
        raw = raw.lstrip(",")
        if not raw:
            return {}
        data: dict[str, str] = {}
        for item in raw.split(","):
            key, sep, value = item.partition("=")
            if sep:
                data[key.strip()] = value.strip()
        return data

    @staticmethod
    def _attachment_from_segment(seg_type: str, data: Mapping[str, Any]) -> MessageAttachment:
        name = _coerce_text(
            data.get("name")
            or data.get("filename")
            or data.get("file")
            or data.get("path")
            or data.get("summary")
        )
        url = _coerce_text(data.get("url") or data.get("file_url") or "")
        return MessageAttachment(type=seg_type, name=name, url=url)

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\u200b", "")
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    async def send_group_message(self, group_id: str | int, text: str, reply_to: str = "") -> None:
        message: str | list[dict[str, Any]] = text
        if reply_to:
            message = [
                {"type": "reply", "data": {"id": str(reply_to)}},
                {"type": "text", "data": {"text": text}},
            ]
        payload = {
            "group_id": int(group_id),
            "message": message,
        }
        resp = await self._client.post("/send_group_msg", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, Mapping):
            if data.get("status") not in {"ok", "success", None}:
                raise RuntimeError(f"NapCat send_group_msg failed: {data}")

    async def get_group_bot_names(self, group_id: str | int, cache_seconds: int = 3600) -> tuple[str, ...]:
        if not self.bot_qq_id:
            return ()
        group_key = str(group_id)
        now = time.time()
        cached = self._bot_name_cache.get(group_key)
        if cached and cached[1] > now:
            return cached[0]

        names: list[str] = []
        try:
            payload = {
                "group_id": int(group_id),
                "user_id": int(self.bot_qq_id),
                "no_cache": False,
            }
            resp = await self._client.post("/get_group_member_info", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, Mapping):
                member = data.get("data") or {}
                if isinstance(member, Mapping):
                    for key in ("card", "nickname"):
                        value = str(member.get(key) or "").strip()
                        if value and value not in names:
                            names.append(value)
        except Exception:  # noqa: BLE001
            names = []

        if not names:
            try:
                resp = await self._client.post("/get_login_info", json={})
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, Mapping):
                    info = data.get("data") or {}
                    if isinstance(info, Mapping):
                        name = str(info.get("nickname") or "").strip()
                        if name:
                            names.append(name)
            except Exception:  # noqa: BLE001
                names = []

        result = tuple(names)
        self._bot_name_cache[group_key] = (result, now + cache_seconds)
        return result
