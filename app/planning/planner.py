from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.planning.contracts import TaskComplexity, TaskPlan
from app.prompts import PLANNER_SYSTEM_PROMPT, REPLANNER_SYSTEM_PROMPT


_ACTION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("查", "查询", "检索", "搜索", "找一下", "查找"),
    ("读取", "阅读", "打开", "查看原文", "读第一段"),
    ("核对", "验证", "确认", "校验"),
    ("比较", "对比", "结合", "综合", "分析"),
    ("计算", "求和", "换算", "加上", "减去"),
    ("设计", "规划", "生成", "编写", "创作", "写一个", "编故事", "编一个故事", "实现"),
)
_RESEARCH = re.compile(r"(wiki|github|社区|来源|资料|教程|版本|研究|最新|新闻|配方|源码|文档)", re.I)
_SEQUENCE = re.compile(r"(先|然后|接着|之后|再|最后|并且|同时|基于|根据|在此基础上)")


def assess_task_complexity(goal: str) -> TaskComplexity:
    """Cheap deterministic gate; ambiguous requests stay on the original path."""
    text = goal.strip()
    if not text:
        return TaskComplexity.SIMPLE
    actions = sum(any(term in text for term in group) for group in _ACTION_GROUPS)
    research = bool(_RESEARCH.search(text))
    sequence_count = len(_SEQUENCE.findall(text))
    if research and actions >= 3:
        return TaskComplexity.RESEARCH
    if actions >= 3 or (actions >= 2 and sequence_count >= 1):
        return TaskComplexity.MULTI_STEP
    return TaskComplexity.SIMPLE


# Backward-compatible names; the contracts themselves live in app.prompts.
PLANNER_SYSTEM = PLANNER_SYSTEM_PROMPT
REPLAN_SYSTEM = REPLANNER_SYSTEM_PROMPT


def decode_plan_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("planner response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("planner response must be a JSON object")
    return value


async def generate_task_plan(
    llm: Any,
    *,
    task_id: str,
    goal: str,
    constraints: tuple[str, ...],
    enabled_tools: frozenset[str],
    deadline: float,
    loop: asyncio.AbstractEventLoop,
    existing: TaskPlan | None = None,
) -> TaskPlan:
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError("task deadline expired before planning")
    prompt = {
        "goal": goal,
        "immutable_constraints": list(constraints),
        "enabled_tools": sorted(enabled_tools),
        "current_plan": existing.as_prompt_data() if existing is not None else None,
    }
    response = await asyncio.wait_for(
        llm.chat(
            [
                {"role": "system", "content": PLANNER_SYSTEM if existing is None else REPLAN_SYSTEM},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))},
            ],
            temperature=0.1,
            max_tokens=1800,
        ),
        timeout=remaining,
    )
    draft = decode_plan_json(str(response))
    return TaskPlan.from_draft(
        task_id=task_id,
        goal=goal,
        constraints=constraints,
        draft=draft,
        allowed_tools=enabled_tools,
        revision=(existing.revision + 1 if existing else 1),
        replan_count=(existing.replan_count + 1 if existing else 0),
        completed_steps=(
            tuple(step for step in existing.steps if step.status.value == "completed")
            if existing else ()
        ),
        previous_steps=existing.steps if existing is not None else (),
    )
