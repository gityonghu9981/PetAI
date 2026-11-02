# -*- coding: utf-8 -*-
"""照片分享与回看（确定性最小层，2026-11-01 主人定调）。

不强制她截图、不强制她发图——拍不拍、发不发、什么时候发，由她自己决定。
这里只做"主人开口"时的确定性配合：

- "看看 / 继续 / 下一个 / 照片"        → 发下一张未看的，一次一张；
- "你收藏里有什么 / 几张 / 清单"        → 报收藏摘要（不发图）；
- "把上次拍的那张猫找出来 / 找一下…"    → 按备注/标签/窗口关键词找一张并发出；
- "不看 / 不用了"                      → 什么都不做，照片留着，不催；
- 其余情况不自动追问、不自动发图。
"""
import json
import os
import re
import threading
import time

# 2026-11-02：裸"看看"不再单独算"要图"（"边打边自言自语让我看看"被误判过）。
# 明确口令仍然直接命中；只有"看看"时必须与 图/张/照片/截图 同现。
_WANT = re.compile(
    r"(^看$|想看|给我看|发我|发给我|发来看看|继续看|接着看|下一个|下一张|"
    r"还有吗|还有别的|再来一张|照片|截图|看图|看下图|发图|上图|图呢|图在哪)")
_RESEND_CMD = re.compile(r"(再发(?:我|给|一次|一张|张|图|这|那|来|一下|吧|$)|"
                         r"重发|重新发|单独发)")
# "第三张没看到 / 还是看不到 / 再发一次"这类话以前**一个都不匹配**，
# 于是分享状态机连分支都不进，她只能嘴上说"发给你了"（2026-11-01 16:12 实测）。
# "没看到/看不到"必须和图/张/照片同现才算要图，否则"我看不见屏幕"会误触发。
_NOTSEEN = re.compile(r"(没看到|看不到|没收到|没显示|再发|重发|单独发|重新发|"
                      r"第[一二三四五六七八九十0-9]+张)")
_IMG_HINT = re.compile(r"(图|张|照片|截图)")
# 桌面操作类的话不是"要照片"：2026-11-01 实测，主人说"看看屏幕上现在有什么东西"，
# 被当成要图，她顺手把一张收藏照片塞进了回答里（本该是 hands 去看屏）。
# 屏幕/桌面/窗口 一出现，这条就交给 hands，照片状态机不掺和。
_DESKTOP_TALK = re.compile(r"(屏幕|桌面|窗口|任务栏|手脚机)")
# 2026-11-02 主人定：游戏类上下文直接排除。两句真实误触发原话分别是
# "边打边自言自语让我看看"（含"这一局"）和"看看结算页……奖励是多少"
# （含"结算页/奖励/输了"）。宁可漏发一张，也不能在游戏回答末尾硬塞旧照片。
_GAME_TALK = re.compile(
    r"(游戏|结算页|棋盘|战斗|奖励|输赢|这一局|对局|战报|关卡|法力|宝石|"
    r"任务节点|开打|打赢|输了|赢了)")
_REFUSE = re.compile(r"(不看|不用了|不想看|先不|算了|改天)")
_SEARCH = re.compile(r"(收藏|你拍的|上次拍|找一下|找出来|翻翻|翻一下|哪张|那张|有没有)")
_SUMMARY = re.compile(r"(有什么|有哪些|多少张|几张|清单|都有啥|都有什么)")
# 找图时要从问句里剥掉的虚词/功能词（剩下的才当关键词）
_STOP_CHARS = set("把我你的了那上张次里有什么多少找出来看看一下收藏照片截图拍给想要")
_STOP_WORDS = ("帮忙", "找一下", "看看", "出来", "那张", "这个", "那个", "收藏",
               "照片", "截图", "上次", "哪里", "什么", "多少", "几张")


def _keywords(q):
    s = _WANT.sub(" ", q)
    s = _SEARCH.sub(" ", s)
    s = _SUMMARY.sub(" ", s)
    for w in _STOP_WORDS:
        s = s.replace(w, " ")
    s = re.sub(r"[，。！？、；：\"'（）()\[\]]", " ", s)
    out = []
    for tok in re.split(r"\s+", s):
        tok = tok.strip()
        if not tok:
            continue
        # 单字关键词只留实词（"猫/狗/猫猫"），防止"的/我/你"混进来
        if len(tok) == 1 and tok in _STOP_CHARS:
            continue
        if tok:
            out.append(tok)
    return out


