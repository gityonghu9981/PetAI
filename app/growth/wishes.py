# -*- coding: utf-8 -*-
"""桌宠AI · 她的心愿/提议留痕（wishes.py）——提议→主人拍板→（人工）接入。

主人定调（2026-10-27）：**她只逛、只看、只提议，不往家拿东西。**
接入永远由主人侧人工执行，系统不做任何"带回/下载/自动接入"的动作。

这个模块因此**只有留痕与拍板状态**，没有任何执行分支：
- `propose()`：把她的提议（链接+名字+理由）落到 data/wishes.jsonl，状态 pending；
- `approve()/reject()`：只改状态并留痕，**不调用任何安装/下载/注册逻辑**
  （代码里不存在这类分支，可审计）；
- `list_wishes()`：给面板和主人审阅用。

分层：L3（外部行动先过闸门这条红线的端点）。
不 import pipeline/router；只依赖 config 与 lexicon 级工具。
"""
import json
import time
from pathlib import Path

from core import config

WISH_PATH = Path(config.DATA_DIR) / "wishes.jsonl"
EVENTS_PATH = Path(config.DATA_DIR) / "pet_guard_events.jsonl"

# 状态：pending（待主人看）→ approved（主人点头，人工去接）/ rejected（主人摇头）
STATUS = ("pending", "approved", "rejected")

# 提议语义词（用于从她的回答里识别"这是提议，不是随口提个链接"）。
# 放宽口径：宁可多记一条给主人看，也不要漏掉她真正的许愿。
_PROPOSE_HINT = ("我想要", "我想用", "我想学", "我想换", "可以吗", "许愿",
                 "我看到", "我看到了", "挺喜欢", "好喜欢", "想用来", "想要它",
                 "为什么想要", "看上了", "就想要", "想要这个", "能不能给我",
                 "帮我装", "我想要这个", "我想拿", "想拿它", "我想装",
                 "想要它当", "想当我的", "给我装")


def _ensure():
    WISH_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not WISH_PATH.exists():
        WISH_PATH.write_text("", encoding="utf-8")


def _append(rec):
    _ensure()
    with open(WISH_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _event(what):
    """顺手往守护事件里记一笔，桌宠气泡/面板可见（不静默）。"""
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "what": what},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def looks_like_proposal(text):
    """粗判：这段回答像不像"她在提议"（有链接 + 有意愿表达）。

    只用于留痕，不做任何拦截/放行——宁可多记一条给主人看，也不要漏一条。
    """
    if not text or "http" not in text.lower():
        return False
    return any(h in text for h in _PROPOSE_HINT)


