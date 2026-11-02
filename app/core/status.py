# -*- coding: utf-8 -*-
"""桌宠AI · 实时状态灯（status.py）——她正在做什么，写盘给桌宠壳看。

主人定调（2026-10-27）：**状态显示是永久功能**——"她正在做什么要显示，
简短状态跟随桌面形象显示，配上相应的对话（气泡）"。

分工：
- pipeline 各阶段调 from_step()（on_step 事件 → 状态），**只写 data/status.json**；
- 桌宠壳 desktop/pet.py 每秒读一次 status.json，跟随形象显示
  "她正在：逛" + 她当前那句话（气泡）；
- webapp 提供 /api/state 供网页侧查看。

纪律：
- 状态必须**真实**（全部来自 pipeline 实际事件，不猜、不定时假装在忙）；
- "配上相应的对话"＝气泡里那句是**她当前正在干的事的第一人称短句**，
  由本模块按事件模板生成（不是编她的台词，是描述她正在做的事）；
- 写盘用 tmp+replace 原子替换，桌宠壳读到的一半文件不会发生；
- 同状态+同 detail 节流不重写（1s 级轮询下减少无谓 IO）。

状态去哪了：桌宠气泡壳（desktop/pet.py）直接读本文件渲染气泡；外部形象
引擎（如有）可轮询同一份文件或自己的通道，各管各的，互不覆盖。
"""
import json
import os
import threading
import time
from pathlib import Path

from core import config

STATUS_PATH = Path(config.DATA_DIR) / "status.json"

# 状态机（任务单 §三：八态；2026-10-29 加 talking＝自言自语；
# 2026-11-02 加 working/thinking 等中继状态（各活动模块写入）。
# 由形象侧映射回其既有动画档，不新增素材）
STATES = {
    "idle":           "发呆",
    "thinking":       "思考",
    "searching":      "搜索",
    "wandering":      "逛",
    "working":        "干活",
    "writing":        "写作",
    "talking":        "自言自语",
    "waiting_master": "等主人回话",
    "error":          "报错",
    "game_playing":   "打游戏中",
    "game_thinking":  "在想棋路",
    "game_settle":    "看结算",
    "game_stuck":     "卡住了",
    "game_win":       "赢啦",
    "game_lose":      "输啦",
    "game_hurt":      "掉血了",
}

_lock = threading.Lock()
_last = {"state": "", "detail": ""}      # 节流：同状态+同detail不重写


def _line(state, detail):
    """状态 → 她的第一人称短句（气泡里"配上相应的对话"那句）。
    模板描述她正在做的事；detail 是 pipeline 的真实事件细节。"""
    d = (detail or "").strip()
    if state == "thinking":
        # detail 常常是内部步骤名（"技能1·判断：纠错校验…"、"弱思考：预算 200 token"、
        # "日常交流：不思考直答（no_think）"），给主人看没意义——过滤掉，只说"在想怎么回答你"
        noise = ("技能", "预算", "纠错", "判定", "路由", "token", "分级", "思考分级",
                 "日常交流", "no_think", "直答", "不思考")
        if d and not any(k in d for k in noise):
            return f"我在想：{d[:24]}"
        return "我在想怎么回答你"
    if state == "searching":
        return f"我去查查『{d[:24]}』…" if d else "我去查点资料…"
    if state == "wandering":
        return f"我出去逛逛：{d[:24]}" if d else "我出去逛逛～"
    if state == "working":
        return d[:40] or "我在干活呢"
    if state == "writing":
        return "我在写东西，先理理思路…" if not d else f"写着呢：{d[:24]}"
    if state == "talking":
        # 自言自语：她一个人的时候也会嘟囔（autonomy 的 talk / 想到什么小声说）
        return d[:40] or "我自己念叨两句…"
    if state == "waiting_master":
        # 等回复机制（B26）：气泡显示"在等主人回话"+她刚说了什么——
        # 主人一眼看到她在等什么，回了话就解锁
        d = (detail or "").replace("刚跟主人说：", "").strip()
        return f"在等主人回话～（刚说：{d[:30]}）" if d else "在等主人回话～"
    if state == "error":
        return "刚才出了点小状况，主人看看日志？"
    return d[:40] or "我在发呆"


def set_state(state, detail="", line=None):
    """写一帧状态。state 不在 STATES 里忽略（防手滑写错键）。"""
    if not config.STATUS_ENABLED:
        return
    if state not in STATES:
        return
    global _last
    with _lock:
        if state == _last["state"] and (detail or "") == _last["detail"]:
            return                           # 节流：同帧不重写
        rec = {"state": state, "state_cn": STATES[state],
               "detail": (detail or "")[:120],
               "line": (line if line is not None else _line(state, detail))[:120],
               "ts": int(time.time()),
               "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        _last = {"state": state, "detail": detail or ""}
    try:
        tmp = str(STATUS_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, STATUS_PATH)         # 原子替换：读方永远读到完整帧
    except Exception:
        pass                                 # 状态灯坏了也不能影响对话


def from_step(name, detail=""):
    """pipeline 的 on_step(name, detail) → 状态。映射表集中在这一处，
    新的步骤名进来先在这里登记（不登记的状态不写盘，宁可少显示不可瞎显示）。"""
    if name in ("search", "fetch", "tool_search"):
        set_state("searching", detail)
    elif name == "wander":
        set_state("wandering", detail)
    elif name in ("skill", "audit", "expand", "fallback"):
        set_state("working", detail)
    elif name == "generate":
        # 生成阶段：写作类 detail 由 pipeline 带"写作"字样，这里粗分
        set_state("writing" if "写作" in (detail or "") else "thinking", detail)
    elif name == "route":
        set_state("thinking", detail)
    elif name == "error":
        set_state("error", detail)
    # 其余步骤名（wish 等）不映射状态——只进审计，不上状态灯
    elif name == "done":
        # 追问规则（2026-10-27 主人拍板）：答完=话轮交还，她自由——不是
        # "她进入等待态"（那是旧语义，等于把她冻结了）。这轮对话里她有没有
        # 提议在等主人看，看 waiter 等待帧（气泡叠加"有件事在等主人回话"）。
        set_state("idle", "答完啦，等你回话～")


def read():
    """读当前状态（桌宠壳/网页用）。文件不存在或坏了返回 idle。"""
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "idle", "state_cn": "发呆", "detail": "",
                "line": "", "ts": 0, "time": ""}
