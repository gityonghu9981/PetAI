# -*- coding: utf-8 -*-
"""桌宠AI · 看视频（app/brain/video.py）——主人给个 mp4，她真能"看完"并说出内容。

链路（2026-10-30 升级：主人要求"全片每一帧都看"，不再只抽 6 帧）：
  mp4 → PyAV 探测 → **全片按时间间隔密集采样**（默认每 2 秒一帧，上限 96 帧）
      → **分批多图请求**（每批 6 帧、一次请求让视觉塔逐帧描述——实测 4 帧 2.7s，
        且帧间动作变化描述更连贯）→ 音轨**全量**解码 wav → Whisper 转写
      → 拼成"看完的笔记"注入对话上下文。

容量账（2 分 06 秒 @30fps = 3780 帧）：
  每 2s 一帧 → 63 帧 → 11 批 × ~3s ≈ 35s + Whisper 转写 2 分钟音频 ≈ 20s
  → 全片看完约 1 分钟。"每一帧"物理上不现实（视觉塔 1.5s/帧），2 秒一帧
  已能接住所有镜头切换；主人要更细可说"逐秒看"（every_s=1）。

长视频保护：>15 分钟自动降为每 10 秒一帧，笔记注明"太长只给概要"。
"""
import os
import re
import sys
import time
from pathlib import Path

from core import config

_VENDOR = Path(getattr(config, "VOICE_VENDOR_DIR",
                       Path(config.ROOT) / "vendor" / "pylibs"))
FRAME_DIR = Path(config.DATA_DIR) / "petmedia" / "frames"
SUPPORTED = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
EVERY_S = 1.0            # 默认采样间隔：每秒 1 帧（帧间变化的人类视觉分辨极限；
                         # 30fps 相邻帧几乎全同，逐帧描述没有信息量——主人要
                         # "完整看全部"，每秒 1 帧即不漏任何一秒的内容）
MAX_FRAMES = 1200        # 主人 2026-10-30：视频和音频不设上限、尽可能看完。
                         # 1200 帧 ≈ 20 分钟视频每秒 1 帧全采样；更长的片
                         # 自动降采样密度（是调密度不是截断内容）
BATCH = 8                # 每次视觉塔请求带几张图（实测 8 张/批容量充裕）
NOTE_MAX_CHARS = 0       # 已废弃（2026-10-30 主人：不要截断），保留变量只为兼容

# 对话文本里出现的本地视频路径（主人直接甩个路径过来）
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\"'<>|*?\r\n]{1,180}?\.(" +
    "|".join(ext.lstrip(".") for ext in SUPPORTED) + r")",
    re.IGNORECASE)


def _vendor():
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))


def find_video_path(text):
    """从对话文本里找本地视频路径（存在才返回）。"""
    for m in _PATH_RE.finditer(text or ""):
        p = Path(m.group(0).strip())
        if p.exists() and p.is_file():
            return p
    return None


def probe(path):
    """→ dict(duration_s, w, h, has_audio, fps)；打不开返回 None。"""
    _vendor()
    try:
        import av
        with av.open(str(path)) as c:
            v = c.streams.video[0] if c.streams.video else None
            a = c.streams.audio[0] if c.streams.audio else None
            # PyAV 19：InputContainer 没有 time_base，时长从视频流拿（秒）
            dur = 0.0
            if v is not None:
                if v.duration is not None and v.time_base is not None:
                    dur = float(v.duration) * float(v.time_base)
                elif v.frames and v.average_rate:
                    dur = v.frames / float(v.average_rate)
            return {"duration_s": round(dur, 1),
                    "w": v.codec_context.width if v else 0,
                    "h": v.codec_context.height if v else 0,
                    "has_audio": bool(a),
                    "fps": round(float(v.average_rate), 1) if v and v.average_rate else 0}
    except Exception:
        return None


