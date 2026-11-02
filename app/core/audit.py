# -*- coding: utf-8 -*-
"""桌宠AI · 行为审计日志（audit.py）——她干的每一件事都单独记账。

主人定调（2026-10-27）："她的提议/要求、聊天记录、所有行为都要单独记录、
单独管理；这些记录她不用知道。"——主人侧隐形审核台的数据层。

**性质：暂时功能**（前期核对测试用）。PET_AUDIT=0 可整体关闭；
留痕失败绝不影响对话（全部 try/except 包裹——审计是旁观者，不是参与者）。

两份账本：
1. data/audit/behavior.jsonl —— 行为流（一事件一行）：
   {ts, time, kind, session_id, detail, ...extra}
   kind：message/wish/wander/skill/tool_call/tool_result/generate/
         fallback/error/recheck
2. data/audit/sessions/<sid>/<ts>_<n>.jsonl —— 聊天完整落档，
   **照 OpenClaw session event log 结构**（行业标准，openclaw audit 同款）：
     {"type":"message",  "role":"user"|"assistant", "content":..., "ts":...}
     {"type":"thinking", "chars":..., "ts":...}          —— 思考块（字数）
     {"type":"tool_call","name":..., "args":..., "ts":...}
     {"type":"tool_result","name":..., "chars":..., "ts":...}
     {"type":"usage",    "gen_s":..., "elapsed_s":..., "ts":...}
     {"type":"step",     "name":..., "detail":..., "ts":...}
     {"type":"error",    "msg":..., "ts":...}
   一轮对话一个文件，按时间排序回放即完整还原她这一轮怎么想的、干了什么。

查询：webapp /api/audit（行为流/会话/回放统一查询）。
"""
import json
import threading
import time
from pathlib import Path

from core import config

BEHAVIOR_PATH = Path(config.DATA_DIR) / "audit" / "behavior.jsonl"
SESSIONS_DIR = Path(config.DATA_DIR) / "audit" / "sessions"

_lock = threading.Lock()          # behavior.jsonl 追加锁（多会话并发写）
_turn_seq = {}                    # sid -> 当天轮次号（会话文件命名用）
_seq_lock = threading.Lock()

# 行为流里登记过的 kind（doctor/面板用它校验，写错 kind 一眼可见）
KINDS = ("message", "wish", "wander", "skill", "tool_call", "tool_result",
         "generate", "fallback", "recheck", "error", "propose", "guard",
         "whitelist", "comfy", "busy", "notify")


