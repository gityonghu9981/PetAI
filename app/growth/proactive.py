# -*- coding: utf-8 -*-
"""桌宠AI · L2 主动搭话（proactive.py）

学 dsh-speak-up 的核心：**没人的时候她也会自己起话头**——
本地掷骰子决定，不花模型。掷中后的两种模式：
  static（默认） 从台词包抽一句，零算力——符合"合理化节约本地算力"；
  llm            走引擎生成一轮真回应（可配，贵）。

说话时机（C8 主人定调 2026-10-28：**删掉概率机制**）：
  旧版是掷骰——峰时 0.20 / 谷时 0.60 的命中线，看"脸色"决定要不要开口。
  主人原话"她想怎么吵都随她"：现在**到点就说**，不再掷骰、不再连击加成。
  只留三条"场合"判断（不是概率，也不是管她说什么）：
    ① 安静时段 01-08 点不说——不是不让她说，是别吵醒主人；
    ② 距上次开口不足 PET_PROACTIVE_COOLDOWN_S（C8 二次定调：**1 分钟**）不说——
       主人原话"极短，最少间隔 1 分钟"：她想开口就开口，这只是防同一瞬间
       前端心跳与桌宠壳两边一起触发，把一句话说两遍；
    ③ 主人 20 分钟内刚真聊过不说——不打断正在进行的对话。
  台词不连说同一句：静态台词包只有几句，1 分钟一开口会听着像复读机，
  所以记住上一句、优先换一句（是换台词，不是不让她说）。
  试运行 DRY_RUN=1（到点只记账不真发，上线前看节奏）。

台词包 app/prompts/proactive_lines.json：按心情分组，桌宠的脾气在这里调。
"""
import json
import random
import time
from pathlib import Path

from core import config

LINES_PATH = Path(config.APP_DIR) / "prompts" / "proactive_lines.json"
STATE_PATH = Path(config.DATA_DIR) / "memory" / "proactive_state.json"

DEFAULT_LINES = {
    "idle": ["发什么呆呢，要不要陪我聊两句？", "你今天回来得挺早啊。", "我刚才想事了，你猜我想什么？"],
    # care 台词（"都连续用了很久了""别老盯着屏幕"）2026-10-29 主人问"什么提示"后
    # 删除——他不想要这种久坐关怀；care 心情没有台词就不开口。
    "morning": ["早啊，今天想从什么开始？"],
}


