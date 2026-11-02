# -*- coding: utf-8 -*-
"""桌宠AI · 多模态入站（media_in.py）——主人发来的图片 → 她"看见"的文字。

为什么单独一个模块（2026-10-29 主人报"两次发图两次异常降级"）：
之前 OpenAI 兼容端点把 content 当字符串 `str(c)` 处理，`content` 是
`[{type:text},{type:image_url}]` 时，字典里的 **`type` 键**被当成了用户
问题去搜索——搜回一堆字典站释义（baike/item/type、typingclub…），图片
本身完全没进模型。这是"异常降级"的真因。

本模块只做三件事（纯函数，方便单测）：
1. `split_content()` —— 把 OpenAI content（str 或 list）拆成 (文本, [图片源])；
2. `save_data_uri()` —— data URI / 本地路径 → data/petmedia 下的文件；
3. `describe_images()` —— 走本机引擎视觉塔（vision.py）拿到画面描述，
   拼成注入块给 pipeline 当 query 的一部分。

红线：只用本机引擎（mmproj 常驻），不引任何外部视觉 API；图片落盘在项目
data/ 内，文件名带时间戳；失败一律返回空块 + 留痕，绝不抛给调用方。
"""
import base64
import json
import hashlib
import re
import time
from pathlib import Path

from core import config

_DATA_URI_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", re.S)
_MAX_IMAGE_BYTES = 12 * 1024 * 1024          # 12MB 上限，防超大图拖死引擎