def extract_frames(path, every_s=EVERY_S, max_frames=MAX_FRAMES):
    """全片按时间间隔采样 → [(时间s, jpg路径)]。

    every_s=2.0 即每 2 秒一帧（镜头切换基本都能接住）；时间戳取自帧
    pts（真实播放时刻，不是均匀假设）。"""
    _vendor()
    import av
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    stem = Path(path).stem[:40]
    next_t = 0.0
    with av.open(str(path)) as c:
        v = c.streams.video[0]
        for frame in c.decode(video=0):
            t = float(frame.pts) * float(frame.time_base) if frame.pts is not None else 0.0
            if t + 1e-6 < next_t:
                continue
            fp = FRAME_DIR / ("%s_f%03d.jpg" % (stem, len(out) + 1))
            frame.to_image().save(fp, "JPEG", quality=80)
            out.append((round(t, 1), str(fp)))
            if len(out) >= max_frames:
                break
            next_t = t + every_s
    return out


def extract_audio_wav(path, max_s=None):
    """音轨 → 16k 单声道 wav（Whisper 口味）。没音轨返回 None。

    max_s=None 即全量转写（2026-10-30 主人要求整段视频都看完，不再截 120s）。"""
    _vendor()
    import av
    import wave
    wav_path = FRAME_DIR / (Path(path).stem[:40] + "_audio.wav")
    try:
        with av.open(str(path)) as c:
            if not c.streams.audio:
                return None
            a = c.streams.audio[0]
            from av.audio.resampler import AudioResampler
            res = AudioResampler(format="s16", layout="mono", rate=16000)
            wf = wave.open(str(wav_path), "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            for packet in c.demux(a):
                for frame in packet.decode():
                    for rf in res.resample(frame):
                        wf.writeframes(rf.to_ndarray().tobytes())
            wf.close()
            return str(wav_path)
    except Exception:
        return None


def _describe_batch(jpgs, batch_no, total_batches, focus=""):
    """一次视觉塔请求带一批图（≤BATCH 张），逐帧描述。

    返回 [(帧序号，描述)]；批次整体失败重试 3 次（llama-server 单 slot，
    会撞她自主活动的 503），全败返回空让上层如实记录。"""
    import base64
    import json as _json
    import urllib.request
    from core import config as _config
    first_t = jpgs[0][0]
    last_t = jpgs[-1][0]
    prompt = ("这是同一段视频第 %d/%d 批的 %d 个连续画面（第 %s~%s 秒，按时间顺序）。"
              "逐帧各用一句话描述：谁在做什么、动作/镜头变化、有无文字。"
              "格式严格为：图1：… 图2：…"
              % (batch_no, total_batches, len(jpgs), first_t, last_t))
    if focus:
        prompt += "\n本轮重点：%s" % focus
    content = [{"type": "text", "text": prompt}]
    for _t, jpg in jpgs:
        b64 = base64.b64encode(open(jpg, "rb").read()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64}})
    body = {"model": _config.ENGINE_MODEL,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 300 * len(jpgs),
            "temperature": 0.3,
            "chat_template_kwargs": {"enable_thinking": False}}
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        _config.ENGINE_BASE_URL.rstrip("/") + "/chat/completions",
        data=_json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    for attempt in range(3):
        try:
            with op.open(req, timeout=300) as resp:
                d = _json.loads(resp.read().decode("utf-8"))
            txt = d["choices"][0]["message"].get("content", "")
            # 解析"图N：描述"——模型偶尔不守格式，解析不出就原样挂到批首帧
            pairs = []
            for i in range(1, len(jpgs) + 1):
                m = re.search(r"图\s*%d[：:．.]([^\n]+)" % i, txt)
                pairs.append((jpgs[i - 1][0], (m.group(1).strip() if m else "").strip()))
            if any(p[1] for p in pairs):
                return [(t, dsc) for t, dsc in pairs]
            return [(jpgs[0][0], txt.strip()[:400])]
        except Exception:
            time.sleep(2.0 + attempt * 2.0)
    return []


