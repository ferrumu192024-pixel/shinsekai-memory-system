"""
随机回忆模块：以极低概率自动触发，从历史日记中检索相关记忆，
通过 AI 关键词扩展 + 预筛日记后，直接将记忆片段注入当前对话。
AI 自行判断是否自然插入；所有链路日志写入 logs/random_recall.log。
"""

import json
import random
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

from plugins.memory_system.character_context import get_archive_dir

# ========== 独立的日志配置 ==========
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

recall_logger = logging.getLogger("random_recall")
recall_logger.setLevel(logging.DEBUG)
recall_logger.propagate = False

file_handler = RotatingFileHandler(
    LOG_DIR / "random_recall.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S"
))

recall_logger.addHandler(file_handler)
recall_logger.addHandler(console_handler)
# ========== 日志配置结束 ==========


class RandomRecallManager:
    """管理随机记忆回忆的整个链路"""

    def __init__(self, llm_adapter, probability: float = 0.03):
        self.llm_adapter = llm_adapter
        self.probability = probability

    def try_recall(self, user_message: str) -> Optional[Dict]:
        """
        尝试触发随机回忆。整条链路：roll → 扩展关键词 → 预筛日记 → 粗筛匹配。
        粗筛命中后直接将记忆片段返回，不做额外精判，由 AI 自行决定是否插入。
        """

        # 1. 概率 roll
        if random.random() > self.probability:
            recall_logger.debug(f"roll 未命中（阈值={self.probability}）")
            return None
        recall_logger.info(f"roll 命中！用户消息：\"{user_message[:80]}\"")

        # 2. AI 关键词扩展
        recall_logger.info("调用 AI 扩展关键词...")
        expanded = self._expand_keywords(user_message)
        if not expanded:
            recall_logger.info("关键词扩展失败或返回为空，中断")
            return None
        recall_logger.info(f"扩展结果：{expanded}")

        # 3. 预筛日记并随机选一份
        diary = self._pick_random_diary(expanded_keywords=expanded)
        if not diary:
            recall_logger.info("无可用的日记文件，中断")
            return None
        recall_logger.info(f"选中日记：date={diary.get('date')}，keywords={diary.get('keywords', [])}")

        # 4. 粗筛匹配（扩展词 vs 日记 keywords）
        diary_keywords = [kw.lower() for kw in diary.get("keywords", [])]
        matched_kw = []
        for kw in expanded:
            kw_lower = kw.lower()
            for dk in diary_keywords:
                if kw_lower in dk or dk in kw_lower:
                    matched_kw.append(kw)
                    break
        if not matched_kw:
            recall_logger.info(f"粗筛未匹配（扩展词：{expanded[:5]}，日记keywords：{diary_keywords}），中断")
            return None
        recall_logger.info(f"粗筛命中！匹配词：{matched_kw}")

        # 5. 直接命中，不再精判,让AI自己决定是否开口
        recall_logger.info(
            f"命中记忆！直接注入，由 AI 自行判断是否插入。\n"
            f"  日期：{diary.get('date')}\n"
            f"  摘要：{diary.get('summary', '')}\n"
            f"  关键词：{diary.get('keywords', [])}"
        )
        return {
            "summary": diary.get("summary", ""),
            "date": diary.get("date", ""),
            "keywords": diary.get("keywords", []),
        }

    def _list_diary_files(self) -> List[Path]:
        """列出所有日记文件"""
        archive_dir = get_archive_dir()
        if not archive_dir.exists():
            return []
        return sorted(archive_dir.glob("diary_*.json"))

    def _load_valid_diary(self, fp: Path) -> Optional[Dict]:
        """读取日记文件，跳过空日记占位。返回 None 表示无效或读取失败。"""
        try:
            with open(fp, "r", encoding="utf-8") as f:
                diary = json.load(f)
            if diary.get("empty"):
                return None
            return diary
        except Exception as e:
            recall_logger.warning(f"读取日记失败 {fp}: {e}")
            return None

    def _pick_random_diary(self, expanded_keywords: List[str] | None = None) -> Optional[Dict]:
        """
        随机选一份有效日记（跳过空日记占位）。
        如果有扩展关键词，优先选择 keywords 匹配的日记；
        若没有匹配的，则从全部有效日记中随机选一份。
        """
        files = self._list_diary_files()
        if not files:
            return None

        if expanded_keywords:
            candidates = []
            for fp in files:
                diary = self._load_valid_diary(fp)
                if not diary:
                    continue
                diary_kw_lower = [kw.lower() for kw in diary.get("keywords", [])]
                for ekw in expanded_keywords:
                    ekw_lower = ekw.lower()
                    if any(ekw_lower in dk or dk in ekw_lower for dk in diary_kw_lower):
                        candidates.append(diary)
                        break

            if candidates:
                recall_logger.info(
                    f"关键词预筛：{len(candidates)}/{len(files)} 份日记匹配，优先从中选择"
                )
                return random.choice(candidates)
            else:
                recall_logger.info(
                    f"关键词预筛：0/{len(files)} 份日记匹配，从全部有效日记中随机选择"
                )

        # 无关键词或关键词预筛无匹配 → 从全部有效日记中随机选
        valid_files = []
        for fp in files:
            if self._load_valid_diary(fp) is not None:
                valid_files.append(fp)

        if not valid_files:
            recall_logger.info("没有找到任何有效日记（所有日记均为空占位或读取失败）")
            return None

        chosen = random.choice(valid_files)
        return self._load_valid_diary(chosen)

    def _expand_keywords(self, message: str) -> Optional[List[str]]:
        """调用 AI 将用户消息扩展为一组联想关键词（网络异常时快速失败）"""
        prompt = f"""请将以下用户消息扩展为一组联想关键词（5-10个），包括：
- 核心概念
- 可能相关的梗、典故、俗语
- 同义或近义表达
- 相关话题或场景

只返回一个 JSON 字符串数组，不要加任何解释。

用户消息："{message}"

输出示例：["关键词1", "关键词2", "关键词3"]"""

        try:
            response = self.llm_adapter.chat(
                [
                    {"role": "system", "content": "你是一个关键词扩展助手，只输出 JSON 字符串数组。"},
                    {"role": "user", "content": prompt}
                ],
                stream=False,
            )
            content = self._extract_response_text(response)
            if not content:
                return None

            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            result = json.loads(content.strip())
            if isinstance(result, list):
                return result
        except Exception as e:
            recall_logger.warning(f"关键词扩展失败: {e}")
        return None

    def _extract_response_text(self, response) -> Optional[str]:
        """从 LLM 响应中提取文本内容"""
        if not response:
            return None
        if hasattr(response, 'choices') and response.choices:
            return response.choices[0].message.content
        if isinstance(response, str):
            return response
        return None
