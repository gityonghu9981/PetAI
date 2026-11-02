# -*- coding: utf-8 -*-
"""有事找主人 → 弹本地气泡（主人 2026-10-28 深夜要求）。

她不是任何时候都往主人面前蹦：**只有"有事"才叫**，而且叫一次要隔一会儿。

本通道（v2 开源档）只做**本机**提示：
  ① 写 status 状态帧（working/气泡文案）→ 桌宠壳读到就弹气泡；
  ② 审计留痕 + 冷却记账——同一类事不刷屏；
  ③ 全程不涉及任何外部消息通道（一律不接）。

谁在用它：
  - 桌宠壳：守护播报"摔了一跤" → 气泡提示主人（她出事了，主人得知道）
  - 自主唤醒：她选了 talk（"我有事想跟主人说"）→ 气泡
  - 等待升级：waiter 的升级链走这里（气泡→本地提示，都在这台机器上）
"""
import json
import time
from pathlib import Path

from core import audit
from core import config

STATE_PATH = Path(config.DATA_DIR) / "notify_state.json"


def _state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _reason_cn(why):
    return {"guard": "她摔跤了", "talk": "她有事找你", "wait": "她在等你回话",
            "comfy": "图画好了", "manual": "她叫你"}.get(why, why or "她找你")


def _bubble(line, clear_after=60):
    """写一帧气泡：桌宠壳读 status.json 显示，N 秒后自动收回 idle。"""
    try:
        from core import status as _status
        _status.set_state("working", "她有事找你", line=line)

        def _back():
            time.sleep(clear_after)
            try:
                _status.set_state("idle", "")
            except Exception:
                pass

        import threading
        threading.Thread(target=_back, daemon=True).start()
    except Exception:
        pass


def tell_master(text, why="manual", cooldown=None, force=False, dry_run=False):
    """有事 → 弹本地气泡。返回 {ok, channel, detail, skipped, tried}。

    cooldown：同类事情的最小间隔（秒），默认 config.PET_NOTIFY_COOLDOWN_S。
    force=True 绕过冷却（"摔跤"这类必须说的事）。
    dry_run=True 只走逻辑不发气泡（测试用）。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "channel": None, "detail": "空文本", "skipped": True}
    if not getattr(config, "PET_NOTIFY_ENABLED", 1):
        return {"ok": False, "channel": None, "detail": "PET_NOTIFY_ENABLED=0", "skipped": True}

    now = time.time()
    cd = getattr(config, "PET_NOTIFY_COOLDOWN_S", 1800) if cooldown is None else cooldown
    st = _state()
    last = st.get("last_ts", 0)
    if not force and now - last < cd:
        left = int((cd - (now - last)) / 60) + 1
        return {"ok": False, "channel": None, "skipped": True,
                "detail": f"冷却中（还要 {left} 分钟）"}

    if dry_run:
        tried = [("bubble", True, "dry-run")]
        detail = "dry-run"
        ok = True
    else:
        _bubble(text[:200])
        tried = [("bubble", True, "ok")]
        detail = "ok"
        ok = True

    if ok:
        st["last_ts"] = now
        st["last_text"] = text[:120]
        _save(st)
    try:
        audit.log("notify",
                  f"{_reason_cn(why)} → 气泡{'已弹出' if ok else '没弹出'}："
                  f"{text[:40]}｜" + "；".join(f"{c}:{d}" for c, o, d in tried),
                  session_id="notify")
    except Exception:
        pass
    return {"ok": ok, "channel": "bubble", "detail": detail,
            "tried": [{"channel": c, "ok": o, "detail": d} for c, o, d in tried],
            "skipped": False}


def tell_master_image(image, caption=None, why="image", force=False, dry_run=False):
    """给主人提示一张图（本地气泡）。image = 本地路径 或 bytes。

    2026-10-29 新增：她画完的图要能真的到主人眼前，而不是只回一句"画好了"
    让主人自己去翻目录。这里负责"提示到位"：气泡里写明图在哪儿，
    绝不静默。
    """
    if not getattr(config, "PET_NOTIFY_ENABLED", 1):
        return {"ok": False, "channel": None, "detail": "PET_NOTIFY_ENABLED=0",
                "skipped": True}
    path_txt = image if isinstance(image, str) else "(bytes)"
    if dry_run:
        return {"ok": True, "channel": "bubble", "detail": "dry-run",
                "skipped": False}
    _bubble((caption or "给你画的图") + f"\n（图在这儿：{path_txt}）", clear_after=120)
    try:
        audit.log("notify", f"{_reason_cn(why)} → 气泡提示图："
                            f"{str(path_txt)[-40:]}", session_id="notify")
    except Exception:
        pass
    return {"ok": True, "channel": "bubble", "detail": "ok", "skipped": False}