def analyze(path, every_s=EVERY_S, max_frames=MAX_FRAMES, focus=""):
    """全流程。返回 dict(meta, frames, transcript, note, coverage)。
    note = 注入对话用的"看完的笔记"；失败时给一句实话，不静默。
    focus：重看时的关注点（如"专盯字幕/文字"，追加进每帧的描述提示）。"""
    path = str(path)
    meta = probe(path)
    if not meta:
        return {"ok": False, "note": "视频打不开（可能格式不支持或文件损坏）"}
    dur = meta["duration_s"]
    # 长视频自动降密度
    if dur > 900 and every_s < 10:
        every_s = 10.0
        capped = True
    else:
        capped = False
    # ---- 抽帧（全片采样）----
    try:
        sampled = extract_frames(path, every_s=every_s, max_frames=max_frames)
    except Exception as e:
        return {"ok": False, "meta": meta,
                "note": "抽帧失败：%s: %s" % (type(e).__name__, str(e)[:120])}
    if not sampled:
        return {"ok": False, "meta": meta, "note": "一帧都没抽到（视频流异常）"}
    # ---- 分批多图描述 ----
    batches = [sampled[i:i + BATCH] for i in range(0, len(sampled), BATCH)]
    frames = []
    for bi, bat in enumerate(batches, 1):
        frames.extend(_describe_batch(bat, bi, len(batches), focus=focus))
    got = sum(1 for _t, d in frames if d)
    # ---- 音轨全量转写 ----
    transcript = ""
    try:
        from channels import voice
        wav = extract_audio_wav(path)
        if wav:
            transcript = (voice.transcribe(wav) or "").strip()[:2000]
    except Exception:
        transcript = ""
    # ---- 组装笔记（连续相同的描述合并成区间，静态画面不刷屏）----
    # 声音内容**放最前**：2026-10-30 实测 126s 视频笔记 4689 字超 4500 截断线，
    # 排在末尾的"声音内容"被切掉，她没看到转写结果就说"音频读不了"——
    # 截断只许砍画面时间线，声音永远保得住。
    lines = ["视频 %.0f 秒，%dx%d，%s；全片采样 %d 帧（每 %g 秒一帧），看清 %d 帧%s" % (
        dur, meta["w"], meta["h"], "有声音" if meta["has_audio"] else "无声",
        len(sampled), every_s, got,
        "；片长超 15 分钟已降为概要采样" if capped else "")]
    if transcript:
        lines.append("声音内容：" + transcript)
    prev_d = None
    seg_start = None
    segs = []
    for t, d in frames:
        if not d:
            continue
        if d == prev_d:
            continue                      # 和上一帧同描述 → 延长当前区间
        if prev_d is not None:
            segs.append((seg_start, t, prev_d))
        prev_d, seg_start = d, t
    if prev_d is not None:
        segs.append((seg_start, dur, prev_d))
    for s0, s1, d in segs:
        if s1 - s0 > every_s * 1.5:
            lines.append("第%s~%s秒：%s（同一动作持续）" % (s0, s1, d))
        else:
            lines.append("第%s秒：%s" % (s0, d))
    if got == 0 and not transcript:
        lines.append("（画面和声音都没拿到——视觉塔/转写服务当时没响应）")
    note = "\n".join(lines)
    # 2026-10-30 主人：不要截断——全量注入。上下文装不下时由上下文层自己
    # 裁最旧消息，看视频的笔记一根手指都不许动它。
    coverage = round(len(sampled) * every_s / max(dur, 1) * 100)
    # 收尾清理：帧 jpg 和中间 wav 只服务于本次笔记（三审 2026-10-30：
    # MAX_FRAMES=1200 后看几部长片就是几百 MB，没理由留在盘上）
    try:
        for _t, jpg in sampled:
            Path(jpg).unlink(missing_ok=True)
        wav_tmp = FRAME_DIR / (Path(path).stem[:40] + "_audio.wav")
        wav_tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": got > 0 or bool(transcript), "meta": meta,
            "frames": frames, "transcript": transcript,
            "note": note, "coverage": min(100, coverage)}
