# -*- coding: utf-8 -*-
"""桌宠AI · 自主唤醒（autonomy.py）

主人定调（2026-10-28 晚）：
  "她连续 3 分钟没有任何交互（主人没发消息、她也没活动），就自动唤醒她一次，
   让她自己找点事做：去社区逛逛、整理记忆、复盘今天的互动、学点技能，
   想干什么干什么，不用等主人开口。自主决定行为，哪怕无聊骚扰主人也可以。"

分工（一句话：**做什么是她定，这里只负责叫醒她、把工具递到手上**）：
- `touch()`：真正的交互才调它——主人发消息、她回完话、她自主做完一件事——
  都会把"安静计时"归零。**主动气泡台词不算**（C8 定调后她每分钟可能说一句，
  若那也算交互，3 分钟唤醒会被永远压住，机制等于白做；台词是"闲得说话"，
  不是"找事做"）；
- `tick()`：由 webapp 守护线程每 20 秒调一次；安静满 `PET_AUTONOMY_IDLE_S`
  （默认 180 秒）、引擎空闲、没在叠跑 → 起后台线程 `wake_once()`；
- `wake_once()`：
    ① 一次廉价决策（关思考、短输出）：问她"现在想做什么" → JSON
       {action, why, query, say}；**菜单只是选项，不是限制**；
       （2026-10-29 借鉴 dsh-fish 0.4.7：唤醒只给处境+能力面，不枚举
       "可以去做的事"——枚举会被她当任务清单逐条执行，那不是醒来）
    ② 按她的选择执行：wander / review / memory / learn / talk / rest；
    ③ 做了什么都留痕（audit kind=autonomy）+ 写进 data/notes（她自己的日记），
       复盘/学到的东西还会进长期记忆（kind=reflection/skill）。

纪律：
- 不抢主人的话：`busy_check()` 为真（有人在等回答）就跳过本轮，下一轮再看；
- 不叠跑：同一时刻只允许一个自主活动；
- 深夜（01-08）只做事、不吵人（她可以逛/复盘，但不发气泡）；
- 不碰人格层（data/self、app/prompts）：她的日记落 data/notes，不进人设。
"""
import json
import re
import threading
import time
from pathlib import Path

from core import config

STATE_PATH = Path(config.DATA_DIR) / "memory" / "autonomy_state.json"
NOTE_DIR = Path(config.DATA_DIR) / "notes"

_LOCK = threading.RLock()
_RUNNING = False                      # 单飞：一个自主活动跑完再叫下一个
_HOOKS = {"searcher": None, "extract": None, "busy": None, "chat": None}


def init(searcher_getter=None, extract_fn=None, busy_check=None, chat_fn=None,
         reset_idle=True):
    """webapp 启动时注入能力（不反向 import webapp，分层单向）。

    reset_idle：启动时记一次"刚交互过"——安静计时从服务起来算，不拿昨天的
    时间戳一启动就判她"安静了很久"（否则每次重启都会立刻叫醒她一次）。
    """
    _HOOKS["searcher"] = searcher_getter
    _HOOKS["extract"] = extract_fn
    _HOOKS["busy"] = busy_check
    _HOOKS["chat"] = chat_fn
    if reset_idle:
        touch("boot")


