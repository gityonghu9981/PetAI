# -*- coding: utf-8 -*-
"""桌宠AI · L1 引擎进程管理（engine.py）

职责边界（学 DeepSeek Harness 的"一能力一模块"）：
- 只管 llama-server 进程的启动/健康/停止/重启；
- 不掺任何业务（对话/记忆/技能是别的模块的事）；
- 参数一律来自 config.py（L0 真源），本文件不读环境变量；
- 启动前跑 vram.fit() 显存预算——装不下会自动降档并写明原因，
  而不是让 CUDA OOM 的报错留给用户猜。

对外约定（与旧版兼容）：
  python app/engine.py start|stop|restart|status
"""
import os
import subprocess
import sys
import time
import traceback
import urllib.request

# 入口自举：必须先插 app 根再 import core——2026-10-29 实测分层迁移时把
# 自举代码弄丢了只剩注释，engine.py 直接跑报 ModuleNotFoundError: core。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 回环请求专用无代理 opener（HTTP_PROXY 注入的环境里 urllib 会把 127.0.0.1
# 也交给代理 → 502，表现为"明明活着却被判死"。B26 实测）。
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

from core import config
from core import vram

_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW：桌面壳无控制台，子进程不许闪黑框
HEALTH_TIMEOUT_S = 180           # 18GB 权重 + 冷盘加载，给足时间（实测约 30s，机械盘更久）
_proc = None                     # 本模块持有的引擎句柄
_open_logs = []                  # Popen 占用的日志句柄（stop 时统一关闭）


def _log(msg):
    print(f"[engine] {msg}")


