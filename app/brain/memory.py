# -*- coding: utf-8 -*-
"""桌宠AI · L2/L3 记忆系统（memory.py）

借鉴 OpenViking dsh-memory-plugin 的四个模式，但**独立实现**：
本机 SQLite 单文件，无外部服务、无 outbox 重放（单机单用户不需要——
OpenViking 那套离线韧性是给网络服务准备的，直接砍掉是刻意的简化）。

四个借鉴来的模式 → 这里的落地：
  自动召回(pre-step 检索注入)  → recall(query, budget_tokens) 返回带来源标注的上下文块
  会话捕获(pending_tokens 阈值提交) → capture() 攒会话，commit() 按阈值落盘
  语义去重合并                 → remember() 里做相似归并（阶段1字符 n-gram，
                                  向量路留接口，等接入 bge-m3 后启用）
  schema 版本迁移链(Harness 五级迁移) → SCHEMA_VERSION + _MIGRATIONS 逐级升级

L3 升级（对照 本地线-实施方案.md §L3）：
- 三路召回融合：SQL 直查 + FTS5(trigram, BM25) + 向量路（bge-m3 留接口，
  未接入时自动退化为两路，接口签名不变）→ RRF k=60 融合排序
- 核心记忆集 core_block()：≤50 条无条件注入 system prompt（占独立预算，
  **永不因检索被砍**——红线）。promote_state 标记 + confidence×hit_count 排序
- 每日巩固 consolidate(extract_fn)：04:00 把已捕获会话提炼成事实（extract_fn
  由调用方注入——记忆模块不 import llm，避免分层反向依赖；webapp 侧接引擎，
  引擎没跑就跳过等明天，不阻塞）
- 失效而非覆盖：expire() 下架不删除（可审计），valid_until 支持带过期期的事实

设计约束：
- 单文件库 data/memory/pet.db，一切读写走本模块，别的模块不直接摸 SQL；
- 连接管理：**全模块共享一个连接 + RLock（单写者模式）**。B26 实测"每次新建
  连接"在 uvicorn 线程里偶发 sqlite readonly（Windows WAL 的 -shm 竞争）。
"""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from core import config

DB_PATH = Path(config.DATA_DIR) / "memory" / "pet.db"
SCHEMA_VERSION = 2

_MIGRATIONS = {
    2: [
        # L3：带过期期的事实 + 核心记忆集标记 + FTS5 全文索引
        "ALTER TABLE facts ADD COLUMN valid_until REAL",
        "ALTER TABLE facts ADD COLUMN promote_state INTEGER DEFAULT 0",
        "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
        "  content, kind, content='facts', content_rowid='id', tokenize='trigram')",
        "INSERT INTO facts_fts(rowid, content, kind) "
        "  SELECT id, content, kind FROM facts",
    ],
}

_LOCK = threading.RLock()
_CONN = None
_FTS_OK = True          # trigram 不可用（老 sqlite）时自动降级为纯 SQL 路


def _connect():
    """返回共享连接（首次时建库/迁移）。调用方必须处于 _LOCK 内。"""
    global _CONN, _FTS_OK
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # trigram 分词需要 sqlite ≥ 3.34；不支持就整体退回 SQL 直查路
        _FTS_OK = tuple(int(x) for x in sqlite3.sqlite_version.split(".")) >= (3, 34, 0)
        _ensure_schema(conn)
        _CONN = conn
    return _CONN


