# -*- coding: utf-8 -*-
"""桌宠AI · L5 技能建档（skillbook.py）

对照 本地线-实施方案.md §L5：
- data/skills/{技能名}/：skill.md（本体）+ sop/ + knowledge/ + corpus/ + archive/
- 注册表：data/skills/index.json（触发词/加载文件/配额/状态）。
  **刻意偏差**：方案写 index.yaml，这里用 JSON——零新依赖（PyYAML），
  结构同 yaml 语义，等技能多了需要人工批量编辑时再换，迁移只动本文件。
- 按任务加载 ≤配额 token（默认 1500），**平时 0 占用**：每次对话现场
  读取，不常驻内存、不进 system prompt 模板。
- 状态机（成长闭环）：draft（写）→ published（人工闸门，POST
  /api/skills/{name}/publish）→ metrics（hit_count/feedback 自动记）
  → retro.md（复盘点，人工写，skillbook 只负责找到它）。
- draft 状态**不可被触发加载**——主人没点头的技能，桌宠不许拿去用。

分层：本模块只管"存与取"，不碰 llm/pipeline（生成侧要技能素材时调用
load_for(query) 拿文本块自己注入）。
"""
import json
import re
import time
from pathlib import Path

from core import config

SKILLS_DIR = Path(config.DATA_DIR) / "skills"
INDEX_FILE = SKILLS_DIR / "index.json"
DEFAULT_QUOTA_TOKENS = 1500
_CHARS_PER_TOKEN = 1.6

_LOCK = None            # 惰性建（桌面壳进程也可能 import）
import threading        # noqa: E402


def _lock():
    global _LOCK
    if _LOCK is None:
        _LOCK = threading.RLock()
    return _LOCK


# ---------------------------------------------------------------- 注册表
def _load_index():
    if not INDEX_FILE.exists():
        return {"version": 1, "skills": {}}
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "skills": {}}      # 坏档不崩，按空表跑


def _save_index(idx):
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def _entry(idx, name):
    return idx["skills"].setdefault(name, {
        "title": name,
        "status": "draft",             # draft / published / retired
        "triggers": [],                # 命中即加载（子串，大小写不敏感）
        "files": ["skill.md"],         # 相对技能目录，按序拼接
        "quota_tokens": DEFAULT_QUOTA_TOKENS,
        "hit_count": 0,
        "feedback": [],                # {"ts": ..., "score": 1/-1, "note": ...}
        "created_at": time.time(),
    })


# ---------------------------------------------------------------- 建档
def create(name, title=None, triggers=None, files=None, quota=None,
           skill_md=""):
    """建一个技能（含目录骨架）。已存在则只更新元数据不覆盖内容。"""
    name = _safe_name(name)
    d = SKILLS_DIR / name
    for sub in ("sop", "knowledge", "corpus", "archive"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    if not (d / "skill.md").exists():
        (d / "skill.md").write_text(skill_md or
                                    f"# {title or name}\n\n（待撰写）\n",
                                    encoding="utf-8")
    with _lock():
        idx = _load_index()
        e = _entry(idx, name)
        e.update({"title": title or e["title"],
                  "triggers": triggers or e["triggers"],
                  "files": files or e["files"],
                  "quota_tokens": int(quota or e["quota_tokens"])})
        _save_index(idx)
    return d


def publish(name, on=True):
    """人工闸门：只有主人点头（publish）后技能才可被触发加载。"""
    with _lock():
        idx = _load_index()
        e = _entry(idx, _safe_name(name))
        e["status"] = "published" if on else "draft"
        _save_index(idx)
        return e["status"]


def retire(name):
    """退役：不删档（archive 目录留素材），注册表标 retired 不可加载。"""
    with _lock():
        idx = _load_index()
        e = _entry(idx, _safe_name(name))
        e["status"] = "retired"
        _save_index(idx)


def feedback(name, score, note=""):
    """成长闭环 metrics：+1/-1 与备注，进 retro 时的数据底子。"""
    with _lock():
        idx = _load_index()
        e = _entry(idx, _safe_name(name))
        e["feedback"].append({"ts": time.time(), "score": int(score), "note": note[:200]})
        e["feedback"] = e["feedback"][-100:]
        _save_index(idx)


def _safe_name(name):
    n = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", (name or "").strip())
    if not n or n.startswith("."):
        raise ValueError(f"非法技能名: {name!r}")
    return n


# ---------------------------------------------------------------- 按任务加载
def match(query):
    """触发词匹配（published 才参与）。返回命中技能名列表（0~n 个）。"""
    q = (query or "").lower()
    with _lock():
        idx = _load_index()
        hits = [n for n, e in idx["skills"].items()
                if e["status"] == "published"
                and any(t.lower() in q for t in e["triggers"] if t)]
    return hits


def load_for(query):
    """按任务加载：命中的全部已发布技能，各按配额截断后拼接。

    返回 (block, used_tokens, matched_names)。没命中返回 ("", 0, [])——
    平时 0 占用就是这么来的：不命中就一个字都不读。
    """
    names = match(query)
    if not names:
        return "", 0, []
    with _lock():
        idx = _load_index()
        out, used = [], 0
        for n in names:
            e = idx["skills"][n]
            quota = int(e.get("quota_tokens", DEFAULT_QUOTA_TOKENS))
            parts, took = [], 0
            for rel in e["files"]:
                f = SKILLS_DIR / n / rel
                if not f.exists():
                    continue
                txt = f.read_text(encoding="utf-8", errors="replace").strip()
                budget = quota - took
                if budget <= 0:
                    break
                t = int(len(txt) / _CHARS_PER_TOKEN)
                if t > budget:                      # 超配额：头部截断
                    txt = txt[:int(budget * _CHARS_PER_TOKEN)] + "\n…（超配额截断）"
                    t = budget
                parts.append(f"### {rel}\n{txt}")
                took += t
            if parts:
                out.append(f"【技能·{e.get('title', n)}】\n" + "\n".join(parts))
                used += took
                e["hit_count"] = e.get("hit_count", 0) + 1
        _save_index(idx)
    return ("\n\n".join(out), used, names) if out else ("", 0, [])


def retro_path(name):
    """成长闭环的复盘点（retro.md 由人工/后续自动化写，这里只给路径）。"""
    return SKILLS_DIR / _safe_name(name) / "archive" / "retro.md"


def stats():
    with _lock():
        idx = _load_index()
        by = {"draft": 0, "published": 0, "retired": 0}
        for e in idx["skills"].values():
            by[e["status"]] = by.get(e["status"], 0) + 1
        return {"total": len(idx["skills"]), **by, "dir": str(SKILLS_DIR)}


# ---------------------------------------------------------------- 模板
def ensure_template():
    """首次运行给一个 _template 骨架（draft 状态，永不触发——名字带下划线
    前缀且无触发词），主人照着填第一个技能。"""
    create("_template", title="技能模板（照这个填）", triggers=[],
           skill_md="# 技能名\n\n## 这个技能是干什么的\n\n## 什么时候用"
                    "\n（触发词写进注册表 triggers）\n\n## 怎么做（步骤）\n"
                    "1. \n2. \n\n## 红线\n- ")
