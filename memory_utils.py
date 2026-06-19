"""记忆系统公共工具：时间戳提取、提示词规则构建等。"""
import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# 匹配消息中的 [本地时间 YYYY-MM-DD HH:MM:SS]
_TIME_PATTERN = re.compile(r"\[本地时间\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]")


def extract_time_from_message(msg: Dict[str, Any]) -> Optional[datetime]:
    """从单条消息的 content 字段提取时间戳，返回 datetime 或 None。"""
    content = msg.get("content", "")
    if not isinstance(content, str):
        return None
    match = _TIME_PATTERN.search(content)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def build_memory_prompt_rules() -> Tuple[str, str, str]:
    """构建记忆系统提示词规则，返回 (diary_recall, memory_priority, archive_tool_desc)。"""
    diary_recall_rule = (
        "日记辅助回想规则：\n"
        "当用户提到过去的事、某个日期，或当前话题可能与历史相关时：\n"
        "0. 优先级：先查日记，再查归档。日记可快速判断当日是否有相关内容。\n"
        "1. 调用 archive_list 查看可用的 diary_*.json 和 chat_archive_*.json 文件。\n"
        "2. 若存在相关日期的日记，调用 diary_read 读取日记内容。\n"
        "3. 根据日记的 summary、keywords、notable_events 判断：\n"
        "   a. 与当前话题无关 → 不主动提起。\n"
        "   b. 轻度相关，但不需要原文 → 用日记中的摘要信息轻描淡写带一句。\n"
        "   c. 高度相关，需原文细节 → 调用 archive_search，以日记 date 为范围"
        "（当天04:00 到次日03:59），用关键词搜索原始对话，自然融入回复。\n"
        "4. 若没有对应日记但用户坚持，可直接尝试 archive_search 搜索大致时间范围。\n"
    )

    memory_priority_rule = (
        "记忆查询规则：\n"
        "当用户询问过去发生的事或提到某个时间，必须严格遵守以下优先级："
        "archive 工具 > memory 工具。只有当 archive_list 返回\"没有找到任何归档文件\"时，"
        "才允许尝试调用 memory_search。\n"
    )

    archive_tool_desc = (
        "以下记忆工具位于 memory 工具组中，与 memory_search 并列，可直接调用：\n"
        "- **archive_list**：列出所有归档文件和日记文件，含时间范围和消息数。"
        "用于确认可用数据。\n"
        "- **archive_search**：搜索历史对话归档。参数：keyword(可选), limit(默认5), "
        "max_files(默认10), from_time, to_time。\n"
        "- **diary_read**：读取指定日期的日记（格式 YYYYMMDD）。"
        "返回日记摘要、关键词、氛围与重要事件。用于快速了解某日话题，避免盲目搜索。\n"
    )

    return diary_recall_rule, memory_priority_rule, archive_tool_desc