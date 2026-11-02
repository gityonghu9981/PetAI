# -*- coding: utf-8 -*-
"""桌宠AI · 多模态接口（vision.py）——图片理解与"多模态博主"二期入口。

对照两份规划：
- 引擎侧：llama-server 带 mmproj 视觉塔（0.9GB 常驻，已随引擎加载）——
  看图说话的**底层能力已就位**，本模块只是给它一个体面的入口。
- 产品侧：docs/桌宠AI-视觉方案讨论稿 + 工作记录的"多模态博主（二期）"
  （自动拆解视频产出脚本，发布人工确认）——留接口，二期填。

当前状态：describe_image() 是**半实现**——HTTP 骨架已通（llama-server
OpenAI 兼容的 image 输入格式），但未实测视觉链路，先标 stub 防误信。
实测通过后把 _VERIFIED 翻 True 即可。

红线：博主类任务一律走 L4 调度（P3 EXPLORE 切片），不能挤掉聊天（P0）；
产出发布前必须人工确认（工作记录表明确要求）。
"""
import base64
import json
import mimetypes
import urllib.request
from pathlib import Path

from core import config

# 回环请求不过代理（B26 教训：代理注入的环境里 urlopen 会 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_VERIFIED = True   # 2026-10-26 视觉链路实测通过（红圆+蓝条识别，见任务单交付记录）          # 视觉链路实测通过后翻 True
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_EXTS


def _data_url(path, max_side=768):
    """本地图片 → data URI（llama-server 接受的 image_url 形态）。

    2026-10-29 提速：长边超过 768 先等比缩小再编码——视觉塔的耗时基本跟
    "图块数"成正比，主人发的是 832×1216，原样喂要多等一两秒；缩到 768
    肉眼与描述质量无损（她看的是发型/服装/风格，不是像素级细节）。
    """
    p = Path(path)
    try:
        import io

        from PIL import Image
        im = Image.open(p)
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=88)
            return ("data:image/jpeg;base64,"
                    + base64.b64encode(buf.getvalue()).decode())
    except Exception:
        pass                                   # 缩图失败就原样上（可用性优先）
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def describe_image(image_path, question="这张图里有什么？", max_tokens=2048):
    """看图说话：本机引擎视觉塔（mmproj 常驻），不引外部 API（红线）。

    max_tokens 1024→2048（外部审核 P1-1：低于红线思考吃光预算 content 空）。
    2026-10-29 提速：关思考（enable_thinking=False + /no_think 双保险）——
    描述是"看图说话"不是推理题，思考只会让主人多等十几秒；
    主人发图对话必须快，慢就是"异常降级"体感。
    """
    p = Path(image_path)
    if not p.exists():
        raise FileNotFoundError(f"图片不存在: {p}")
    body = {
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": _data_url(p)}},
            {"type": "text", "text": question + " /no_think"},
        ]}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        config.ENGINE_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=300) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return d["choices"][0]["message"].get("content", "")


# ---------------------------------------------------------------- 博主二期（接口）
def video_brief(video_path_or_url):
    """视频 → 选题拆解稿。【接口占位】二期：抽帧（视觉塔看关键帧）→
    转写 → 结构化 brief。属 P3 EXPLORE 任务，走 scheduler 切片。"""
    return {"status": "not_ready",
            "reason": "多模态博主属二期（工作记录 v0.5 表已列）",
            "prereq": ["抽帧工具（ffmpeg）接入", "视觉链路实测（_VERIFIED）",
                       "L4 调度的 EXPLORE 任务模板"]}


def publish_gate(brief):
    """发布人工确认闸门。【接口占位】二期的发布前强制环节，
    在主人点头前任何产出不得自动发布（红线）。"""
    return {"status": "not_ready", "pass": False,
            "reason": "发布确认 UI 属二期"}
