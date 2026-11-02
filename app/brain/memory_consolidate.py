# -*- coding: utf-8 -*-
"""桌宠AI · 记忆沉淀加固（app/brain/memory_consolidate.py，2026-10-30 豆包交接）。

两个职责：

任务 2 · 对话结束 5 分钟增量巩固：
  对话增量（主人消息 + 桌宠回复）先暂存会话缓冲，5 分钟后后台小规模提炼
  （只针对本会话、上限 20 条）写入长期记忆。解决"白天说的状态要等凌晨
  consolidate、上下文滚动就丢"的问题。

任务 3 · 凌晨窗口引擎没跑时补跑：
  04:00-04:59 巩固窗口引擎 down 时写待补跑标记；引擎恢复或 webapp 启动时
  自动补跑一次，跑完清标记；attempts 上限 3 次防死循环。

分层纪律：本模块不 import llm——提炼函数由调用方（webapp）注入。
所有后台动作走 daemon 线程，失败静默降级（buffer 保留，下次再试）。
"""
import json
import threading
import time
from pathlib import Path

from core import config

_BUF_PATH = Path(config.DATA_DIR) / "memory" / "incremental_buffer.json"
_PENDING_PATH = Path(config.DATA_DIR) / "memory" / "pending_consolidate.json"
_DELAY_S = 300                 # 任务 2：对话结束 5 分钟
_MAX_FACTS = 20                # 任务 2：单次提炼上限（任务书 10-20）
_PENDING_MAX_ATTEMPTS = 3      # 任务 3：补跑尝试上限

_lock = threading.Lock()       # 进程内互斥（webapp 是单进程多线程）


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def _audit(detail):
    try:
        from core import audit
        audit.log("memory", detail)
    except Exception:
        pass


# ================================================================ 任务 2

def capture_increment(session_id, role, text):
    """对话增量入会话缓冲（对话发生时由 webapp 立即调用，不阻塞）。"""
    if not (text or "").strip() or not session_id:
        return
    with _lock:
        buf = _read_json(_BUF_PATH, {})
        entry = buf.setdefault(session_id, {"items": [], "scheduled": False})
        entry["items"].append({"role": role, "text": text[:2000],
                               "ts": time.time()})
        # 缓冲防爆：单会话最多留 60 条（提炼只取最近的）
        entry["items"] = entry["items"][-60:]
        _write_json(_BUF_PATH, buf)


def schedule_flush(session_id, extract_fn, delay_s=_DELAY_S):
    """安排一次 5 分钟后的增量提炼（已排程则跳过——同一段对话只提炼一次）。"""
    with _lock:
        buf = _read_json(_BUF_PATH, {})
        entry = buf.get(session_id)
        if not entry or not entry.get("items"):
            return False
        if entry.get("scheduled"):
            return False
        entry["scheduled"] = True
        _write_json(_BUF_PATH, buf)

    def _late():
        time.sleep(delay_s)
        try:
            flush_session(session_id, extract_fn)
        except Exception as e:
            # 失败静默降级：取消 scheduled 标记，下一轮对话会重新排程
            _audit(f"增量提炼失败（{session_id}）将重试：{type(e).__name__}: {str(e)[:100]}")
            with _lock:
                buf = _read_json(_BUF_PATH, {})
                if session_id in buf:
                    buf[session_id]["scheduled"] = False
                    _write_json(_BUF_PATH, buf)

    threading.Thread(target=_late, daemon=True,
                     name=f"incr-consolidate-{session_id[:20]}").start()
    return True


def flush_session(session_id, extract_fn, max_facts=_MAX_FACTS):
    """把该会话缓冲的增量提炼成事实写入长期记忆，然后清缓冲。

    返回写入条数；无缓冲返回 -1。提炼源归属：主人原话 source="user"，
    桌宠回复 source="pet"。失败抛出（由调用方决定降级策略）。"""
    from brain import memory
    with _lock:
        buf = _read_json(_BUF_PATH, {})
        entry = buf.get(session_id)
        if not entry or not entry.get("items"):
            return -1
        items = entry["items"]
        buf.pop(session_id)                      # 先摘除（提炼中再来新对话不丢：新对话会重建缓冲）
        _write_json(_BUF_PATH, buf)

    dialog = "\n".join(("主人: " if it["role"] == "user" else "桌宠: ") + it["text"]
                       for it in items[-40:])          # 最多取最近 40 条防超长
    facts = extract_fn(dialog)[:max_facts]
    n = 0
    for f in facts:
        if not isinstance(f, str) or len(f.strip()) < 4:
            continue
        memory.remember(f.strip(), kind="fact",
                        source="user" if "主人" in f[:6] else "pet",
                        confidence=1.0)
        n += 1
    _audit(f"增量巩固（{session_id}）：增量 {len(items)} 条 → 提炼 {n} 条入长期记忆")
    return n


# ================================================================ 任务 3

def mark_pending(date_key):
    """凌晨窗口引擎没跑 → 写待补跑标记（幂等：同日重复标记只记一次）。"""
    with _lock:
        p = _read_json(_PENDING_PATH, {})
        if p.get("date") == date_key:
            return
        p.update({"date": date_key, "attempts": 0,
                  "marked_at": time.strftime("%F %T")})
        _write_json(_PENDING_PATH, p)
    _audit(f"巩固补跑标记已写：{date_key}")


def pending_exists():
    p = _read_json(_PENDING_PATH, {})
    return bool(p.get("date")) and int(p.get("attempts", 0)) < _PENDING_MAX_ATTEMPTS


def run_pending_consolidate(consolidate_fn):
    """有补跑标记且引擎活着 → 补跑一次当日巩固 → 清标记。

    返回 dict(status)；无标记返回 None。attempts 递增防死循环，
    超过上限放弃并留审计（等主人手动跑 /api/memory/consolidate）。
    consolidate_fn 由调用方注入（webapp 传 lambda: memory.consolidate(_extract_facts)）。"""
    with _lock:
        p = _read_json(_PENDING_PATH, {})
        if not p.get("date"):
            return None
        if int(p.get("attempts", 0)) >= _PENDING_MAX_ATTEMPTS:
            return None
        p["attempts"] = int(p.get("attempts", 0)) + 1
        _write_json(_PENDING_PATH, p)            # 先记账再跑：跑崩也不会无限重试
    _audit(f"开始补跑 {p['date']} 的每日巩固（第 {p['attempts']} 次）")
    r = consolidate_fn()
    status = (r or {}).get("status", "?")
    with _lock:
        cur = _read_json(_PENDING_PATH, {})
        if cur.get("date") == p["date"]:
            cur.pop("date")                       # 跑完（无论提炼几条）清标记=幂等
            _write_json(_PENDING_PATH, cur)
    _audit(f"补跑完成：status={status}")
    return {"status": "pending_done", "date": p["date"], "result": r}
