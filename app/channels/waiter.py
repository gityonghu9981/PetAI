# -*- coding: utf-8 -*-
"""桌宠AI · 等回复状态机（waiter.py）——她说完话必须等主人回，回了才继续。

主人定调（2026-10-27，《请接单-等回复机制与多通道呼叫》+《执着允许》合并）：
- 她跟主人说话**必须叫到主人**，然后**等主人回答再往下走**——自说自话没用；
- 当前通道没回（超时）→ **再叫一次**（气泡 → 本地提示）；
- 主人从任何通道回了话 → 回复进她的上下文 → 解锁继续；
- **等待回复 = 那件事挂起，不是她挂起**：她本人不冻结，逛/学/想/聊天全照常，
  "特别想→一直问"和"不着急→边玩边等"是她自己选的两种心情（mood）。

纪律（写死在本模块头上，防后人"顺手"破坏）：
- 发消息"叫主人"属**表达动作**，可自主（主人已明确授权）——但每次都全量留痕；
- **通话发起属重动作**，本模块 v1 不做（如实标注限制）；
- 本模块只在本机提示（气泡/状态帧），不接任何外部消息通道；
- PET_MASTER_CALL=0 一键关停自动呼叫（气泡永远可用，那是她自己的嘴）。

文件：data/waiting.json（等待帧，原子替换）——pet.py / webapp 跨进程共用。
"""
import json
import os
import threading
import time
from pathlib import Path

from core import config

WAIT_PATH = Path(config.DATA_DIR) / "waiting.json"

# 每级通道的"再等多久才升级"都从表达时刻起算（不是链式累加，语义更直观：
# 气泡后 8 分钟没回 → 本地提示再叫一次）
_LADDER = [c.strip().lower() for c in config.CALL_LADDER if c.strip()]
_CN = {"bubble": "桌宠气泡", "notify": "本地提示"}

_lock = threading.Lock()


