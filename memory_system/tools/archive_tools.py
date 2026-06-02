"""
Archive tools: search and list historical conversation archives.

功能说明：
    本模块提供两个 LLM 工具，用于搜索和列出通过 compact_manager 自动归档的
    历史对话文件。归档文件为 JSON 格式，存储在 ./data/archives/ 目录下，
    文件名格式为 chat_archive_YYYYMMDD_HHMMSS.json。

工具列表：
    - archive_list:   列出所有归档文件，含时间范围（first_time / last_time）和消息数。
                      这是 AI 定位历史对话的第一步，先看有哪些文件、各覆盖什么时间段。
    - archive_search: 搜索归档，支持关键词（可选）和时间范围过滤（可选）。
                      默认只扫描最新的 10 个归档文件，防止全量扫描。

AI 查询建议路径：
    1. 用户提到某个时间 → 先调用 archive_list 获取所有文件的时间范围
    2. 根据 first_time / last_time 锁定可能包含目标对话的文件
    3. 调用 archive_search，传入 from_time/to_time 缩小范围
       （可同时传 keyword 做内容过滤，或不传 keyword 只看时间段内全部对话）
    4. 返回匹配结果及上下文，用自然语言回复用户

技术要点：
    - 采用纯 Python 标准库 + 项目自身的 ToolManager 注册机制，无额外依赖。
    - 搜索使用简单的子串匹配（大小写不敏感），未来可升级为 jieba 分词索引。
    - 时间提取依赖用户消息中的 [本地时间 YYYY-MM-DD HH:MM:SS] 前缀。
    - 所有磁盘 I/O 异常均被捕获并记录日志，不会中断对话。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 归档文件存放目录，与 compact_manager.py 中保持一致
ARCHIVE_DIR = Path("data/archives")

# 用于从消息内容中提取 [本地时间 YYYY-MM-DD HH:MM:SS] 的正则
_TIME_PATTERN = re.compile(r"\[本地时间\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]")


def _list_archive_files() -> List[Path]:
    """
    获取所有归档文件的路径列表（按文件名倒序，最新的在前）。

    Returns:
        List[Path]: 归档 JSON 文件的 Path 对象列表，若目录不存在则返回空列表。
    """
    if not ARCHIVE_DIR.exists():
        return []
    files = sorted(
        [f for f in ARCHIVE_DIR.glob("chat_archive_*.json") if f.is_file()],
        key=lambda f: f.name,
        reverse=True,  # 最新的在前
    )
    return files


def _parse_archive_timestamp(filename: str) -> str | None:
    """
    从归档文件名中提取并格式化归档触发时间。

    Args:
        filename: 文件名（如 'chat_archive_20260601_001530.json'）

    Returns:
        格式化后的时间字符串（如 '2026-06-01 00:15:30'），
        若解析失败则返回 None。
    """
    try:
        stem = Path(filename).stem
        parts = stem.split("_")
        if len(parts) >= 3:
            date_str = parts[2]
            time_str = parts[3] if len(parts) > 3 else "000000"
            dt = datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


def _extract_time_from_message(msg: Dict[str, Any]) -> Optional[datetime]:
    """
    从单条消息的 content 字段中提取 [本地时间 ...] 并解析为 datetime 对象。

    Args:
        msg: 包含 'content' 字段的消息字典。

    Returns:
        解析成功的 datetime 对象，若未找到或解析失败则返回 None。
    """
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


def _get_archive_time_range(messages: List[Dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    """
    获取归档文件中对话的实际时间范围。

    通过提取所有用户消息中的 [本地时间 ...] 前缀，
    取最早和最晚的时间作为范围边界。

    Args:
        messages: 消息列表。

    Returns:
        (first_time, last_time) 元组，格式为 "YYYY-MM-DD HH:MM:SS"，
        若无法提取则对应位置为 None。
    """
    first_dt: Optional[datetime] = None
    last_dt: Optional[datetime] = None

    for msg in messages:
        dt = _extract_time_from_message(msg)
        if dt:
            if first_dt is None or dt < first_dt:
                first_dt = dt
            if last_dt is None or dt > last_dt:
                last_dt = dt

    first_time = first_dt.strftime("%Y-%m-%d %H:%M:%S") if first_dt else None
    last_time = last_dt.strftime("%Y-%m-%d %H:%M:%S") if last_dt else None
    return first_time, last_time


def _load_archive(filepath: Path) -> List[Dict[str, Any]] | None:
    """
    从指定的 JSON 文件中加载对话消息列表。

    Args:
        filepath: 归档文件的完整路径。

    Returns:
        消息列表（每个元素为包含 'role' 和 'content' 的字典），
        若读取或解析失败则返回 None。
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning(f"Failed to load archive {filepath}: {e}")
    return None