def split_content(content):
    """OpenAI content → (纯文本, [图片源])。

    - str → (str, [])
    - [{type:"text"|"image_url", ...}] → ("拼接文本", ["data:..."|"http://...", ...])
    无法识别的项跳过（不把字典 str 化——那正是旧 bug 的来源）。
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content.strip(), []
    if not isinstance(content, (list, tuple)):
        return str(content).strip(), []
    parts, images = [], []
    for it in content:
        if isinstance(it, str):
            parts.append(it)
            continue
        if not isinstance(it, dict):
            continue
        itype = it.get("type")
        if itype == "text" or ("text" in it and itype not in ("image_url", "image")):
            parts.append(str(it.get("text") or ""))
        elif itype in ("image_url", "image", "input_image"):
            ref = it.get("image_url") or it.get("image") or it.get("source") or {}
            url = ref.get("url") if isinstance(ref, dict) else str(ref or "")
            if url:
                images.append(url)
    return "\n".join(p.strip() for p in parts if p and p.strip()).strip(), images


def save_data_uri(url):
    """data URI / 本地路径 → data/petmedia 下的图片文件。失败返回 None。"""
    try:
        if url.startswith("data:"):
            m = _DATA_URI_RE.match(url)
            if not m:
                return None
            ext = m.group(1).lower().replace("jpeg", "jpg")
            raw = base64.b64decode(m.group(2), validate=False)
        elif url.startswith(("http://", "https://")):
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=20) as resp:
                raw = resp.read()
            ext = (url.rsplit(".", 1)[-1] or "png").lower().split("?")[0][:4]
            if ext not in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
                ext = "png"
        else:
            p = Path(url)
            if not p.exists():
                return None
            raw = p.read_bytes()
            ext = p.suffix.lstrip(".").lower() or "png"
        if not raw or len(raw) > _MAX_IMAGE_BYTES:
            return None
        cache = Path(config.DATA_DIR) / "petmedia"
        cache.mkdir(parents=True, exist_ok=True)
        name = f"inbox_{time.strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(raw).hexdigest()[:6]}.{ext}"
        path = cache / name
        path.write_bytes(raw)
        return path
    except Exception:
        return None


def describe_images(images, user_text=""):
    """图片源列表 → 注入块（她"看见"的画面）。失败返回 ("", meta)。

    描述走本机视觉塔（vision.describe_image，已接 no_think 提速）。
    多图时逐张描述、编号拼接；单图失败不影响其它图。
    """
    if not images:
        return "", {"count": 0, "ok": 0, "paths": []}
    paths = [p for p in (save_data_uri(u) for u in images[:4]) if p]
    if not paths:
        return "", {"count": len(images), "ok": 0, "paths": []}
    try:
        from brain import vision
    except Exception as e:                                    # noqa: BLE001
        return "", {"count": len(images), "ok": 0,
                    "error": f"{type(e).__name__}: {e}"}
    descs = []
    for i, p in enumerate(paths, 1):
        try:
            d = vision.describe_image(
                p,
                question=("用中文客观描述这张图片：画面主体是谁/什么、外观细节"
                          "（人物就写发型、服装、气质）、颜色与构图、整体风格。"
                          "300 字以内，只描述你看到的，不要评价好坏。"),
                max_tokens=600)
            d = (d or "").strip()
            if d:
                descs.append(f"（图{i}）{d}")
        except Exception as e:                                # noqa: BLE001
            try:
                from core import audit as _audit_mod
                _audit_mod.log("vision", f"图片理解失败 {p.name}："
                                         f"{type(e).__name__}: {str(e)[:80]}")
            except Exception:
                pass
    meta = {"count": len(images), "ok": len(descs),
            "paths": [str(p) for p in paths]}
    if not descs:
        return "", meta
    block = ("【主人刚发来图片，你亲眼看到的画面是】\n" + "\n".join(descs)
             + "\n（这是你自己看到的，回话时自然提到就行，不用逐条复述描述。）")
    return block, meta


def compose_query(content, extra_text=""):
    """content（str/list）+ 附加文本 → (给 pipeline 的 query, 图片 meta)。

    无图时原样返回文本（零额外开销）；有图时拼上视觉描述块。
    2026-10-29 新增：文本里带本地视频路径（mp4 等）→ 同步看完（抽帧+音轨），
    拼上"看完的笔记"。重操作（秒级~分钟级），调用方状态灯已打"在看视频"。
    """
    text, images = split_content(content)
    if extra_text:
        text = (text + "\n" + extra_text).strip() if text else extra_text.strip()
    text, vmeta = watch_video_if_any(text)
    if not images:
        return text, {"count": 0, "ok": 0, "paths": [], **vmeta}
    block, meta = describe_images(images, text)
    meta.update(vmeta)
    if block:
        return (text + "\n\n" + block).strip(), meta
    # 视觉失败兜底：明确告诉她"图没看清"，别装看不见、也别瞎猜
    return (text + "\n\n【主人刚发来图片，但这次我没看清画面，可以说一句没看清】").strip(), meta


def watch_video_if_any(text):
    """文本里有本地视频路径 → 看完 → 返回 (新文本, meta)。

    失败绝不静默：看不了也要拼一句实话进上下文，她好告诉主人为什么没看成。"""
    try:
        from brain import video as _video
        from core import status as _status
        vp = _video.find_video_path(text)
        if not vp:
            return text, {}
        _status.set_state("working", "我在看主人发的视频")
        r = _video.analyze(vp)
        note = r.get("note") or "视频没看成"
        from core import audit as _audit_mod
        _audit_mod.log("vision", f"视频入站：{vp.name}｜看完 {'成功' if r.get('ok') else '失败'}")
        # 记住最近看过的视频（她说"我再去看看/多看几遍"时承诺执行要用）
        try:
            _lv = Path(config.DATA_DIR) / "memory" / "last_video.json"
            _lv.parent.mkdir(parents=True, exist_ok=True)
            _lv.write_text(json.dumps({"path": str(vp), "ts": time.time(),
                                       "name": vp.name},
                                      ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        _status.set_state("idle", "主人回话了")
        block = ("【主人给你看了一个视频（%s），你看完的笔记如下】\n%s\n"
                 "——笔记里有画面描述就照实聊；画面全是空的就直说没看成、"
                 "请主人重发，**不要联网去猜视频内容**。"
                 % (vp.name, note))
        return (text + "\n\n" + block).strip(), {"video": vp.name,
                                                 "video_ok": r.get("ok")}
    except Exception as e:
        try:
            from core import audit as _audit_mod
            _audit_mod.log("vision", f"视频入站异常：{type(e).__name__}: {str(e)[:100]}")
        except Exception:
            pass
        return (text + "\n\n【主人给了个视频，但这次没看成（%s），照实说别装看完了】"
                % type(e).__name__), {}