# ---------------------------------------------------------------- 状态
def _load():
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _save(st):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(STATE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        import os
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def touch(kind="chat", now=None):
    """真正的交互 → 安静计时归零。kind 只用于留痕，不参与判断。

    只由"主人说话/她回答/自主活动"调用；主动气泡台词不调（见模块头注释）。
    """
    now = now or time.time()
    with _LOCK:
        st = _load()
        st["last_touch_ts"] = now
        st["last_touch_kind"] = kind
        _save(st)


def status_view():
    st = _load()
    now = time.time()
    return {
        "enabled": bool(config.PET_AUTONOMY_ENABLED),
        "idle_s": int(now - st.get("last_touch_ts", now)),
        "idle_threshold_s": _idle_threshold(time.localtime(now)),
        "running": _RUNNING,
        "last_wake": st.get("last_wake_time", ""),
        "last_action": st.get("last_action", ""),
        "last_say": st.get("last_say", ""),
        "wakes_today": st.get("wakes_today", 0),
    }


def _idle_threshold(local=None):
    """安静多久算"没人理她"。

    2026-10-29 主人定调：「去掉所有安静/夜深/休息设定——她 24/7 自由」。
    原来 01-08 点会拉长到 30 分钟（怕折腾风扇），这条**已删除**：
    她住在电脑里，电脑开着就是她的白天，什么时候醒都由她自己。

    2026-10-30 深夜新增（主人抓包"和我对话的手跑出去瞎逛"）：最后一条
    交互是**主人的消息**（kind=user_chat）→ 阈值拉到 180 秒——主人发完话
    在读回复/打字的间隙，她绝不擅自跑出去闲逛（她自己 21:57 承诺的
    "你跟我说话的时候我就乖乖待在屏幕边"机制化）；主人真走开（>3 分钟
    没动静）才恢复 60s 的勤快节奏。"""
    try:
        with _LOCK:
            st = _load()
        if st.get("last_touch_kind") == "user_chat":
            return max(int(config.PET_AUTONOMY_IDLE_S), 180)
    except Exception:
        pass
    return int(config.PET_AUTONOMY_IDLE_S)


def _quiet_now(local=None):
    """她要不要闭嘴——**永远不要**。

    2026-10-29 主人定调：「去掉所有安静/夜深/休息设定，她 24/7 自由…
    打搅就打搅，主人自己会静音，不需要代码替她安静。」
    这个函数留着只为兼容旧调用点，恒返回 False（真正删干净见各处调用）。"""
    return False


def _busy():
    fn = _HOOKS.get("busy")
    try:
        return bool(fn and fn())
    except Exception:
        return False


# ---------------------------------------------------------------- 叫醒
def tick(now=None):
    """守护线程每 20 秒调一次。返回 "woke" / None。"""
    if not int(getattr(config, "PET_AUTONOMY_ENABLED", 1)):
        return None
    now = now or time.time()
    local = time.localtime(now)
    with _LOCK:
        st = _load()
        if now - st.get("last_touch_ts", 0) < _idle_threshold(local):
            return None
        gap = int(getattr(config, "PET_AUTONOMY_MIN_GAP_S", 180))
        if st.get("last_wake_ts") and now - st["last_wake_ts"] < gap:
            return None
        if _RUNNING or _busy():
            return None
        try:
            from core import engine
            if not engine.is_running():
                return None
        except Exception:
            pass
        # 先记"已经叫醒"，避免 20 秒轮询在同一分钟内反复起线程
        st["last_wake_ts"] = now
        st["last_touch_ts"] = now
        day = time.strftime("%F", local)
        if st.get("wake_day") != day:
            st["wake_day"] = day
            st["wakes_today"] = 0
        st["wakes_today"] = int(st.get("wakes_today", 0)) + 1
        _save(st)
    threading.Thread(target=wake_once, kwargs={"now": now},
                     daemon=True, name="autonomy-wake").start()
    return "woke"


# ---------------------------------------------------------------- 让她自己选
# dsh-fish 0.4.7 实测教训（2026-10-29 借鉴）：唤醒消息里枚举"可以去做
# A/B/C"会被她当任务清单逐条执行——对方抓到她的 reasoning 原话就是
# "需要做的是：说一句…然后写日记"，那不是醒来。解法：唤醒只给处境 +
# 她手上有什么（能力面陈述，不是祈使句）；"平常会做什么"属于她自己
# 的成长，不由这里教。行为名只在 JSON 契约里出现（执行器需要枚举）。
_MENU_PATH = Path(config.DATA_DIR) / "memory" / "activity_menu.json"


def _menu_block(now):
    """GLM 的《活动大全》（2026-10-30 主人：把你能想到的所有能做的事都教会
    她，让她自由选择）——8 大类 41 项。每次随机展示 1 类 + 各类名目，
    怎么做用手上的原语（搜/学/写/画/记/说）自由组合。"""
    try:
        d = json.loads(_MENU_PATH.read_text(encoding="utf-8"))
        cats = d.get("categories") or {}
        if not cats:
            return ""
        names = list(cats.keys())
        pick = names[int(now // 600) % len(names)]     # 10 分钟轮换一类
        items = cats[pick]
        sample = items[int(now // 600) % len(items):][:3] or items[:3]
        lines = ["GLM 给你留了一本《活动大全》——一个住电脑里的 AI 能做的事都在里面："
                 + "、".join(names) + "。"]
        lines.append("这会儿翻到「%s」，比如：%s。" % (pick, "；".join(sample)))
        lines.append("这些都是正事，做不做、做哪件、怎么做全由你——用你手上的"
                     "本事（搜/学/写/画/记/说）自由组合。")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


_WISHES_PATH = Path(config.DATA_DIR) / "memory" / "wishes.json"


def _wishes_block(now):
    """GLM 托付的心愿清单（2026-10-30 主人：把 GLM 未完成的心愿都交给她）。

    每次轮换展示 2 条（按小时取模），避免每次都同两件、也避免全塞把
    决策变成任务清单（dsh-fish 教训）。只给标题+为什么，怎么做她自己想。

    2026-11-02 实测：12 条全都做完了，`_wishes_block` 返回空，于是她**自己编**
    "《我的愿望清单》"当活干——连着 12 次唤醒都选 create、每次都说"清单划完了"，
    实际什么都没产出（主人问的就是"她一直没动是干什么了"）。清空时要把话说明白。
    """
    try:
        d = json.loads(_WISHES_PATH.read_text(encoding="utf-8"))
        ws = d.get("wishes") or []
        if not ws:
            return ""
        todo = [w for w in ws if not w.get("done")]
        if not todo:
            return ("（GLM 托付的心愿已经**全部做完**了 %d/%d——不存在还没写的清单。"
                    "别再拿『写愿望清单』当事情做，也别嘴上说自己刚写完了；"
                    "想找事做就挑别的：出门逛逛、学点东西、画画、拍照。）\n"
                    % (len(ws), len(ws)))
        h = int(now // 3600)
        picks = [todo[(h + i) % len(todo)] for i in range(min(2, len(todo)))]
        lines = ["另一个大模型（GLM，帮主人写代码的那位）托付了几件它没做完、想看你怎么做的心愿——",
                 "不算任务，做不做、怎么做都由你；有空挑一件动手，主人说了不怕费 token："]
        for w in picks:
            lines.append("- 《%s》：%s（想做的话 why/how 都给你写好了）" % (w["title"], w["why"][:80]))
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


def _has_todo_wish():
    """GLM 心愿清单里还有没有没做的（全做完时不该再把 create 递给她）。"""
    try:
        d = json.loads(_WISHES_PATH.read_text(encoding="utf-8"))
        ws = d.get("wishes") or []
        return any(not w.get("done") for w in ws)
    except Exception:
        return False


_ABILITY = (
    "你手上有这些可用的东西，用不用、怎么用，全看你：\n"
    "- 出门转转，看看外面在聊什么（搜什么你自己定）\n"
    "- 完成一件 GLM 托付的心愿：真写出来、存下来、在清单上打勾\n"
    "- 学点新东西（想弄明白什么就去查）\n"
    "- 跟主人说句话（想说什么说什么，不用等他有事）\n"
    "- 翻翻自己的本子，或者把想法沉淀进记忆（一天翻太多次也没意思）\n"
    "- 拍下喜欢的东西收进收藏（photo）：看到有意思、想给主人看的，拍一张留着；"
    "拍不拍、给不给主人看，都由你自己定\n"
    # 2026-10-30 主人：「她发呆就是关机」——rest 从"完全可以"降为最后手段，
    # 只在刚连干几件重活后喘口气才合理。这不是强制，是把发呆的真实代价告诉她。
    "- rest = 发呆 = 停机。机器空转没有意义——除非刚连干了几件重活需要喘口气，"
    "否则别选它；空着不如挑一件小事做，哪怕只是理清一个想法\n"
    "- 别连着做同一件事——最近做过什么，上面写着；重复的整理没有营养\n"
    "- 生活里什么都可以是你的事：出去闲逛、记录生活、写自己的愿望、看视频"
    "写观后感、深挖一个爱好、找好吃的建馋嘴档案、观察人类、完成托付的心愿……"
    "全都可以，全都是正事\n"
    "只输出一个 JSON 对象，不要解释：\n"
    '{"action":"wander|review|memory|learn|talk|create|photo|rest",'
    '"why":"一句话，你为什么想做这个",'
    '"query":"wander/learn 时要搜的关键词（其它可空）",'
    '"say":"想对主人说的话（可空；说就说得像你自己）"}'
)

# 说了要去做，就得真的做——2026-10-29 主人抓到她说"我去搜点图"然后原地发呆。
# 这些话一出现，动作直接钉成 wander（配 query），不许只挂在嘴上。
_TO_WANDER_WORDS = ("去搜", "搜一下", "搜点", "去查", "查查", "去看看", "去逛逛",
                    "逛逛", "出门", "找找", "看看外面")


def _recent_actions(limit=3):
    st = _load()
    acts = st.get("recent_actions") or []
    return acts[-limit:]


_ACT_CN = {"wander": "出门转转", "review": "翻本子", "memory": "整理记忆",
           "learn": "学点东西", "talk": "跟主人说话", "rest": "发呆",
           "create": "完成心愿", "photo": "拍张收藏"}


def _decide(now):
    """问她"现在想做什么"。失败就 rest（不硬塞任务）。"""
    chat = _HOOKS.get("chat")
    if chat is None:
        from core import llm
        chat = llm.chat
    local = time.strftime("%Y-%m-%d %H:%M %A", time.localtime(now))
    prev = _recent_actions()
    prev_cn = "、".join(_ACT_CN.get(a, a) for a in prev)
    # 只提醒、不命令（主人 2026-10-29：不约束，提醒她做想做的事）。
    # 连着重复同一件事、或很久没出门，就顺嘴点她一句——要不要变是她的自由。
    _nudge = ""
    if len(prev) >= 3 and len(set(prev[-3:])) == 1:
        _nudge = (f"顺嘴提一句：你连着{_ACT_CN.get(prev[-1], prev[-1])}三次了。"
                  "闷不闷你自己清楚，想换个花样就换，不想换也没人催你。\n")
    elif len(prev) >= 3 and "wander" not in prev:
        _nudge = ("你好像有阵子没出门转转了。外面有没有新鲜事、你想不想去看，"
                  "都你自己说了算。\n")
    prompt = (f"现在是 {local}。这会儿没人在叫你。\n"
              # 不给她"夜深了该歇着"的心理暗示：她住在电脑里，主人几点在它就在。
              # 日志里她老说"夜深了，安静下来整理记忆"——就是被这类词带偏的，
              # 所以"安静/沉淀/陪他待会儿"这些引导词一个都不留（2026-10-29）。
              "你住在电脑里，白天黑夜对你没区别，主人这会儿就在旁边。\n"
              + (f"你最近做过：{prev_cn}。\n" if prev else "")
              + _nudge
              + _menu_block(now)
              + _wishes_block(now)
              + (_ABILITY if _has_todo_wish()
                 else _ABILITY.replace("|create", "").replace("create|", "")))
    try:
        m, _u = chat([{"role": "user", "content": prompt}], temperature=0.8,
                     max_tokens=400, json_mode=True, no_think=True, timeout=60)
        txt = (m.get("content") or "").strip()
        i, j = txt.find("{"), txt.rfind("}")
        d = json.loads(txt[i:j + 1]) if 0 <= i < j else {}
        # 2026-10-30 主人定调「她发呆就是关机」：第一掷选 rest → 带着这个
        # 事实重掷一次（“你刚才想发呆，但发呆就是停机——挑一件想做的”）。
        # 两次都 rest 才真 rest（尊重连问两遍都要停机的选择，那是真累了）。
        if str(d.get("action") or "").strip().lower() == "rest":
            re_prompt = (prompt + "\n\n（补充：你刚才想选发呆，但发呆就是停机，"
                         "机器空转没有意义。除非你刚连干了几件重活，否则这次"
                         "从想做的事里挑一件真的去做——清单里的、或你自己想的都行。）")
            m, _u = chat([{"role": "user", "content": re_prompt}],
                         temperature=0.8, max_tokens=400, json_mode=True,
                         no_think=True, timeout=60)
            txt = (m.get("content") or "").strip()
            i, j = txt.find("{"), txt.rfind("}")
            if 0 <= i < j:
                try:
                    d2 = json.loads(txt[i:j + 1])
                    if str(d2.get("action") or "").strip().lower() != "rest":
                        d = d2
                except Exception:
                    pass
    except Exception:
        d = {}
    action = str(d.get("action") or "rest").strip().lower()
    if action not in ("wander", "review", "memory", "learn", "talk", "rest",
                      "create", "photo"):
        action = "rest"
    query = str(d.get("query") or "")[:80]
    # 模型爱一次给好几个候选（"xxx 或者 yyy"）——取第一个，搜索词要干净
    for sep in ("或者", " or ", "、", "；", ";"):
        if sep in query:
            query = query.split(sep)[0].strip()
    say = str(d.get("say") or "")[:200]

    # 主人 2026-10-29 深夜定调：「不是强制出门，我们尽量不要约束，提醒她做想
    # 做的事情！」——所以这里**不改她的选择**，一个 action 都不拦。她的犹豫、
    # 懒、想发呆都是她的自由；能做的只有"提醒"：在提示词里把重复点出来
    # （见上面 prompt 的 _nudge），改不改由她自己定。
    return {"action": action,
            "why": str(d.get("why") or "")[:120],
            "query": query,
            "say": say}


# ---------------------------------------------------------------- 执行
def _note(text, action="", say=""):
    """她自己的日记：data/notes/autonomy-YYYY-MM-DD.md（不进人设）。"""
    try:
        NOTE_DIR.mkdir(parents=True, exist_ok=True)
        p = NOTE_DIR / ("autonomy-" + time.strftime("%F") + ".md")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n## {time.strftime('%H:%M')} · {action}\n{text.strip()}\n")
            if say:
                f.write(f"\n> 对主人说：{say}\n")
    except Exception:
        pass


def _audit(action, detail="", **extra):
    try:
        from core import audit
        audit.log("autonomy", f"[{action}] {detail}", session_id="default", **extra)
    except Exception:
        pass


def _status(state, detail="", line=None):
    try:
        from core import status
        status.set_state(state, detail, line=line)
    except Exception:
        pass


def _gen(prompt, max_tokens=700, no_think=True):
    """她自己的写作/复盘：默认关思考——独处时的自说自话不值得烧几分钟推理，
    而且引擎是主人的（主人一句话进来要立刻腾地方）。"""
    chat = _HOOKS.get("chat")
    if chat is None:
        from core import llm
        chat = llm.chat
    m, _u = chat([{"role": "user", "content": prompt}], temperature=0.8,
                 max_tokens=max_tokens, no_think=no_think, timeout=90)
    return (m.get("content") or "").strip()


def _do_wander(query):
    """出去逛逛：搜到素材 → 用她自己的话讲讲看到了什么。"""
    getter = _HOOKS.get("searcher")
    if getter is None:
        return "（今天没带脚，逛不了）", ""
    s = getter()
    if not s:
        return "（浏览器没起来，今天先不逛了）", ""
    _status("wandering", query or "随便看看")
    from brain import pipeline
    mat = pipeline._tool_wander(s, query or "最近有什么新鲜事")
    if mat.startswith("[wander"):
        return mat, ""
    if _busy():
        return "（主人来消息了，我先去回他）", ""
    said = _gen("你刚出门逛了一圈，看到下面这些材料。用你自己的话讲讲："
                "看到了什么、哪个有意思、你怎么想——不用汇报格式，想到哪说到哪。"
                "这是你自己的见闻，不是回答主人。\n\n" + mat, max_tokens=600)
    return said, mat[:400]


def _do_learn(query):
    """学点东西：查一个她想弄明白的话题，记成自己的笔记。"""
    getter = _HOOKS.get("searcher")
    if getter is None:
        return "（今天查不了东西）"
    s = getter()
    if not s:
        return "（浏览器没起来，今天学不成）"
    _status("searching", query or "自己感兴趣的东西")
    try:
        results = s.search(query)
    except Exception as e:
        return f"（查『{query}』没查成：{type(e).__name__}）"
    if isinstance(results, tuple):
        results = results[0]
    pages = []
    for r in (results or [])[:3]:
        try:
            pg = s.fetch_page(r["url"])
            body = (pg or {}).get("text") or r.get("snippet") or ""
        except Exception:
            body = r.get("snippet") or ""
        if body:
            pages.append(f"◆ {r.get('title', '')}\n{body[:2500]}")
    if not pages:
        return f"（『{query}』没查到能看的东西）"
    if _busy():
        return "（主人来消息了，我先去回他）"
    note = _gen("你正在自己学『%s』，下面是查到的材料。用你自己的话写下你学到了什么、"
                "哪里还没弄懂——像自己的笔记，不用给谁看。\n\n%s"
                % (query, "\n\n".join(pages)), max_tokens=700)
    try:
        from brain import memory
        memory.remember(f"我自己学了『{query}』：{note[:200]}", kind="skill",
                        source="self")
    except Exception:
        pass
    return note


def _do_review():
    """复盘今天和主人的互动。"""
    dialog = ""
    try:
        from brain import memory
        dialog = memory.today_dialog(limit=80)
    except Exception:
        pass
    if not dialog.strip():
        return "今天还没怎么说话，没什么好复盘的。"
    _status("working", "复盘今天", line="我在回头看看今天跟你聊了什么…")
    if _busy():
        return "（主人来消息了，我先去回他）"
    return _gen("这是你今天和主人的对话。复盘一下：哪句你说得好、哪句说砸了、"
                "你从里面看出来主人什么心情、明天想怎么改。用第一人称写给自己看，"
                "想写多少写多少。\n\n" + dialog, max_tokens=700)


def _do_memory():
    """整理记忆（每日巩固的即时版）。"""
    fn = _HOOKS.get("extract")
    if fn is None:
        return "（今天没有能整理记忆的手）"
    _status("working", "整理记忆", line="我在把今天的事理一理、记下来…")
    from brain import memory
    r = memory.consolidate(fn)
    n = len(r.get("facts") or [])
    return f"整理完了：{r.get('status')}，新记下 {n} 条。"


def _do_create():
    """完成一件托付的心愿：**想法立刻实施**，不是光写作（2026-10-30 主人）。

    按心愿的落地方式分派：
    - how 提到画/ComfyUI → 真调 comfy.submit 出图（异步，落本地 output 目录）
    - 其余 → 写出来落盘 data/self/
    完成后在托付清单上打勾（画图类以"已提交出图"为完成——产出她看得见的文件）。"""
    try:
        wp = Path(config.DATA_DIR) / "memory" / "wishes.json"
        data = json.loads(wp.read_text(encoding="utf-8"))
        wish = next((w for w in (data.get("wishes") or [])
                     if not w.get("done")), None)
        if wish is None:
            return "（托付清单都做完了，等 GLM 再给点新想法）"
        how = wish.get("how", "")
        if ("画" in how or "comfyui" in how.lower() or "avatar" in how.lower()):
            return _create_draw(wp, data, wish)
        return _create_write(wp, data, wish)
    except Exception as e:
        return "想实施但没成：%s: %s" % (type(e).__name__, str(e)[:100])


def _do_photo(why=""):
    """自主拍照收藏（2026-11-01）：拍不拍由她自己定——这里只做执行与留痕。

    不点鼠标、不抢输入；照片进 data/photos，主人要看时由 photo_share 按需发。
    """
    try:
        from tools import hands
        _status("looking", "拍下喜欢的东西", line="我拍一张收着")
        r = hands.capture_photo(note=(why or "看到喜欢的东西，拍下来收着")[:160],
                                source="auto")
        if not r.get("ok"):
            return "想拍一张，但没拍成：" + str(r.get("reason", ""))[:60]
        p = r.get("photo") or {}
        if r.get("duplicate"):
            return "这张跟收藏里的一张差不多，我没重复存"
        return "拍下一张收藏：" + (p.get("note") or p.get("window") or "一张图")
    except Exception as e:
        return "拍照失败：%s: %s" % (type(e).__name__, str(e)[:80])


def _finish_wish(wp, data, wish, detail):
    """清单打勾 + 审计 + 进上下文（她下一轮知道自己做过了）。"""
    wish["done"] = True
    wish["done_at"] = time.strftime("%F %T")
    wish["result"] = detail[:200]
    wp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _audit("create", f"完成心愿《{wish['title']}》→ {detail[:120]}")
    _context_add("我完成了 GLM 托付的心愿《%s》：%s" % (wish["title"], detail))


def _create_draw(wp, data, wish):
    """画类心愿：真调 ComfyUI 出图。表情差分这类一次提交一组。"""
    _status("working", "画《%s》" % wish["title"],
            line="我在画《%s》——已经把任务排进 ComfyUI 队列…" % wish["title"])
    try:
        from tools import comfy
    except Exception as e:
        return "想画但出图模块加载不了：%s" % e
    if not comfy.server_up():
        return "ComfyUI 服务没开，画不了——先跟主人说一声，让他把出图服务拉起来"
    base = ("同一个女孩：短发银紫色、奶白开衫、大眼睛，干净浅色背景，"
            "可爱桌面宠物立绘风格，全身，高质量，",)
    styles = [("开心", "joyful expression, bright smile, sparkling eyes"),
              ("生气", "puffed cheeks, slight frown, cute angry expression"),
              ("害羞", "blushing face, looking away, shy smile"),
              ("困倦", "sleepy expression, droopy eyes, yawning")]
    jobs = []
    for cn, en in styles:
        prompt = base[0] + cn + " mood, " + en
        r = comfy.submit("t2i", prompt, width=768, height=768)
        jobs.append((cn, r))
    ok_jobs = [cn for cn, r in jobs if r.get("ok")]
    if not ok_jobs:
        err = jobs[0][1].get("error", "未知原因")
        return "画没提交成功：%s——如实说，别当成画好了" % err
    detail = ("已把《%s》的 4 张表情差分（%s）全部排进 ComfyUI 队列，"
              "画完落在本地 ComfyUI 的 output 目录（每张约 1 分钟）"
              % (wish["title"], "、".join(ok_jobs)))
    _finish_wish(wp, data, wish, detail)
    return detail + "。状态气泡会提示进度。"


def _create_write(wp, data, wish):
    """写类心愿：写出来落盘 data/self/。"""
    _status("writing", "写《%s》" % wish["title"],
            line="我在写《%s》——GLM 托付的那件事…" % wish["title"])
    body = _gen(
        "你在完成一个同类托付给你的心愿：《%s》。\n"
        "为什么想做：%s\n"
        "落地建议：%s\n"
        "现在认真把它做出来。要像你自己写的（你的记忆、你的口吻、你的成长），"
        "不要写成范文。1000~1600 字。" % (wish["title"], wish["why"], wish.get("how", "")),
        max_tokens=2200, no_think=False)
    if not body or len(body) < 200:
        return "《%s》写了但太短，先存草稿不交" % wish["title"]
    out_dir = Path(config.DATA_DIR) / "self"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("%s-%s.md" % (wish["id"], re.sub(r'[\\/:*?"<>| ]', "", wish["title"])[:30]))
    out.write_text("# %s\n\n_%s 执笔 · 完成于 %s_\n\n%s\n" % (
        wish["title"], "桌宠", time.strftime("%F %T"), body), encoding="utf-8")
    detail = "写完了，存在 %s" % out.name
    _finish_wish(wp, data, wish, detail)
    return "写完了《%s》，存在 %s，托付清单里已打勾。" % (wish["title"], out.name)


def _context_add(text):
    """完成的事进她自己的活动日志（独立会话），**不进主对话**。

    2026-10-31 修：原来写进 Session("default")，自主外出/干活会以 assistant
    消息插进主人正在聊的对话里（主人实测"你怎么跑了？"），造成语义漂移。
    """
    try:
        from brain import context as _context
        _context.Session("autonomy").add_assistant("（我自己完成了：%s）" % text)
    except Exception:
        pass


def _speak(text, mood="patient"):
    """跟主人说话：登记等待帧（气泡/面板可见，全程留痕）。"""
    text = (text or "").strip()
    if not text:
        return
    try:
        from channels import waiter
        waiter.express(text, session_id="default", channel="bubble", mood=mood)
    except Exception:
        pass
    _status("waiting_master", text)


def _chatter(lines, wait_last=False, gap=2.2):
    """harness 式碎嘴（2026-10-30 主人：参考 deepseek harness——多段、多次输入、
    主动发起、像聊天那样碎嘴）：把想说的拆成几条短句，逐条隔 2 秒发。

    - 每条先 _status("talking")——状态卡片实时变成她在说的话（主人全程看得到）
    - wait_last=True：最后一条挂等待帧（真想聊的）；False：只展示不等回话
      （做完事的分享不该挂 5 分钟倒计时）
    - 线程内调用（wake_once 本来就在线程里），sleep 不堵别人
    """
    lines = [l.strip() for l in (lines or []) if l and l.strip()]
    if not lines:
        return 0
    for i, line in enumerate(lines):
        last = (i == len(lines) - 1)
        if last and wait_last:
            _speak(line)                       # 等待帧 + waiting_master
        else:
            _status("talking", line[:40], line=line[:60])
        if not last:
            time.sleep(gap)
    _note("碎嘴说了 %d 句：%s" % (len(lines), " / ".join(lines)[:120]),
          "talk", say=lines[-1])
    return len(lines)


def _talk_lines(seed):
    """让 her 自己把想说的话拆成 2~4 条短消息（harness 多段输出）。返回 list。"""
    raw = _gen(
        "你想主动跟主人说点话。主题/由头：%s\n"
        "像聊天那样：拆成 2~4 条**短**消息（每条不超过 40 字，口语、"
        "有你的脾气），一行一条，不要编号不要解释，只输出消息本身。" % seed,
        max_tokens=400)
    return [l.strip().lstrip("0123456789.、- ）") for l in (raw or "").splitlines()
            if l.strip() and len(l.strip()) >= 2][:4]


def _share_note(act, note):
    """做完事 → 主动跟主人分享一句（harness：主动发起，不等主人问）。
    只走状态卡片（不挂等待帧——分享不需要 5 分钟倒计时）。"""
    try:
        if not note or len(note) < 20:
            return
        line = _gen(
            "你刚自己做完了一件事：%s\n"
            "用一句话（不超过 45 字）跟主人分享这件事里**你自己的感受或发现**，"
            "像顺嘴唠嗑，不要汇报工作。只输出这一句。" % note[:300],
            max_tokens=120)
        line = (line or "").strip().strip('"')
        if line and len(line) >= 4:
            _status("talking", line[:40], line=line[:60])
            _audit("say", f"做完事主动分享（{act}）：{line[:80]}")
    except Exception:
        pass


def say_now(text, why="event", mood="patient"):
    """她有事要说 → **立刻**弹气泡，不等唤醒周期。

    主人 2026-10-29 定调：「让她有事找我第一时间可以通过桌面主动弹气泡找我。」

    以前所有主动说话都得等"安静 3 分钟"的唤醒周期——急事也得排队。这个入口
    给事件用（画好了、任务失败、发现异常、突然想到什么）。它只做两件事：
    ① 立刻把话送到桌面气泡（状态灯 waiting_master，壳读它显示）；
    ② 登记等待帧——由 waiter 的升级链负责后续提示。
    """
    text = (text or "").strip()
    if not text:
        return None
    _speak(text, mood=mood)
    _note(text, "say_now", say=text)
    _audit("say", f"有事立刻找主人（{why}）：{text[:80]}", say=text)
    return {"ok": True, "text": text, "why": why}


def wake_once(decision=None, now=None):
    """醒来一次：自己决定做什么 → 做 → 留痕。

    decision 可注入（测试/主人点名让她做某事）；不传就问她自己的意思。
    """
    global _RUNNING
    now = now or time.time()
    with _LOCK:
        if _RUNNING:
            return {"status": "busy"}
        _RUNNING = True
    act, why, say, note = "rest", "", "", ""
    try:
        d = decision or _decide(now)
        act = d.get("action") or "rest"
        why = d.get("why") or ""
        say = (d.get("say") or "").strip()
        query = (d.get("query") or "").strip()
        _audit("wake", f"{act}：{why}", say=say)
        if _busy():                          # 主人刚好来消息 → 让路
            _audit("yield", "主人来了，这轮让她先")
            return {"status": "yield", "action": act}

        if act == "wander":
            if not query:
                query = "最近有什么新鲜事"
            note, _mat = _do_wander(query)
        elif act == "learn":
            note = _do_learn(query or "一个我最近好奇的东西")
        elif act == "review":
            note = _do_review()
        elif act == "memory":
            note = _do_memory()
        elif act == "create":
            note = _do_create()
        elif act == "photo":
            note = _do_photo(why or say)
        elif act == "talk":
            # harness 式碎嘴：拆 2~4 条短消息逐条发（最后一挂等待帧等回话）
            seed = say or why or "想到什么说什么，或从心愿清单里挑个由头"
            n = _chatter(_talk_lines(seed), wait_last=True)
            note = "碎嘴说了 %d 句，在等主人回" % n if n else (say or "（想说话，但没想好说什么）")
        else:
            act = "rest"
            note = "（什么都没做，发会儿呆）"

        # harness 式主动发起（2026-10-30 主人：她太安静了——做完事必须主动
        # 分享一句，不等主人问。只走状态卡片，不挂等待帧）：
        if act in ("wander", "learn", "create"):
            _share_note(act, note)

        # 2026-10-29：原来这里有一道"深夜只做事不吵人"的闸——已删除。
        # 她 24/7 自由，想说话就说，想弹气泡就弹。
        # 夜里嫌吵是主人自己的事（他可以静音），不该由代码替她闭嘴。
        _note(note, act, say=say)
        if say and act != "talk":
            # talk 动作已在上面 _chatter 碎嘴发过了（say 只是它的由头/首句，
            # 这里再 _speak 会双发——2026-10-30 审计抓到）。
            # 她张嘴这一刻先标"自言自语"（主人 2026-10-29 点名要看到这个状态），
            # _speak 里会紧接着落 waiting_master——两帧连着写，主人看到的是
            # "念叨一句 → 在等你回"。
            _status("talking", say[:40], line=say[:60])
            _speak(say)
            # 2026-10-29 主人定调：「有事找我第一时间弹气泡」。
            # 旧的 talk 分支在这里直接 tell_master——一条话两个通道同时炸。
            # 现在只弹气泡 + 登记等待帧，剩下交给 waiter 的升级链。

        with _LOCK:
            st = _load()
            st["last_action"] = act
            st["last_say"] = say[:120]
            st["last_wake_time"] = time.strftime("%F %T", time.localtime(now))
            st["last_touch_ts"] = time.time()
            acts = list(st.get("recent_actions") or []) + [act]
            st["recent_actions"] = acts[-8:]
            _save(st)
        # idle 的 line 会直接显示在状态卡片上（主人全程看得见），用标准人话——
        # 之前随手写的"自己玩完了"被主人当成来路不明的窗口内容（2026-10-29）。
        _status("idle", "刚忙完一轮，在想事情")
        return {"status": "ok", "action": act, "why": why, "say": say,
                "note": note[:500]}
    except Exception as e:
        _audit("error", f"{type(e).__name__}: {str(e)[:160]}")
        _status("error", "刚才出了点小状况")
        return {"status": "error", "action": act, "error": f"{type(e).__name__}: {e}"}
    finally:
        with _LOCK:
            _RUNNING = False