def propose(text, urls=None, name=None, source="chat", session_id=""):
    """记一条她的提议。**只留痕**，不触发任何接入动作。

    text：她的原话（含理由）；urls：回答里出现的链接；name：她说的名字
    （没有就从《》/""/URL 附近猜一个，猜不到留空，主人看原文即可）。

    追问规则（2026-10-27 主人拍板《执着允许》）——摇头不是永久封印：
    - 冷却期内（REJECT_COOLDOWN_DAYS，默认 5 天）：拦下 + 留痕，
      审计里写明"还剩几天可以再提"；
    - 冷却期已过：**放行重提**——新提议正常落 pending，带 ref_old=原提议
      id（主人能看出"这是她隔了几天又想要的同一件东西"）。
    """
    urls = urls or []
    if not name:
        name = _guess_name(text)
    hit = blocked_by_rejected(name or "", urls)
    if hit:
        remain_s = _cooldown_remain(hit)
        if remain_s > 0:
            # 冷却期内：拦 + 留痕（主人审核台看得见她试过、还剩多久解禁）
            why = ("她又一次提议主人摇头过的内容（%s：%s），还在冷却期内，"
                   "已拦下——约剩 %.1f 天可以再提一次"
                   % (hit["id"], (hit.get("name") or "未命名")[:40],
                      remain_s / 86400))
            _event(why)
            try:
                from core import audit as _audit
                _audit.log("wish", why, session_id=session_id,
                           name=name or "", urls=urls[:5], status="blocked_cooldown",
                           wish_id=hit["id"])
            except Exception:
                pass
            return {"blocked": True, "blocked_by": hit["id"],
                    "cooldown_days_left": round(remain_s / 86400, 1),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 冷却期已过：放行重提（ref_old 指回原提议，主人看得出前缘）
        rec = _new_rec(text, urls, name, source, session_id)
        rec["ref_old"] = hit["id"]
        _append(rec)
        _event(f"冷却期已过，她又提了一次「{name or '（未命名）'}」——等主人看")
        try:
            from core import audit as _audit
            _audit.log("wish", f"冷却期已过，她重提「{name or '（未命名）'}」"
                               f"（ref_old={hit['id']}），照常等主人拍板",
                       session_id=session_id, name=name or "", urls=urls[:5],
                       status="released", wish_id=rec["id"], ref_old=hit["id"])
        except Exception:
            pass
        return rec
    rec = _new_rec(text, urls, name, source, session_id)
    _append(rec)
    _event(f"她提议了：{name or '（未命名）'} —— 等主人看（只提议，不自动接入）")
    # 行为流同步记一笔（audit，主人侧审核台；失败不影响提议本身）
    try:
        from core import audit as _audit
        _audit.log("wish", f"她提议：{name or '（未命名）'}（等主人看）",
                   session_id=session_id, name=name or "", urls=urls[:5],
                   status="pending", wish_id=rec["id"])
    except Exception:
        pass
    return rec


def _new_rec(text, urls, name, source, session_id):
    """构造一条 pending 提议记录（字段集中一处，重提/首提共用）。"""
    return {"id": "w%d" % int(time.time() * 1000), "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "pending",
            "name": name or "", "urls": urls, "reason": (text or "")[:2000],
            "source": source, "session_id": session_id,
            "decided_at": None, "decided_by": None}


def _cooldown_remain(hit):
    """这条 rejected 提议还剩多少秒冷却（<=0 表示冷却期已过，可重提）。"""
    decided = hit.get("decided_at") or hit.get("ts") or 0
    return decided + max(1, config.REJECT_COOLDOWN_DAYS) * 86400 - time.time()


def _guess_name(text):
    """从原话里猜个名字：《》> 引号 > URL 前的短语。猜不到返回空。"""
    import re
    m = re.search(r"《([^》]{2,30})》", text or "")
    if m:
        return m.group(1)
    m = re.search(r"[“”\"']([^\"“”']{2,30})[“”\"']", text or "")
    if m:
        return m.group(1)
    m = re.search(r"([^\s，。！？、]{2,20})\s*[（(]?\s*https?://", text or "")
    if m:
        return m.group(1)
    return ""


def list_wishes(status=None, limit=100):
    """列出提议（默认全部，可按 pending/approved/rejected 过滤）。"""
    _ensure()
    out = []
    try:
        for line in WISH_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if status and r.get("status") != status:
                continue
            out.append(r)
    except Exception:
        pass
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out[:limit]


def blocked_by_rejected(name, urls):
    """匹配器：名字或链接是否命中某条已 rejected 的提议，返回那条或 None。

    只做匹配，**不做冷却判断**——拦还是放行由 propose() 按冷却期定
    （2026-10-27 主人拍板：摇头不是永久封印，冷却期后可再提一次）。
    匹配口径：
    - 名字：精确匹配（去空格）；
    - 链接：除精确匹配外，**前缀包含也算**（>=20 字符）——她原话里的
      URL 常被空格/标点截断存档，只存了前缀，精确比对会漏（实测踩过）。
    宽松方向匹配：宁可认出"是同一件东西"，拦不拦交给 propose 的冷却期判断。
    """
    key_name = (name or "").strip()
    key_urls = {(u or "").strip().rstrip("/") for u in (urls or []) if u}
    if not key_name and not key_urls:
        return None
    for r in list_wishes(status="rejected", limit=1000):
        if key_name and (r.get("name") or "").strip() == key_name:
            return r
        for u in (r.get("urls") or []):
            u = (u or "").strip().rstrip("/")
            if not u:
                continue
            if u in key_urls:
                return r
            # 前缀包含：存档截断的 URL 能拦住完整版，反之亦然
            for k in key_urls:
                if len(u) >= 20 and (k.startswith(u) or u.startswith(k)):
                    return r
    return None


_DECIDE_LOCK = __import__("threading").Lock()   # 面板并发拍板互斥（三审 2026-10-30）


def decide(wish_id, ok, by="master", note=None):
    with _DECIDE_LOCK:
        return _decide_locked(wish_id, ok, by=by, note=note)


def _decide_locked(wish_id, ok, by="master", note=None):
    """主人拍板：approved / rejected。**只改状态 + 留痕**。

    note：主人的审阅意见（review_note）——为什么点头/摇头，审核台可见。
    红线（写死在注释里，防止后人"顺手"接执行）：本函数**不得**调用任何
    下载/安装/注册/发布的逻辑——接入由主人侧人工执行，系统不代劳。
    """
    _ensure()
    lines = WISH_PATH.read_text(encoding="utf-8").splitlines()
    hit, out = None, []
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("id") == wish_id:
            r["status"] = "approved" if ok else "rejected"
            r["decided_at"] = time.time()
            r["decided_by"] = by
            if note:
                r["review_note"] = str(note)[:500]
            hit = r
        out.append(json.dumps(r, ensure_ascii=False))
    if not hit:
        return None
    WISH_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    _event(f"主人{'点头' if ok else '摇头'}：{hit.get('name') or '（未命名）'}"
           f"（接入由主人侧人工执行，系统不代劳）")
    return hit


# 兼容命名：approve/reject 是 decide 的可读包装
def approve(wish_id, by="master", note=None):
    return decide(wish_id, True, by, note)


def reject(wish_id, by="master", note=None):
    return decide(wish_id, False, by, note)


def stats():
    allw = list_wishes(limit=10000)
    return {"total": len(allw),
            "pending": sum(1 for r in allw if r.get("status") == "pending"),
            "approved": sum(1 for r in allw if r.get("status") == "approved"),
            "rejected": sum(1 for r in allw if r.get("status") == "rejected")}