def _wants_photo(q, kws=None):
    """主人是否明确要图。裸"看看"必须和图/张/照片/截图同现；
    游戏/桌面上下文一律让路。post_answer 与抢答共用这一把尺子。"""
    q = str(q or "").strip()
    if not q:
        return False
    if _REFUSE.search(q) or _DESKTOP_TALK.search(q) or _GAME_TALK.search(q):
        return False
    if _WANT.search(q) or _RESEND_CMD.search(q):
        return True
    if "看看" in q and _IMG_HINT.search(q):
        return True
    if _NOTSEEN.search(q) and _IMG_HINT.search(q):
        return True
    return bool(kws and _SEARCH.search(q))


def _explicit_resend(q, kws=None):
    """主人明确要"再发/没看到/找那张"时才允许绕过台账强制发一次。"""
    q = str(q or "").strip()
    if _REFUSE.search(q) or _DESKTOP_TALK.search(q) or _GAME_TALK.search(q):
        return False
    if _RESEND_CMD.search(q):
        return True
    if _NOTSEEN.search(q) and _IMG_HINT.search(q):
        return True
    return bool(kws and _SEARCH.search(q))


def _find_photo(hands, keywords, limit=100):
    """按关键词找一张收藏（备注/窗口/标签 做子串匹配，命中最多者优先）。"""
    rows = hands.list_photos(limit=limit)
    best, best_score = None, 0
    for r in rows:
        text = (r.get("note", "") + " " + r.get("window", "") + " "
                + " ".join(r.get("tags") or []))
        score = sum(1 for k in keywords if k in text)
        if score > best_score:
            best, best_score = r, score
    return best


def context_hint(query, max_items=5):
    """生成前的上下文提示：让模型知道她的收藏里有什么，别答"我没有收藏夹"。"""
    try:
        from tools import hands
    except Exception:
        return ""
    q = (query or "").strip()
    # 2026-11-02：不再因裸"看看"就注入收藏上下文（游戏句会被带偏）。
    if not (_wants_photo(q) or _SUMMARY.search(q)
            or _SEARCH.search(q) or "收藏" in q):
        return ""
    rows = hands.list_photos(limit=max_items)
    if not rows:
        return "【你的照片收藏】现在还是空的；不要编造收藏内容。"
    total = len(hands.photo_state())
    unseen = hands.unseen_count()
    names = "、".join((r.get("note") or r.get("window") or "一张")[:20] for r in rows)
    return (f"【你的照片收藏】共 {total} 张（还没给主人看 {unseen} 张），最近：{names}。"
            "主人问起就据实回答；发图时一次只发一张。")


# ── 统一发送台账（2026-11-02）──────────────────────────────────────
# 现象：10:50/11:18 两条游戏回答末尾各推了一次同一张旧图，间隔 27m39s。
# 10 分钟冷却挡不住；桌面聊天侧更是完全不去重。
# 用同一份 data/photo_sent.json：key = id + 内容哈希，记录 last_sent_ts/channels/reason。
SENT_LOG = None
SENT_LOCK = threading.RLock()
SENT_TTL_S = 6 * 3600      # 6 小时：覆盖同一段活跃聊天，避免同一天反复贴旧图
# 为什么选 6h 不是 24h：24h 会把"隔了半天主人明确想再看看那张"也挡掉；
# 而 6h 已覆盖本次 28 分钟这种跨轮/跨 session 场景，且显式"再发/没看到"会 force 放行。


def _sent_path():
    global SENT_LOG
    if SENT_LOG is None:
        from core import config
        SENT_LOG = os.path.join(str(config.DATA_DIR), "photo_sent.json")
    return str(SENT_LOG)


def _photo_key(rec):
    pid = str(rec.get("id") or rec.get("rel") or "")
    h = rec.get("hash")
    return ("%s:%s" % (pid, h)) if h is not None else pid


def _read_sent():
    p = _sent_path()
    if not os.path.exists(p):
        return {}
    try:
        data = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_sent(data):
    p = _sent_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def sent_recent(rec, ttl=None):
    """这张图在 TTL 内是否已经发过（不分渠道，桌面/本地共用一份台账）。"""
    key = _photo_key(rec or {})
    if not key:
        return False
    with SENT_LOCK:
        row = _read_sent().get(key) or {}
        if not isinstance(row, dict):
            row = {}
        ts = float(row.get("last_sent_ts") or 0)
        return (time.time() - ts) < (SENT_TTL_S if ttl is None else ttl)


