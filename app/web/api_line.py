# -*- coding: utf-8 -*-
"""桌宠AI · API 线接口（api_line.py）——双线方案的另一条腿，框架占位。

对照 docs/API线-实施方案.md 与"双线方案"：本地线（已收官 L0-L5）之外，
API 线用在线大模型补深度推理/长文能力。**当前默认焊死关闭**——
config.EXT_WEB_ENABLED=False 且红线"不引入在线 API 依赖"指的就是它。

框架定位（先立骨架，开关在主人手里）：
- available()/status()：探测配置是否齐（url/token/model 三件套）
- chat()：唯一对外入口——调用方只管发消息，本模块负责路由与降级：
  未启用 → 返回 None（调用方走本地线，行为与现在完全一致）
  启用但网络失败 → 返回 None + 记一次失败（不重试轰炸，桌面场景）
- 用途边界（写死在语义里）：只做"深度思考外挂"（pipeline 的
  EXT_WEB 分支已预留），**不做日常聊天**——人设与记忆是本地线的资产，
  不让外接模型破坏人格一致性。

启用方式（到时候三步，不改代码）：
1. settings.json 加 ext_web: {url, token, model}
2. config.EXT_WEB_ENABLED=True（或环境变量）
3. 重启 webapp——pipeline 深度思考分支自动开始路由
"""
import json
import time
import urllib.request

from core import config

# 回环/外呼统一无代理 opener？不——这条线**就是要走系统代理**才能出网，
# 但工具链注入的回环代理同样会坏它。折中：显式读环境代理，无则直连。
_OPENER = urllib.request.build_opener()   # 默认策略（跟随环境）

_FAIL_COUNT = 0                          # 失败计数（内存态即可，桌面场景）


def _settings():
    """从 settings.json 读 ext_web 三件套（config 里有默认关的总开关）。"""
    try:
        s = config.load_settings()
        return s.get("ext_web") or {}
    except Exception:
        return {}


def status():
    """框架自检用：这条线现在的完整状态。"""
    ext = _settings()
    configured = bool(ext.get("url") and ext.get("model"))
    return {"enabled": bool(config.EXT_WEB_ENABLED),
            "configured": configured,
            "ready": bool(config.EXT_WEB_ENABLED and configured),
            "reason": ("红线默认关（config.EXT_WEB_ENABLED=False）" if not config.EXT_WEB_ENABLED
                       else "配置齐全" if configured else "缺 url/token/model 配置")}


def available():
    return status()["ready"]


def chat(messages, max_tokens=4096, timeout=120):
    """API 线对话入口。【接口占位——骨架通、未实测】。

    返回 {"content": str} 或 None（未启用/失败——调用方无感降级本地线）。
    """
    st = status()
    if not st["ready"]:
        return None
    ext = _settings()
    body = {"model": ext["model"], "messages": messages, "max_tokens": max_tokens}
    req = urllib.request.Request(
        ext["url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {ext.get('token', '')}"},
        method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return {"content": d["choices"][0]["message"].get("content", "")}
    except Exception:
        return None                     # 降级：调用方走本地线，不炸不重试
