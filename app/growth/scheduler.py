# -*- coding: utf-8 -*-
"""桌宠AI · L4 调度三模式（scheduler.py）

对照 本地线-实施方案.md §L4：
- 单引擎优先级队列：CHAT(P0) > 心跳(P1) > IDLE(P2) > EXPLORE(P3)
- 长任务**切小片**入队：EXPLORE 类长任务按片执行，片间可被高优先级插入
  ——红线"不能听不见主人喊她"的机制化：每片开跑前先问 busy_check()，
  主人在聊天（或心跳要说话）就立刻让路，checkpoint 记下进度下次继续。
- checkpoint 恢复：每片结束即落盘 data/scheduler/jobs.json，进程重启后
  running 的任务自动回 pending 从 checkpoint 继续（recover()）。

设计取舍（v1 如实记录）：
- chat 本身不走队列——FastAPI 线程池天然并行，队列管的是**后台长任务**；
  "CHAT 最高优先"落地为：长任务每片之间让路（busy_check），而不是给
  聊天也排队（那反而慢）。
- 追问规则（2026-10-27 主人拍板）：**等回复只挂提议，不挂人**——她有提议
  在等主人回话（waiter 等待帧）时，本调度器的 idle/explore/wander 一概
  **照常跑**，不做任何"等待中就暂停后台活动"的判断；行动闸门只拦
  "需主人点头的落地/接入动作"（wishes 那条线）。
- 心跳（P1）由 proactive/webapp 现有路径承担，这里提供 submit 让它也能
  排（比如"凌晨帮她跑一轮巩固"）。
- EXPLORE 任务本体（浏览采集）属后续功能；本模块是它的运行底座，
  用 slice_fn 把"做什么"和"怎么调度"解耦——测试用假切片即可全链路验证。

分层：不 import llm/engine——busy_check 由调用方注入（webapp 传"聊天忙吗"）。
"""
import json
import threading
import time
import uuid
from pathlib import Path

from core import config

STATE_FILE = Path(config.DATA_DIR) / "scheduler" / "jobs.json"

P0_CHAT, P1_HEARTBEAT, P2_IDLE, P3_EXPLORE = 0, 1, 2, 3
_PRIO_NAME = {0: "CHAT", 1: "HEARTBEAT", 2: "IDLE", 3: "EXPLORE"}

_LOCK = threading.RLock()
_THREAD = None
_FNS = {}                             # jid -> slice_fn（内存态；持久化任务重启后
                                      # fn 缺失 → 自动 paused，等注册方重新挂）
_BUSY_CHECK = lambda: False          # noqa: E731  默认无人抢占


def set_busy_check(fn):
    """注入抢占探针：返回 True 表示高优先级工作在忙，长任务立刻让路。"""
    global _BUSY_CHECK
    _BUSY_CHECK = fn or (lambda: False)


def _load():
    """读任务队列状态。外部审核 P2 修复（2026-10-27）：
    半截/损坏的 JSON **不再静默清空队列**——坏文件改名保留
    （jobs.json.corrupt-<时间戳>），审计留痕，返回空队列但带 corrupt 标记。
    （_save 是 tmp+replace 原子写，损坏概率低；可一旦发生，静默归零会把
    排队任务全丢且无告警——现在至少主人能在审核台看到出过这事。）"""
    if not STATE_FILE.exists():
        return {"version": 1, "jobs": []}
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(st, dict) or not isinstance(st.get("jobs"), list):
            raise ValueError("结构不对（jobs 非列表）")
        st.setdefault("version", 1)
        return st
    except Exception as e:
        # 坏文件保留可追溯 + 大声告警，绝不静默吞掉
        try:
            bak = STATE_FILE.with_name(
                "jobs.json.corrupt-" + time.strftime("%Y%m%d-%H%M%S"))
            STATE_FILE.replace(bak)
            try:
                from core import audit as _audit
                _audit.log("error", f"调度任务队列文件损坏已隔离：{bak.name}"
                                    f"（{type(e).__name__}: {str(e)[:120]}）"
                                    f"——排队任务不可恢复，请主人检查")
            except Exception:
                pass
        except Exception:
            pass
        return {"version": 1, "jobs": [], "corrupt_recovered": True}