# ---------------------------------------------------------------- 等待帧读写
def _read():
    try:
        return json.loads(WAIT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write(rec):
    WAIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(WAIT_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, WAIT_PATH)               # 原子替换：读方不会读到半帧


def _clear():
    try:
        WAIT_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 表达（她开口叫主人）
def express(text, session_id="default", channel="bubble", mood="patient",
            wx_nudge=False):
    """她主动表达（提议/许愿/问事/想分享）→ 登记等待。

    mood（2026-10-27 主人拍板《执着允许》——两种心情由她自己定，不是轮着来）：
    - "patient"（不着急）：那件事挂着等，她继续玩自己的——不自动升级通道，
      主人回复了自然解锁；
    - "eager"（特别想）：可以一直问——换通道再叫、全梯子叫完隔
      REASK_INTERVAL_S 还可重提一次（换句话、不刷屏）。

    核心语义：**等待回复 = 那件事挂起，不是她挂起**——本函数**不再把全局
    状态灯打成 waiting_master**（那是"她冻结"的旧语义，已废）。挂起只记录
    在等待帧里，气泡/面板读帧叠加显示"有件事在等主人回话"，她的状态灯
    （在逛/在学/在聊）照常由 pipeline/各活动写。

    做三件事：
    1. 写等待帧（谁、说了什么、什么通道、什么心情、从何时等）；
    2. 表达内容进她对应会话的上下文（add_assistant）——主人回话时她
       "想起来自己说过什么"，不是凭空接茬；
    3. 审计留痕（wait 事件）。
    返回等待帧（rec）；失败不影响表达本身。
    """
    text = (text or "").strip()
    if not text:
        return None
    # ⚠️ 已有等待帧 → **保留原 since_ts**，只更新内容（2026-10-29 抓到的真 bug：
    # 她每 2 分钟自主唤醒说一句话，帧就刷新一次，5 分钟计时永远从头算——
    # 结果她叫了主人一下午，一次都没响。主人眼里"说了好几遍都没下文"，
    # 计时当然要从她**第一次开口**算起。主人回话清帧后，新帧才重新计时。）
    prev = is_waiting()
    rec = {"state": "waiting", "text": text[:500], "session_id": session_id,
           "channel": channel, "mood": mood if mood in ("patient", "eager") else "patient",
           "since_ts": time.time(),
           "since": time.strftime("%Y-%m-%d %H:%M:%S"),
           "tried": [channel],                 # 原通道算已叫过
           "escalated": []}
    if prev and prev.get("session_id") == session_id:
        rec["since_ts"] = prev.get("since_ts") or rec["since_ts"]
        rec["since"] = prev.get("since") or rec["since"]
        rec["tried"] = list(dict.fromkeys(
            list(prev.get("tried", [])) + [channel]))
        rec["escalated"] = list(prev.get("escalated", []))
        rec["mood"] = "eager" if (prev.get("mood") == "eager"
                                  or rec["mood"] == "eager") else "patient"
    _write(rec)
    _audit("wait", f"她开口了（{_CN.get(channel, channel)}·"
                   f"{'特别想' if rec['mood'] == 'eager' else '不着急'}），"
                   f"那件事挂起等主人回话：{text[:60]}",
           session_id=session_id)
    # 表达进上下文（她记得自己说过；失败只影响记忆不影响等待）
    try:
        from brain import context as _context
        _context.Session(session_id).add_assistant(f"（我主动跟主人说：{text[:200]}）")
    except Exception:
        pass
    return rec


def is_waiting():
    rec = _read()
    return rec if (rec and rec.get("state") == "waiting") else None


def master_replied(text, channel="bubble"):
    """主人回话（任何通道）→ 解锁继续。返回等待帧（可查她当时说了啥）或 None。

    解锁语义：清等待帧 + 审计留痕。**状态灯不在这里定调**——主人回话后
    她接什么（继续聊/继续逛/落地那件事）由调用方和 pipeline 决定，这里只
    把灯交还给"idle"底线（她本人的活动一直没停过，等的是那件事不是她）。
    回复文本进上下文由调用方做（webapp /api/chat 本来就 add_user）——
    这里不代写，避免重复入库。
    """
    with _lock:
        rec = is_waiting()
        if not rec:
            return None
        _clear()
    _audit("wait", f"主人从{_CN.get(channel, channel)}回话了，那件事解锁"
                   f"（她等了{_waited_min(rec)}分钟，期间活动照常）",
           session_id=rec["session_id"])
    try:
        from core import status as _status
        _status.set_state("idle", "主人回话了")
    except Exception:
        pass
    return rec


def _waited_min(rec):
    return round((time.time() - rec.get("since_ts", time.time())) / 60, 1)


# ---------------------------------------------------------------- 再叫一次（升级链）
def escalate_check():
    """后台线程每 15s 调一次：按她的心情决定要不要再叫主人。

    - patient（不着急）：**不升级、不重提**——那件事放着等，她继续玩自己的。
      主人回复了自然解锁，没有回音也不打扰（她选了边玩边等）。
    - eager（特别想）：可以一直问——当前通道超时 → 再叫一次（每通道一次，
      不刷屏）；全梯子叫完仍无回音，隔 REASK_INTERVAL_S（默认 30 分钟）
      **换句话再提一次**（回到气泡，一次一提）——问得有她的样子，不是轰炸。
    """
    rec = is_waiting()
    if not rec:
        return None
    if not config.MASTER_CALL:
        return None                            # 主人关了自动呼叫：只等气泡
    if rec.get("mood", "patient") != "eager":
        return None                            # 不着急：放着等，主人回复自然解锁
    now = time.time()
    waited = now - rec["since_ts"]
    tried = list(rec.get("tried", []))
    failed_ch = list(rec.get("channel_failed", []))
    for ch in _LADDER:
        if ch in tried or ch in failed_ch:
            continue
        if waited < config.WAIT_TIMEOUT_S * (len(tried)):# 每级给足一个超时窗口
            continue
        ok = _call(ch, rec)
        if ok:
            tried.append(ch)
            rec.setdefault("escalated", []).append(
                {"channel": ch, "ts": now, "time": time.strftime("%H:%M:%S")})
            _audit("wait", f"特别想，等了{_waited_min(rec)}分钟没回音，"
                           f"再叫一次主人（换了窗口，不是轰炸）",
                   session_id=rec["session_id"])
        else:
            failed_ch.append(ch)               # 这条通道叫不通：本轮跳过
            rec.setdefault("escalated", []).append(
                {"channel": ch, "ts": now, "failed": True})
            _audit("wait", f"{_CN.get(ch, ch)}叫不通（未配置/失败/未接入），本轮跳过",
                   session_id=rec["session_id"])
        rec["tried"] = tried
        rec["channel_failed"] = failed_ch
        _write(rec)
        break                                  # 一轮只升一级，节奏稳一点
    else:
        # 全梯子叫完仍无回音 + 距上次开口过了重提间隔 → 换句话再提一次
        if _maybe_reask(rec, now):
            return rec
    return rec


def _maybe_reask(rec, now):
    """"特别想"的重提循环：全梯子叫完 + 隔 REASK_INTERVAL_S → 气泡再提一次。

    每次重提计数 +1（reasks），审计留痕"换句话再提"——语义上是她按主人
    拍板"特别想就可以一直问"，节奏由间隔兜底（默认 30 分钟一次，不刷屏）。
    返回 True 表示这次真的重提了。
    """
    last_ts = rec.get("since_ts", now)
    for e in reversed(rec.get("escalated", [])):
        last_ts = max(last_ts, e.get("ts", 0))
    if now - last_ts < config.REASK_INTERVAL_S:
        return False
    rec["reasks"] = rec.get("reasks", 0) + 1
    rec["since_ts"] = now                      # 重置升级窗口：下一轮从头算
    rec["tried"] = ["bubble"]                  # 气泡重新叫过（其它通道保持记录）
    rec.setdefault("escalated", []).append(
        {"channel": "bubble", "ts": now, "reask": rec["reasks"],
         "time": time.strftime("%H:%M:%S")})
    _write(rec)
    _audit("wait", f"特别想（第{rec['reasks']}次重提），换个说法再跟主人提一次："
                   f"{rec.get('text', '')[:50]}", session_id=rec["session_id"])
    return True


def _call(channel, rec):
    """往指定通道叫主人。返回 True=已发出。**只做表达**，不做任何接入动作。"""
    if channel == "bubble":
        return True                            # 气泡是她的嘴，表达即已发生
    if channel == "notify":
        try:
            from channels import notify as _notify
            r = _notify.tell_master(rec.get("text", ""), why="wait", force=True)
            return bool(r.get("ok"))
        except Exception as e:
            _audit("error", f"本地提示异常：{type(e).__name__}: {str(e)[:100]}",
                   session_id=rec.get("session_id", ""))
            return False
    return False


def state_view():
    """给面板/桌宠看的等待概要（挂起的是那件事；她的活动状态另见 status.json）。"""
    rec = is_waiting()
    if not rec:
        return {"waiting": False,
                "ladder": [{"channel": c, "cn": _CN.get(c, c)} for c in _LADDER],
                "timeout_s": config.WAIT_TIMEOUT_S}
    return {"waiting": True, "text": rec.get("text", ""),
            "channel": rec.get("channel"),
            "mood": rec.get("mood", "patient"),
            "mood_cn": "特别想" if rec.get("mood") == "eager" else "不着急",
            "since": rec.get("since", ""),
            "waited_min": _waited_min(rec),
            "reasks": rec.get("reasks", 0),
            "tried": rec.get("tried", []),
            "escalated": rec.get("escalated", []),
            "session_id": rec.get("session_id")}


def _audit(kind, detail, session_id=""):
    try:
        from core import audit as _audit_mod
        _audit_mod.log(kind, detail, session_id=session_id)
    except Exception:
        pass                                   # 审计失败绝不影响等待机制本身