def mark_sent(rec, channel, reason=""):
    """记账：last_sent_ts / channels / reason。读-改-写全程持锁。"""
    key = _photo_key(rec or {})
    if not key:
        return
    now = time.time()
    with SENT_LOCK:
        data = _read_sent()
        row = data.get(key) if isinstance(data.get(key), dict) else {}
        channels = row.get("channels") if isinstance(row.get("channels"), dict) else {}
        channels[str(channel)] = now
        row["last_sent_ts"] = now
        row["channels"] = channels
        row["reason"] = reason or row.get("reason", "")
        data[key] = row
        _write_sent(data)


def _push_local(rec, caption, reason="photo_share", force=False):
    """把收藏里的图**当文件**做本地提示；与桌面补图共用同一份发送台账。
    force=True 只给"主人明确再发/没看到"用，普通自动路径一律先查台账。"""
    from core import config
    if not rec:
        return False
    pid = rec.get("id") or rec.get("rel") or ""
    if not pid:
        return False
    if not force and sent_recent(rec):
        _audit("photo_share 跳过重复推送：%s｜%s"
               % ((rec.get("note") or "一张")[:24], rec.get("rel", "")))
        return False
    path = os.path.join(str(config.DATA_DIR), "photos", rec.get("rel", ""))
    if not os.path.isfile(path):
        return False
    try:
        from channels import notify
        # 注意返回形状：tell_master_image 给的是 **dict**（{"ok":..,"channel":..,"detail":..}），
        # 不是元组。按元组解包会 ValueError 被吞成 False ⇒ 图明明发出去了，
        # 却报"未推"、台账也不写，主人每问一次就重复推一次（16:22 实测踩过）。
        res = notify.tell_master_image(path, caption=(caption or "这张")[:40],
                                       why=reason)
        ok = bool(res.get("ok")) if isinstance(res, dict) else bool(res)
    except Exception:
        return False
    if ok:
        mark_sent(rec, "local", reason)
    return bool(ok)


def push_photo(rec, caption=None, reason="photo_share", force=False):
    """给 pipeline._run_photo_promise 的统一入口：不许绕过台账直接调 notify。"""
    return _push_local(rec, caption, reason=reason, force=force)


def _audit(detail):
    try:
        from core import audit
        audit.log("photo_share", detail)
    except Exception:
        pass


def _pick_for(hands, q, kws, allow_resend=False):
    """要图时挑哪张：关键词找到 → 下一张未看 → 只有主人明确再发才回退最近一张。
    没有未看的新图且不是明确重发 ⇒ (None, "")，绝不自动重发旧图。"""
    rec = _find_photo(hands, kws) if (_SEARCH.search(q) and kws) else None
    if rec is not None:
        if not rec.get("shown"):
            hands.mark_photo(rec["id"], shown=True)
        return rec, "给你找到这张："
    rec = hands.next_unseen_photo()
    if rec:
        hands.mark_photo(rec["id"], shown=True)
        return rec, "这张给你看："
    if not allow_resend:
        return None, ""
    rows = hands.list_photos(limit=1)
    rec = rows[0] if rows else None
    return rec, ("按你说的，再发这张：" if rec else "")


# ── 确定性抢答（§21）───────────────────────────────────────────────
# 实测：一次"看图看图"要跑一整轮 27B（GPU 峰值 99%、功率 495W vs 空载 59W，6.2 秒），
# 而图是哪张根本由这层的规则决定，模型只贡献一句客套。主人连着要三次 = 转三次风扇。
# 所以**极短的纯口令**直接模板回 + 附图，不过模型；带任何疑问/评价口吻一律让路给她答。
_QUICK_LEN_MAX = 14
_QUICK_QUESTION = re.compile(
    r"(吗|呢|？|\?|像不像|觉得|好看|丑|好不好|行不行|怎么样|如何|怎样|什么|为啥|为什么"
    r"|是不是|对不对|哪张|偏不偏|像谁|猜)")
_QUICK_TEMPLATES = (
    "来啦～", "给你这张 🐾", "喏，这张（小声）喜欢就说一声，我再去找同调子的",
    "这张！我挑了半天的～")
_QUICK_NEXT = re.compile(r"(下一张|下一个|再来一张|还有吗|还有别的|新的|别的)")
_LAST_QUICK = {}          # sid -> ts（防同一轮被抢答和 post_answer 各发一次）


