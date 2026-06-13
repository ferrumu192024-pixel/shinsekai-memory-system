"""
记忆系统插件：为 AI 桌宠提供长期记忆管理能力。
包括自动归档、每日日记、精准搜索和随机模糊回忆。
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

from plugins.memory_system.character_context import set_character_name

# ── 插件级管理器引用（避免挂在 llm_manager 上） ────────────────
_diary_manager = None
_random_recall = None


def _on_before_compact(messages: list) -> None:
    """精简前钩子：将完整对话写入归档文件。"""
    try:
        from datetime import datetime
        import json
        from plugins.memory_system.character_context import get_archive_dir

        archive_dir = get_archive_dir()
        archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"chat_archive_{timestamp}.json"

        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

        logging.getLogger("random_recall").info(
            f"归档已保存：{archive_path}（{len(messages)} 条消息）"
        )
    except Exception:
        logging.getLogger("random_recall").exception(
            "精简前归档写入失败，跳过本次归档（不影响精简流程）"
        )


def _get_or_create_managers():
    """延迟初始化日记管理器和随机回忆管理器，保存在模块级变量中。"""
    global _diary_manager, _random_recall

    if _diary_manager is not None and _random_recall is not None:
        return

    try:
        from core.runtime.app_runtime import try_get_app_runtime
        runtime = try_get_app_runtime()
        if not runtime or not hasattr(runtime, "llm_manager"):
            return
        llm = runtime.llm_manager

        if _diary_manager is None:
            from plugins.memory_system.diary_manager import DiaryManager
            _diary_manager = DiaryManager(llm.llm_adapter)

        if _random_recall is None:
            from plugins.memory_system.random_recall import RandomRecallManager
            _random_recall = RandomRecallManager(llm.llm_adapter, probability=0.03)
    except Exception:
        logging.getLogger("random_recall").exception(
            "Failed to initialize diary/recall managers"
        )


def _memory_processor(user_input: str) -> str | None:
    """用户输入处理器：触发日记检查 + 随机回忆。"""
    try:
        _get_or_create_managers()

        if _diary_manager is not None:
            from core.runtime.app_runtime import try_get_app_runtime
            runtime = try_get_app_runtime()
            if runtime and hasattr(runtime, "llm_manager"):
                _diary_manager.check_and_generate(
                    runtime.llm_manager.get_messages()
                )

        if _random_recall is not None:
            recall = _random_recall.try_recall(user_input)
            if recall:
                return (
                    user_input
                    + f"\n【随机回忆】你突然想起了 {recall['date']} 的一件事："
                    + f"{recall['summary']} "
                    + "如果和当前话题相关且提起来自然，你可以顺带提一句；"
                    + "如果不相关或不自然，忽略即可。"
                )

        return user_input
    except Exception:
        logging.getLogger("random_recall").exception(
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
        return "0.1.0"

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
        # ── 0. 获取当前角色名，设置记忆隔离 ──────────────────────
        character_name = self._resolve_character_name(host)
        self._set_character(character_name)

        # ── 1. 注册 LLM 工具 ──────────────────────────────────────
        self._register_tools(register)

        # ── 2. 注入系统提示词规则 ─────────────────────────────────
        self._patch_prompt(register)

        # ── 3. 注册消息处理器（日记检查 + 随机回忆） ─────────────
        register.register_user_input_processor(_memory_processor)

        # ── 4. 注册精简前归档钩子 ─────────────────────────────────
        register.compact_hooks.append(_on_before_compact)

    # ── 角色名解析 ────────────────────────────────────────────────

    def _resolve_character_name(self, host: PluginHostContext) -> str:
        """从宿主上下文解析当前活跃角色名。"""
        # 优先从 host 直接获取
        name = getattr(host, "character_name", None)
        if name:
            return str(name).strip()

        # 备选：从 AppRuntime 的配置中读取第一个角色名
        try:
            from core.runtime.app_runtime import try_get_app_runtime
            runtime = try_get_app_runtime()
            if runtime and hasattr(runtime, "config"):
                characters = runtime.config.config.characters
                if characters:
                    first = characters[0]
                    if hasattr(first, "name"):
                        return str(first.name).strip()
                    return str(first).strip()
        except Exception:
            pass

        return "default"

    def _set_character(self, character_name: str) -> None:
        """将当前角色名传递给记忆系统的所有模块（通过 character_context）。"""
        set_character_name(character_name)

    # ── 工具注册 ──────────────────────────────────────────────────

    def _register_tools(self, register: PluginCapabilityRegistry) -> None:
        """直接从插件目录加载并注册工具，不依赖 llm/tools/ 的导入链"""
        from plugins.memory_system.tools.archive_tools import _register_archive_tools
        from plugins.memory_system.tools.diary_tools import _register_diary_tools

        def _do_register(tm):
            _register_archive_tools(tm)
            _register_diary_tools(tm)

        register.register_llm_tool(_do_register)

    # ── 提示词注入 ────────────────────────────────────────────────

    def _patch_prompt(self, register: PluginCapabilityRegistry) -> None:
        """以 Patch 形式向系统提示词追加记忆相关的规则"""
        from plugins.memory_system.memory_utils import build_memory_prompt_rules

        diary_recall_rule, random_recall_rule, memory_priority_rule, archive_tool_desc = \
            build_memory_prompt_rules()

        combined_text = (
            archive_tool_desc + "\n"
            + diary_recall_rule + "\n"
            + memory_priority_rule + "\n"
            + random_recall_rule
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
                    id="memory_system.random_recall",
                    text=random_recall_rule.strip(),
                    order=81,
                ),
                RequirementSpec(
                    id="memory_system.archive_priority",
                    text=memory_priority_rule.strip(),
                    order=82,
                ),
            ),
        )
        register.register_output_contract_patch(patch)
