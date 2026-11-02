# -*- coding: utf-8 -*-
r"""桌宠AI · 语音转写（app/channels/voice.py）——本地音频 → 文字。

链路：上层拿到本地音频文件 → 本机 Whisper large-v3 转写 → 当普通文本消息
走现有链路（审计留痕）。只解决"听"，不做 TTS。

技术选型：
- faster-whisper（CTranslate2）+ 本地模型（默认放项目 vendor/whisper-models/large-v3，可经 PET_VOICE_MODEL 改）
  （现成 3GB 权重，零下载）。依赖装在 vendor/pylibs（--target，不碰 C 盘
  site-packages），本模块 sys.path 注入。
- 音频解码走 PyAV（wheel 自带 FFmpeg 库），系统无需装 ffmpeg。
- 设备：优先 CUDA float16（借 torch 自带 cuDNN DLL），失败退 CPU int8——
  短语音（≤60s）CPU int8 也在秒级~十几秒，兜底可用。
- 模型进程内常驻懒加载：首条语音多等一次加载（几十秒），之后秒级。加载失败
  不抛出——返回 None 由上层兜底回复"没听清"，消息不能静默消失（主人红线）。

线程模型：转写串行锁（模型推理不能并发打爆显存/内存）；由调用方决定线程池
线程里会阻塞长轮询——由调用方（webapp）决定放不放线程池。
"""
import os
import sys
import threading
import time
from pathlib import Path

from core import config

_VENDOR = Path(getattr(config, "VOICE_VENDOR_DIR",
                          str(config.ROOT / "vendor" / "pylibs")))
_MODEL_DIR = Path(getattr(config, "VOICE_MODEL_DIR",
                          str(config.ROOT / "vendor" / "whisper-models" / "large-v3")))

_model = None
_model_device = ""
_model_lock = threading.Lock()      # 懒加载 + 推理共用：串行最稳
_infer_lock = threading.Lock()


def _vendor_ready():
    return _VENDOR.is_dir() and (_VENDOR / "faster_whisper").is_dir()


def _load_model():
    """懒加载 faster-whisper 模型。成功返回 (model, device)，失败返回 (None, "")。"""
    global _model, _model_device
    if _model is not None:
        return _model, _model_device
    if not _vendor_ready():
        return None, ""
    if not _MODEL_DIR.joinpath("model.bin").exists():
        return None, ""
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))
    from faster_whisper import WhisperModel      # noqa: E402（vendor 注入后）

    # 借 torch 自带的 cuDNN/cuBLAS DLL（torch 站点目录加进 DLL 搜索路径）
    try:
        import torch                             # noqa: F401（只为 DLL 路径）
        torch_lib = Path(torch.__file__).parent / "lib"
        if torch_lib.is_dir():
            os.add_dll_directory(str(torch_lib))
    except Exception:
        pass

    for device, ct, tag in (("cuda", "float16", "cuda-fp16"),
                            ("cpu", "int8", "cpu-int8")):
        try:
            t0 = time.time()
            m = WhisperModel(str(_MODEL_DIR), device=device,
                             compute_type=ct)
            _model, _model_device = m, tag
            return m, tag            # 调用方记日志，这里保持安静
        except Exception:
            continue
    return None, ""


def transcribe(audio_path, language="zh"):
    """转写一个音频文件 → str。失败返回 ""（上层兜底"没听清"，不静默）。

    language 固定 zh：主人语音只会是中文；initial_prompt 用简体提示抑制
    繁体输出（whisper 中文常见坑）。
    """
    global _model, _model_device
    p = Path(audio_path)
    if not p.exists() or p.stat().st_size < 512:
        return ""
    with _infer_lock:
        m, tag = _load_model()
        if m is None:
            return ""
        try:
            t0 = time.time()
            segments, info = m.transcribe(str(p), language=language,
                                          beam_size=2, vad_filter=True,
                                          vad_parameters={"min_silence_duration_ms": 500},
                                          initial_prompt="以下是普通话简体中文对话。")
            text = "".join(seg.text for seg in segments).strip()
            print(f"[voice] 转写完成 device={tag} 耗时={time.time()-t0:.1f}s "
                  f"字数={len(text)}", flush=True)
            return text
        except Exception as e:
            print(f"[voice] 转写失败：{type(e).__name__}: {str(e)[:120]}", flush=True)
            return ""


def is_ready():
    """给管理端/审计用：模型是否已加载、用什么设备。"""
    return _model is not None, _model_device