def _audit_kill(what):
    """杀引擎审计（GLM 任务 2026-10-28）：谁在什么时候动了引擎——追加一行到
    data/logs/engine_kill.log，带完整调用链。老毛病"引擎静默消失"（engine_err.log
    止于 listening、无退出日志 = 被 Stop-Process -Force 强杀）缺的就是这个证据：
    下次再消失，直接看这份账就知道是哪条代码路径动的手。写失败不影响主流程。"""
    try:
        stack = " <- ".join(
            f"{f.filename.split('/')[-1].split(chr(92))[-1]}:{f.lineno}:{f.name}"
            for f in traceback.extract_stack()[:-1][-6:])
        import datetime
        line = (f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {what} | caller: {stack}")
        with open(config.LOG_DIR / "engine_kill.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 健康与进程
def is_running():
    """引擎是否存活：以 /health 探测为准（2026-10-28 修复）。

    旧逻辑第一道闸"句柄不在/句柄已退 → 直接 False"有个坑：引擎常由独立进程
    （start_all.ps1 / 守护用 engine.py start 拉起、或手动起 llama-server）托管，
    webapp 进程内 _proc 恒为空 → 永远判 False → 形象面板报"引擎离线"误报，
    而引擎 18123 的 /health 一直是 200。

    现在：仅当句柄明确已退出才短路判死；句柄为空（外部托管）或句柄存活时，
    一律以回环 /health 200 为准——引擎死活看端口健康，不看"是不是我启动的"。
    """
    if _proc is not None and _proc.poll() is not None:
        return False
    try:
        url = f"http://{config.ENGINE_HOST}:{config.ENGINE_PORT}/health"
        # 无代理 opener：回环请求不走系统/环境代理（B26 实测代理注入会 502）
        with _NOPROXY.open(url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ps_quote(s):
    """字符串 → PowerShell 单引号字面量（'→''）。外部审核 P2 修复：
    路径含单引号会提前闭合字符串，把命令撕成两截（误杀/漏杀）。"""
    return "'" + str(s).replace("'", "''") + "'"


def _ps_like(s):
    """字符串 → -like 通配符安全片段：转义 [ ] * ?（反引号）。
    外部审核 P2 修复：路径含 [ ]（如 [release] 目录）会被当字符集通配，
    -like 匹配失真 → 误杀或漏杀。"""
    for ch in ("[", "]", "*", "?"):
        s = str(s).replace(ch, "`" + ch)
    return _ps_quote(s)                       # 通配转义后再做引号字面量


def _kill_by_path():
    """按可执行文件路径清掉本项目的引擎残留进程（跨进程重启时句柄不可用）。

    双条件：exe 是本项目的 llama-server，且命令行 -m 指向本项目 models 目录——
    后者是为了放过"用本项目 exe 跑别项目模型"的实例（Bonsai B18 的教训）。
    注意用 -like 通配而不是 -match 正则：路径里的反斜杠+字母在 .NET 正则里是
    无效转义会直接抛异常，Where-Object 静默跳过 → 一个进程都杀不掉
    （B26 实测：显存 20GB 一直降不下来就是这个原因）。
    外部审核 P2 修复（2026-10-27）：exe 用 -eq 单引号字面量（引号转义）、
    models 用 -like 且通配符转义——路径含 ' [ ] * ? 都不再破坏命令。
    """
    exe = str(config.ENGINE_EXE)
    models = str(config.MODEL_DIR)
    _audit_kill("kill_by_path 按路径清理本项目 llama-server 残留")   # GLM 2026-10-28 审计
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
          f"Where-Object {{ $_.ExecutablePath -eq {_ps_quote(exe)} -and "
          f"$_.CommandLine -like {_ps_like('*' + models + '*')} }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0 and (r.stderr or "").strip():
            _log(f"清理残留进程返回非零: {(r.stderr or '').strip()[:150]}")
    except Exception as e:
        _log(f"清理残留进程失败: {e}")


def wait_ready(timeout=HEALTH_TIMEOUT_S):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(2)
    return is_running()


# ---------------------------------------------------------------- 启停
def _spawn_alive():
    """是否已有本项目的 llama-server 进程在跑（含**加载中**——端口未监听但
    进程存在）。外部审核修复期间实测抓到的守护自残缺陷（2026-10-27）：
    引擎加载需 30~60s，期间端口未监听 → 守护每轮探活失败 → 再 start() →
    _kill_by_path 把"正在加载"的上一个实例杀掉 → 无限自残，永远起不来。
    start() 开头先查进程：加载中就等它，不要再杀不要再拉。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
          f"Where-Object {{ $_.ExecutablePath -eq {_ps_quote(str(config.ENGINE_EXE))} }} | "
          "Select-Object -First 1 ProcessId | "
          "ForEach-Object { $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=_NO_WINDOW)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def start(extra=None, wait=True, timeout=HEALTH_TIMEOUT_S):
    """启动引擎（幂等）。返回 True/False。

    启动前会跑显存预算：装不下时自动降档（先降 ctx 再降 ngl），
    决策与原因都会写进日志——用户不该被 CUDA OOM 裸奔报错伺候。
    """
    global _proc
    if _proc is None and _spawn_alive():
        # 已有实例在跑（哪怕还在加载中）：绝不杀绝不再拉——守护自残缺陷的修复点
        _log("已有引擎实例在跑（可能加载中），等待其就绪而非重复拉起")
        return wait_ready(timeout) if wait else True
    if _proc is None:
        _kill_by_path()                     # 跨进程场景：句柄为空但残留占着端口
        time.sleep(1)
    if is_running():
        _log(f"已在运行 {config.ENGINE_HOST}:{config.ENGINE_PORT}")
        return True

    # --- 显存预算（L0 政策：给其它应用留空间）---
    fit = vram.fit()
    _log("显存预算: " + vram.report())
    if not fit["ok"]:
        _log("拒绝启动: " + fit["reason"])
        return False
    if fit["action"] != "as-is":
        _log("自动降档: " + fit["reason"])
    # 实际生效值必须与日志一致（降档后的 ctx/ngl），不要打印 config 原值误导人
    eff_ctx, eff_ngl = fit["ctx"], fit["ngl"]

    if not Path(config.ENGINE_EXE).exists():
        _log(f"FATAL 引擎不存在: {config.ENGINE_EXE}")
        return False
    if not Path(config.MODEL).exists():
        _log(f"FATAL 模型不存在: {config.MODEL}")
        return False

    args = [str(config.ENGINE_EXE)] + config.engine_args(ctx=eff_ctx, ngl=eff_ngl,
                                                         extra=extra)
    _log(f"启动: engine={config.ENGINE_EXE.name}  model={config.MODEL.name}  "
         f"ctx={eff_ctx}  ngl={eff_ngl}")
    try:
        out = open(config.LOG_DIR / "engine_out.log", "a", encoding="utf-8")
        err = open(config.LOG_DIR / "engine_err.log", "a", encoding="utf-8")
        _open_logs.extend([out, err])
        _proc = subprocess.Popen(args, stdout=out, stderr=err,
                                 creationflags=_NO_WINDOW)
    except OSError as e:
        _log(f"启动失败: {type(e).__name__}: {e}")
        return False

    if wait:
        ok = wait_ready(timeout)
        if not ok:
            _log(f"启动超时（{timeout}s），看日志: {config.LOG_DIR / 'engine_err.log'}")
            _log("注意：确认实际用的引擎是不是 b11889——环境变量 PET_ENGINE 若指向"
                 "旧引擎会静默用错（B26 审查 P0-2）。")
        else:
            _close_logs()
        return ok
    return True


def stop():
    """停止引擎（幂等）。无论句柄在不在，都按路径再清一遍——
    引擎可能是守护/别的进程起的，只 terminate 本模块句柄会漏（B26 审查 P1-2）。"""
    global _proc
    _audit_kill("stop() 主动停止引擎")          # GLM 2026-10-28 审计
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None
    _kill_by_path()
    _close_logs()
    _log("已停止")


def restart(extra=None):
    stop()
    time.sleep(1)
    return start(extra)


def _close_logs():
    while _open_logs:
        try:
            _open_logs.pop().close()
        except Exception:
            pass


# ---------------------------------------------------------------- 自测入口
if __name__ == "__main__":
    import sys
    print("=== 桌宠AI 引擎自测 ===")
    print(config.summary())
    print(vram.report())
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        print("启动结果:", "就绪" if start() else "失败")
    elif cmd == "stop":
        stop()
    elif cmd == "restart":
        print("重启结果:", "就绪" if restart() else "失败")
    elif cmd == "fit":
        import json
        print(json.dumps(vram.fit(), ensure_ascii=False, indent=1))
    else:
        print("运行状态:", "运行中" if is_running() else "未运行")
