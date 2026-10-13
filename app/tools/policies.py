from __future__ import annotations

from app.config import Settings
from app.models import GroupMessageEvent
from app.tools.contracts import PolicySnapshot


def group_member_policy_snapshot(event: GroupMessageEvent, settings: Settings) -> PolicySnapshot:
    """Capture roles supported by the authenticated OneBot group event.

    Existing tools remain available to any group member, matching the current
    command behavior. Bot-admin membership is additive and does not silently
    grant a new tool permission; a future restricted tool must declare its own
    role or permission in its ToolPolicy.
    """
    roles = {"group_member"}
    if event.user_id and settings.is_admin(event.user_id):
        roles.add("bot_admin")
    return PolicySnapshot(roles=frozenset(roles))
