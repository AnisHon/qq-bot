from __future__ import annotations

import hashlib
from typing import Any

from app.context.budget import ContextWindowBudget
from app.memory import MemoryStore
from app.tools.contracts import ToolResult, ToolStatus


class MemoryService:
    """Policy-light domain facade for persisted facts and their provenance."""

    def __init__(self, store: MemoryStore, *, max_context_tokens: int = 2000) -> None:
        if max_context_tokens < 1:
            raise ValueError("memory context budget must be positive")
        self.store = store
        self.max_context_tokens = max_context_tokens

    def add_admin_fact(
        self, *, group_id: str, actor_user_id: str, content: str, source_message_id: str | None,
    ) -> int:
        """Called only after CommandRegistry's administrator policy has passed."""
        source_id = str(source_message_id) if source_message_id else None
        memory_id, _created = self.store.add_structured_memory(
            group_id=group_id, owner_user_id=None, content=content,
            actor_user_id=actor_user_id, verification="admin_confirmed", visibility="group",
            kind="fact", source_kind="qq_admin_command", source_id=source_id,
            locator=(f"onebot:{group_id}:{source_id}" if source_id else None),
        )
        return memory_id

    def add_user_assertion(
        self, *, group_id: str, user_id: str, content: str, source_message_id: str,
    ) -> int:
        memory_id, _created = self.store.add_structured_memory(
            group_id=group_id, owner_user_id=user_id, content=content,
            actor_user_id=user_id, verification="user_asserted", visibility="user",
            kind="fact", source_kind="qq_message", source_id=str(source_message_id),
            locator=f"onebot:{group_id}:{source_message_id}",
        )
        return memory_id

    def add_tool_observation(
        self, *, group_id: str, actor_user_id: str, content: str,
        result: ToolResult, source_message_id: str | None = None,
    ) -> int:
        if not isinstance(result, ToolResult) or result.status is not ToolStatus.OK or not result.evidence:
            raise ValueError("tool-observed facts require a successful typed ToolResult with evidence")
        memory_id, _created = self.store.add_structured_memory(
            group_id=group_id, owner_user_id=None, content=content,
            actor_user_id=actor_user_id, verification="tool_observed", visibility="group",
            kind="fact", source_kind="tool_evidence", source_id=source_message_id,
            source_hash=hashlib.sha256(
                "\n".join(f"{item.source_id}|{item.revision}|{item.locator}" for item in result.evidence).encode("utf-8")
            ).hexdigest(),
            locator=";".join(f"{item.source_id}:{item.locator}" for item in result.evidence)[:2048],
        )
        return memory_id

    def select_for_context(
        self, query: str, *, group_id: str, user_id: str, limit: int,
    ) -> list[dict[str, Any]]:
        rows = self.store.search_structured_memories(
            query, group_id=group_id, user_id=user_id, limit=min(max(1, limit), 100),
        )
        selected: list[dict[str, Any]] = []
        remaining = self.max_context_tokens
        for row in rows:
            content = str(row.get("content") or "")
            cost = ContextWindowBudget.estimate_text_tokens(content)
            if cost > remaining:
                continue
            selected.append(row)
            remaining -= cost
        return selected

    def search(
        self, query: str, *, group_id: str, user_id: str, limit: int = 20,
    ) -> list[dict[str, object]]:
        return self.store.search_memories(
            query, group_id=group_id, user_id=user_id, limit=limit,
        )

    def search_episodes(
        self, query: str, *, group_id: str, epoch: int, limit: int = 4,
    ) -> list[dict[str, object]]:
        return self.store.search_episodes(
            query, group_id=group_id, epoch=epoch, limit=limit,
        )

    def retract(self, memory_id: int, *, group_id: str) -> bool:
        return self.store.retract_structured_memory(memory_id, group_id=group_id)

    def revise_admin_fact(
        self, memory_id: int, *, group_id: str, actor_user_id: str,
        content: str, source_message_id: str | None,
    ) -> bool:
        source_id = str(source_message_id) if source_message_id else None
        return self.store.revise_structured_memory(
            memory_id, group_id=group_id, actor_user_id=actor_user_id, content=content,
            source_kind="qq_admin_revision", source_id=source_id,
            locator=(f"onebot:{group_id}:{source_id}" if source_id else None),
        )