def _save(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    st["version"] = 1                        # 带版本：将来 schema 变更有据可查
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def submit(kind, payload=None, priority=P3_EXPLORE, slice_fn=None,
           max_slices=50, note=""):
    """提交任务。slice_fn(checkpoint, payload) -> (result|None, checkpoint, done)
    ——每片调用一次；返回 done=True 结束。checkpoint 任意 JSON 值。"""
    jid = uuid.uuid4().hex[:10]
    if slice_fn is not None:
        _FNS[jid] = slice_fn
    with _LOCK:
        st = _load()
        st["jobs"].append({
            "id": jid, "kind": kind, "priority": int(priority),
            "status": "pending",             # pending/running/paused/done/cancelled
            "checkpoint": None, "payload": payload or {},
            "slices_done": 0, "max_slices": int(max_slices),
            "note": note[:100], "created_at": time.time(),
            "updated_at": time.time(),
        })
        _save(st)
    return jid


def _recover():
    """启动时把上次"跑到一半"的任务放回 pending（checkpoint 还在，接着跑）。"""
    with _LOCK:
        st = _load()
        n = 0
        for j in st["jobs"]:
            if j["status"] == "running":
                j["status"] = "pending"
                n += 1
        if n:
            _save(st)
    return n


def pause(jid):
    return _set_status(jid, "paused", only=["pending", "running"])


def resume(jid):
    return _set_status(jid, "pending", only=["paused"])


def cancel(jid):
    return _set_status(jid, "cancelled", only=["pending", "running", "paused"])


def _set_status(jid, status, only):
    with _LOCK:
        st = _load()
        for j in st["jobs"]:
            if j["id"] == jid and j["status"] in only:
                j["status"] = status
                j["updated_at"] = time.time()
                _save(st)
                return True
    return False


def list_jobs():
    with _LOCK:
        return _load()["jobs"]


def _pick_next():
    """挑下一个该跑的任务：pending 里优先级最高（小者胜），同优先级 FIFO。"""
    pend = [j for j in _load()["jobs"] if j["status"] == "pending"]
    if not pend:
        return None
    pend.sort(key=lambda j: (j["priority"], j["created_at"]))
    return pend[0]


def _run_one(j):
    """跑一个任务的一片：先让路（红线），再执行，再落 checkpoint。"""
    if _BUSY_CHECK():
        return False                          # 主人在喊她——这片不跑
    fn = _FNS.get(j["id"])
    if fn is None:                            # 重启后 fn 不在内存 → 挂起等重挂
        with _LOCK:
            st = _load()
            for jj in st["jobs"]:
                if jj["id"] == j["id"]:
                    jj["status"] = "paused"
                    jj["last_error"] = "slice_fn 不在内存（进程重启后需重新注册）"
                    _save(st)
        return True
    with _LOCK:
        st = _load()
        for jj in st["jobs"]:
            if jj["id"] == j["id"]:
                jj["status"] = "running"
                _save(st)
    try:
        _result, cp, done = fn(j["checkpoint"], j["payload"])
    except Exception as e:
        with _LOCK:
            st = _load()
            for jj in st["jobs"]:
                if jj["id"] == j["id"]:
                    jj["status"] = "paused"
                    jj["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                    jj["updated_at"] = time.time()
                    _save(st)
        return True
    with _LOCK:
        st = _load()
        for jj in st["jobs"]:
            if jj["id"] == j["id"]:
                jj["checkpoint"] = cp
                jj["slices_done"] = jj.get("slices_done", 0) + 1
                if done or jj["slices_done"] >= jj["max_slices"]:
                    jj["status"] = "done"
                    if _result is not None:
                        jj["result"] = _result
                else:
                    jj["status"] = "pending"   # 片间让路窗口
                jj["updated_at"] = time.time()
                _save(st)
    return True


def loop_once():
    """调度一步：取最高优先级任务跑一片。供后台线程与测试复用。"""
    j = _pick_next()
    if j is None:
        return False
    return _run_one(j)


def _loop():
    _recover()
    while True:
        try:
            if not loop_once():
                time.sleep(1.0)               # 空转节流（桌面常驻，省电）
        except Exception:
            time.sleep(2.0)                   # 调度器自身不许死


def start(bg=True):
    """启动调度器（webapp startup 调用；bg=False 供测试手动步进）。"""
    global _THREAD
    n = _recover()
    if bg and _THREAD is None:
        _THREAD = threading.Thread(target=_loop, daemon=True)
        _THREAD.start()
    return n


def stats():
    jobs = list_jobs()
    by = {}
    for j in jobs:
        by[j["status"]] = by.get(j["status"], 0) + 1
    return {"total": len(jobs), **by, "state_file": str(STATE_FILE)}
