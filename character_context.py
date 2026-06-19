"""
集中管理当前活跃角色名，供记忆系统的所有模块使用。
"""

from pathlib import Path

_CHARACTER_NAME = "default"


def get_character_name() -> str:
    """返回当前活跃角色名。"""
    global _CHARACTER_NAME
    return _CHARACTER_NAME


def set_character_name(name: str) -> None:
    """设置当前活跃角色名。"""
    global _CHARACTER_NAME
    _CHARACTER_NAME = str(name).strip() or "default"


def get_archive_dir() -> Path:
    """获取当前角色的归档目录路径。"""
    return Path("data/archives") / get_character_name()