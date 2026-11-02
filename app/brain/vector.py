# -*- coding: utf-8 -*-
"""桌宠AI · 向量召回（vector.py）——三路召回的第三路，2026-10-27 主人拍板：路线 A。

**路线 A（默认，已落地）**：复用聊天引擎进程的 /v1/embeddings
（引擎启动参数加 `--embedding --pooling mean`，见 config.engine_args）。
- 零额外显存、零额外进程、零额外磁盘（实测：17.46GB 显存不变，单条 36~49ms）
- **错峰是硬纪律**：--parallel 1 下 embedding 与生成串行，生成中插入会让出字
  停顿 1.95s（实测）。故 encode() 在生成中会抛 GenerationBusy，由调用方
  （memory._vector_scores）退化为两路召回——不是报错，是让路。
- 真实执行路径：memory.recall() 发生在生成**之前**（core_block 无条件注入 +
  recall 在 answer_stream 之前），所以正常对话永远走的是"空闲时打"，
  错峰与对话节奏天然不冲突（主人拍板理由 1）。

**路线 B（保底）**：transformers 直载 bge-m3（CPU）。仅当引擎不支持 embedding
（老版本 llama-server / 明确设 PET_EMBED_ROUTE=B）时自动兜底，惰性 import torch，
不用 sentence-transformers（ST 5.7/6.2 对纯文本模型库 AutoProcessor 硬失败，实测）。

性格安全（主人拍板理由 2）：性格锚 = 核心记忆集（≤50 条，无条件进 system prompt，
**不走向量**）+ 人格文件。embedding 只影响"临时召回哪几条旧事"，碰不到性格锚。

分层：L3 只读 L1（llm.is_generating）与 L0（config），不 import pipeline/router。
"""
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

from core import config

# 回环无代理 opener：同 llm.py——HTTP_PROXY 注入时 urllib 会把 127.0.0.1 也交给
# 代理 → 502/空响应（B26 实测根因）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_MODEL_DIR_NAME = "bge-m3"

# 探测缓存：引擎 embedding 能力不会频繁变，负结果缓存 60s 免得每次召回都打一次
_probe = {"ok": None, "at": 0.0, "err": ""}
_NEG_TTL_S = 60

_lock = threading.Lock()
_stats = {"calls": 0, "blocked": 0, "fallback_b": 0, "last_ms": 0, "last_err": ""}


class GenerationBusy(RuntimeError):
    """错峰拦截：她正在说话，embedding 不给插队（调用方应退化为两路召回）。"""


