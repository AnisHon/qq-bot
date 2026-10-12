from __future__ import annotations

"""Request-scoped logging context.

Usage
-----
Set the context before processing a message:
    from app.logging_ctx import request_ctx
    token = request_ctx.set(RequestContext(request_id="abc123", group_id="100"))
    try:
        ...
    finally:
        request_ctx.reset(token)

In any module, import the filter once and attach to the root logger:
    from app.logging_ctx import install_context_filter
    install_context_filter()

The filter injects [req=abc123 grp=100] into every log record emitted
while the context is active.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
import uuid


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    group_id: str = ""
    user_id: str = ""


_EMPTY = RequestContext(request_id="", group_id="", user_id="")
request_ctx: ContextVar[RequestContext] = ContextVar("request_ctx", default=_EMPTY)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        ctx = request_ctx.get()
        if ctx.request_id:
            record.msg = f"[req={ctx.request_id} grp={ctx.group_id}] {record.msg}"
        return True


_filter_installed = False


def install_context_filter() -> None:
    global _filter_installed  # noqa: PLW0603
    if _filter_installed:
        return
    logging.getLogger().addFilter(_ContextFilter())
    _filter_installed = True


def new_context(group_id: str = "", user_id: str = "") -> RequestContext:
    return RequestContext(group_id=group_id, user_id=user_id)
