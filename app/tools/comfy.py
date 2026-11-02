# -*- coding: utf-8 -*-
"""桌宠AI · ComfyUI 出图技能执行层（comfy.py）

主人 2026-10-28 定的形状：**技能库只放"怎么用"，这里放"怎么跑"**。

分工
----
- 技能库（data/skills/ComfyUI*/skill.md）：给她看的入口说明——什么时候用哪个、
  参数怎么定、纪律三条。命中触发词才加载，平时 0 占用。
- 注册表（data/comfy/registry.json）：**调用时现读**的工作流清单 + 命令 + 参数
  规则。改一个数字（比如换模型、换默认步数）不用碰代码，也不用进对话上下文。
- 本模块：把"她说的"翻译成"固定命令"，跑、等、收文件、量显存、落审计、
  用完关服务。

三条纪律（写在注册表与技能里，也在这里强制）
------------------------------------------
1. **禁止编辑或构造工作流 JSON**——只读引用，参数一律经 comfy_run.py 的 CLI 传。
2. **所有参数显式传值**——注册表给了每个参数的默认值，缺哪个补哪个，绝不靠
   工作流 JSON 里的默认（工作流的默认有的是占位符，跑出来是废图）。
3. 素材先放本地 ComfyUI 的 input 目录；出图后卸载模型；服务"要用才启动、用完就关"。

两个实测坑（2026-10-28，写在代码里防回退）
------------------------------------------
- `--outprefix` 在 comfy_run.py 里**无条件加 "video/" 前缀**（那是给视频用的），
  图片会被塞进 output\\video\\。这里改用 `--override filename_prefix=...`，
  图落在 output\\ 根下。
- `图生图_sdxl_realvis.json` 的正向提示词节点（4）默认是**空字符串**，而
  comfy_run.py 按"最长非空文本当正向"猜——这一猜会把负向节点（5）当正向，
  正负颠倒。所以正负提示词一律用 `--override <节点>.text=...` 显式钉死。
"""
import atexit
import json
import os
import random
import re
import subprocess
import threading
import time
from pathlib import Path

from core import config

DATA = Path(config.DATA_DIR)
REG_PATH = DATA / "comfy" / "registry.json"
STATE_PATH = DATA / "comfy" / "server.json"
LAST_PATH = DATA / "comfy" / "last_result.json"
LOG_PATH = DATA / "comfy" / "server.log"
ERR_PATH = DATA / "comfy" / "last_error.json"       # 最近一次失败（要能回灌给她）
JOBS_PATH = DATA / "comfy" / "jobs.json"            # job 落盘（webapp 重启不丢）
PROMISE_PATH = DATA / "comfy" / "pending_promise.json"  # 她说要画却没动（空承诺）

_LOCK = threading.RLock()
_REG_CACHE = {"mtime": 0.0, "data": None}
_JOBS = {}                       # job_id -> {"state": running/done/error, ...}

_WIN = os.name == "nt"
_CREATE_DETACHED = 0x00000008 | 0x00000200 | 0x01000000   # 脱离作业，服务不陪葬