def _ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS facts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      content   TEXT NOT NULL,
      kind      TEXT NOT NULL DEFAULT 'event',   -- fact/event/preference/emotion
      polarity  INTEGER DEFAULT 0,               -- 正面 1 / 负面 -1 / 中性 0
      source    TEXT DEFAULT 'user',             -- user/pet/inferred
      confidence REAL DEFAULT 1.0,
      hit_count INTEGER DEFAULT 0,               -- 被召回次数（越被用越重要）
      status    TEXT DEFAULT 'active',           -- active/expired（失效而非删除）
      occurred_at TEXT,                          -- 事实发生时间（用户口径）
      valid_until REAL,                          -- 带过期期的事实（L3）
      promote_state INTEGER DEFAULT 0,           -- 1=核心记忆集（无条件注入）
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status, kind);
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      role     TEXT NOT NULL,                    -- user/pet
      content  TEXT NOT NULL,
      ts       REAL NOT NULL,
      captured INTEGER DEFAULT 0                -- 是否已被提交进长期记忆
    );
    """)
    if _FTS_OK:
        conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
          content, kind, content='facts', content_rowid='id', tokenize='trigram');
        CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
          INSERT INTO facts_fts(rowid, content, kind)
            VALUES (new.id, new.content, new.kind);
        END;
        CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
          INSERT INTO facts_fts(facts_fts, rowid, content, kind)
            VALUES ('delete', old.id, old.content, old.kind);
          INSERT INTO facts_fts(rowid, content, kind)
            VALUES (new.id, new.content, new.kind);
        END;
        """)
    v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if v is None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    else:
        cur = int(v["value"])
        while cur < SCHEMA_VERSION:              # 逐级迁移链（学 Harness）
            step = _MIGRATIONS.get(cur + 1)
            if step:
                for sql in step:
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError:
                        pass                     # 列已存在（幂等重跑）
            cur += 1
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(cur),))
    conn.commit()


_PUNCT = re.compile(r"[\s，。！？、；：\"'（）()\[\]{}]")


def _norm(s):
    """去标点空白后的比较键——语义去重的粗判（向量路接入前的兜底）。"""
    return _PUNCT.sub("", (s or "").lower())


_NEG_WORDS = ("不", "没", "未", "别", "无", "非", "否")


def _num_set(s):
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


