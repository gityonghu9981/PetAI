# -*- coding: utf-8 -*-
"""桌宠AI · L0 显存预算（vram.py）

政策：4090 24GB 也必须给系统/桌面/其它应用留空间（config.VRAM_RESERVE_MB，
默认 4096MB）。本模块只做三件事：
  1) 查询当前显存占用；
  2) 估算模型上卡需要多少；
  3) 在装不下时给出"降 ctx / 降 ngl"的具体建议。

纯计算 + 一次 nvidia-smi 子进程，不碰引擎进程本身。
"""
import subprocess

from core import config


def query_gpu():
    """整机显存（MiB）。失败返回 None——调用方自行决定是否跳过预算检查。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)
        parts = [x.strip() for x in (out.stdout or "").split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            return {"used_mb": int(parts[0]), "total_mb": int(parts[1])}
    except Exception:
        pass
    return None


def model_footprint_mb():
    """权重 + 视觉塔的**文件大小**（上卡近似值，Q4 权重上卡开销与文件大小接近）。"""
    import os
    total = 0
    for p in (config.MODEL, config.MMPROJ):
        try:
            if p and os.path.exists(p):
                total += os.path.getsize(p) // (1024 * 1024)
        except OSError:
            pass
    return total


def kv_mb(ctx_tokens):
    """KV 缓存估算（0.06MB/token 量级，见 config.KV_MB_PER_TOKEN）。"""
    return int(ctx_tokens * config.KV_MB_PER_TOKEN)


# CUDA 上下文 / 计算工作区等固定开销（与模型大小无关）。
# B26 实测校准：ngl=90 时实际占 18576MB，其中权重按层折算 15932 + KV 983，
# 差值 ~1660MB 即固定开销。没有这一项，预算会乐观 1.6GB，把"留空间"的政策
# 静默吃掉（实测：不校准时留给其它应用的只有 2.9GB）。
# **注意粒度限制**：实测 ngl=82 与 ngl=90 的占用完全相同（18576MB）——
# Windows WDDM 下 nvidia-smi 的显存报告粒度很粗，±1.5GB 级别。
# 所以本模块的定位是**防灾难**（两个 27B 同时上卡的必然 OOM、环境变量
# 指错引擎这类结构性问题），不是精细调控；后者需要 Linux/iGPU 专用环境。
CUDA_FIXED_MB = 1700


def need_mb(ngl, ctx):
    """估算 ngl/ctx 组合的显存需求（文件大小按层折算 + KV + 固定开销）。"""
    weight = int(model_footprint_mb() * ngl / 99)
    return weight + kv_mb(ctx) + CUDA_FIXED_MB


def fit(ctx=None, ngl=None):
    """启动前预算检查。返回 dict：
    ok          是否放行
    action      'as-is' | 'shrink-ctx' | 'shrink-ngl' | 'refuse'
    ngl / ctx   建议值（原样或降档后）
    reason      给日志/界面看的一句话
    """
    ctx = ctx or config.CTX
    ngl = ngl or config.NGL
    gpu = query_gpu()
    if gpu is None:
        return {"ok": True, "action": "as-is", "ngl": ngl, "ctx": ctx,
                "reason": "查不到 nvidia-smi，跳过显存预算（GPU 不可用？）"}

    budget = gpu["total_mb"] - config.VRAM_RESERVE_MB          # 引擎可用上限
    avail = budget - gpu["used_mb"]                            # 还能塞多少
    need = need_mb(ngl, ctx)                                   # 全量上卡需求
    over = need - avail

    if over <= 0:
        return {"ok": True, "action": "as-is", "ngl": ngl, "ctx": ctx,
                "reason": f"预算内：需 {need}MB / 可用 {avail}MB（预留 {config.VRAM_RESERVE_MB}MB 给其它应用）"}

    # 第一步：降 ctx（权重保持全上卡，速度不受影响；桌宠聊天 8K 够用）
    for c in (8192, 6144, 4096):
        if c >= ctx:
            continue
        if avail >= need_mb(ngl, c):
            return {"ok": True, "action": "shrink-ctx", "ngl": ngl, "ctx": c,
                    "reason": f"为预留 {config.VRAM_RESERVE_MB}MB，ctx 从 {ctx} 降到 {c}"
                              f"（需 {need_mb(ngl, c)}MB / 可用 {avail}MB）"}

    # 第二步：降 ngl（每层 ≈ 权重/层数；层数未知按 64 层估）
    per_layer = max(1, model_footprint_mb() // 64)
    for n in range(ngl - 1, -1, -max(1, 4)):
        need2 = need_mb(n, ctx)
        if avail >= need2:
            return {"ok": True, "action": "shrink-ngl", "ngl": n, "ctx": ctx,
                    "reason": f"为预留显存降 ngl 到 {n}（速度会变慢；每层约 {per_layer}MB）"}

    return {"ok": False, "action": "refuse", "ngl": 0, "ctx": ctx,
            "reason": f"装不下且降档也不够：需 {need}MB / 可用 {avail}MB。"
                      f"请关闭其它占显存的 AI 栈，或把 PET_VRAM_RESERVE_MB 调小"}


def report():
    """给日志/运行台用的一段话。"""
    gpu = query_gpu()
    fp = model_footprint_mb()
    if gpu is None:
        return f"显存查询不可用；模型文件 {fp}MB"
    free = gpu["total_mb"] - gpu["used_mb"]
    return (f"显存 {gpu['used_mb']}/{gpu['total_mb']}MB（剩 {free}MB）｜"
            f"模型需求 {fp}MB + KV{kv_mb(config.CTX)}MB｜"
            f"预留政策 {config.VRAM_RESERVE_MB}MB")