# ---------------------------------------------------------------- 注册表
def registry():
    """读注册表（按 mtime 缓存，改文件立即生效，不用重启）。"""
    try:
        m = REG_PATH.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _REG_CACHE["data"] is not None and _REG_CACHE["mtime"] == m:
            return _REG_CACHE["data"]
        try:
            reg = json.loads(REG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return _REG_CACHE["data"] or {}
        _REG_CACHE.update({"mtime": m, "data": reg})
        return reg


def skills():
    return (registry().get("skills") or {})


def enabled():
    return bool(getattr(config, "COMFY_ENABLED", 1))


def _triggers():
    return (registry().get("common") or {}).get("triggers") or []


# 2026-10-29 主人抓的实例：他说"你去画你自己的形象，必须全身图…"，词表里
# 没有一条命中（"画个/画一张/出图"都没有），画笔没递到她手上，她只能回
# "好嘞我去画啦"——然后什么都没发生。词表是主判据，但必须有组合规则兜住
# 人说人话的各种说法。
_DRAW_VERB = ("画", "绘制", "作图", "出图", "重绘", "修图", "改图", "生成图")
_DRAW_OBJ = ("图", "形象", "全身", "半身", "头像", "插画", "壁纸", "海报",
             "立绘", "封面", "表情", "卡通", "画风", "场景", "模样")
# 同形不同义的词：有"画/图"但不是在让她画图
_DRAW_FALSE = ("计划", "策划", "规划", "动画", "画质", "笔画", "刻画", "划分",
               "企图", "图书", "地图", "图片库", "画廊")
# 出现同形词时，只有这些明确动作短语能推翻它
_DRAW_STRONG = ("画一张", "画个", "画幅", "画张", "生成一张", "来一张", "出图",
                "作图", "重绘", "帮我画", "给我画", "画一下")
# 问"画得怎么样了"——要的是监察，不是画笔
_STATUS_WORDS = ("画好了吗", "画好了没", "画完了吗", "画完了没", "在画吗", "在画了",
                 "画到哪", "进度", "出图了吗", "图呢", "怎么还没", "还没好吗",
                 "画得怎么样", "画好没")


def wants_status(text):
    """她/主人是在问进度（该递监察工具），不是要画（该递画笔）。"""
    q = (text or "").lower()
    return any(w in q for w in _STATUS_WORDS)


def intent(text):
    """一句话判断"是不是想让她画图"——只用来决定要不要把工具递到她手上。"""
    if not enabled():
        return False
    q = (text or "").lower().strip()
    if not q:
        return False
    # 问进度的话不算"要画"（"图画好了没"要的是监察工具，不是画笔）；
    # 但"帮我画一张，画好了叫我"含明确动作短语，仍算要画
    if wants_status(q) and not any(s in q for s in _DRAW_STRONG):
        return False
    # ① 注册表词表（主判据，随时可加，不用改代码）
    if any(t.lower() in q for t in _triggers() if t):
        return True
    # ② 组合规则：动作词 + 对象词（"你去画你自己的形象"走这条）
    if any(v in q for v in _DRAW_VERB) and any(o in q for o in _DRAW_OBJ):
        if any(f in q for f in _DRAW_FALSE):
            return any(s in q for s in _DRAW_STRONG)
        return True
    # ③ "来一张 / 整一张 …图"
    if "图" in q and any(k in q for k in ("来一张", "整一张", "搞一张",
                                          "做一张", "出一张")):
        return True
    return False


def pick_skill(text):
    """从自然语言猜子技能：说了"图生图/重绘/改这张"→ i2i，其余 → t2i。"""
    q = (text or "").lower()
    if any(k in q for k in ("图生图", "重绘", "换风格", "换个风格", "改这张", "按这张",
                            "改图", "修图", "改背景", "换个背景", "把这张")):
        return "i2i"
    return "t2i"


# ---------------------------------------------------------------- HTTP / 环境
def _url(path):
    base = (registry().get("server") or {}).get("base", "http://127.0.0.1:8188")
    return base.rstrip("/") + path


def _http(path, payload=None, timeout=8):
    import urllib.request
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_url(path), data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def server_up(timeout=2.0):
    try:
        _http("/system_stats", timeout=timeout)
        return True
    except Exception:
        return False


def vram():
    """显存占用（MiB）。拿不到返回 None——不因为没有 nvidia-smi 就罢工。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000 if _WIN else 0)
        line = (out.stdout or "").strip().splitlines()[0]
        used, total = [int(float(x)) for x in re.split(r"[,\s]+", line)[:2]]
        return {"used_mb": used, "total_mb": total, "free_mb": total - used}
    except Exception:
        return None


def _pid_alive(pid):
    if not pid:
        return False
    if _WIN:
        import ctypes
        import ctypes.wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
        k.OpenProcess.restype = wt.HANDLE
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        k.CloseHandle.argtypes = (wt.HANDLE,)
        k.CloseHandle(h)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _pid_on_port(port):
    """谁在监听这个端口（我们自己起的进程号丢了时用）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=10,
                             creationflags=0x08000000 if _WIN else 0)
        for ln in (out.stdout or "").splitlines():
            if f":{port} " in ln and "LISTENING" in ln.upper():
                return int(ln.split()[-1])
    except Exception:
        pass
    return 0


def _kill(pid):
    if not pid:
        return False
    if _WIN:
        import ctypes
        import ctypes.wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
        k.OpenProcess.restype = wt.HANDLE
        h = k.OpenProcess(0x0001, False, int(pid))      # PROCESS_TERMINATE
        if not h:
            return False
        ok = bool(k.TerminateProcess(h, -1))
        k.CloseHandle.argtypes = (wt.HANDLE,)
        k.CloseHandle(h)
        return ok
    try:
        os.kill(int(pid), 15)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 服务生命周期