def quick_photo_turn(query, sid="default"):
    """命中就返回 (她的话, 图块文本)，否则 None。判定故意做窄：
    只吃 ≤14 字的纯口令（看图/发来看看/下一张/第三张没看到/再发一次）。"""
    import time as _t
    s = str(query or "").strip()
    if not s or len(s) > _QUICK_LEN_MAX or _QUICK_QUESTION.search(s):
        return None
    if not _wants_photo(s):
        return None
    try:
        from tools import hands
    except Exception:
        return None
    allow_resend = _explicit_resend(s)
    rec, head = _pick_for(hands, s, [], allow_resend=allow_resend)
    if rec is None:
        if _QUICK_NEXT.search(s):
            _LAST_QUICK[sid] = _t.time()
            return ("看过的就这几张啦🥺 想看新的跟我说'去找几张图'，我再去翻一批。", "")
        return None
    note = (rec.get("note") or rec.get("window") or "这张").strip()
    # 明确重发才说"再发"；自动路径已经没有回退旧图这一说了。
    if head.startswith("按你说的"):
        text = "按你说的，再发这张～"
    else:
        text = _QUICK_TEMPLATES[int(_t.time()) % len(_QUICK_TEMPLATES)]
    extra = "\n\n%s%s\n\n![她收藏的图](%s)" % (head, note, rec["url"])
    pushed = _push_local(rec, note, reason="photo_share_quick",
                          force=allow_resend)
    if pushed:
        extra += "\n（也直接推给你了）"
    mark_sent(rec, "chat", "photo_share_quick")
    _audit("photo_share 抢答发图：%s｜%s｜推送%s｜%s"
           % (note[:24], rec.get("rel"), "已推" if pushed else "未推",
              "明确重发" if allow_resend else "新图"))
    _LAST_QUICK[sid] = _t.time()
    return text, extra


def post_answer(query, answer, sid="default"):
    """生成完回答后调用：返回 (新回答, 需要额外推送的文本)。"""
    try:
        from tools import hands
    except Exception:
        return answer, ""
    q = (query or "").strip()
    import time as _t
    if _t.time() - _LAST_QUICK.get(sid, 0) < 10:
        return answer, ""              # 这一轮已经由抢答发过图了，不重复发
    if _REFUSE.search(q):
        return answer, ""                         # 不看：留着，不催

    # 1) 收藏摘要：先靠上下文提示让模型答；模型仍没提"收藏"就补一句
    if _SUMMARY.search(q) and ("收藏" in q or "照片" in q or "截图" in q):
        rows = hands.list_photos(limit=8)
        if rows and not any(k in answer for k in ("收藏", "照片", "张")):
            names = "、".join((r.get("note") or r.get("window") or "一张")[:20]
                              for r in rows[:5])
            extra = f"\n\n我收藏里有 {len(rows)} 张：{names}…要看哪张？"
            return answer + extra, extra
        return answer, ""

    # 2) 明确要看：能按关键词找就找，否则给下一张未看的（一次一张）；
    #    **没有未看的新图就不再回退旧图**（2026-11-02：旧图被游戏句反复触发）。
    kws = _keywords(q)
    if not _wants_photo(q, kws):
        return answer, ""
    allow_resend = _explicit_resend(q, kws)
    rec, head = _pick_for(hands, q, kws, allow_resend=allow_resend)
    if rec is None:
        rows = hands.list_photos(limit=1)
        if not rows:
            say = "\n\n我收藏现在是空的——得先拍一张或收一张图才有东西给你看。"
            _audit("photo_share 要图但收藏为空")
        else:
            say = "\n\n收藏里的图都给你看过了，想看新的跟我说'去找几张图'。"
            _audit("photo_share 没有未看新图，未附图")
        return answer + say, say
    note = (rec.get("note") or rec.get("window") or "这张").strip()
    if not allow_resend and sent_recent(rec):
        _audit("photo_share 跳过重复发图：%s｜%s"
               % (note[:24], rec.get("rel", "")))
        return answer, ""
    extra = "\n\n%s%s\n\n![她收藏的图](%s)" % (head, note, rec["url"])
    pushed = _push_local(rec, note, reason="photo_share", force=allow_resend)
    if pushed:
        extra += "\n（也直接推给你了）"
    mark_sent(rec, "chat", "photo_share")
    _audit("photo_share 发图：%s｜%s｜推送%s"
           % (note[:24], rec.get("rel"), "已推" if pushed else "未推"))
    return answer + extra, extra
