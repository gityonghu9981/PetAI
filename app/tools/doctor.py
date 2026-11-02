# -*- coding: utf-8 -*-
"""桌宠AI · 框架自检（doctor.py）——"整体先起来"的体检入口。

对全部模块做三件事：
1. 可导入（框架完整的底线：任何模块 ImportError 就是骨架破了）
2. 实现状态（implemented / interface 占位 / 规划中）——逐模块核对关键函数
3. 运行时探测（引擎/webapp 端口、显存、数据池、检索通道）

跑法：python app/doctor.py            （人类可读报告）
      python app/doctor.py --json    （机读，给 /api/framework 用）

设计原则：**只读不写、不起服务**——引擎没跑也要能给全量报告
（运行时探测项标 offline，不是失败）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # app 根（分层后在 tools/）

# ---------------------------------------------------------------- 小工具
def _port_up(port, host="127.0.0.1", timeout=1.5):
    """端口是否在监听（跨进程兜底：引擎可能由守护/一键启动拉起，本进程无句柄）。"""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------- 框架清单
# (模块, 层级, 职责, 实现状态, 核对函数列表)
FRAMEWORK = [
    ("config",     "L0", "配置真源（路径/端口/采样/预留政策）",        "implemented", ("summary", "engine_args")),
    ("lexicon",    "L0", "词表层（调词不动代码）",                     "implemented", ()),
    ("vram",       "L0", "显存预算（fit/降档/预留政策）",              "implemented", ("fit", "report")),
    ("engine",     "L1", "引擎进程管理（启动/停止/健康）",             "implemented", ("start", "stop", "is_running")),
    ("llm",        "L1", "对话客户端（自愈裁剪/重试）",                "implemented", ("chat", "chat_stream")),
    ("context",    "L1", "上下文分区（配额钳制，记忆块独立）",         "implemented", ("build_messages", "Session")),
    ("searcher",   "L2", "多引擎检索（并行/首达/合并过滤/冷却换源）",  "implemented", ("Searcher",)),
    ("router",     "L2", "意图判定（规则+兜底）",                      "implemented", ()),
    ("skills",     "L2", "技能判定（时效/纠错/写作豁免）",             "implemented", ()),
    ("pipeline",   "L2", "编排（路由→检索→生成→后处理）",              "implemented", ("answer_once", "answer_stream")),
    ("memory",     "L3", "记忆（三路RRF/核心集/巩固/迁移链）",         "implemented", ("recall", "core_block", "consolidate")),
    ("proactive",  "L3", "主动搭话（C8 起不再掷骰：到点就说/static 零算力）",       "implemented", ("maybe_speak",)),
    ("autonomy",   "L3", "自主唤醒（安静 3 分钟→自己找事做/留痕/日记）", "implemented", ("tick", "wake_once", "touch", "status_view")),
    ("comfy",      "L3", "ComfyUI 出图（技能库+注册表+要用才启动/用完即关）", "implemented", ("generate", "submit", "start", "stop", "status")),
    ("scheduler",  "L4", "调度三模式（优先级/抢占/切片/checkpoint）",  "implemented", ("submit", "loop_once", "start")),
    ("skillbook",  "L5", "技能建档（闸门/配额/metrics/retro）",        "implemented", ("create", "load_for", "publish")),
    ("vector",     "L3", "向量召回第三路腿（路线 A 引擎复用，已启用）",  "implemented", ("available", "encode", "best_match", "route", "stats")),
    ("vision",     "L2", "多模态（看图骨架通未实测；博主二期占位）",   "interface",   ("describe_image", "video_brief", "publish_gate")),
    ("api_line",   "L2", "API 线外挂深度推理（红线默认关）",           "interface",   ("status", "chat", "available")),
    ("wishes",     "L3", "她的提议留痕（只逛只提议，接入主人人工执行）",   "implemented", ("propose", "list_wishes", "approve", "reject")),
    ("audit",      "L3", "行为审计（暂时功能：behavior.jsonl + 会话落档可回放）", "implemented", ("log", "query", "sessions", "TurnLogger")),
    ("status",     "L3", "实时状态灯（永久功能：状态机→status.json→桌宠气泡）", "implemented", ("set_state", "from_step", "read")),
]


# 代码位置（2026-10-29 分层重构）：报告里沿用习惯短名，实际导入走新的包路径。
# 新加模块时在这里登记一行，doctor 才会体检它。
MOD_PATH = {
    "config": "core.config", "lexicon": "core.lexicon", "vram": "core.vram",
    "engine": "core.engine", "llm": "core.llm", "audit": "core.audit",
    "status": "core.status",
    "context": "brain.context", "searcher": "brain.searcher",
    "router": "brain.router", "skills": "brain.skills",
    "pipeline": "brain.pipeline", "memory": "brain.memory",
    "vector": "brain.vector", "vision": "brain.vision",
    "media_in": "brain.media_in",
    "proactive": "growth.proactive", "autonomy": "growth.autonomy",
    "scheduler": "growth.scheduler", "skillbook": "growth.skillbook",
    "wishes": "growth.wishes",
    "notify": "channels.notify",
    "waiter": "channels.waiter",
    "voice": "channels.voice",
    "comfy": "tools.comfy", "api_line": "web.api_line",
}


def framework_report():
    """全量框架报告（机读结构，doctor 命令做人类可读渲染）。"""
    import importlib
    report = {"modules": [], "import_fail": [], "runtime": {}}
    for name, layer, duty, state, fns in FRAMEWORK:
        m = {"name": name, "path": MOD_PATH.get(name, name), "layer": layer,
             "duty": duty, "state": state,
             "imported": False, "missing_functions": []}
        try:
            mod = importlib.import_module(MOD_PATH.get(name, name))
            m["imported"] = True
            for fn in fns:
                if not callable(getattr(mod, fn, None)):
                    m["missing_functions"].append(fn)
        except Exception as e:
            m["error"] = f"{type(e).__name__}: {str(e)[:100]}"
            report["import_fail"].append(name)
        report["modules"].append(m)

    # ---- 运行时探测（只读，不起服务）----
    r = report["runtime"]
    try:
        from core import config
        r["config"] = config.summary()
    except Exception as e:
        r["config"] = f"FAIL {e}"
    try:
        from core import engine
        # 引擎可能是别的进程（守护/一键启动）拉起的，本模块没有句柄——
        # is_running() 只认自己句柄会误报"未运行"。这里先探端口兜底。
        r["engine_running"] = engine.is_running() or _port_up(config.ENGINE_PORT)
    except Exception as e:
        r["engine_running"] = f"FAIL {e}"
    try:
        from core import vram
        g = vram.query_gpu()
        r["vram"] = f"{g['used_mb']}/{g['total_mb']}MiB"
    except Exception as e:
        r["vram"] = f"FAIL {e}"
    try:
        from brain import memory
        r["memory"] = memory.stats()
    except Exception as e:
        r["memory"] = f"FAIL {e}"
    try:
        from growth import skillbook
        r["skills"] = skillbook.stats()
    except Exception as e:
        r["skills"] = f"FAIL {e}"
    try:
        from growth import scheduler
        r["scheduler"] = scheduler.stats()
    except Exception as e:
        r["scheduler"] = f"FAIL {e}"
    try:
        from web import api_line
        r["api_line"] = api_line.status()
    except Exception as e:
        r["api_line"] = f"FAIL {e}"
    try:
        from brain import vector
        r["vector_ready"] = vector.available()
        r["vector_route"] = vector.route()          # A（引擎复用）/ B(兜底) / 不可用
        r["vector_stats"] = vector.stats()          # 调用数/错峰拦截数/兜底次数
    except Exception as e:
        r["vector_ready"] = f"FAIL {e}"
    report["ok"] = not report["import_fail"]
    return report


def _render(rep):
    """人类可读树。"""
    ok = "✓"
    print("桌宠AI · 框架自检（整体先起来）")
    print("=" * 62)
    cur_layer = None
    for m in rep["modules"]:
        if m["layer"] != cur_layer:
            cur_layer = m["layer"]
            print(f"\n[{cur_layer}]")
        flag = ok if m["imported"] and not m["missing_functions"] else "✗"
        state = {"implemented": "实现", "interface": "接口占位"}.get(
            m["state"], m["state"])
        miss = f" 缺{m['missing_functions']}" if m["missing_functions"] else ""
        err = f" 导入失败:{m.get('error', '')}" if not m["imported"] else ""
        print(f"  {flag} {m['name']:<12} {state}  {m['duty']}{miss}{err}")
    print("\n[运行时]")
    rt = rep["runtime"]
    print("  配置   :", rt.get("config"))
    print("  引擎   :", "运行中" if rt.get("engine_running") is True else "未运行（桌宠壳/静态台词不受影响）")
    print("  显存   :", rt.get("vram"))
    print("  记忆库 :", rt.get("memory"))
    print("  技能   :", rt.get("skills"))
    print("  调度   :", rt.get("scheduler"))
    print("  API线  :", rt.get("api_line"))
    _vs = rt.get("vector_stats") or {}
    print("  向量路 :", f"路线 {rt.get('vector_route')}｜可用={rt.get('vector_ready')}"
          f"｜调用 {_vs.get('calls', 0)} 次、错峰拦截 {_vs.get('blocked', 0)} 次"
          if rt.get("vector_ready") is True else "未接入（两路融合运行中）")
    print("\n框架状态:", "完整 ✓（全部模块可导入）" if rep["ok"]
          else f"骨架破损 ✗ 导入失败: {rep['import_fail']}")


if __name__ == "__main__":
    rep = framework_report()
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        _render(rep)
    sys.exit(0 if rep["ok"] else 1)