def _state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def start(timeout=None):
    """要用才启动：`别动venv` 的 python + --lowvram。已在跑就直接返回。"""
    srv = registry().get("server") or {}
    port = int(srv.get("port", 8188))
    if server_up(timeout=1.5):
        st = _state()
        st["port"] = port
        st.setdefault("started_at", time.time())
        _save_state(st)
        return {"ok": True, "already": True, "pid": st.get("pid") or _pid_on_port(port)}
    v = vram()
    need = int(srv.get("min_free_vram_mb", 3500))
    if v and v["free_mb"] < need:
        return {"ok": False, "error": f"显存不够：剩 {v['free_mb']}MB < 需要 {need}MB"
                                      f"（先把别的大任务停一下再画）", "vram": v}
    py = srv.get("python")
    if not py or not Path(py).exists():
        return {"ok": False, "error": f"ComfyUI 的 python 不在：{py}"}
    cwd = srv.get("cwd") or str(Path(py).parent)   # python 通常就在 ComfyUI 目录
    args = list(srv.get("args") or ["main.py", "--port", str(port), "--lowvram"])
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"          # 日志里有中文模型名，别炸编码
    try:
        log = open(LOG_PATH, "ab")
        try:
            proc = subprocess.Popen([py] + args, cwd=cwd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, close_fds=True,
                                    creationflags=_CREATE_DETACHED if _WIN else 0)
        except OSError:
            # 作业对象不允许 breakaway（WinError 5）→ 退回普通启动：
            # 服务跟着拉起它的进程走，不影响功能，只是父进程退出时会被一起带走。
            proc = subprocess.Popen([py] + args, cwd=cwd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, close_fds=True,
                                    creationflags=0x00000200 if _WIN else 0)
    except Exception as e:
        return {"ok": False, "error": f"启动失败：{type(e).__name__} {e}"}
    deadline = time.time() + int(timeout or srv.get("ready_timeout_s", 300))
    while time.time() < deadline:
        if server_up(timeout=2):
            _save_state({"pid": proc.pid, "port": port, "started_at": time.time(),
                         "last_used": time.time(), "self_started": True})
            _audit("启动 ComfyUI", pid=proc.pid, lowvram=True)
            return {"ok": True, "already": False, "pid": proc.pid,
                    "waited_s": round(time.time() - (deadline - int(timeout or 300)), 1)}
        if proc.poll() is not None:
            return {"ok": False, "error": f"ComfyUI 进程退出了（code={proc.returncode}），"
                                          f"看日志 {LOG_PATH}"}
        time.sleep(1.5)
    return {"ok": False, "error": f"等了 {timeout or srv.get('ready_timeout_s', 300)}s 还没就绪"}


def stop(unload=True):
    """用完就关：先让它自己卸模型，再结束进程，把算力还回去。"""
    srv = registry().get("server") or {}
    port = int(srv.get("port", 8188))
    st = _state()
    pid = st.get("pid") or 0
    if unload and server_up(timeout=1.5):
        try:
            _http("/free", {"unload_models": True, "free_memory": True}, timeout=15)
        except Exception:
            pass
    if not _pid_alive(pid):
        pid = _pid_on_port(port)          # 不是我们起的（主人手动开的）也认
    if not pid:
        _save_state({})
        return {"ok": True, "stopped": False, "reason": "本来就没在跑"}
    ok = _kill(pid)
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.3)
    _save_state({})
    _audit("关闭 ComfyUI", pid=pid, freed=True)
    return {"ok": bool(ok), "stopped": True, "pid": pid}


def status():
    srv = registry().get("server") or {}
    st = _state()
    up = server_up(timeout=1.5)
    v = vram()
    return {"enabled": enabled(), "running": up,
            "pid": (st.get("pid") or _pid_on_port(int(srv.get("port", 8188)))) if up else 0,
            "started_at": st.get("started_at") if up else None,
            "idle_s": int(time.time() - st.get("last_used", time.time())) if up else None,
            "vram": v, "skills": {k: s.get("title") for k, s in skills().items()},
            "workflows": {k: s.get("workflow_name") for k, s in skills().items()}}


def autostop_tick():
    """常驻线程调：空闲超时就关服务（"不常驻"落到地上）。"""
    srv = registry().get("server") or {}
    if not server_up(timeout=1.0):
        return
    st = _state()
    if not st.get("self_started"):
        return                              # 主人自己开的，不替主人关
    idle = int(srv.get("idle_stop_s", 420))
    if time.time() - st.get("last_used", time.time()) > idle:
        stop()
        _audit("空闲自动关闭 ComfyUI", idle_s=idle)


