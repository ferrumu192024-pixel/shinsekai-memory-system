"""
日记管理器：自动为每一天的对话生成一份日记摘要。
日记以"对话日"为单位，每天凌晨 4:00 到次日凌晨 3:59 为一个对话日。
每 10 条消息检查一次，自动补写最近两周内所有缺失日期的日记（跳过已有日记）。
有消息则生成正常日记，无消息则生成空日记占位，确保日记名单的完整性。
生成日记时，自动合并热记忆（当前上下文）和冷记忆（归档文件）中属于该日的消息。
"""

import json
import os
import re
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from plugins.memory_system.character_context import get_archive_dir
from plugins.memory_system.memory_utils import extract_time_from_message

logger = logging.getLogger(__name__)

# ── 模块级状态（单例约束） ──────────────────────────────────────
_message_count = 0


class DiaryManager:
    """管理日记的自动生成与存储"""

    CHECK_INTERVAL = 10          # 每 10 条消息检查一次
    SCAN_RANGE_DAYS = 14         # 补写范围：从昨天往回推 14 天

    def __init__(self, llm_adapter):
        """
        Args:
            llm_adapter: LLM 适配器实例，用于调用 AI 生成日记
        """
        self.llm_adapter = llm_adapter
        self._generating_lock = threading.Lock()
        self._pending_dates: set = set()  # 正在生成日记的日期集合，防止竞态条件
        os.makedirs(get_archive_dir(), exist_ok=True)

    def _get_previous_day_str(self) -> str:
        """
        返回昨天的日期字符串（刚完结的对话日）。
        规则：每天凌晨 4:00 划断。
        例如：
          - 6月2日 03:00 → 当前仍在 6月1日的对话日，昨天是 5月31日，返回 "20260531"
          - 6月2日 05:00 → 当前已在 6月2日的对话日，昨天是 6月1日，返回 "20260601"
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
        """扫描归档目录，返回已有日记文件的日期集合。"""
        archive_dir = get_archive_dir()
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
        """
        生成从 start_date 到 end_date（含）的所有日期字符串列表。
        日期格式 YYYYMMDD。
        """
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

    def check_and_generate(self, messages: List[Dict[str, Any]]) -> None:
        """
        每 CHECK_INTERVAL 条消息检查一次。
        扫描昨天往前 SCAN_RANGE_DAYS 天范围内的所有日期，
        已有日记跳过，缺失的补写（有消息则正常日记，无消息则空日记占位）。
        今天的日记不写。
        """
        global _message_count

        _message_count += 1
        if _message_count < self.CHECK_INTERVAL:
            return
        _message_count = 0

        # 确保当前角色目录存在（归档钩子可能尚未触发）
        os.makedirs(get_archive_dir(), exist_ok=True)

        # 扫描范围：从 scan_start 到昨天
        scan_start = self._get_scan_start_date()
        yesterday = self._get_previous_day_str()

        all_dates = self._date_range(scan_start, yesterday)
        if not all_dates:
            return

        existing_dates = self._get_existing_diary_dates()

        for date_str in all_dates:
            if date_str in existing_dates or date_str in self._pending_dates:
                continue

            diary_path = get_archive_dir() / f"diary_{date_str}.json"
            self._generate_for_date(date_str, messages, diary_path)

    def _generate_for_date(self, date_str: str, messages: List[Dict[str, Any]], diary_path: Path) -> None:
        """为指定日期生成日记：有对话则正常日记，无对话则空日记占位。"""
        with self._generating_lock:
            if date_str in self._pending_dates:
                logger.info(f"日期 {date_str} 的日记正在生成中，跳过重复触发。")
                return
            self._pending_dates.add(date_str)

        # 锁外执行耗时操作
        hot_messages = self._filter_messages_by_date(messages, date_str)
        cold_messages = self._load_archive_messages_by_date(date_str)
        all_messages = self._merge_messages(hot_messages, cold_messages)

        if all_messages:
            logger.info(
                f"准备为 {date_str} 生成日记 "
                f"（热记忆 {len(hot_messages)} 条，冷记忆 {len(cold_messages)} 条，"
                f"合并后 {len(all_messages)} 条）..."
            )
            thread = threading.Thread(
                target=self._generate_diary_with_cleanup,
                args=(date_str, all_messages, diary_path),
                daemon=True
            )
            thread.start()
        else:
            logger.info(f"日期 {date_str} 无对话记录，生成空日记占位。")
            self._write_empty_diary(date_str, diary_path)
            with self._generating_lock:
                self._pending_dates.discard(date_str)

    def _generate_diary_with_cleanup(self, date_str: str, messages: List[Dict], diary_path: Path) -> None:
        """调用 _generate_diary，并在完成后从 _pending_dates 中移除标记。"""
        try:
            self._generate_diary(date_str, messages, diary_path)
        finally:
            with self._generating_lock:
                self._pending_dates.discard(date_str)

    def _write_empty_diary(self, date_str: str, diary_path: Path) -> None:
        """写入空日记占位文件，标记该日期已检查且无对话。"""
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

    def _filter_messages_by_date(self, messages: List[Dict], date_str: str) -> List[Dict]:
        """
        从消息列表中筛选出属于指定对话日的消息。
        对话日范围：当日 04:00:00 到次日 03:59:59。
        """
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
        """
        从所有归档文件中加载属于指定日期的消息（冷记忆）。
        """
        try:
            target_date = datetime.strptime(date_str, "%Y%m%d").date()
            day_start = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=4)
            day_end = day_start + timedelta(days=1)
        except ValueError:
            return []

        cold_messages = []
        archive_files = sorted(get_archive_dir().glob("chat_archive_*.json"))

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
        """
        合并热记忆和冷记忆，按时间戳排序并去重。
        去重依据：去掉时间前缀后的消息内容完全相同则视为重复。
        """
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

        from datetime import datetime as dt
        merged.sort(key=lambda m: extract_time_from_message(m) or dt.min)
        return merged

    def _generate_diary(self, date_str: str, messages: List[Dict], diary_path: Path) -> None:
        """后台线程：调用 LLM 生成日记并保存到文件。"""
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
        try:
            messages = [
                {"role": "system", "content": "你是一个专业的日记助手，负责将对话历史总结为 JSON 格式的日记。请严格遵守 JSON 输出格式。"},
                {"role": "user", "content": prompt}
            ]
            response = self.llm_adapter.chat(messages, stream=False)

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