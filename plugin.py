"""
记忆系统插件：为 AI 桌宠提供长期记忆管理能力。
包括自动归档、每日日记、精准搜索。
记忆归属按宿主存档文件隔离，通过 data/chat_history/*.json.tmp 推断当前存档指纹。
"""

import logging
from pathlib import Path

from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry
from sdk.types import (
    OutputContractPatch,
    RequirementPatch,
    RequirementSpec,
)


def _on_before_compact(messages: list) -> None:
    """精简前钩子：将完整对话写入当前指纹目录下的归档文件。"""
    try:
        from datetime import datetime
        import json
        from plugins.memory_system.character_context import get_archive_dir

        archive_dir = get_archive_dir()
        if archive_dir is None:
            logging.getLogger("memory_system").warning(
                "存档指纹未确认，跳过精简前归档。"
            )
            return

        archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"chat_archive_{timestamp}.json"

        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

        logging.getLogger("memory_system").info(
            f"归档已保存：{archive_path}（{len(messages)} 条消息）"
        )
    except Exception:
        logging.getLogger("memory_system").exception(
            "精简前归档写入失败，跳过本次归档（不影响精简流程）"
        )


def _memory_processor(user_input: str) -> str | None:
    """用户输入处理器：尝试确认存档指纹，首次确认后触发日记补写。"""
    try:
        from plugins.memory_system.character_context import (
            resolve_fingerprint,
            get_archive_dir,
        )

        # resolve_fingerprint 内部会在第二条消息时才实际扫描 .tmp
        fingerprint = resolve_fingerprint()

        # 如果本次调用刚好确认了指纹（返回值非 None 且此前未确认），触发一次日记补写
        if fingerprint is not None:
            archive_dir = get_archive_dir()
            if archive_dir is not None:
                from plugins.memory_system.diary_manager import DiaryManager
                dm = DiaryManager()
                dm.generate_all_missing_diaries()

        return user_input
    except Exception:
        logging.getLogger("memory_system").exception(
            "memory_processor failed, returning original user input"
        )
        return user_input


# ── 插件基本信息 ──────────────────────────────────────────────────

class MemorySystemPlugin(PluginBase):
    @property
    def plugin_id(self) -> str:
        return "com.ferr.memory_system"

    @property
    def plugin_version(self) -> str:
        return "0.2.0"

    @property
    def plugin_name(self) -> str:
        return "长期记忆系统"

    @property
    def priority(self) -> int:
        return 100

    def initialize(
        self,
        register: PluginCapabilityRegistry,
        plugin_root: Path,
        host: PluginHostContext,
    ) -> None:
        # ── 1. 注册 LLM 工具 ──────────────────────────────────────
        self._register_tools(register)

        # ── 2. 注入系统提示词规则 ─────────────────────────────────
        self._patch_prompt(register)

        # ── 3. 注册消息处理器（指纹确认 + 日记补写） ─────────────
        register.register_user_input_processor(_memory_processor)

        # ── 4. 注册精简前归档钩子（使用专用方法，避免追加到副本） ─
        register.register_compact_hook(_on_before_compact)

    # ── 工具注册 ──────────────────────────────────────────────────

    def _register_tools(self, register: PluginCapabilityRegistry) -> None:
        """直接从插件目录加载并注册工具。"""
        from plugins.memory_system.tools.archive_tools import _register_archive_tools
        from plugins.memory_system.tools.diary_tools import _register_diary_tools

        def _do_register(tm):
            _register_archive_tools(tm)
            _register_diary_tools(tm)

        register.register_llm_tool(_do_register)

    # ── 提示词注入 ────────────────────────────────────────────────

    def _patch_prompt(self, register: PluginCapabilityRegistry) -> None:
        """以 Patch 形式向系统提示词追加记忆相关的规则。"""
        from plugins.memory_system.memory_utils import build_memory_prompt_rules

        diary_recall_rule, memory_priority_rule, archive_tool_desc = \
            build_memory_prompt_rules()

        combined_text = (
            archive_tool_desc + "\n"
            + diary_recall_rule + "\n"
            + memory_priority_rule + "\n"
        )

        patch = OutputContractPatch(
            id="memory_system.prompt_rules",
            target_contract="default.dialog.v1",
            priority=50.0,
            requirement_patches={
                "r_speech": RequirementPatch(
                    mode="append",
                    text=combined_text,
                ),
            },
            add_requirements=(
                RequirementSpec(
                    id="memory_system.diary_recall",
                    text=diary_recall_rule.strip(),
                    order=80,
                ),
                RequirementSpec(
                    id="memory_system.archive_priority",
                    text=memory_priority_rule.strip(),
                    order=82,
                ),
            ),
        )
        register.register_output_contract_patch(patch)