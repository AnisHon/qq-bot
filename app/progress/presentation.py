from __future__ import annotations

from app.progress.coordinator import ProgressCandidate, ProgressMode
from app.progress.events import ToolCategory, ToolOutcome


MAX_STATUS_CHARS = 240

_CATEGORY_TEXT = {
    ToolCategory.WEB_SEARCH: "网页搜索",
    ToolCategory.WEB_READ: "网页读取",
    ToolCategory.NEWS_SEARCH: "新闻检索",
    ToolCategory.WIKI: "本地知识检索",
    ToolCategory.RECIPE: "配方查询",
    ToolCategory.MACHINE: "机器配方查询",
    ToolCategory.REPOSITORY: "仓库检索",
    ToolCategory.SFM_VALIDATION: "SFM 校验",
    ToolCategory.WEATHER: "天气查询",
    ToolCategory.OTHER: "相关工具",
}
_OUTCOME_TEXT = {
    ToolOutcome.SUCCESS: "成功",
    ToolOutcome.PARTIAL: "部分完成",
    ToolOutcome.NO_RESULTS: "无结果",
    ToolOutcome.UNAVAILABLE: "暂不可用",
    ToolOutcome.DENIED: "被策略拒绝",
    ToolOutcome.TIMEOUT: "超时",
    ToolOutcome.ERROR: "失败",
    ToolOutcome.CANCELLED: "已取消",
    ToolOutcome.REJECTED: "未获准执行",
    ToolOutcome.UNKNOWN: "状态未知",
}


def render_progress(candidate: ProgressCandidate) -> str:
    """Render only static categories and bounded outcome counts from real events."""
    active = tuple(dict.fromkeys(candidate.categories))
    if candidate.active_tools:
        if candidate.mode == ProgressMode.VERBOSE and len(active) > 1:
            labels = "、".join(_CATEGORY_TEXT.get(item, "相关工具") for item in active[:3])
            text = f"正在并行进行{labels}等资料处理……"
        elif candidate.mode == ProgressMode.VERBOSE and len(active) == 1:
            text = f"正在进行{_CATEGORY_TEXT.get(active[0], '相关工具')}……"
        else:
            text = "正在处理相关资料……"
    elif candidate.active_models:
        if candidate.outcomes:
            text = "已完成工具处理，正在整理结果……"
        else:
            text = "正在生成回复……"
    else:
        text = "任务仍在运行，正在处理已取得的信息……"

    counts = [(kind, count) for kind, count in candidate.outcomes if count > 0]
    if counts:
        total = sum(count for _, count in counts)
        if candidate.mode == ProgressMode.VERBOSE:
            parts = [f"{count} 项{_OUTCOME_TEXT.get(kind, '状态未知')}" for kind, count in counts]
            text += f"\n已结束 {total} 项处理：" + "，".join(parts) + "。"
        else:
            text += f"\n已结束 {total} 项工具处理。"
    # Progress must never interpret CQ-looking untrusted content as a segment.
    text = text.replace("[CQ:", "［CQ:").replace("[cq:", "［cq:")
    return text[:MAX_STATUS_CHARS]
