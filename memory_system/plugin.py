"""
记忆系统插件：为 AI 桌宠提供长期记忆管理能力。
包括自动归档、每日日记、精准搜索和随机模糊回忆。
"""

from pathlib import Path

from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry
from sdk.types import (
    OutputContractPatch,
    RequirementPatch,
    RequirementSpec,
)

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
        # ── 1. 注册 LLM 工具 ──────────────────────────────────────
        self._register_tools(register)

        # ── 2. 注入系统提示词规则 ─────────────────────────────────
        self._patch_prompt(register)

    # ── 工具注册 ──────────────────────────────────────────────────

    def _register_tools(self, register: PluginCapabilityRegistry) -> None:
        """直接从插件目录加载并注册工具，不依赖 llm/tools/ 的导入链"""
        from plugins.memory_system.tools.archive_tools import _register_archive_tools
        from plugins.memory_system.tools.diary_tools import _register_diary_tools

        def _do_register(tm):
            _register_archive_tools()
            _register_diary_tools()

        register.register_llm_tool(_do_register)

    # ── 提示词注入 ────────────────────────────────────────────────

    def _patch_prompt(self, register: PluginCapabilityRegistry) -> None:
        """以 Patch 形式向系统提示词追加记忆相关的规则"""

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

        random_recall_rule = (
            "随机回忆规则：\n"
            "偶尔你会看到以【随机回忆】开头的系统消息，这是程序自动从历史日记中"
            "随机检索到的记忆片段。\n"
            "处理方式：\n"
            "1. 如果这段记忆与当前话题相关，且提起来自然 → 在回复中顺带提一句。\n"
            "   参考语气：\"说起来……\"\"上次你也……\"\"这让我想起……\"\"我记得那天……\"\n"
            "2. 如果这段记忆与当前话题无关，或者提起来会显得生硬 → 完全忽略，正常回复即可。\n"
            "   不要为了插入而插入，宁可错过一次回忆，也不要让对话变得不自然。\n"
            "3. 如果用户没有接你的回忆话题，不要继续追问，自然过渡回当前对话。\n"
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

        memory_priority_rule = (
            "记忆查询规则：\n"
            "当用户询问过去发生的事或提到某个时间，必须严格遵守以下优先级："
            "archive 工具 > memory 工具。只有当 archive_list 返回\"没有找到任何归档文件\"时，"
            "才允许尝试调用 memory_search。\n"
        )

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