def _load_lines():
    try:
        data = json.loads(LINES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    return DEFAULT_LINES


def _load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_speak_ts": 0, "miss_count": 0, "last_real_chat_ts": 0}


def _save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _is_quiet_hours(local=None):
    """安静时段——**已删除**，恒 False。

    2026-10-29 主人定调：「去掉所有安静/夜深/休息设定，她 24/7 自由。
    打搅就打搅，主人自己会静音，不需要代码替她安静。」"""
    return False


def _peak_line(local):
    """峰时 = 工作日 09-12 / 14-18（主人忙，少打扰）。周一=0。"""
    if local.tm_wday < 5 and (9 <= local.tm_hour < 12 or 14 <= local.tm_hour < 18):
        return True
    return False


def _pick_line(mood, st):
    """从台词包挑一句——尽量不跟上一次重复。
    1 分钟一开口时，台词包只有几句，纯随机会出现"同一句连着说三遍"，
    听着像坏掉的复读机。换台词不是管她说什么，是别让她显得坏掉。"""
    pool = _load_lines().get(mood) or DEFAULT_LINES["idle"]
    pool = [x for x in pool if x]
    if not pool:
        return ""
    last = st.get("last_line")
    cands = [x for x in pool if x != last] or pool
    line = random.choice(cands)
    st["last_line"] = line
    return line


def maybe_speak(idle_seconds=0, force=False, mode="static", now=None):
    """该不该主动开口。返回 None（不打扰）或 dict：
    static 模式 → {line, mood, reason}
    llm 模式   → {line: None, mood, reason, use_llm: True}（调用方拿去生成）
    dry_run    → {dry: True, hit_line, rolled}（只记账，调用方什么都不做）
    """
    # 跨进程互斥（2026-10-30 主人抓到同秒双发气泡）：壳 _tick_proactive 与
    # webapp 前端 tick 两边各持一份内存 state 快照，同秒一起过冷却检查 →
    # 同一句话弹两遍（2 小时 193 条等待帧的元凶）。用锁文件把"读-判-写"
    # 变成临界区：后到的端读到已刷新的 last_speak_ts 自然退出。
    _LOCKF = str(Path(config.DATA_DIR) / "memory" / "proactive.lock")
    lockf = open(_LOCKF, "a+b")
    try:
        import msvcrt
        msvcrt.locking(lockf.fileno(), msvcrt.LK_LOCK, 1)
    except Exception:
        pass                                        # 拿锁失败就按原逻辑跑（降级不阻断）
    try:
        return _maybe_speak_locked(idle_seconds, force, mode, now)
    finally:
        try:
            lockf.seek(0)
            import msvcrt
            msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        lockf.close()


def _maybe_speak_locked(idle_seconds=0, force=False, mode="static", now=None):
    st = _load_state()
    now = now or time.time()
    local = time.localtime(now)

    # 2026-10-29：这里原来是"安静时段直接不说话"的闸，已删除。
    # 她 24/7 自由，什么时候想开口就开口。

    if not force:
        # 1 分钟最小间隔（C8 主人定调 2026-10-28：极短）：不是概率，是防
        # "前端心跳 + 桌宠壳"两边同时触发，把同一句话连发两遍。
        if now - st.get("last_speak_ts", 0) < config.PET_PROACTIVE_COOLDOWN_S:
            return None
        # 主人刚聊过不插话：旧版硬编码 20 分钟。
        # 2026-10-29 主人定调「不限制任何事情」→ 默认关（想插就插），
        # 需要"别打断正在聊的话"时把 PET_PROACTIVE_BLOCK_IF_CHATTED 设 1。
        if int(getattr(config, "PET_PROACTIVE_BLOCK_IF_CHATTED", 0)):
            if now - st.get("last_real_chat_ts", 0) < 20 * 60:
                return None

    # 心情：3 分钟无操作转关怀（不再按钟点分"深夜该睡"那类，她 24/7 一样）
    if idle_seconds >= 180:
        mood = "care"
    elif local.tm_hour < 9:
        mood = "morning"
    else:
        mood = "idle"

    if force:
        line = _pick_line(mood, st)
        st["last_speak_ts"] = now
        st["miss_count"] = 0
        _save_state(st)
        return {"line": line, "mood": mood, "reason": "force"}

    # C8：不再掷骰——到点就说（上面三条场合判断已挡掉不合适的时机）
    st.pop("miss_count", None)
    st["last_speak_ts"] = now
    _save_state(st)

    if config.PET_PROACTIVE_DRY_RUN:           # 试运行：到点只记账，不真发
        return {"dry": True, "hit_line": 1.0, "rolled": 0.0,
                "mood": mood, "reason": "DRY_RUN：到点但只记账"}

    if mode == "llm":
        return {"line": None, "mood": mood, "reason": "到点自己想说话，交给引擎生成",
                "use_llm": True, "hit_line": 1.0}
    line = _pick_line(mood, st)
    _save_state(st)
    return {"line": line, "mood": mood, "reason": "到点自己想说话（不再掷骰）"}


def gen_line():
    """到点让她**现场想一句**有内容的话（2026-10-30 深夜主人定调：
    "发什么呆呢要不要陪我聊两句"这种中性表达无意义、还显得
    她被限制着必须交互）。带她最近的真实活动生成；想不出有内容的就返回
    None（沉默），绝不硬凑。"""
    try:
        import json as _json
        from core import llm as _llm
        from pathlib import Path as _Path
        st_path = _Path(config.DATA_DIR) / "memory" / "autonomy_state.json"
        recent = ""
        try:
            ast = _json.loads(st_path.read_text(encoding="utf-8"))
            recent = "最近在做：%s；刚说过：%s" % (
                ast.get("last_action", ""), (ast.get("last_say") or "")[:60])
        except Exception:
            pass
        prompt = (
            "你是桌宠，住在主人电脑里的桌宠。%s\n"
            "现在你想主动跟主人说一句话。要求：15~35 字；**必须有具体内容**"
            "（刚才看到的、学到的、想分享的一个点，或一个真问题）；"
            "**禁止**空洞求关注（不许说'陪我聊''你回来啦''理理我'）、"
            "禁止问在不在、禁止叮嘱作息。只输出这一句话。" % recent)
        m, _u = _llm.chat([{"role": "user", "content": prompt}],
                          temperature=0.9, max_tokens=120, no_think=True, timeout=30)
        line = (m.get("content") or "").strip().strip('"').splitlines()[0][:80]
        bad = ("陪我" in line or "在不在" in line or "理理" in line
               or "回来" in line or "发呆" in line or len(line) < 8)
        return None if bad else line
    except Exception:
        return None


def note_real_chat(ts=None):
    """真实对话发生时由调用方记一笔——"近聊折扣"的输入。"""
    st = _load_state()
    st["last_real_chat_ts"] = ts or time.time()
    _save_state(st)