def _append(path, rec):
    """追加一行 JSON（带锁；目录不存在自建；失败静默——审计不许弄断对话）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def log(kind, detail="", session_id="", **extra):
    """记一条行为流事件。任何模块（pipeline/webapp/wishes）都可以调。"""
    if not config.AUDIT_ENABLED:
        return
    rec = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
           "kind": kind, "session_id": session_id, "detail": (detail or "")[:300]}
    rec.update(extra)
    _append(BEHAVIOR_PATH, rec)


# ---------------------------------------------------------------- 会话落档
class TurnLogger:
    """一轮对话的完整落档器（OpenClaw session event log 结构）。

    用法（pipeline 内）：
        tl = TurnLogger(sid)
        tl.user(query)                     # 开轮
        tl.step("search", "搜索：上证")     # pipeline 每步
        tl.tool_call("web_search", {"query": "上证"}) / tl.tool_result("web_search", 5500)
        tl.assistant(answer, usage, elapsed_s, decision)   # 收轮
    异常收尾：tl.error(msg)（webapp worker 调）。
    """

    def __init__(self, sid):
        self.sid = str(sid or "default")
        self.path = None
        if not config.AUDIT_ENABLED:
            return
        try:
            d = SESSIONS_DIR / _safe(self.sid)
            d.mkdir(parents=True, exist_ok=True)
            with _seq_lock:
                n = _turn_seq.get(self.sid, 0) + 1
                _turn_seq[self.sid] = n
            self.path = d / (time.strftime("%Y%m%d-%H%M%S") + f"_{n:03d}.jsonl")
            log("message", "会话开轮", session_id=self.sid, file=self.path.name)
        except Exception:
            self.path = None

    def _ev(self, rec):
        if not config.AUDIT_ENABLED or self.path is None:
            return
        rec = dict(rec)
        rec.setdefault("ts", time.time())
        _append(self.path, rec)

    def user(self, query):
        self._ev({"type": "message", "role": "user", "content": (query or "")[:4000]})
        log("message", (query or "")[:120], session_id=self.sid, role="user")

    def step(self, name, detail=""):
        self._ev({"type": "step", "name": name, "detail": (detail or "")[:200]})

    def thinking(self, chars):
        self._ev({"type": "thinking", "chars": int(chars)})

    def tool_call(self, name, args=None):
        self._ev({"type": "tool_call", "name": name, "args": args or {}})
        log("tool_call", f"{name} {json.dumps(args or {}, ensure_ascii=False)[:100]}",
            session_id=self.sid)

    def tool_result(self, name, chars):
        self._ev({"type": "tool_result", "name": name, "chars": int(chars)})

    def assistant(self, answer, usage=None, elapsed_s=None, decision=None,
                  reasoning_chars=None):
        self._ev({"type": "message", "role": "assistant",
                  "content": (answer or "")[:8000]})
        u = dict(usage or {})
        u.setdefault("elapsed_s", elapsed_s)
        u["answer_chars"] = len(answer or "")
        if reasoning_chars is not None:
            u["reasoning_chars"] = int(reasoning_chars)
        if decision:
            u["via"] = decision.get("via", "")
            u["reason"] = (decision.get("reason") or "")[:100]
        self._ev({"type": "usage", **u})
        log("generate", f"答 {len(answer or '')} 字 / {elapsed_s}s",
            session_id=self.sid, **{"answer_chars": len(answer or ""),
                                    "elapsed_s": elapsed_s,
                                    "engine": u.get("engine", "")})

    def fallback(self, what):
        self._ev({"type": "step", "name": "fallback", "detail": (what or "")[:200]})
        log("fallback", what or "", session_id=self.sid)

    def error(self, msg):
        self._ev({"type": "error", "msg": (msg or "")[:400]})
        log("error", (msg or "")[:200], session_id=self.sid)


def _safe(sid):
    """会话 id → 目录名（只留安全字符）。

    安全注记（B26 自查修正）：原实现允许 "."，sid=".." 可拼出
    SESSIONS_DIR/".." 逃逸一层目录——现在开头加点一律加前缀中和，
    顺带挡住 ".." / ".hidden" 这类路径花招。
    """
    s = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(sid))[:40]
    if s.startswith("."):
        s = "s_" + s.lstrip(".")
    return s or "default"


# ---------------------------------------------------------------- 查询（/api/audit 用）
def _read_jsonl(path, limit, t_start=0.0, t_end=None):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) < t_start:
                    continue
                if t_end and r.get("ts", 0) > t_end:
                    continue
                out.append(r)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    out.sort(key=lambda r: r.get("ts", 0))
    return out[-limit:]


def query(kind=None, session_id=None, limit=300, t_start=0.0, t_end=None):
    """行为流统一查询：按类型/会话/时间过滤。kind="wish" 时把 wishes.jsonl
    合并进来（提议也是行为的一种，主人要求一个口径看全）。"""
    evs = _read_jsonl(BEHAVIOR_PATH, 100000, t_start, t_end)
    if kind == "wish":
        evs = [e for e in evs if e.get("kind") == "wish"]
        try:
            from growth import wishes
            for w in wishes.list_wishes(limit=10000):
                if w.get("ts", 0) < t_start:
                    continue
                if t_end and w.get("ts", 0) > t_end:
                    continue
                evs.append({"ts": w.get("ts", 0), "time": w.get("time"),
                            "kind": "wish", "session_id": w.get("session_id", ""),
                            "detail": (w.get("reason") or "")[:200],
                            "name": w.get("name", ""), "urls": w.get("urls", []),
                            "status": w.get("status", ""), "wish_id": w.get("id")})
        except Exception:
            pass
        evs.sort(key=lambda r: r.get("ts", 0))
        return evs[-limit:]
    if kind:
        evs = [e for e in evs if e.get("kind") == kind]
    if session_id:
        evs = [e for e in evs if e.get("session_id") == session_id]
    return evs[-limit:]


def kinds_count():
    """各类型计数（面板徽标/异常页用）。"""
    evs = _read_jsonl(BEHAVIOR_PATH, 100000)
    c = {}
    for e in evs:
        c[e.get("kind", "?")] = c.get(e.get("kind", "?"), 0) + 1
    return c


def sessions():
    """会话清单（sid、轮数、最近活动时间）——聊天回放页用。"""
    out = []
    try:
        for d in SESSIONS_DIR.iterdir():
            if not d.is_dir():
                continue
            files = sorted(d.glob("*.jsonl"))
            if not files:
                continue
            last_ts = 0
            try:
                last_ts = files[-1].stat().st_mtime
            except Exception:
                pass
            out.append({"sid": d.name, "turns": len(files),
                        "last_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(last_ts))
                        if last_ts else "", "last_ts": last_ts})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    out.sort(key=lambda x: x.get("last_ts", 0), reverse=True)
    return out


def session_turns(sid, limit=50):
    """一个会话的轮次清单（文件名+起始时间+事件数）+ 每轮事件（回放）。
    事件里 user/assistant 正文原样返回（主人侧审核台要看全文）。"""
    d = SESSIONS_DIR / _safe(str(sid or "default"))
    out = []
    try:
        files = sorted(d.glob("*.jsonl"))[-limit:]
    except Exception:
        return out
    for f in files:
        evs = []
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        evs.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            continue
        out.append({"file": f.name, "events": evs})
    return out