# ---------------------------------------------------------------- 出图
def _audit(detail, **extra):
    try:
        from core import audit
        audit.log("comfy", detail[:200], **extra)
    except Exception:
        pass


# ---------------------------------------------------------------- 监察层
# 2026-10-29：主人抓过一次"她说去画了，结果没动"——她没有任何办法确认自己
# 到底有没有在画。这里补齐三件东西：失败要留痕、job 要落盘、她要能查。
def _note_error(kind, message, **extra):
    """失败落盘（下一轮会作为提醒注入给她，让她知道没画成）。"""
    rec = {"ts": time.time(), "skill": kind,
           "time": time.strftime("%Y-%m-%d %H:%M:%S"),
           "error": str(message)[:800]}
    rec.update(extra)
    try:
        ERR_PATH.parent.mkdir(parents=True, exist_ok=True)
        ERR_PATH.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    _audit("出图失败：" + str(message)[:120], skill=kind)


def last_error_recent(max_age_s=1800):
    try:
        d = json.loads(ERR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - float(d.get("ts") or 0) > max_age_s:
        return None
    return d


def clear_error():
    try:
        ERR_PATH.unlink()
    except Exception:
        pass


def _persist_job(job_id, rec):
    """job 落盘（内存字典重启就没，她却要能回查）。只留最近 8 条。"""
    try:
        cur = {}
        if JOBS_PATH.exists():
            try:
                cur = json.loads(JOBS_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                cur = {}
        cur[job_id] = rec
        items = sorted(cur.items(),
                       key=lambda kv: kv[1].get("started_at") or 0)[-8:]
        JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        JOBS_PATH.write_text(json.dumps(dict(items), ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        pass


def _load_jobs():
    try:
        return json.loads(JOBS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def note_promise(said="", why="说了要画，但没有真的调用 draw"):
    """她说要去画、实际什么都没发生——记一笔，下一轮提醒她补上。
    这是"自我欺骗"的刹车：嘴上说 ≠ 手上做，必须留痕。"""
    try:
        PROMISE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROMISE_PATH.write_text(json.dumps(
            {"ts": time.time(), "said": (said or "")[:300], "why": why},
            ensure_ascii=False), encoding="utf-8")
        _audit("空承诺（说画没画）", said=(said or "")[:150], why=why)
    except Exception:
        pass


def pending_promise(max_age_s=3600):
    try:
        d = json.loads(PROMISE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - float(d.get("ts") or 0) > max_age_s:
        return None
    return d


def clear_promise():
    try:
        PROMISE_PATH.unlink()
    except Exception:
        pass


def _is_stale(rec, server_running, now=None, queue_running=None):
    """标着 running 但其实早死了的任务。

    2026-10-29 实测踩到两种假象：
      ① webapp 一退出，后台线程被带走，job 永远停在 running；
      ② 重启后服务被别人拉起来，旧 job 看着还"在跑"，其实队列里什么都没有。
    单图实测 8~16 秒、最慢也不到 1 分钟，所以：
      - 超过 15 分钟一律判死（没人会画这么久）；
      - 服务都没在跑、还 running 超过 2 分钟 → 死了；
      - 服务在跑但队列里 0 个在采样、这单已经挂了 3 分钟 → 是僵尸。"""
    now = now or time.time()
    age = now - float(rec.get("started_at") or 0)
    if age > 900:
        return True
    if not server_running and age > 120:
        return True
    if server_running and queue_running == 0 and age > 180:
        return True
    return False


def running_jobs(server_running=None, queue_running=None):
    """还在跑的 job（内存 + 落盘都算）——判断"到底有没有在画"。
    服务没在跑、或队列空着却标 running 的，一律不算在画（见 _is_stale）。"""
    up = server_up(timeout=1.5) if server_running is None else server_running
    now = time.time()
    out = []
    for jid, rec in list(_JOBS.items()) + list(_load_jobs().items()):
        if rec.get("state") != "running":
            continue
        if _is_stale(rec, up, now, queue_running):
            continue
        out.append(jid)
    return sorted(set(out))


def _reap_zombies(max_age_s=180):
    """进程起来时清一次：上一轮被强杀留下的 running 任务，标成 interrupted。
    （atexit 只在正常退出时跑；被 kill 的时候什么都不做，只能靠这里兜。）"""
    try:
        jobs = _load_jobs()
        now = time.time()
        for jid, rec in list(jobs.items()):
            if rec.get("state") != "running":
                continue
            if now - float(rec.get("started_at") or 0) > max_age_s:
                _persist_job(jid, {"state": "interrupted",
                                   "started_at": rec.get("started_at"),
                                   "ended_at": now, "skill": rec.get("skill"),
                                   "error": "上次进程退出时被中断（没画完）"})
    except Exception:
        pass


def _mark_interrupted_at_exit():
    """进程退出时把还标着 running 的任务改成 interrupted——不然下次她查
    状态会看到一堆永远"在跑"的僵尸任务。"""
    try:
        for jid, rec in list(_JOBS.items()):
            if rec.get("state") == "running":
                _persist_job(jid, {"state": "interrupted",
                                   "started_at": rec.get("started_at"),
                                   "ended_at": time.time(),
                                   "skill": rec.get("skill"),
                                   "error": "进程退出，任务被中断（没画完）"})
    except Exception:
        pass


atexit.register(_mark_interrupted_at_exit)
_reap_zombies()


def progress():
    """她现在到底在不在画——真实数据，不靠感觉。

    三个来源：ComfyUI 自己的 /queue（真正在跑的采样）、她的 job 记录、
    最近一次成功/失败的文件。她问"画好了吗"或者自己心里没底时调这个。"""
    srv = registry().get("server") or {}
    up = server_up(timeout=1.5)
    res = {"ok": True, "server_running": up, "queue": None,
           "running_jobs": [], "last": None, "last_error": None}
    qrun = None
    if up:
        try:
            q = _http("/queue", timeout=5)
            qrun = len(q.get("queue_running") or [])
            res["queue"] = {"running": qrun,
                            "pending": len(q.get("queue_pending") or [])}
        except Exception as e:
            res["queue"] = {"error": type(e).__name__}
    else:
        res["queue"] = {"running": 0, "pending": 0}
    # 队列数据到手后再判"到底在不在画"（队列空着还标 running 的是僵尸）
    res["running_jobs"] = running_jobs(server_running=up, queue_running=qrun)
    lr = last_result()
    if lr:
        fp = Path(str(lr.get("file") or ""))
        res["last"] = {"file": lr.get("file"), "skill": lr.get("skill"),
                       "gen_s": lr.get("gen_s"), "seed": lr.get("seed"),
                       "age_s": int(time.time() - float(lr.get("ts") or 0)),
                       "file_exists": fp.exists()}
    res["last_error"] = last_error_recent()
    # 僵尸任务：标 running 但其实早死了（服务没在跑 / 超时）——不能算"在画"
    zombie = [jid for jid, rec in _load_jobs().items()
              if rec.get("state") == "running"
              and jid not in res["running_jobs"]]
    if zombie:
        res["stale_jobs"] = zombie
    # 一句话结论（她直接照着说人话就行）
    if res["running_jobs"] or (res["queue"] or {}).get("running"):
        res["verdict"] = "真在画"
    elif zombie:
        res["verdict"] = "任务卡住了（服务已不在跑），这张没画成"
    elif res["last_error"]:
        res["verdict"] = "上次失败了，现在没在画"
    elif res["last"]:
        res["verdict"] = "没在画，最近一次画好了"
    else:
        res["verdict"] = "没在画，也没有画过的记录"
    return res


def _num(v, typ, lo=None, hi=None, name=""):
    try:
        v = typ(float(v)) if typ is float else typ(v)
    except Exception:
        raise ValueError(f"参数 {name} 不是数字：{v!r}")
    if lo is not None and v < lo:
        raise ValueError(f"参数 {name} 太小（{v} < {lo}）")
    if hi is not None and v > hi:
        raise ValueError(f"参数 {name} 太大（{v} > {hi}）")
    return v


def _prepare(skill, prompt, params):
    """归一化参数：缺的补默认、越界的拒绝、种子 0 换成真随机（并记下来）。"""
    reg = registry()
    sk = skills().get(skill)
    if not sk:
        raise ValueError(f"没有这个技能：{skill}（可选：{', '.join(skills())}）")
    if not (prompt or "").strip():
        raise ValueError("提示词不能为空")
    p = dict(params or {})
    out = {"prompt": prompt.strip()}
    specs = sk.get("params") or {}
    common = reg.get("common") or {}
    for name, spec in specs.items():
        if name == "prompt":
            continue
        val = p.get(name, None)
        if val in (None, ""):
            if spec.get("default_from") == "common.negative":
                val = common.get("negative", "")
            else:
                val = spec.get("default")
        if spec.get("type") == "int":
            val = _num(val, int, spec.get("min"), spec.get("max"), name)
        elif spec.get("type") == "float":
            val = _num(val, float, spec.get("min"), spec.get("max"), name)
        if name in ("width", "height") and int(val) % 32:
            val = int(round(int(val) / 32.0)) * 32          # 32 倍数硬约束
        out[name] = val
    if sk.get("mode") == "img2img":
        img = out.get("image")
        if not img:
            raise ValueError("图生图要给参考图（文件名或本地路径）")
        out["image"] = ensure_input(img)
    seed = int(out.get("seed") or 0)
    if seed <= 0:
        seed = random.randint(1, 2 ** 31 - 1)   # API 模式 seed=0 是**固定种子**，不是随机
    out["seed"] = seed
    if sk.get("mode") == "txt2img":
        out["denoise"] = 1.0                    # 文生图重绘强度恒 1，传别的没意义
    out["skill"] = skill
    out["workflow"] = sk["workflow"]
    out["workflow_name"] = sk.get("workflow_name") or Path(sk["workflow"]).name
    out["nodes"] = sk.get("nodes") or {}
    out["timeout_s"] = int(common.get("timeout_s", 900))
    return out


def build_cmd(prep):
    """把参数拼成**固定命令**。所有值显式传，正负提示词用 override 钉死节点。"""
    run = registry().get("runner") or {}
    nodes = prep["nodes"]
    prefix = "pet_%s_%s" % (prep["skill"], time.strftime("%Y%m%d_%H%M%S"))
    cmd = [run["python"], run["script"],
           "--workflow", prep["workflow"],
           "--prompt", prep["prompt"]]
    # 分辨率只有文生图有（图生图没有空 latent，尺寸由输入图决定，传了也不生效）
    if prep.get("width") and prep.get("height"):
        cmd += ["--width", str(prep["width"]), "--height", str(prep["height"])]
    cmd += ["--steps", str(prep["steps"]), "--cfg", str(prep["cfg"]),
            "--seed", str(prep["seed"]),
            "--sampler", str(prep["sampler"]), "--scheduler", str(prep["scheduler"]),
            "--denoise", str(prep["denoise"])]
    if prep.get("image"):
        cmd += ["--image", prep["image"]]
    if nodes.get("positive"):
        cmd += ["--override", f"{nodes['positive']}.text={prep['prompt']}"]
    if nodes.get("negative"):
        cmd += ["--override", f"{nodes['negative']}.text={prep['negative']}"]
    cmd += ["--override", f"filename_prefix={prefix}"]   # 别用 --outprefix（会塞进 video/）
    prep["prefix"] = prefix
    return cmd


def _resolve_out(name):
    od = (registry().get("dirs") or {}).get("output") or str(Path(config.ROOT) / "data" / "comfy_output")
    p = Path(od) / name.replace("/", os.sep)
    return p


def generate(skill="t2i", prompt="", **params):
    """同步出图（内部 API / 我自测用）。返回 dict，含命令、耗时、文件、显存。"""
    if not enabled():
        return {"ok": False, "error": "出图技能未启用（PET_COMFY_ENABLED=0）"}
    t0 = time.time()
    try:
        prep = _prepare(skill, prompt, params)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    v0 = vram()
    srv = start()
    if not srv.get("ok"):
        _note_error(skill, "服务起不来：" + str(srv.get("error")), prompt=prompt[:200])
        return {"ok": False, "error": srv.get("error"), "vram": v0}
    cmd = build_cmd(prep)
    _audit(f"{prep['skill']}：{prep['prompt'][:80]}",
           skill=prep["skill"], workflow=prep["workflow_name"], seed=prep["seed"],
           cmd=" ".join(cmd))
    t1 = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=prep["timeout_s"], encoding="utf-8",
                             errors="replace",
                             creationflags=0x08000000 if _WIN else 0)
    except subprocess.TimeoutExpired:
        _note_error(skill, f"出图超时（>{prep['timeout_s']}s）", prompt=prompt[:200])
        return {"ok": False, "error": f"出图超时（>{prep['timeout_s']}s）",
                "cmd": " ".join(cmd)}
    gen_s = time.time() - t1
    stdout = out.stdout or ""
    m = re.search(r"RESULT_(?:IMAGE|VIDEO):\s*(\S+)", stdout)
    if out.returncode != 0 or not m:
        err = (out.stderr or stdout or "").strip()[-600:]
        _note_error(skill, f"rc={out.returncode} {err}", prompt=prompt[:200],
                    cmd=" ".join(cmd))
        return {"ok": False, "error": f"出图失败（rc={out.returncode}）：{err}",
                "cmd": " ".join(cmd), "gen_s": round(gen_s, 1)}
    f = _resolve_out(m.group(1))
    v1 = vram()
    try:
        _http("/free", {"unload_models": True, "free_memory": True}, timeout=15)
    except Exception:
        pass
    st = _state()
    st["last_used"] = time.time()
    _save_state(st)
    res = {"ok": True, "skill": prep["skill"], "workflow": prep["workflow_name"],
           "file": str(f), "exists": f.exists(),
           "size_kb": round(f.stat().st_size / 1024) if f.exists() else None,
           "seed": prep["seed"],
           "params": {k: prep.get(k) for k in
                      ("width", "height", "steps", "cfg", "sampler",
                       "scheduler", "denoise")},
           "gen_s": round(gen_s, 1), "total_s": round(time.time() - t0, 1),
           "vram_before_mb": v0 and v0["free_mb"], "vram_after_mb": v1 and v1["free_mb"],
           "cmd": " ".join(cmd), "server": srv}
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        LAST_PATH.write_text(json.dumps({**res, "ts": time.time()}, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        pass
    clear_error()          # 画成了：旧失败记录清掉（别让她一直以为上次没画成）
    clear_promise()        # 真画了，"说要画"这笔账勾掉
    _audit(f"{prep['skill']} 出图完成 {res['gen_s']}s {f.name}",
           skill=prep["skill"], file=str(f), gen_s=res["gen_s"], seed=prep["seed"])
    return res


def submit(skill="t2i", prompt="", **params):
    """异步出图：立刻返回 job，画完落 last_result + 状态灯提示（不堵对话）。

    2026-10-29：job 状态必须落盘 + 失败必须留痕。她说过"我在画了"而实际
    失败的那种情况，靠内存字典是查不出来的——重启一次就什么都看不见了。"""
    if not enabled():
        return {"ok": False, "error": "出图技能未启用"}
    try:
        _prepare(skill, prompt, params)     # 先校验，坏参数当场告诉她
    except ValueError as e:
        _note_error(skill, "参数不对：" + str(e), prompt=prompt[:200])
        return {"ok": False, "error": str(e)}
    job_id = "cj%d" % int(time.time() * 1000)
    rec = {"state": "running", "started_at": time.time(),
           "skill": skill, "prompt": prompt[:300]}
    _JOBS[job_id] = rec
    _persist_job(job_id, dict(rec))

    def _work():
        try:
            r = generate(skill, prompt, **params)
            ok = bool(r.get("ok"))
            _JOBS[job_id].update({"state": "done" if ok else "error",
                                  "result": r, "ended_at": time.time()})
            _persist_job(job_id, {"state": "done" if ok else "error",
                                  "started_at": rec["started_at"],
                                  "ended_at": time.time(), "skill": skill,
                                  "prompt": prompt[:300],
                                  "file": r.get("file"),
                                  "error": (r.get("error") or "")[:300],
                                  "gen_s": r.get("gen_s")})
            if ok:
                _status_line(f"画好了：{Path(r['file']).name}")
                _maybe_push_image(r)
            else:
                _status_line(f"没画成：{str(r.get('error'))[:40]}")
        except Exception as e:
            _JOBS[job_id].update({"state": "error", "result": {"ok": False,
                                  "error": f"{type(e).__name__} {e}"}})
            _note_error(skill, f"{type(e).__name__} {e}", prompt=prompt[:200])
            _persist_job(job_id, {"state": "error", "started_at": rec["started_at"],
                                  "ended_at": time.time(), "skill": skill,
                                  "error": f"{type(e).__name__} {e}"[:300]})

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "job": job_id, "skill": skill,
            "note": "已开画，画好会告诉你（大约 1 分钟内）"}


def job(job_id):
    j = _JOBS.get(job_id)
    if not j:
        return {"ok": False, "error": "没有这个任务"}
    return {"ok": True, **j}


def last_result():
    try:
        return json.loads(LAST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _maybe_push_image(res):
    """画好了 → 本地气泡提示主人。

    2026-10-29 主人问「不能把图片提示到桌面气泡？」——以前画完只写一句
    "画好了：文件名"，图和主人都隔着两层文件夹。现在：她只要之前跟主人说过话
    （等待帧还在），画完就提示图的位置。

    只在有等待帧时推，不是每张都推——她说过"画好叫你"才叫，没说过就只走气泡。
    提示失败时 notify 会退成文字（带文件路径），不静默。
    """
    try:
        from channels import waiter
        if not waiter.is_waiting():
            return
        from channels import notify
        r = notify.tell_master_image(res.get("file"),
                                     caption="画好啦，你看看这个～",
                                     why="draw_done")
        _audit(f"画完推送：{'ok' if r.get('ok') else r.get('detail')}",
               file=res.get("file"))
    except Exception as e:
        _audit(f"画完推送异常：{type(e).__name__} {e}")


def _status_line(line):
    """桌宠气泡读 status.json（idle 不显示），所以用 working 帧挂一句话，
    30 秒后自己收回 idle——主人抬头就能看见"画好了"。"""
    try:
        from core import status as status_mod
        status_mod.set_state("working", "刚画完图", line=line)

        def _back():
            time.sleep(30)
            try:
                status_mod.set_state("idle", "")
            except Exception:
                pass

        threading.Thread(target=_back, daemon=True).start()
    except Exception:
        pass


def ensure_input(image):
    """参考图必须在本地 ComfyUI 的 input 目录下；给的是本地路径就复制进去。"""
    d = Path((registry().get("dirs") or {}).get("input") or str(Path(config.ROOT) / "data" / "comfy_input"))
    src = Path(image)
    if src.exists() and src.is_file():
        if src.parent.resolve() == d.resolve():
            return src.name
        d.mkdir(parents=True, exist_ok=True)
        dst = d / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.write_bytes(src.read_bytes())
        return dst.name
    return Path(image).name            # 当文件名用（已经在 input 里的）


def recent_hint(max_age_s=900):
    """下一轮对话给她一句"你刚画好的图"——不然她画完就忘了。

    2026-10-29 深夜升级：顺手把图抄一份到 data/petmedia/（webapp /files 静态
    路由），并在提示里给出 markdown 图片语法——主人发
    `![](http://127.0.0.1:8094/files/xxx.png)` 就能直接看到图（网页和手机
    App 的聊天窗都会渲染）。"""
    r = last_result()
    if not r or not r.get("ok"):
        return ""
    if time.time() - float(r.get("ts") or 0) > max_age_s:
        return ""
    src = Path(r["file"])
    name = src.name
    try:
        pm = Path(config.DATA_DIR) / "petmedia"
        pm.mkdir(parents=True, exist_ok=True)
        dst = pm / name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.write_bytes(src.read_bytes())
    except Exception:
        pass                                   # 抄送失败不影响提示
    url = f"http://127.0.0.1:{config.WEB_PORT}/files/{name}"
    return (f"【你自己刚画的图】{name}（{r['file']}｜"
            f"{r['params']['width']}x{r['params']['height']}｜"
            f"{r['params']['steps']}步｜{r['gen_s']}秒｜种子 {r['seed']}）"
            f"——想让主人直接看到图，就把 `![刚画的图]({url})` 原样发给主人"
            f"（聊天气泡会直接渲染出来）。URL 一个字都别改，整段复制——"
            f"127.0.0.1 打成 127.0.01 图就裂了。")


def context_hints():
    """每轮对话开头注入的三件事（webapp _build_block 调）。

    2026-10-29 加前两件：主人让她画自画像，她回"好嘞我去画啦"但根本没调用
    draw（触发词没命中，画笔没递到手上），然后一直以为自己"在画"。这类
    自我欺骗只能靠机制兜——① 空承诺记账 ② 失败回灌，让她下一轮自己纠正。"""
    parts = []
    p = pending_promise()
    if p:
        said = (p.get("said") or "").replace("\n", " ")[:80]
        parts.append(
            "【你上一轮说要画图，但实际没有开始】你说的是：「" + said + "」。"
            "画画这件事只有 draw 工具真的返回「[draw 已开画] 任务 cj…」才算"
            "在画；嘴上说去画 = 什么都没发生。现在补上：调 draw 工具"
            "（mode 和 prompt 给全），或者老实告诉主人你还没开始。")
    e = last_error_recent()
    if e:
        parts.append("【你上次画图失败了】" + str(e.get("error"))[:220] +
                     "——这次要么改了重画，要么把失败原因如实告诉主人，"
                     "绝不能当成画好了。")
    h = recent_hint()
    if h:
        parts.append(h)
    return "\n".join(parts)