def _search_in_messages(
    messages: List[Dict[str, Any]],
    keyword: Optional[str] = None,
    context: int = 1,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    在消息列表中执行搜索，返回匹配项及其上下文。
    支持按关键词过滤、按时间范围过滤，或两者组合。

    Args:
        messages:  消息列表，每条消息包含 'role' 和 'content' 字段。
        keyword:   搜索关键词（可选）。为 None 或空字符串时不做内容过滤，仅按时间返回。
        context:   每条匹配消息前后包含的上下文消息数量（默认为1）。
        from_time: 时间范围起始（含），格式 "YYYY-MM-DD HH:MM:SS"，为 None 则不限制。
        to_time:   时间范围结束（含），格式同上，为 None 则不限制。

    Returns:
        匹配结果列表，每个元素包含：
            - match_index:   匹配消息在原列表中的索引
            - match_role:    匹配消息的角色
            - match_content: 匹配消息的完整内容
            - match_time:    从匹配消息中提取的时间戳（若存在）
            - context:       包含前后上下文消息的列表
    """
    matches = []
    keyword_lower = (keyword or "").strip().lower() if keyword else ""
    n = len(messages)

    # 解析时间范围
    dt_from = None
    dt_to = None
    if from_time:
        try:
            dt_from = datetime.strptime(from_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if to_time:
        try:
            dt_to = datetime.strptime(to_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    for i, msg in enumerate(messages):
        content = msg.get("content") or ""

        # 内容过滤（仅当有关键词时才执行）
        if keyword_lower and keyword_lower not in content.lower():
            continue

        # 提取该消息的时间
        msg_dt = _extract_time_from_message(msg)
        msg_time = msg_dt.strftime("%Y-%m-%d %H:%M:%S") if msg_dt else None

        # 时间范围过滤
        if dt_from and (msg_dt is None or msg_dt < dt_from):
            continue
        if dt_to and (msg_dt is None or msg_dt > dt_to):
            continue

        # 收集上下文
        start = max(0, i - context)
        end = min(n, i + context + 1)
        context_msgs = []
        for j in range(start, end):
            ctx_time = _extract_time_from_message(messages[j])
            context_msgs.append({
                "index": j,
                "role": messages[j].get("role", ""),
                "content": messages[j].get("content", ""),
                "time": ctx_time.strftime("%Y-%m-%d %H:%M:%S") if ctx_time else None,
            })

        matches.append({
            "match_index": i,
            "match_role": msg.get("role", ""),
            "match_content": msg.get("content", ""),
            "match_time": msg_time,
            "context": context_msgs,
        })

    return matches


# ── LLM 工具函数 ──────────────────────────────────────────────────

def _tool_archive_list() -> list[dict]:
    """
    列出所有可用的归档文件及其基本信息，包括时间范围。

    这是 AI 定位历史对话的第一步：先看有哪些归档文件，
    每个文件覆盖什么时间段（first_time ~ last_time），
    然后根据需要调用 archive_search 在特定时间范围内搜索。

    Returns:
        列表，每个元素包含：
            - file:          文件名
            - archived_at:   归档触发时间（来自文件名）
            - message_count: 消息总数
            - first_time:    文件中最早一条带时间戳的消息时间
            - last_time:     文件中最晚一条带时间戳的消息时间
        若无归档文件，返回空列表。
    """
    files = _list_archive_files()
    result = []
    for fp in files:
        messages = _load_archive(fp)
        count = len(messages) if messages else 0
        archived_at = _parse_archive_timestamp(fp.name)
        first_time, last_time = _get_archive_time_range(messages) if messages else (None, None)
        result.append({
            "file": fp.name,
            "archived_at": archived_at,
            "message_count": count,
            "first_time": first_time,
            "last_time": last_time,
        })
    return result


def _tool_archive_search(
    keyword: str = "",
    limit: int = 5,
    max_files: int = 10,
    from_time: str = "",
    to_time: str = "",
) -> dict[str, Any]:
    """
    搜索归档文件中的历史对话，支持关键词（可选）和时间范围（可选）过滤。

    默认只扫描最新的 10 个归档文件。如需搜索更早的对话，可增大 max_files。
    建议先用 archive_list 确认目标时间段，再用本工具精准搜索。

    Args:
        keyword:   搜索关键词（可选）。为空时返回时间范围内的所有消息。
        limit:     每个归档文件返回的最大匹配数量（默认5）。
        max_files: 最多扫描的归档文件数量（默认10，从最新文件开始扫描）。
        from_time: 时间范围起始（可选），格式 "YYYY-MM-DD HH:MM:SS"。
        to_time:   时间范围结束（可选），格式同上。

    Returns:
        字典，包含：
            - keyword:       使用的搜索关键词（可能为空）
            - from_time:     时间过滤起始（若传入）
            - to_time:       时间过滤结束（若传入）
            - files_scanned: 实际扫描的归档文件数量
            - total_files:   归档文件总数
            - total_matches: 所有文件中的匹配总数
            - results:       每个文件的匹配详情
    """
    keyword = (keyword or "").strip()
    from_time = (from_time or "").strip() or None
    to_time = (to_time or "").strip() or None

    all_files = _list_archive_files()
    if not all_files:
        return {"message": "没有找到任何归档文件", "results": []}

    total_files = len(all_files)
    # 只扫描最新的 max_files 个文件
    files_to_scan = all_files[:max_files]

    all_matches = []
    files_scanned = 0

    for fp in files_to_scan:
        messages = _load_archive(fp)
        if messages is None:
            continue
        files_scanned += 1
        file_matches = _search_in_messages(
            messages,
            keyword=keyword if keyword else None,
            context=1,
            from_time=from_time,
            to_time=to_time,
        )
        if file_matches:
            all_matches.append({
                "file": fp.name,
                "archived_at": _parse_archive_timestamp(fp.name),
                "total_messages": len(messages),
                "matches": file_matches[:limit],
            })

    return {
        "keyword": keyword if keyword else None,
        "from_time": from_time,
        "to_time": to_time,
        "files_scanned": files_scanned,
        "total_files": total_files,
        "total_matches": sum(len(f["matches"]) for f in all_matches),
        "results": all_matches,
    }


# ── 工具注册 ──────────────────────────────────────────────────────

def _register_archive_tools():
    """
    将本模块提供的工具函数注册到全局 ToolManager 单例。

    分组为 'archive'，低风险等级。
    模块导入时自动调用此函数。
    """
    try:
        from llm.tools.tool_manager import ToolManager
        tm = ToolManager()
        tm.register_function(
            _tool_archive_list,
            name="archive_list",
            group="default",
            description=(
                "List all available conversation archive files with their time ranges and message counts. "
                "Each result includes: file name, archived_at (when compaction happened), "
                "message_count, first_time (earliest message timestamp in the file), "
                "and last_time (latest message timestamp in the file). "
                "Call this FIRST when the user asks about a past conversation or mentions a specific date/time. "
                "Use the first_time and last_time fields to identify which archive file(s) cover the time period "
                "the user is interested in. Then call archive_search with appropriate from_time/to_time filters. "
                "If no time is mentioned, still call this first to see available archives before searching."
            ),
            risk="low",
        )
        tm.register_function(
            _tool_archive_search,
            name="archive_search",
            group="default",
            description=(
                "Search historical conversation archives. "
                "Parameters: keyword (optional, empty = return all messages in time range), "
                "limit (default 5, max matches per file), "
                "max_files (default 10, only scans the most recent N archives to avoid performance issues), "
                "from_time (optional, format 'YYYY-MM-DD HH:MM:SS'), "
                "to_time (optional, same format). "
                "Best practice: call archive_list first to see available time ranges, "
                "then call archive_search with from_time/to_time to narrow the search. "
                "Use keyword when looking for specific topics; omit keyword when you want to see "
                "all conversations within a time period. "
                "Only increase max_files if the user explicitly asks to search older archives."
            ),
            risk="low",
        )
        logger.info("Archive tools registered successfully.")
    except Exception as e:
        logger.error(f"Failed to register archive tools: {e}")


# 模块导入时自动执行注册
_register_archive_tools()
