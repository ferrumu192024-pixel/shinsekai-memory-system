"""
记忆系统插件：为 AI 桌宠提供长期记忆管理能力。
包括自动归档、每日日记、精准搜索和随机模糊回忆。
"""

from pathlib import Path

from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry

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
        # 新版 sdk 暂不支持 OutputContractPatch，暂时手动改角色模板
        # self._patch_prompt(register)

    # ── 工具注册 ──────────────────────────────────────────────────

    def _register_tools(self, register: PluginCapabilityRegistry) -> None:
        """直接从插件目录加载并注册工具，不依赖 llm/tools/ 的导入链"""
        from plugins.memory_system.tools.archive_tools import _register_archive_tools
        from plugins.memory_system.tools.diary_tools import _register_diary_tools

        def _do_register(tm):
            _register_archive_tools()
            _register_diary_tools()

        register.register_llm_tool(_do_register)