def _near_dup(a, b):
    """近似重复判定（同义改写兜底，2026-10-31）：2-gram Jaccard ≥0.7，
    且数字集合相同、否定词有无一致——防止"喜欢/不喜欢""主人在深圳/上海"
    被误并成一条。长度差异 >20% 也不判重。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    la, lb = len(na), len(nb)
    if min(la, lb) / max(la, lb) < 0.8:
        return False
    if _num_set(a) != _num_set(b):
        return False
    if any(w in na for w in _NEG_WORDS) != any(w in nb for w in _NEG_WORDS):
        return False
    ba = {na[i:i + 2] for i in range(la - 1)} if la > 1 else {na}
    bb = {nb[i:i + 2] for i in range(lb - 1)} if lb > 1 else {nb}
    if not ba or not bb:
        return False
    return len(ba & bb) / len(ba | bb) >= 0.7


def remember(content, kind="event", polarity=0, source="user",
             occurred_at=None, valid_until=None, confidence=1.0, dedupe=True):
    """写一条长期记忆。语义去重：内容几乎相同 → 合并（confidence 提升而非重复入库）。

    返回 (fact_id, merged)。merged=True 表示命中已有条目走了合并。
    """
    if not (content or "").strip():
        return None, False
    now = time.time()
    key = _norm(content)[:120]
    with _LOCK:
        conn = _connect()
        try:
            if dedupe and key:
                rows = conn.execute(
                    "SELECT id, confidence, content, polarity FROM facts WHERE status='active'"
                ).fetchall()
                for r in rows:
                    # 极性不同（喜欢/不喜欢）绝不合并；同义改写走 _near_dup
                    if int(r["polarity"] or 0) != int(polarity or 0):
                        continue
                    if (_norm(r["content"])[:120] == key
                            or _near_dup(content, r["content"])):
                        conn.execute(
                            "UPDATE facts SET confidence=MIN(1.0, confidence+0.1),"
                            " updated_at=? WHERE id=?", (now, r["id"]))
                        conn.commit()
                        return r["id"], True
            cur = conn.execute(
                "INSERT INTO facts(content, kind, polarity, source, confidence,"
                " occurred_at, valid_until, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (content.strip(), kind, polarity, source, confidence,
                 occurred_at, valid_until, now, now))
            conn.commit()
            return cur.lastrowid, False
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------- 会话捕获
def capture(role, content):
    """会话捕获：把一轮对话原样记进 session 缓冲（未提交状态）。"""
    with _LOCK:
        conn = _connect()
        conn.execute("INSERT INTO sessions(role, content, ts) VALUES (?,?,?)",
                     (role, content, time.time()))
        conn.commit()


def _pending_tokens_raw(conn, chars_per_token=1.6):
    """commit 内部用：连接已在锁内，直接复用（避免重入）。"""
    n = conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM sessions "
                     "WHERE captured=0").fetchone()["n"]
    return int(n / chars_per_token)


def pending_tokens(chars_per_token=1.6):
    """未提交会话的体量估算（中文约 1.6 字符/token）。"""
    with _LOCK:
        conn = _connect()
        return _pending_tokens_raw(conn, chars_per_token)


def today_dialog(limit=80, max_chars=6000):
    """今天（本地日界起）的对话原文——给她复盘用（autonomy.review）。

    只读 sessions 表；归档/提交过的行也在（captured 只标记不删），所以
    早上整理过记忆也不影响她晚上回看今天的聊天。
    """
    lt = time.localtime()
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT role, content, ts FROM sessions WHERE ts>=? "
            "ORDER BY ts DESC LIMIT ?", (start, int(limit))).fetchall()
    lines = [("主人" if r["role"] == "user" else "桌宠") + "：" + (r["content"] or "")
             for r in reversed(rows)]
    return "\n".join(lines)[-max_chars:]


def commit(threshold_tokens=20000, keep_recent=10):
    """阈值提交：攒够就把旧会话标记为已捕获（保留最近 keep_recent 条活跃）。
    事实**提取**由 consolidate(extract_fn) 做（需要模型，由 webapp 注入）。"""
    with _LOCK:
        conn = _connect()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE captured=0").fetchone()["n"]
        if total <= keep_recent:
            return {"committed": 0, "pending": total, "hit_threshold": False}
        if _pending_tokens_raw(conn) < threshold_tokens:
            return {"committed": 0, "pending": total, "hit_threshold": False}
        cutoff = conn.execute(
            "SELECT ts FROM sessions WHERE captured=0 ORDER BY ts DESC LIMIT 1 OFFSET ?",
            (keep_recent - 1,)).fetchone()
        if cutoff is None:
            return {"committed": 0, "pending": total, "hit_threshold": True}
        cur = conn.execute("UPDATE sessions SET captured=1 WHERE ts <= ? AND captured=0",
                           (cutoff["ts"],))
        conn.commit()
        return {"committed": cur.rowcount, "pending": total - cur.rowcount,
                "hit_threshold": True}


# ---------------------------------------------------------------- 三路召回
def _sql_scores(conn, keys, kinds):
    """路1 SQL 直查：关键词命中 × 置信度 × 被用次数。返回 [(row, rank)]。"""
    rows = conn.execute(
        "SELECT id, content, kind, polarity, source, confidence, hit_count,"
        " occurred_at, created_at, promote_state, valid_until FROM facts WHERE status='active' "
        "ORDER BY confidence DESC, updated_at DESC LIMIT 400").fetchall()
    scored = []
    for r in rows:
        if kinds and r["kind"] not in kinds:
            continue
        hay = _norm(r["content"])
        hit = sum(1 for k in keys if k and k in hay)
        score = hit * 2 + r["confidence"] + min(r["hit_count"], 5) * 0.2
        scored.append((score, r))
    scored.sort(key=lambda t: -t[0])
    return [r for _s, r in scored]


def _fts_tokens(query):
    """查询切词（千问审核 2026-10-28 加固）：旧版把整句当一个引号短语查，
    _norm 又先抹掉全部空白——中文长句要求"连续子串精确命中"基本必空转，
    三路召回退化两路。切法复用 searcher._kw_words 的 2-gram 滑窗逻辑
    （短中文块≤4 字整块、长块 2 字滑窗、排口语虚词；零依赖不引 jieba），
    英文/数字块取整词。返回 (toks2, toks3)：2 字词与 3 字以上词分开——
    trigram 分词器 MATCH 匹配不了 2 字 token，2 字词要走 LIKE 路。"""
    words = []
    try:
        from brain.searcher import Searcher                 # 函数内导入，避免加载耦合
        words = [w for w in Searcher._kw_words(query or "") if len(w) >= 2]
    except Exception:                                 # 兜底：自行 2 字滑窗（同规则）
        for b in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
            if len(b) <= 4:
                words.append(b)
            else:
                words.extend(b[i:i + 2] for i in range(len(b) - 1))
    words += re.findall(r"[A-Za-z0-9]{2,}", query or "")
    seen, out = set(), []
    for w in words:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    toks2 = [w for w in out if len(w) == 2][:12]
    toks3 = [w for w in out if len(w) >= 3][:20]
    return toks2, toks3


def _fts_scores(conn, query, kinds):
    """路2 FTS5(trigram) BM25：真全文检索，中文子串友好。返回 [(row, rank)]。

    两层查询（千问审核 2026-10-28 加固）：
    ① toks3 走 MATCH（OR 组合 3 字词，中文长块另碎切 3 字滑窗）吃 BM25 排序；
    ② toks2（2 字词，如'喜欢'）trigram MATCH 不了 → LIKE '%词%' 补召回
       （trigram 表对 LIKE 有索引优化，兜底全扫也在几百行量级），按 2 字词
       命中数排序。两层合并去重——非连续字词的中文长句召回由此恢复。"""
    if not _FTS_OK:
        return []
    toks2, toks3 = _fts_tokens(query)
    for b in re.findall(r"[\u4e00-\u9fff]{3,}", query or ""):   # 长中文块碎 3 字滑窗
        for i in range(len(b) - 2):
            g = b[i:i + 3]
            if g not in toks3:
                toks3.append(g)
    toks3 = toks3[:24]
    rows, seen = [], set()
    if toks3:
        q = " OR ".join(f'"{w}"' for w in toks3)
        try:
            for r in conn.execute(
                    "SELECT f.* FROM facts_fts ft JOIN facts f ON f.id = ft.rowid "
                    "WHERE facts_fts MATCH ? AND f.status='active' "
                    "ORDER BY bm25(facts_fts) LIMIT 30", (q,)).fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"])
                    rows.append(r)
        except sqlite3.OperationalError:
            pass                                          # MATCH 串异常就走 LIKE 路
    if toks2:
        where = " OR ".join("ft.content LIKE ?" for _ in toks2)
        params = [f"%{w}%" for w in toks2]
        try:
            like_rows = conn.execute(
                f"SELECT f.* FROM facts_fts ft JOIN facts f ON f.id = ft.rowid "
                f"WHERE f.status='active' AND ({where}) LIMIT 60", params).fetchall()
        except sqlite3.OperationalError:
            like_rows = []
        like_rows = [r for r in like_rows if r["id"] not in seen]
        like_rows.sort(key=lambda r: -sum(w in (r["content"] or "") for w in toks2))
        rows.extend(like_rows[:30])
    if not rows and not toks2 and not toks3:              # 全空兜底：旧整句短语
        try:
            rows = conn.execute(
                "SELECT f.* FROM facts_fts ft JOIN facts f ON f.id = ft.rowid "
                "WHERE facts_fts MATCH ? AND f.status='active' "
                "ORDER BY bm25(facts_fts) LIMIT 30",
                (f'"{_norm(query)[:20]}"',)).fetchall()
        except sqlite3.OperationalError:
            return []
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    return rows


def _vector_scores(query, rows):
    """路3 向量召回（vector.py 契约）：未接入时返回空，RRF 自动退化为两路。
    bge-m3 接入后（vector.available() 变 True）这里自动变三腿——
    调用方无感。1.2GB 常驻，接入前先过显存预算（vram.fit），红线不让它
    把聊天引擎挤出卡。

    2026-10-29 提速（主人："和我交流还是太慢"）：向量路要打一次引擎
    /v1/embeddings，正常 0.2s、引擎被生成占着时实测 2.3s——而它在
    **开口之前**，等于每次聊天都先罚站。两刀：
    ① 短查询（<10 字，日常闲聊主力）直接跳过——SQL+全文足够，语义召回
       对"今天有点累"这种口语没有增益；
    ② 长查询限时 VECTOR_RECALL_TIMEOUT_S（默认 1.2s），超时按空处理——
       记忆是增强项，宁可少召回一条，不能让她半天不开口。
    """
    if not rows:
        return []
    if len((query or "").strip()) < 10:
        return []                       # ① 日常短句：不走向量路
    from brain import vector
    if not vector.available():
        return []
    import threading
    box = {}

    def _job():
        try:
            box["r"] = vector.best_match(query, [r["content"] for r in rows],
                                         top_k=min(30, len(rows)))
        except Exception as e:          # noqa: BLE001
            box["r"] = e

    t = threading.Thread(target=_job, daemon=True)
    t.start()
    t.join(getattr(config, "VECTOR_RECALL_TIMEOUT_S", 1.2))
    if t.is_alive():
        return []                       # ② 超时：本轮放弃向量路，不阻塞开口
    res = box.get("r")
    if isinstance(res, Exception) or res is None:
        return []                       # 召回是增强项：任何异常都不能拖垮回答
    return [rows[i] for i, _s in res]


# 来源权重（2026-11-01 主人定调·最小改动）：只抬"主人的原话"（source=user），
# 其余来源保持 1.0。目的：让"主人亲口说的"在注入排序里压过推断/自述/学习笔记，
# 但不过滤任何条目（别的先不改，先看效果）。
_SOURCE_BOOST = {"user": 2.0}


def _src_boost(source):
    return _SOURCE_BOOST.get((source or "").strip(), 1.0)


def _rrf_fuse(*ranked_lists, k=60):
    """Reciprocal Rank Fusion：score(d) = Σ 1/(k + rank_i(d))。
    三路异构打分（SQL 线性分 / BM25 负值 / 余弦）不可直接比，RRF 只看名次。"""
    fused = {}
    for lst in ranked_lists:
        for i, r in enumerate(lst):
            fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (k + i + 1)
    order = sorted(fused, key=lambda i: -fused[i])
    return order, fused


def recall(query, budget_tokens=2000, kinds=None, limit=24):
    """自动召回（L3 三路 RRF 融合）。

    返回 (block, used_tokens)：block 是拼好的上下文块（带来源标注，可直接进
    system prompt）；budget_tokens 超了就截断。召回的条目 hit_count+1。
    """
    with _LOCK:
        conn = _connect()
        keys = [k for k in _norm(query).split() if k] or [_norm(query)]
        sql_rows = _sql_scores(conn, keys, kinds)
        fts_rows = _fts_scores(conn, query, kinds)
        vec_rows = _vector_scores(query, sql_rows[:50])
    order, fused = _rrf_fuse(sql_rows, fts_rows, vec_rows)
    if not order:
        return "", 0
    by_id = {r["id"]: r for r in (*sql_rows, *fts_rows, *vec_rows)}
    # 主人原话加权（2026-11-01）：RRF 只看名次，按来源乘权重后重排；
    # 只改排序、不删条目——非 user 来源仍可召回，只是排在后面。
    order = sorted(order, key=lambda i: -(fused[i] * _src_boost(by_id[i]["source"])))

    lines, used = [], 0
    for fid in order[:limit]:
        r = by_id[fid]
        # 带过期期的事实：过了 valid_until 就当不存在
        if r["valid_until"] and time.time() > r["valid_until"]:
            continue
        line = f"- [{r['kind']}] {r['content']}（{r['source']}，{r['confidence']:.1f}）"
        t = int(len(line) / 1.6)
        if used + t > budget_tokens:
            break
        lines.append(line)
        used += t
        _touch(r["id"])
    return ("\n".join(lines), used) if lines else ("", 0)


# ---------------------------------------------------------------- 核心记忆集
def promote(fact_id, on=True):
    """把一条事实标记/取消核心记忆集（无条件注入 system prompt）。"""
    with _LOCK:
        conn = _connect()
        conn.execute("UPDATE facts SET promote_state=? WHERE id=?",
                     (1 if on else 0, fact_id))
        conn.commit()


def core_block(limit=50, budget_tokens=800):
    """核心记忆集：≤limit 条无条件注入（独立预算，检索砍不到——红线）。
    选取：promote_state=1 优先，其余按 confidence×(1+hit_count)×来源权重 排。
    主人原话（source=user）权重 2.0（2026-11-01 最小改动，只调排序不删条目）。"""
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, content, kind, source, confidence, hit_count FROM facts "
            "WHERE status='active' AND (promote_state=1 "
            "  OR valid_until IS NULL OR valid_until > ?) "
            "ORDER BY promote_state DESC, "
            "  confidence * (1 + hit_count) * "
            "  (CASE WHEN source='user' THEN 2.0 ELSE 1.0 END) DESC "
            "LIMIT ?", (time.time(), limit)).fetchall()
    lines, used = [], 0
    for r in rows:
        line = f"- [{r['kind']}] {r['content']}"
        t = int(len(line) / 1.6)
        if used + t > budget_tokens:
            break
        lines.append(line)
        used += t
    return "\n".join(lines), used


def _touch(fact_id):
    """被召回一次就 +1——越常用越靠前（OpenViking 同思路）。"""
    with _LOCK:
        conn = _connect()
        conn.execute("UPDATE facts SET hit_count = hit_count + 1 WHERE id=?", (fact_id,))
        conn.commit()


def expire(content_substr):
    """失效而非覆盖：新事实与旧事实冲突时，旧的下架但不删（可审计）。"""
    with _LOCK:
        conn = _connect()
        cur = conn.execute("UPDATE facts SET status='expired', updated_at=? "
                           "WHERE status='active' AND content LIKE ?",
                           (time.time(), f"%{content_substr}%"))
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------- 每日巩固
def consolidate(extract_fn, max_chars=6000, source="inferred"):
    """每日巩固（04:00，时间盒 5 分钟）：把已捕获会话提炼成长期事实。

    extract_fn：调用方注入的提炼函数（webapp 侧用本地引擎实现"对话→事实列表"），
    记忆模块不 import llm（分层单向：memory 是 L2，不依赖 L1 的对话客户端）。
    引擎没跑/提炼失败 → 返回 skipped，守护第二天再试，不硬来。
    返回 {"status": "ok"/"skipped"/"empty", "facts": [...], "archived": n}
    """
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            "SELECT role, content, ts FROM sessions WHERE captured=0 "
            "ORDER BY ts ASC LIMIT 200").fetchall()
    if not rows:
        return {"status": "empty", "facts": [], "archived": 0}
    dialog = "\n".join(f"{'主人' if r['role'] == 'user' else '桌宠'}：{r['content']}"
                       for r in rows)[-max_chars:]
    try:
        facts = extract_fn(dialog) or []
    except Exception as e:
        return {"status": "skipped", "reason": f"{type(e).__name__}: {str(e)[:80]}",
                "facts": [], "archived": 0}
    if not isinstance(facts, list):
        facts = [str(facts)]
    saved = []
    for f in facts[:30]:                    # C9（2026-10-28）：12→30 条，一天的事记得住
        if isinstance(f, str) and f.strip():
            fid, _merged = remember(f.strip(), kind="fact", source=source,
                                    confidence=0.8)
            saved.append(fid)
    # 提炼过的会话标记归档（保留最近 10 条活跃）
    with _LOCK:
        conn = _connect()
        total = conn.execute(
            "SELECT COUNT(*) n FROM sessions WHERE captured=0").fetchone()["n"]
        if total > 10:
            cutoff = conn.execute(
                "SELECT ts FROM sessions WHERE captured=0 "
                "ORDER BY ts DESC LIMIT 1 OFFSET 9").fetchone()
            if cutoff:
                conn.execute("UPDATE sessions SET captured=1 WHERE ts <= ? AND captured=0",
                             (cutoff["ts"],))
                conn.commit()
    return {"status": "ok", "facts": saved, "archived": len(saved)}


def stats():
    with _LOCK:
        conn = _connect()
        f = conn.execute("SELECT COUNT(*) n, SUM(status='active') a FROM facts").fetchone()
        s = conn.execute("SELECT COUNT(*) n FROM sessions WHERE captured=0").fetchone()
        c = conn.execute("SELECT COUNT(*) n FROM facts WHERE promote_state=1 "
                         "AND status='active'").fetchone()["n"]
        return {"facts_total": f["n"], "facts_active": f["a"] or 0,
                "core_set": c,
                "sessions_pending": s["n"], "schema_version": SCHEMA_VERSION,
                "fts": _FTS_OK, "db": str(DB_PATH)}


def list_facts(limit=100, include_expired=False):
    """给管理界面用的记忆清单（webapp 不直接摸 SQL——分层规矩）。"""
    with _LOCK:
        conn = _connect()
        where = "" if include_expired else "WHERE status='active'"
        rows = conn.execute(
            f"SELECT id, content, kind, confidence, hit_count, status,"
            f" promote_state, occurred_at, created_at FROM facts {where}"
            f" ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
