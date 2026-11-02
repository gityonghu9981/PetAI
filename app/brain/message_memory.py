# -*- coding: utf-8 -*-
"""主人消息即时沉淀（2026-10-30 豆包姐姐落地，需求清单 C7 治本第 1 条）

背景：用户消息默认只进会话上下文（Session JSONL），长期记忆的唯一通道是
每天 04:00 的批量巩固（_consolidator_loop）——主人白天说的话要等第二天
凌晨才可能进记忆，当天全靠上下文，所以"记不住"。

本模块：在桌宠/网页对话的消息入口处调用，命中"状态类声明"
（作息/位置/忙闲/行程）就立即写入长期事实，不等凌晨。

保真约束（需求清单硬性要求）：
  - 只记录原文（"主人告诉桌宠：{原文}"），不推断、不补全、不编造；
  - 疑问句不沉淀（"你在外面吗？"不是陈述）；
  - 过短/过长的消息不沉淀（防垃圾）；
  - 写入失败静默（遥测不许弄断聊天）。
"""
import re

from brain import memory

# 状态类触发词：作息 / 位置 / 忙闲 / 行程
_STATUS_WORDS = (
    "在外面", "在外头", "在忙", "忙完", "忙活", "晚点", "白天", "上班",
    "开会", "出差", "回家", "路上", "不在家", "公司", "单位", "加班",
    "外头", "外边", "回不来", "没空", "有事", "工作忙",
)
# 疑问/试探结尾 → 不沉淀
_QUESTION_END = ("？", "?", "吗", "呢", "嘛", "吧", "不")
_MAX_LEN = 200
_MIN_LEN = 4


def maybe_remember_user_status(text):
    """主人消息命中状态类声明 → 立即写入长期事实。

    返回 fact_id（写入/已合并）或 None（未命中/不沉淀/失败）。
    任何异常都不向上抛，调用方一行接入，不打断主流程。
    """
    t = (text or "").strip()
    if not t:
        return None
    if len(t) < _MIN_LEN or len(t) > _MAX_LEN:
        return None
    if t.endswith(_QUESTION_END):
        return None
    if not any(w in t for w in _STATUS_WORDS):
        return None
    t = re.sub(r"\s+", " ", t).strip()
    content = f"主人告诉桌宠：{t}"
    try:
        fid, _merged = memory.remember(content, kind="fact", source="user",
                                       confidence=1.0)
        return fid
    except Exception:
        return None
