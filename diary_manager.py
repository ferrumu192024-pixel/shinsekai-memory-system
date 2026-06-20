"""
日记管理器：在路径确认后，为最近两周内缺失的日期补写日记。
日记以"对话日"为单位，每天凌晨 4:00 到次日凌晨 3:59 为一个对话日。
有消息则生成正常日记，无消息则生成空日记占位。
生成日记时，合并热记忆（当前上下文）和当前存档指纹目录下所有归档文件（冷记忆）中属于该日的消息。
"""

import json
import os
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from plugins.memory_system.character_context import get_archive_dir
from plugins.memory_system.memory_utils import extract_time_from_message

logger = logging.getLogger(__name__)


class DiaryManager:
    """管理日记的补写与存储。"""

    SCAN_RANGE_DAYS = 14  # 补写范围：从昨天往回推 14 天

    def __init__(self):
        # 延迟初始化，不再在构造时持有 llm_adapter 或创建目录
        pass

    def _get_llm_adapter(self):
        """每次调用时从运行时获取 LLM 适配器，避免初始化时依赖。"""
        try:
            from core.runtime.app_runtime import try_get_app_runtime
            runtime = try_get_app_runtime()
            if runtime and hasattr(runtime, "llm_manager"):
                return runtime.llm_manager.llm_adapter
        except Exception:
            logger.exception("获取 LLM 适配器失败")
        return None

    def _get_previous_day_str(self) -> str:
        """
        返回昨天的日期字符串（刚完结的对话日）。
        规则：每天凌晨 4:00 划断。
        """
        now = datetime.now()
        if now.hour < 4:
            current_diary_date = now.date() - timedelta(days=1)
        else:
            current_diary_date = now.date()
        target_date = current_diary_date - timedelta(days=1)
        return target_date.strftime("%Y%m%d")

    def _get_scan_start_date(self) -> str:
        """返回扫描范围的起始日期：昨天往回推 SCAN_RANGE_DAYS 天。"""
        yesterday = datetime.strptime(self._get_previous_day_str(), "%Y%m%d").date()
        start = yesterday - timedelta(days=self.SCAN_RANGE_DAYS - 1)
        return start.strftime("%Y%m%d")

    def _get_existing_diary_dates(self) -> set:
        """扫描当前指纹目录，返回已有日记文件的日期集合。"""
        archive_dir = get_archive_dir()
        if archive_dir is None or not archive_dir.is_dir():
            return set()
        existing = set()
        for fp in archive_dir.glob("diary_*.json"):
            try:
                date_str = fp.stem.replace("diary_", "")
                if len(date_str) == 8 and date_str.isdigit():
                    existing.add(date_str)
            except Exception:
                pass
        return existing

    def _date_range(self, start_date: str, end_date: str) -> List[str]:
        """生成从 start_date 到 end_date（含）的所有日期字符串列表。"""
        if not start_date or not end_date:
            return []
        try:
            start = datetime.strptime(start_date, "%Y%m%d").date()
            end = datetime.strptime(end_date, "%Y%m%d").date()
        except ValueError:
            return []
        if start > end:
            return []
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return dates

    # ── 对外暴露的唯一入口 ──────────────────────────────────────

    def generate_all_missing_diaries(self) -> None:
        """
        扫描当前指纹目录，为最近两周内缺失的日期补写日记。
        路径未确认时跳过。
        """
        archive_dir = get_archive_dir()
        if archive_dir is None:
            logger.info("存档指纹未确认，跳过日记补写。")
            return

        os.makedirs(archive_dir, exist_ok=True)

        scan_start = self._get_scan_start_date()
        yesterday = self._get_previous_day_str()

        all_dates = self._date_range(scan_start, yesterday)
        if not all_dates:
            logger.info("无待补写日期范围。")
            return

        existing_dates = self._get_existing_diary_dates()

        for date_str in all_dates:
            if date_str in existing_dates:
                continue

            diary_path = archive_dir / f"diary_{date_str}.json"
            self._generate_for_date(date_str, diary_path)

    # ── 单日生成 ────────────────────────────────────────────────

    def _generate_for_date(self, date_str: str, diary_path: Path) -> None:
        """为指定日期生成日记：合并热记忆 + 当前指纹目录下所有归档文件中该日期的消息。"""
        # 从运行时获取当前热记忆
        hot_messages = self._get_hot_messages_by_date(date_str)
        cold_messages = self._load_archive_messages_by_date(date_str)
        all_messages = self._merge_messages(hot_messages, cold_messages)

        if all_messages:
            logger.info(
                f"准备为 {date_str} 生成日记 "
                f"（热记忆 {len(hot_messages)} 条，冷记忆 {len(cold_messages)} 条，"
                f"合并后 {len(all_messages)} 条）..."
            )
            self._generate_diary(date_str, all_messages, diary_path)
        else:
            logger.info(f"日期 {date_str} 无对话记录，生成空日记占位。")
            self._write_empty_diary(date_str, diary_path)

    def _get_hot_messages_by_date(self, date_str: str) -> List[Dict]:
        """从当前热记忆中获取指定日期的消息。"""
        try:
            from core.runtime.app_runtime import try_get_app_runtime
            runtime = try_get_app_runtime()
            if runtime and hasattr(runtime, "llm_manager"):
                messages = runtime.llm_manager.get_messages()
                return self._filter_messages_by_date(messages, date_str)
        except Exception:
            logger.exception("获取热记忆失败")
        return []

    # ── 以下方法从旧代码保留，逻辑不变 ──────────────────────────

    def _filter_messages_by_date(self, messages: List[Dict], date_str: str) -> List[Dict]:
        """从消息列表中筛选出属于指定对话日的消息。"""
        try:
            target_date = datetime.strptime(date_str, "%Y%m%d").date()
            day_start = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=4)
            day_end = day_start + timedelta(days=1)
        except ValueError:
            return []

        filtered = []
        for msg in messages:
            msg_time = extract_time_from_message(msg)
            if msg_time is not None and day_start <= msg_time < day_end:
                filtered.append(msg)
        return filtered

    def _load_archive_messages_by_date(self, date_str: str) -> List[Dict]:
        """从当前指纹目录下所有归档文件中加载属于指定日期的消息。"""
        archive_dir = get_archive_dir()
        if archive_dir is None or not archive_dir.is_dir():
            return []

        try:
            target_date = datetime.strptime(date_str, "%Y%m%d").date()
            day_start = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=4)
            day_end = day_start + timedelta(days=1)
        except ValueError:
            return []

        cold_messages = []
        archive_files = sorted(archive_dir.glob("chat_archive_*.json"))

        for fp in archive_files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    continue
                for msg in data:
                    msg_time = extract_time_from_message(msg)
                    if msg_time is not None and day_start <= msg_time < day_end:
                        cold_messages.append(msg)
            except Exception as e:
                logger.warning(f"读取归档文件 {fp} 失败: {e}")
                continue

        return cold_messages

    def _merge_messages(self, hot: List[Dict], cold: List[Dict]) -> List[Dict]:
        """合并热记忆和冷记忆，按时间戳排序并去重。"""
        seen = set()
        merged = []

        for msg in cold + hot:
            content = msg.get("content", "")
            clean = re.sub(
                r"\[本地时间\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\n?",
                "",
                content
            )
            if clean not in seen:
                seen.add(clean)
                merged.append(msg)

        merged.sort(key=lambda m: extract_time_from_message(m) or datetime.min)
        return merged

    def _write_empty_diary(self, date_str: str, diary_path: Path) -> None:
        """写入空日记占位文件。"""
        empty_diary = {
            "date": date_str,
            "summary": "",
            "keywords": [],
            "mood": "",
            "notable_events": [],
            "empty": True,
        }
        try:
            with open(diary_path, "w", encoding="utf-8") as f:
                json.dump(empty_diary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"写入空日记失败 {date_str}: {e}")

    def _generate_diary(self, date_str: str, messages: List[Dict], diary_path: Path) -> None:
        """调用 LLM 生成日记并保存到文件。"""
        try:
            logger.info(f"开始生成日记: {date_str}")
            prompt = self._build_diary_prompt(messages, date_str)
            diary_content = self._call_llm_for_diary(prompt)

            if diary_content:
                with open(diary_path, "w", encoding="utf-8") as f:
                    json.dump(diary_content, f, ensure_ascii=False, indent=2)
                logger.info(f"日记已保存: {diary_path}")
            else:
                logger.error(f"日记生成失败: {date_str}（LLM 返回为空）")
        except Exception as e:
            logger.error(f"日记生成异常: {e}")

    def _build_diary_prompt(self, messages: List[Dict], date_str: str) -> str:
        """构建生成日记的 prompt。"""
        conversation = ""
        for msg in messages:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = msg.get("content", "")
            clean = re.sub(r"\[本地时间\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\n?", "", content)
            conversation += f"{role}: {clean}\n"

        prompt = f"""请根据以下对话历史，为 {date_str} 生成一份简洁的日记总结。

要求：
1. 输出必须严格为 JSON 格式，不要包含任何 markdown 代码块标记。
2. JSON 结构如下：
{{
    "date": "{date_str}",
    "summary": "用 2-3 句话概括今天的主要话题和发生的事情",
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "mood": "整体氛围，如：轻松愉快、紧张严肃、感性深沉",
    "notable_events": ["值得记录的事件1", "事件2"]
}}
3. 关键词要能够反映出对话的核心内容，方便日后搜索。
4. 用中文撰写。

对话历史：
{conversation}
"""
        return prompt

    def _call_llm_for_diary(self, prompt: str) -> Optional[Dict]:
        """调用 LLM 并解析返回的 JSON。"""
        llm = self._get_llm_adapter()
        if llm is None:
            logger.error("无法获取 LLM 适配器，跳过日记生成。")
            return None

        try:
            messages = [
                {"role": "system", "content": "你是一个专业的日记助手，负责将对话历史总结为 JSON 格式的日记。请严格遵守 JSON 输出格式。"},
                {"role": "user", "content": prompt}
            ]
            response = llm.chat(messages, stream=False)

            if not response:
                return None

            content = ""
            if hasattr(response, 'choices') and response.choices:
                content = response.choices[0].message.content
            elif isinstance(response, str):
                content = response

            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            return json.loads(content.strip())

        except json.JSONDecodeError as e:
            logger.error(f"解析日记 JSON 失败: {e}")
        except Exception as e:
            logger.error(f"调用 LLM 生成日记失败: {e}")
        return None