# ---------------------------------------------------------------- 可用性
def _probe_engine_embedding():
    """探一次引擎的 /v1/embeddings：能返回向量就 True。缓存负结果 60s。"""
    now = time.time()
    if _probe["ok"] is not None and (now - _probe["at"] < _NEG_TTL_S or _probe["ok"]):
        return _probe["ok"]
    url = config.ENGINE_BASE_URL.rstrip("/") + "/embeddings"
    body = json.dumps({"model": config.ENGINE_MODEL, "input": "探"}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with _OPENER.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vecs = data.get("data") or []
        ok = bool(vecs and (vecs[0].get("embedding") or []))
        _probe.update(ok=ok, at=now, err="" if ok else "引擎未返回向量")
        return ok
    except Exception as e:                                   # noqa: BLE001 探测不问原因
        _probe.update(ok=False, at=now, err=f"{type(e).__name__}: {e}"[:160])
        return False


def _model_dir_exists():
    return (config.ROOT / "models" / _MODEL_DIR_NAME).exists()


def available():
    """第三路是否可用。

    路线 A（默认）：引擎活着 + /v1/embeddings 能出向量。
    路线 B：模型目录存在（惰性加载，首调才 import torch）。
    OFF：恒 False，三路自动退化为两路。
    """
    if config.EMBED_ROUTE == "OFF":
        return False
    if config.EMBED_ROUTE == "B":
        return _model_dir_exists()
    if _probe_engine_embedding():
        return True
    # A 不可用 → B 兜底（引擎老版本没编 embedding 时，主人的保底条款）
    return _model_dir_exists()


def route():
    """当前实际生效的路线（A/B/OFF/不可用），日志与 doctor 用。"""
    if config.EMBED_ROUTE == "OFF":
        return "OFF"
    if config.EMBED_ROUTE == "B":
        return "B" if _model_dir_exists() else "不可用(B 模型缺失)"
    if _probe_engine_embedding():
        return "A"
    return "B(兜底)" if _model_dir_exists() else "不可用(引擎无 embedding 且 B 缺失)"


def stats():
    """错峰与调用统计（给 doctor/面板看，证明纪律真的在跑）。"""
    with _lock:
        return dict(_stats, route=route())


# ---------------------------------------------------------------- 路线 A
def _encode_a(texts):
    """POST /v1/embeddings（批量，一次请求多条，省往返）。"""
    url = config.ENGINE_BASE_URL.rstrip("/") + "/embeddings"
    body = json.dumps({"model": config.ENGINE_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with _OPENER.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        tail = ""
        try:
            tail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {tail}") from None
    rows = sorted((data.get("data") or []), key=lambda d: d.get("index", 0))
    if len(rows) != len(texts):
        raise RuntimeError(f"向量数量不符：要 {len(texts)} 得 {len(rows)}")
    return [r.get("embedding") or [] for r in rows]


# ---------------------------------------------------------------- 路线 B（保底，惰性）
_b = {"model": None, "tok": None}


def _load_b():
    """直载 bge-m3（绕开 sentence-transformers 的 AutoProcessor 硬失败）。"""
    if _b["model"] is not None:
        return _b["model"], _b["tok"]
    import torch                                            # noqa: PLC0415 惰性：只有真走 B 才付 6s
    from transformers import AutoModel, AutoTokenizer        # noqa: PLC0415
    d = str(config.ROOT / "models" / _MODEL_DIR_NAME)
    tok = AutoTokenizer.from_pretrained(d)
    model = AutoModel.from_pretrained(d)
    model.eval()
    _b["model"], _b["tok"] = model, tok
    del torch
    return model, tok


def _encode_b(texts):
    import torch                                            # noqa: PLC0415
    model, tok = _load_b()
    with torch.no_grad():
        enc = tok(texts, padding=True, truncation=True, max_length=512,
                  return_tensors="pt")
        out = model(**enc)
        # mean pooling（带 attention mask，和官方 bge-m3 用法一致）
        mask = enc["attention_mask"].unsqueeze(-1).float()
        emb = (out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        import torch.nn.functional as F                     # noqa: PLC0415
        emb = F.normalize(emb, p=2, dim=1)
    return emb.tolist()


# ---------------------------------------------------------------- 对外接口
def encode(texts, force=False):
    """文本 → 向量列表。

    错峰纪律（硬）：路线 A 下若此刻正在生成，抛 GenerationBusy（调用方退化为
    两路召回）。force=True 可强制（仅供对比测试/离线巩固用，生产别开）。
    """
    if isinstance(texts, str):
        texts = [texts]
    texts = [t if isinstance(t, str) else str(t) for t in texts]
    if not texts:
        return []
    if not available():
        raise RuntimeError("向量召回不可用：" + route())

    from core import llm                                    # noqa: PLC0415 L1，向下依赖合法
    if (not force) and config.EMBED_STRICT and llm.is_generating() \
            and config.EMBED_ROUTE != "B":
        with _lock:
            _stats["blocked"] += 1
        raise GenerationBusy("生成中，embedding 让路（错峰纪律）")

    t0 = time.time()
    try:
        if config.EMBED_ROUTE == "B" or not _probe_engine_embedding():
            vecs = _encode_b(texts)
            with _lock:
                _stats["fallback_b"] += 1
        else:
            vecs = _encode_a(texts)
    except Exception as e:                                  # noqa: BLE001
        with _lock:
            _stats["last_err"] = f"{type(e).__name__}: {e}"[:160]
        raise
    with _lock:
        _stats["calls"] += 1
        _stats["last_ms"] = int((time.time() - t0) * 1000)
    return vecs


def best_match(query, candidates, top_k=3):
    """query 与候选文本的相似度排序，返回 [(index, score)]（分高的在前）。

    未接入/被错峰拦截时返回 []——memory._vector_scores 按空结果处理（两路召回）。
    """
    if not candidates or not available():
        return []
    if not query or not str(query).strip():
        return []
    try:
        vecs = encode([query] + list(candidates))
    except GenerationBusy:
        return []                                   # 让路：静默退化，不打断对话
    except Exception:
        return []                                   # 召回是增强项，失败绝不能拖垮回答
    if len(vecs) != len(candidates) + 1 or not vecs[0]:
        return []
    q = vecs[0]
    scored = [(i, cosine(q, v)) for i, v in enumerate(vecs[1:]) if v]
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def cosine(a, b):
    """余弦相似度（纯 Python 版；向量量大时换 numpy）。

    外部审核 P2-6 防御（2026-10-27）：维度不一致直接返回 0.0 并计数留痕——
    zip 会静默截断，混维（A/B 路线切换、换 embedding 模型）算出来的分数
    毫无意义还不报错。当前架构**没有持久化向量表**（每次查询现算、用完
    即焚），混表风险天然不存在；此守卫保的是未来若加表不会静默降级。
    """
    if len(a) != len(b):
        with _lock:
            _stats["dim_mismatch"] = _stats.get("dim_mismatch", 0) + 1
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def is_chinese_ready(text):
    """粗判文本是否有检索价值（用于跳过无意义 encode，省算力）。"""
    return bool(re.search(r"[\u4e00-\u9fff a-zA-Z0-9]", text or ""))


# ---------------------------------------------------------------- 自测入口
if __name__ == "__main__":
    print("=== 桌宠AI 向量召回自测 ===")
    print("路线:", route(), "| 可用:", available())
    if available():
        pairs = [("今天天气不错", "天气挺好的"), ("今天天气不错", "显卡驱动怎么装")]
        try:
            v = encode([p[0] for p in pairs] + [p[1] for p in pairs])
            print("向量维度:", len(v[0]))
            print("同义相似度: %.3f" % cosine(v[0], v[2]))
            print("无关相似度: %.3f" % cosine(v[1], v[3]))
        except Exception as e:                              # noqa: BLE001
            print("编码失败:", type(e).__name__, e)
    print("统计:", stats())
