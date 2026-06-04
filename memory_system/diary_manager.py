"""
日记管理器：自动为每一天的对话生成一份日记摘要。
日记以“对话日”为单位，每天凌晨 4:00 到次日凌晨 3:59 为一个对话日。
每 10 条消息检查一次，自动补写所有缺失日期的日记（跳过空白日记）。
生成日记时，自动合并热记忆（当前上下文）和冷记忆（归档文件）中属于该日的消息，确保日记完整。
"""

import json
import os
import re
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# ── 记忆隔离：当前活跃角色名 ──────────────────────────────────────
_CHARACTER_NAME = "default"


def set_character(name: str) -> None:
    """设置当前活跃角色名，由 plugin.py 在初始化时调用。"""
    global _CHARACTER_NAME
    _CHARACTER_NAME = str(name).strip() or "default"


def _get_archive_dir() -> Path:
    """获取当前角色的归档目录路径。"""
    return Path("data/archives") / _CHARACTER_NAME


class DiaryManager:
    """管理日记的自动生成与存储"""

    # ── 日记检查频率控制 ──────────────────────────────────────────
    CHECK_INTERVAL = 10          # 每 10 条消息检查一次
    _message_count = 0           # 消息计数器
    _last_check_date: Optional[str] = None  # 上次检查时覆盖到的最新日期

    def __init__(self, llm_adapter):
        """
        Args:
            llm_adapter: LLM 适配器实例，用于调用 AI 生成日记
        """
        self.llm_adapter = llm_adapter
        self._generating_lock = threading.Lock()
        os.makedirs(_get_archive_dir(), exist_ok=True)

    def _get_previous_day_str(self) -> str:
        """
        返回需要补写日记的日期字符串（刚刚完结的对话日）。
        规则：每天凌晨 4:00 划断。
        例如：
          - 6月2日 03:00 → 当前仍在 6月1日的对话日，补写 5月31日，返回 "20260531"
          - 6月2日 05:00 → 当前已在 6月2日的对话日，补写 6月1日，返回 "20260601"
        """
        now = datetime.now()
        if now.hour < 4:
            current_diary_date = now.date() - timedelta(days=1)
        else:
            current_diary_date = now.date()
        target_date = current_diary_date - timedelta(days=1)
        return target_date.strftime("%Y%m%d")

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
        补写从上次检查日期到刚完结的对话日之间所有缺失的日记。
        已有日记或当天无对话记录则跳过，不写空白日记。
        """
        DiaryManager._message_count += 1

        # 未达检查间隔，跳过
        if DiaryManager._message_count < self.CHECK_INTERVAL:
            return

        # 重置计数器
        DiaryManager._message_count = 0

        # 确定需要覆盖的日期范围
        latest_date = self._get_previous_day_str()
        start_date = DiaryManager._last_check_date or latest_date

        # 生成待检查的日期列表
        missing_dates = self._date_range(start_date, latest_date)
        if not missing_dates:
            return

        for date_str in missing_dates:
            diary_path = _get_archive_dir() / f"diary_{date_str}.json"
            if diary_path.exists():
                continue  # 已有日记，跳过

            # 尝试生成日记（如果当天有对话记录）
            self._generate_if_has_messages(date_str, messages)

        # 更新上次检查日期
        DiaryManager._last_check_date = latest_date

    def _generate_if_has_messages(self, date_str: str, messages: List[Dict[str, Any]]) -> None:
        """仅为有对话记录的日期生成日记。"""
        # 筛选当天的热记忆
        hot_messages = self._filter_messages_by_date(messages, date_str)
        # 从归档中捞取冷记忆
        cold_messages = self._load_archive_messages_by_date(date_str)
        # 合并去重
        all_messages = self._merge_messages(hot_messages, cold_messages)

        if not all_messages:
            logger.info(f"日期 {date_str} 无对话记录（含冷热记忆），跳过日记生成。")
            return

        diary_path = _get_archive_dir() / f"diary_{date_str}.json"
        logger.info(
            f"准备为 {date_str} 生成日记 "
            f"（热记忆 {len(hot_messages)} 条，冷记忆 {len(cold_messages)} 条，"
            f"合并后 {len(all_messages)} 条）..."
        )

        thread = threading.Thread(
            target=self._generate_diary,
            args=(date_str, all_messages, diary_path),
            daemon=True
        )
        thread.start()

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
            content = msg.get("content", "")
            match = re.search(r"\[本地时间\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]", content)
            if match:
                try:
                    msg_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                    if day_start <= msg_time < day_end:
                        filtered.append(msg)
                except ValueError:
                    pass
        return filtered

    def _load_archive_messages_by_date(self, date_str: str) -> List[Dict]:
        """
        从所有归档文件（chat_archive_*.json）中加载属于指定日期的消息（冷记忆）。
        """
        try:
            target_date = datetime.strptime(date_str, "%Y%m%d").date()
            day_start = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=4)
            day_end = day_start + timedelta(days=1)
        except ValueError:
            return []

        cold_messages = []
        archive_files = sorted(_get_archive_dir().glob("chat_archive_*.json"))

        for fp in archive_files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    continue
                for msg in data:
                    content = msg.get("content", "")
                    match = re.search(
                        r"\[本地时间\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]",
                        content
                    )
                    if match:
                        msg_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                        if day_start <= msg_time < day_end:
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

        # 先放冷记忆（较早），再放热记忆（较新），保证去重时保留热记忆中的版本
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

        def _extract_time(m):
            content = m.get("content", "")
            match = re.search(
                r"\[本地时间\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]",
                content
            )
            if match:
                try:
                    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            return datetime.min

        merged.sort(key=_extract_time)
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