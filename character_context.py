"""
集中管理当前对话的存档指纹，供记忆系统的所有模块使用。
指纹来源：宿主存档文件名（从 data/chat_history/*.json.tmp 推断）。
目录名直接使用存档文件的主文件名（去掉 .json 后缀），方便用户自行查找。
"""

from pathlib import Path

_FINGERPRINT: str | None = None
_FINGERPRINT_CONFIRMED = False

# 用于延迟到第二条消息才确认路径的计数器
_msg_count = 0
_PATH_CONFIRM_AT_COUNT = 2


def _scan_latest_tmp() -> Path | None:
    """扫描 data/chat_history/ 下 mtime 最新的 .json.tmp 文件，返回其 Path，无则返回 None。"""
    chat_dir = Path("data/chat_history")
    if not chat_dir.is_dir():
        return None
    tmp_files = list(chat_dir.glob("*.json.tmp"))
    if not tmp_files:
        return None
    # 按最后修改时间降序，取最新
    tmp_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return tmp_files[0]


def get_archive_dir() -> Path | None:
    """返回当前对话的归档目录路径，指纹未确认时返回 None。"""
    global _FINGERPRINT_CONFIRMED, _FINGERPRINT
    if not _FINGERPRINT_CONFIRMED or _FINGERPRINT is None:
        return None
    return Path("data/archives") / _FINGERPRINT


def resolve_fingerprint() -> str | None:
    """
    尝试从 data/chat_history/*.json.tmp 解析当前存档指纹。
    仅在 _msg_count 达到 _PATH_CONFIRM_AT_COUNT 时才执行实际扫描。
    成功则缓存指纹并标记已确认，返回指纹；失败返回 None。
    """
    global _FINGERPRINT, _FINGERPRINT_CONFIRMED, _msg_count

    _msg_count += 1
    if _msg_count < _PATH_CONFIRM_AT_COUNT:
        return None
    # 达到阈值后不再递增，避免无限增长
    if _msg_count > _PATH_CONFIRM_AT_COUNT:
        _msg_count = _PATH_CONFIRM_AT_COUNT

    # 如果已经确认过，直接返回缓存的指纹
    if _FINGERPRINT_CONFIRMED and _FINGERPRINT is not None:
        return _FINGERPRINT

    # 未确认则尝试扫描
    latest_tmp = _scan_latest_tmp()
    if latest_tmp is None:
        return None

    # 从 tmp 文件名推导正式存档文件名（去掉 .tmp），取 stem 作为指纹
    # 例如 "123.json.tmp" → "123.json" → "123"
    archive_name = latest_tmp.name.replace(".tmp", "")
    fingerprint = Path(archive_name).stem

    if not fingerprint:
        return None

    _FINGERPRINT = fingerprint
    _FINGERPRINT_CONFIRMED = True
    return fingerprint