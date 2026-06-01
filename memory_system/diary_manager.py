"""
日记管理器：自动为每一天的对话生成一份日记摘要。
日记以“对话日”为单位，每天凌晨 4:00 到次日凌晨 3:59 为一个对话日。
每次收到用户消息时检查是否需要补写前一天的日记。
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

# 日记文件存放目录，与归档文件共用
ARCHIVE_DIR = Path("data/archives")


class DiaryManager:
    """管理日记的自动生成与存储"""

    def __init__(self, llm_adapter):
        """
        Args:
            llm_adapter: LLM 适配器实例，用于调用 AI 生成日记
        """
        self.llm_adapter = llm_adapter
        self._generating_lock = threading.Lock()
        os.makedirs(ARCHIVE_DIR, exist_ok=True)

    def _get_previous_day_str(self) -> str:
        """
        返回需要补写日记的日期字符串（刚刚完结的对话日）。
        规则：每天凌晨 4:00 划断。
        例如：
          - 6月2日 03:00 → 当前仍在 6月1日的对话日，补写 5月31日，返回 "20260531"
          - 6月2日 05:00 → 当前已在 6月2日的对话日，补写 6月1日，返回 "20260601"
        """
        now = datetime.now()
        # current_diary_date = 当前时间所属对话日的日期
        if now.hour < 4:
            # 还在昨天的对话日里
            current_diary_date = now.date() - timedelta(days=1)
        else:
            current_diary_date = now.date()
        # 要补写的是上一个对话日
        target_date = current_diary_date - timedelta(days=1)
        return target_date.strftime("%Y%m%d")

    def check_and_generate(self, messages: List[Dict[str, Any]]) -> None:
        """
        检查是否需要为前一天生成日记。
        合并热记忆和冷记忆（归档文件）中属于该日期的消息，确保日记完整。
        如果文件不存在且未在生成中，启动后台线程生成。
        """
        date_str = self._get_previous_day_str()
        diary_path = ARCHIVE_DIR / f"diary_{date_str}.json"

        if diary_path.exists():
            return

        if not self._generating_lock.acquire(blocking=False):
            return

        try:
            if diary_path.exists():
                return

            # 1. 从热记忆中筛选
            hot_messages = self._filter_messages_by_date(messages, date_str)

            # 2. 从归档文件中捞取冷记忆
            cold_messages = self._load_archive_messages_by_date(date_str)

            # 3. 合并去重（以消息内容为去重依据）
            all_messages = self._merge_messages(hot_messages, cold_messages)

            if not all_messages:
                logger.info(f"日期 {date_str} 无对话记录（含冷热记忆），跳过日记生成。")
                return

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
        finally:
            self._generating_lock.release()

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
        archive_files = sorted(ARCHIVE_DIR.glob("chat_archive_*.json"))

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
            # 去掉时间前缀后比较，避免同一消息因格式微调被误判为不同
            clean = re.sub(
                r"\[本地时间\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\n?",
                "",
                content
            )
            if clean not in seen:
                seen.add(clean)
                merged.append(msg)

        # 按消息中的时间戳排序
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
        """
        构建生成日记的 prompt。
        修改此方法中的 JSON 结构即可自定义日记格式。
        """
        # 组装对话文本，去掉时间前缀以节省 token
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

            # 提取文本内容（兼容不同适配器的返回格式）
            content = ""
            if hasattr(response, 'choices') and response.choices:
                content = response.choices[0].message.content
            elif isinstance(response, str):
                content = response

            # 清理可能的 markdown 代码块标记
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