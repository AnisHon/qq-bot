from __future__ import annotations

from app.progress.coordinator import ProgressCandidate, ProgressMode
from app.progress.events import ToolCategory, ToolOutcome


MAX_STATUS_CHARS = 240

_CATEGORY_TEXT = {
    ToolCategory.WEB_SEARCH: "网页资料",
    ToolCategory.WEB_READ: "网页正文",
    ToolCategory.NEWS_SEARCH: "相关新闻",
    ToolCategory.WIKI: "本地知识",
    ToolCategory.RECIPE: "配方",
    ToolCategory.MACHINE: "机器配方",
    ToolCategory.REPOSITORY: "代码仓库",
    ToolCategory.SFM_VALIDATION: "SFM 程序",
    ToolCategory.WEATHER: "天气数据",
    ToolCategory.OTHER: "相关内容",
}
_CATEGORY_ACTION = {
    ToolCategory.WEB_SEARCH: "查找",
    ToolCategory.WEB_READ: "读取",
    ToolCategory.NEWS_SEARCH: "搜索",
    ToolCategory.WIKI: "检索",
    ToolCategory.RECIPE: "查询",
    ToolCategory.MACHINE: "查询",
    ToolCategory.REPOSITORY: "搜索",
    ToolCategory.SFM_VALIDATION: "校验",
    ToolCategory.WEATHER: "查询",
    ToolCategory.OTHER: "处理",
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
    if candidate.commentary is not None:
        text = candidate.commentary.text
        return text.replace("[CQ:", "［CQ:").replace("[cq:", "［cq:")[:MAX_STATUS_CHARS]
    active = tuple(dict.fromkeys(candidate.categories))
    if candidate.active_tools:
        if len(active) > 1:
            labels = "和".join(_CATEGORY_TEXT.get(item, "相关内容") for item in active[:2])
            suffix = "等" if len(active) > 2 else ""
            text = f"正在{_CATEGORY_ACTION.get(active[0], '处理')}{labels}{suffix}……"
        elif len(active) == 1:
            category = active[0]
            text = f"正在{_CATEGORY_ACTION.get(category, '处理')}{_CATEGORY_TEXT.get(category, '相关内容')}……"
        else:
            text = "正在处理相关内容……"
    elif candidate.active_models:
        if candidate.outcomes:
            text = "已有查询结果，我正在整理答复……"
        else:
            text = "我在结合已有上下文整理答复……"
    else:
        text = "我在根据已有结果继续处理……"

    counts = [(kind, count) for kind, count in candidate.outcomes if count > 0]
    if counts:
        total = sum(count for _, count in counts)
        if candidate.mode == ProgressMode.VERBOSE:
            parts = [f"{count} 项{_OUTCOME_TEXT.get(kind, '状态未知')}" for kind, count in counts]
            text += f"\n已有 {total} 项处理返回：" + "，".join(parts) + "。"
        else:
            text += f"\n已有 {total} 项处理结束。"
    # Progress must never interpret CQ-looking untrusted content as a segment.
    text = text.replace("[CQ:", "［CQ:").replace("[cq:", "［cq:")
    return text[:MAX_STATUS_CHARS]
