"""主流水线：路由 → 检索 → 临时上下文归纳 → 输出 → 销毁临时上下文。

- 临时上下文（检索快照）只在本函数栈内存活，返回后即不可达（CPython 引用回收）；
- 正式上下文（用户消息 + 最终回答）由 context.Session 落盘；
- --keep-evidence 时额外把快照写 snapshots/（默认关闭，遵循用户"不留痕迹"要求）；
- 生成阶段模型可通过 web_search 工具自主追加搜索（上限 TOOL_LOOP_MAX 轮）；
- 写作类：温度 0.9 + 大预算；**不设字数保底**（主人定调 2026-10-28：写多少由她自己决定）。
"""
import json
import re
import threading
import time
from pathlib import Path

from core import config
from core import llm as engine
from brain import official_sources
from brain import router
from brain import skills
from brain import context

try:
    # 必须是 from core import —— 之前写成 `import status`，在 app/ 下根本解析不到，
    # 被下面的 except 静默吞成 None：于是本文件里三处 status_light.from_step("done")
    # 从未执行过（答完不回发呆），audit_log 为 None 也让 TurnLogger 一直没建过。
    # 2026-11-01 主人抓包"关闭对话框后状态一直显示日常交流"才暴露。
    from core import audit as audit_log      # 行为审计（暂时功能，PET_AUDIT=0 可关）
    from core import status as status_light  # 实时状态灯（永久功能）
except Exception:                  # 模块异常也不许弄断对话
    audit_log = None
    status_light = None


def _touch_autonomy(kind="chat"):
    """有对话/回答 = 有交互 → 自主唤醒的"安静计时"归零（2026-10-28 新机制）。
    任何异常都不许影响回答本身。"""
    try:
        from growth import autonomy
        autonomy.touch(kind)
    except Exception:
        pass


# ---------------------------------------------------------------- 分级思考策略（B26 主人拍板 2026-10-27）
# 依据 docs/彻夜特性测试报告-2026-10-27.md 的"想太久"画像分四级：
#   unsolvable 无解/超难题 → 直接结论性回答（省时不降智）
#   writing    创作/写作   → 只提醒"这是写作"，篇幅/写法她自己定（2026-10-28）
#   logic      强逻辑/数学 → 保思考 + 答完廉价复算自检
#   normal     其余        → 原行为不动（日常题本来就健康，别乱救）
_UNSOLVABLE_PAT = re.compile(
    r"(哥德巴赫|黎曼猜想|孪生素数猜想|角谷猜想|证明.{0,8}猜想|"
    r"永生|长生不老|不死之药|真正的随机|真随机|宇宙的终极|"
    r"意识的本?(质|源)|自由意志是否存在|人生的意义是什么)")
_LOGIC_PAT = re.compile(
    r"(悖论|对折.{0,6}(剪|切|折)|概率|排列组合|鸡兔同笼|逻辑(推理|题|推演)|"
    r"推理题|数学题|几段|几刀|几个人|几个人说谎|谁在说谎|至少多少|最多多少|"
    r"算法复杂度|证明.{0,10}(等于|成立))")


def _tier_of(query, decision):
    """判定本题的思考分级。写作优先（创作词命中明确），其次无解，再次强逻辑。"""
    if not config.TIERED_THINK:
        return "normal"
    if decision.get("is_writing"):
        return "writing"
    if _UNSOLVABLE_PAT.search(query or ""):
        return "unsolvable"
    if _LOGIC_PAT.search(query or "") or decision.get("reasoning_effort") == "xhigh":
        return "logic"
    return "normal"


def _tier_hint(tier):
    """分级 → 注入 system 的回答方式提示（换工作模式，不是截断思考）。"""
    if tier == "unsolvable":
        return ("如果这个问题本身没有已知答案/不可证明/没有确定结论"
                "（如未证明的数学猜想、无解的哲学难题），不要长时间推演："
                "直接给出结论性回答——说明它为什么不可解、目前人们的共识是什么，"
                "简洁作答即可。")
    if tier == "writing":
        # 主人定调 2026-10-28（"写作短稿不长篇，直接删，让她自己判断"）：
        # 这里原来说"用最短的思考列 3 点、别做长篇构思"——等于替她定了
        # 篇幅和写法。现在只提醒"这是写作"，写多长、怎么起头她自己定。
        return "写作类任务：写多长、怎么起头，你自己定；想清楚了就动笔。"
    return ""


def _logic_recheck(msgs, query, answer, step, tl=None):
    """强逻辑复算自检（主人点头项 2026-10-27 测试启用）：答完用关思考的
    廉价调用独立复算一遍，两边关键数字完全对不上 → 自审重写一次。
    复算失败/复算不出数字都静默放行（自检是保险，不是审判）。"""
    try:
        body = engine._no_think_body(
            {"model": config.ENGINE_MODEL,
             "messages": [{"role": "user", "content":
                           "独立解答下面的题（不要参考任何已有答案），"
                           "只输出一行最终结果（含关键数字）：\n" + (query or "")}],
             # 复算只要"一行最终结果"：预算 1500→300、超时 60→25
             # （2026-10-28 实测：旧值让复算自己写满 4096 字推理，白烧 56s）
             "temperature": 0.2, "max_tokens": 200},
            [{"role": "user", "content": query}])
        d = engine._post(body, timeout=12)   # 一行结果 12s 足够；超时静默放行
        rechk = ((d["choices"][0]["message"] or {}).get("content") or "").strip()
        nums_a = set(re.findall(r"\d+(?:\.\d+)?", answer or ""))
        nums_b = set(re.findall(r"\d+(?:\.\d+)?", rechk))
        if rechk and nums_b and nums_a and not (nums_a & nums_b):
            step("audit", f"复算不一致（复算得 {rechk[:40]}），自审重写…")
            if audit_log:
                audit_log.log("recheck", f"复算不一致：{rechk[:80]}",
                              detail2="regenerate")
            answer, _u = _audit_regenerate(
                msgs, answer, [f"你的答案与独立复算结果（{rechk[:60]}）数字不一致，"
                               f"请重新仔细推导后再答"], config.BUDGET_NORMAL, 0.4)
        elif audit_log:
            audit_log.log("recheck", "复算一致/复算无效（放行）")
    except Exception:
        pass
    return answer


def _observe(session, on_step):
    """把 on_step 扇出到三个消费者：审计落档 / 状态灯 / 原消费者（网页 SSE）。
    返回 (TurnLogger, step)。审计关着时 TurnLogger 为哑对象（方法照调不落盘）。"""
    sid = getattr(session, "session_id", "")
    tl = audit_log.TurnLogger(sid) if audit_log else None
    if tl is None:
        class _Nop:                                  # 哑对象：接口同 TurnLogger
            def __getattr__(self, name):
                return lambda *a, **k: None
        tl = _Nop()

    def step(name, detail=""):
        if status_light and config.STATUS_ENABLED:
            try:
                status_light.from_step(name, detail)
            except Exception:
                pass
        try:
            if name == "fallback":
                tl.fallback(detail)
            else:
                tl.step(name, detail)
        except Exception:
            pass
        if on_step:
            on_step(name, detail)
    return tl, step

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": ("联网搜索实时信息。当回答需要最新数据、事实不确定、"
                        "或已有材料不足时调用。禁止用闲聊。"),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "中文搜索关键词"}},
            "required": ["query"],
        },
    },
}


# 自主探索工具（L1.5）：与 web_search 的区别——web_search 是"回答需要"时的
# 事实核查；wander 是"她自己想去逛"（任务单 §2③：调度是脚下的路不是驱动者）。
# 是否调用完全由模型自己决定，系统不做任何定时/范围/节奏安排（不做清单 #1/#2）。
WANDER_TOOL = {
    "type": "function",
    "function": {
        "name": "wander",
        "description": ("出去逛逛：按你好奇的关键词搜一搜、看看外面的世界"
                        "（社区/技能/玩法/新鲜事）。想逛就调，不想逛就不调；"
                        "逛到什么由你评价，喜欢不喜欢自己说。"),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "你现在好奇的关键词"}},
            "required": ["query"],
        },
    },
}


# ComfyUI 出图（2026-10-28）：只在她说要画/聊到画图时才递到手上（comfy.intent），
# 平时不占上下文；执行层 app/comfy.py 读 data/comfy/registry.json 现拼固定命令。
DRAW_TOOL = {
    "type": "function",
    "function": {
        "name": "draw",
        "description": ("用本机 ComfyUI 画图。mode='t2i'：只有文字就能画（文生图）；"
                        "mode='i2i'：拿参考图重绘/换风格，必须传 image（文件名，"
                        "图要先放进本地 ComfyUI 的 input 目录）。大约 1 分钟内出图，画好了"
                        "会自动告诉主人。提示词用英文写具体：主体/场景/风格/光线。"
                        "主人让你画图、你想画给他看、或你想给自己画个形象时用。"),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["t2i", "i2i"],
                         "description": "t2i=文生图（默认）；i2i=图生图（要参考图）"},
                "prompt": {"type": "string",
                           "description": "完整提示词（英文），写清主体/姿势/服装/背景/光线/风格"},
                "image": {"type": "string",
                          "description": "i2i 必填：本地 ComfyUI input 目录里的文件名"},
                "width": {"type": "integer", "description": "宽 px（32 倍数，t2i 用；全身竖版 832）"},
                "height": {"type": "integer", "description": "高 px（32 倍数，t2i 用；全身竖版 1216）"},
                "steps": {"type": "integer", "description": "采样步数（20-35 常用）"},
                "cfg": {"type": "number", "description": "引导强度（5-7 常用）"},
                "denoise": {"type": "number",
                            "description": "i2i 重绘强度：0.5-0.7 局部小改，0.8+ 大改"},
                "seed": {"type": "integer", "description": "种子（不传就随机）"},
                "sampler": {"type": "string", "description": "采样器（默认 euler）"},
                "scheduler": {"type": "string", "description": "调度器（默认 karras）"},
            },
            "required": ["mode", "prompt"],
        },
    },
}


def _last_calls_wander(msg):
    """本轮工具调用是否全是 wander/draw（她自己逛/她要画图）——补搜轮限对它们
    放行：探索与创作是她的生活，不是 KPI（不做清单 #3）。"""
    tcs = msg.get("tool_calls") or []
    return bool(tcs) and all(
        (tc.get("function") or {}).get("name") in ("wander", "draw", "draw_status")
        for tc in tcs)


def _calls_hands(msg):
    """本轮工具调用里有没有 hands（她的手脚）。

    有就必须让工具回路继续：开软件 → 看屏幕 是两段动作，一段也不能少。
    2026-11-01 实测：她调了 open 之后就答"还没有新的截屏回执"——因为回路
    只认 searcher/draw，hands 一出现就被当成"该收尾了"，第二轮 look 根本没机会发。
    """
    return any((tc.get("function") or {}).get("name") == "hands"
               for tc in (msg.get("tool_calls") or []))


def _wander_exempt(query, msg):
    """wander/draw 的"补搜轮限豁免"要不要生效。

    只有"她自己想逛"时生效；主人派了具体活儿（`_hands_intent`）时**不豁免**——
    2026-11-01 实测：问她"用你的浏览器打开 example.com 告诉我标题"，她没调 hands，
    反而连逛 3 轮搜索，一轮 162 秒。具体任务不该用"逛"来代替动手。
    """
    return _last_calls_wander(msg) and not _hands_intent(query)


def _hands_nudge(msgs, query, temp, budget, rem, step, tl):
    """她没动手就收尾 → 逼一次工具调用，再拿真实结果重答。拿不到就返回 None（保留原话）。

    2026-11-01 实测根源：工具就在手上、system 里也写了"必须先调 hands"，可同一句
    "同一句指令"两次都只回"我去取啦，稍等一下下～"就结束——
    模型把搬文件当成一句口头承诺。口头承诺不可靠，这里机械兜一次。
    """
    try:
        # 纯桌面活才逼 hands。
        _instr = ("先别用文字回答：现在立刻调用 hands 工具把这件活干完，"
                  "arg 按工具说明填真实参数（剪切板留空就是读）。"
                  "主人要的是**动作**——open / click / key 这类，不是再 look 一次。")
        _tools = [HANDS_TOOL]
        m, _u = engine.chat(
            msgs + [{"role": "user", "content": _instr}],
            tools=_tools, temperature=0.3, max_tokens=200,
            timeout=rem(), no_think=True)
    except Exception as e:
        step("hands", f"补动手失败（{type(e).__name__}），保留原话")
        return None
    blocks = []
    for tc in (m.get("tool_calls") or []):
        _fname = (tc.get("function") or {}).get("name")
        if _fname != "hands":
            continue
        try:
            args = json.loads(tc["function"]["arguments"])
        except Exception:
            args = {}
            continue
        step("hands", "补动一次手：直接操作桌面")
        tl.tool_call("hands", dict(args))
        blk = _tool_hands(dict(args))
        tl.tool_result("hands", len(blk))
        blocks.append(blk)
    if not blocks:
        return None
    step("generate", "按动手拿到的真实结果重答…")
    _reask = "\n请基于上面的真实结果回答主人的原问题，不要再提工具。"
    try:
        m2, _u2 = engine.chat(
            msgs + [{"role": "user", "content": "\n\n".join(blocks) + _reask}],
            tools=None, temperature=temp, max_tokens=budget, timeout=rem(),
            no_think=True)
    except Exception as e:
        step("generate", f"重答失败（{type(e).__name__}），保留原话")
        return None
    out = (m2.get("content") or "").strip()
    return out or None


def _promise_recent(kind):
    """同 kind 10 分钟内已执行过 → True（防多轮承诺重复跑）。"""
    try:
        pl = Path(config.DATA_DIR) / "memory" / "promise_log.json"
        if not pl.exists():
            return False
        log = json.loads(pl.read_text(encoding="utf-8"))
        return time.time() - log.get(kind, 0) < 600
    except Exception:
        return False


def _promise_mark(kind):
    try:
        pl = Path(config.DATA_DIR) / "memory" / "promise_log.json"
        log = {}
        if pl.exists():
            log = json.loads(pl.read_text(encoding="utf-8"))
        log[kind] = time.time()
        pl.write_text(json.dumps(log), encoding="utf-8")
    except Exception:
        pass


def _run_video_promise():
    from brain import video as _video
    lv_path = Path(config.DATA_DIR) / "memory" / "last_video.json"
    if not lv_path.exists():
        return None
    lv = json.loads(lv_path.read_text(encoding="utf-8"))
    if time.time() - lv.get("ts", 0) > 86400:
        return None
    vp = lv.get("path")
    if not vp or not Path(vp).exists():
        return None
    _audit("promise", f"重看 {lv.get('name')}（专盯字幕）")
    r = _video.analyze(vp, focus="画面里的字幕、歌词、文字，逐字读出来")
    lines = (r.get("note") or "").splitlines()
    sound = next((l for l in lines if l.startswith("声音内容")), "")
    caps = [l for l in lines if l.startswith("第")][:12]
    return ("我把《%s》重新看了一遍（专盯字幕）。%s%s"
            % (lv.get("name", ""), sound,
               "；画面要点：" + "；".join(caps) if caps else ""))


def _run_search_promise(txt):
    from growth import autonomy as _aut
    m = re.search(r"(?:搜|查|看看|找找)(?:一下|搜|查)?[：:，,\s]*([\u4e00-\u9fa5A-Za-z0-9]{2,24})", txt)
    topic = (m.group(1) if m else "").strip("的了～~。, ")
    note, _mat = _aut._do_wander(topic or "刚才承诺要查的东西")
    return "我去查了，%s" % (note or "发现都记下来了")[:280]


def _run_draw_promise(txt):
    from tools import comfy
    m = re.search(r"画(?:一张|个|一幅)?([\u4e00-\u9fa5A-Za-z0-9 ]{2,24})", txt)
    topic = (m.group(1) if m else "").strip("的给主人你我吧哈~～。, ")
    if not comfy.server_up():
        return "ComfyUI 服务没开，画不了——跟主人说一声让他把出图服务拉起来"
    r = comfy.submit("t2i", ("%s，可爱桌面宠物立绘风格，高质量" % topic)
                     if topic else "一只可爱的桌面小宠物，高质量")
    if not r.get("ok"):
        return "画没提交成功：%s" % r.get("error")
    return "已经把《%s》排进 ComfyUI 队列开画了，画好落在本地 output 目录" % (topic or "小宠物")


def _run_memory_promise():
    from growth import autonomy as _aut
    note = _aut._do_memory()
    return "我把记忆整理了一遍：%s" % (note or "都记好了")[:200]


def _run_photo_promise(txt):
    """把"我截给你看"落成真事：取她最近一次图片检索的成图，下载进收藏，
    然后**把图真发出去**——不是只丢一个地址（§19）。

    不拍屏幕（15:41 那次她把主人桌面拍回去交差），也不开浏览器占主人桌面。
    发两条路：① 本地提示走 `channels.photo_share.push_photo`（统一发送台账，禁止绕过）；
    ② 聊天窗走 markdown 图（`app/web/ui.html` 的 `rich()` 只认本机地址，会渲染成
    真图并支持点开放大）。"""
    from tools import hands
    pool = hands.last_images(limit=6)
    if not pool:
        return ("我去收图了，但手上没有可用的搜索结果——"
                "得先有一次要图的搜索（壁纸/头像/照片…）才收得到")
    saved, failed = [], 0
    for it in pool[:3]:
        r = hands.save_picture(it.get("img") or "",
                               note=(it.get("title") or "")[:80],
                               tags="承诺收图")
        if r.get("ok") and not r.get("duplicate"):
            p = r.get("photo") or {}
            saved.append((p, (p.get("note") or "一张图")[:40],
                          str(hands.PHOTO_DIR / p.get("rel", ""))))
        elif not r.get("ok"):
            failed += 1
    if not saved:
        return ("图想收却没收下来（%d 张来源站都不给直链下载），我换一批再试" % failed)
    pushed = 0
    try:
        from channels import photo_share
        for p, title, _path in saved[:2]:
            try:
                if photo_share.push_photo(p, caption=title, reason="promise_kept"):
                    pushed += 1
            except Exception:
                pass
    except Exception:
        pass
    imgs = " ".join("![%s](http://127.0.0.1:8094/photos/%s)" % (t, p.get("rel", ""))
                    for p, t, _p in saved)
    _audit("promise", "收图 %d 张，已推送 %d 张" % (len(saved), pushed))
    head = ("图给你收好啦，%d 张进收藏" % len(saved))
    if pushed:
        head += "（也直接提示你了 %d 张）" % pushed
    return (head + "——这次是真图不是屏幕：\n" + imgs)[:900]


def _keep_promises(reply_text):
    """通用承诺闭环：回复里的承诺词 → 后台真做 → say_now 主动汇报。

    五类：视频重看 / 去搜去查 / 画图 / 整理记忆 / **收图**。一轮最多执行 2 类，
    每类 10 分钟冷却。daemon 线程不堵响应。"""
    def _run():
        txt = reply_text or ""
        plan = []
        if any(w in txt for w in _PROMISE_WORDS_VIDEO):
            plan.append(("video", _run_video_promise))
        if any(w in txt for w in _PROMISE_WORDS_SEARCH):
            plan.append(("search", lambda: _run_search_promise(txt)))
        if any(w in txt for w in _PROMISE_WORDS_DRAW):
            plan.append(("draw", lambda: _run_draw_promise(txt)))
        if any(w in txt for w in _PROMISE_WORDS_MEMORY):
            plan.append(("memory", _run_memory_promise))
        if any(w in txt for w in _PROMISE_WORDS_PHOTO):
            plan.append(("photo", lambda: _run_photo_promise(txt)))
        plan = [(k, fn) for k, fn in plan if not _promise_recent(k)][:2]
        if not plan:
            return
        outs = []
        for kind, fn in plan:
            try:
                out = fn()
                _promise_mark(kind)
                if out:
                    outs.append(out)
            except Exception as e:
                _audit("promise", f"{kind} 承诺执行失败：{type(e).__name__}: {str(e)[:100]}")
        if outs:
            try:
                from growth import autonomy as _aut
                _aut.say_now("；".join(outs)[:600], why="promise_kept")
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True, name="keep-promises").start()


HANDS_TOOL = {
    "type": "function",
    "function": {
        "name": "hands",
        "description": ("你的手——直接操作本机桌面。动作："
                        "look=截屏并用你的眼睛看屏幕上有什么；"
                        "windows=列出当前开着的窗口；"
                        "open=打开一个软件（写名字就行，如'Edge浏览器'/'记事本'，"
                        "也可以给完整路径；PATH、注册表、开始菜单、Program Files 都认）；"
                        "close=关掉最前面的窗口（只关窗口，不是关机）；"
                        "type_text=在当前焦点窗口键入文字；"
                        "click=在坐标处点击（x/y 要填，坐标从 look 看到的画面上估）；"
                        "key=按组合键（arg 写 'ctrl+s' 这种）；scroll=滚轮（arg 写正负数）；"
                        "clip=剪切板（arg 留空=读出来，填内容=写进去）；"
                        "photo=把**此刻屏幕上的画面**拍下来收进收藏（arg=备注；"
                        "注意它拍的是屏幕，想要网上那张图本身请用 pic）；"
                        "pic=把刚才搜到的图直接收进收藏（arg=第几张如'2'，或图片直链；"
                        "留空取第一张）——主人要你'把喜欢的图截回来'时用这个，别拍屏幕。"
                        "**规矩：主人问的是这台机器上的事（窗口、剪切板、屏幕内容），"
                        "先调本工具拿到真实结果再回答，不许凭记忆猜、也不许拿搜到的资料冒充'我看到了'。**"
                        "主人叫你操作电脑/开软件/看屏幕/拍照收藏时用。"),
        "parameters": {
            "type": "object",
            "properties": {
                "act": {"type": "string",
                        "enum": ["look", "windows", "open", "close", "type_text", "click",
                                 "key", "scroll", "clip", "photo", "pic"],
                        "description": "动作：look 看屏幕 / windows 列窗口 / open 开软件 / "
                                       "close 关掉最前面的窗口 / type_text 键入 / click 点击 / "
                                       "key 组合键 / scroll 滚轮 / clip 剪切板 / "
                                       "photo 拍屏幕收藏 / pic 收搜到的图"},
                "arg": {"type": "string",
                        "description": "open 的软件名或路径；type_text 的文字；photo 的备注；"
                                       "pic 的张数或图片直链；key 的组合键；scroll 的滚动量；"
                                       "clip 要写入的文字（留空=读）；look/windows 留空"},
                "x": {"type": "integer", "description": "click 的横坐标"},
                "y": {"type": "integer", "description": "click 的纵坐标"}
            },
            "required": ["act"]
        }
    }
}

_HANDS_INTENT = re.compile(
    r"(桌面|屏幕上|看屏幕|开.{0,4}软件|打开(计算器|记事本|画图)|列.{0,3}窗口|"
    r"窗口|输入文字|打字|拍一张|拍下来|截图|收藏|"
    r"操作电脑|"
    r"剪切板|剪贴板|复制|传文件|搬文件|拿回|取回)")


# 定时碎碎念探针（形象侧每 6 分钟发的定式）**不是主人派活**：一律不给工具。
# 2026-11-02 实测：表情包描述里带了"游戏"两个字 → 递了 hands（`玩(游戏|电脑)` 命中）
# → 她真去点棋盘，和主人正在跑的 game auto 轮流点同一对格子。这也违反
# hands.py 红线「自主活动只看不点」。
_PROBE_QUERY = re.compile(r"^\s*随便说一句日常碎碎念")


def _probe_query(q):
    return bool(_PROBE_QUERY.match(q or ""))


# 光杆祈使句（2026-11-02 实测根源）：上面那张 `_HANDS_INTENT` 是"话题名词表"，
# 句子得带名词才命中。主人真实说法偏偏多半不带名词——实测对
#   关掉 / 关机 / 帮我截个图 / 去截你喜欢的 / 好了叫我 全部 = False
# 于是 `_hands_nudge`（她光说不做时机械逼一次动手）根本不触发，
# 她回一句"这就去…"就收尾；攒下来就是主人看到的"她一直说没干完的事"。
_IMPERATIVE = re.compile(
    r"(关掉|关了|关一下|关机|打开|开一下|退掉|退出|"
    r"去(截|找|发|弄|做|取|拿|存|叫|看|开|关)|"
    r"帮我(截|找|发|关|开|弄|取|拿|存|点|按|打|退)|"
    r"截个?图|截(两张|三张|几张|一下)|发过来|存下来|拿回来|取回来|"
    r"点上|按一下|弄一下|做一下)")

# "别做/不用了/算了"是撤回指令，不能当活儿派给她（否则她真去关）
_IMPERATIVE_OFF = re.compile(
    r"(^(算了|不用了|没事了|不用|别了)|"
    r"(别|不要|不用|先别|先不)(关|开|截|发|弄|去|打|退))")


def _hands_intent(q):
    """这句是不是"派了活"。撤回指令一票否决——先判 OFF 再判命中，
    否则 `不用截图` 会被名词表里的"截图"抢先命中，她真去截（实测）。"""
    q = q or ""
    if _IMPERATIVE_OFF.search(q.strip()):
        return False
    return bool(_HANDS_INTENT.search(q)) or bool(_IMPERATIVE.search(q))


# 主人明确叫"别真动手、只说步骤"时，别拿 nudge 逼她动
# （2026-11-01 实测："先别真打开，只说步骤" 她还是被补动了一次 look）
_HANDS_HOLD = re.compile(r"(先别|别真|不要真|先不(开|动|要|用)|别打开|不用打开|"
                         r"只说|光说|先说说|讲讲就行|别动)")


def _hands_hold(q):
    return bool(_HANDS_HOLD.search(q or ""))


# 会改变屏幕状态的动作：发完必须有真实回执，不许凭想象汇报
_MUT_ACTS = {"key", "click", "type_text", "scroll", "open", "close"}


def _tool_hands(args):
    """动手 + 拿真实回执。

    2026-11-02 实测根源：`alt+f4` 只回 22 个字，她据此宣布"游戏已经关啦"，
    其实是盲发瞎猜——主人连着纠正两轮才认。改状态的动作执行完，立刻查一次
    "此刻屏幕上真开着哪些窗口"贴进回执，她下一句就有据可说；查不到也如实写
    "未确认"，绝不给她一个可以脑补的空结果。
    """
    act = (args.get("act") or "").strip()
    res = _tool_hands_raw(args)
    if act not in _MUT_ACTS or not isinstance(res, str) or res.startswith("[hands 失败]"):
        return res
    try:
        from tools import hands
        w = hands.uia_windows()
    except Exception as e:
        return res + "｜结果未确认（查窗口失败 %s）：如实告诉主人你没确认，别猜" % type(e).__name__
    if isinstance(w, str):
        return res + "｜结果未确认（%s）：照实说，别猜" % w
    now = "；".join("%s(%s)" % (n, t) for n, t in w[:8]) or "一个窗口都没有"
    return res + "｜此刻屏幕上真实开着：%s。要汇报结果就照这个说，不许凭想象" % now


def _tool_hands_raw(args):
    """执行 hands 工具：她对话里承诺的桌面操作（主人当轮指令=安全场景）。"""
    try:
        from tools import hands
    except Exception as e:
        return f"[hands 失败] 模块加载不了：{type(e).__name__} {e}"
    act = (args.get("act") or "").strip()
    arg = (args.get("arg") or "").strip()
    if act == "look":
        return "[hands] 我截屏看了一眼：" + hands.describe_screen()
    if act == "windows":
        w = hands.uia_windows()
        if isinstance(w, str):
            return "[hands] " + w
        return "[hands] 当前开着：" + ("；".join(f"{n}({t})" for n, t in w[:10]) or "没窗口")
    if act == "close":
        return "[hands] " + hands.close_window()
    if act == "open":
        return "[hands] " + hands.open_app(arg)
    if act == "type_text":
        if not arg:
            return "[hands 失败] type_text 要传 arg=要输入的文字"
        return "[hands] " + hands.type_text(arg)
    if act == "click":
        try:
            x, y = int(args.get("x")), int(args.get("y"))
        except (TypeError, ValueError):
            return "[hands 失败] click 要传 x、y（坐标从 look 看到的画面上估）"
        return "[hands] " + hands.click(x, y)
    if act == "key":
        if not arg:
            return "[hands 失败] key 要传 arg=组合键，如 'ctrl+s'"
        return "[hands] " + hands.hotkey(arg)
    if act == "scroll":
        try:
            amount = int(float(arg or 0))
        except ValueError:
            return "[hands 失败] scroll 要传 arg=滚动量（正数向上，负数向下）"
        return "[hands] " + hands.scroll(amount)
    if act == "clip":
        if arg:
            return "[hands] " + hands.clipboard_write(arg)
        txt = hands.clipboard_read()
        if not txt:
            return "[hands] 剪切板是空的（或者读不到）"
        return "[hands] 剪切板里是：" + txt[:400]
    if act == "photo":
        r = hands.capture_photo(note=arg or "主人让我拍的画面", source="master")
        if not r.get("ok"):
            return "[hands 失败] " + str(r.get("reason", "没拍成"))
        p = r.get("photo") or {}
        if r.get("duplicate"):
            return "[hands] 这张跟收藏里差不多，我没重复存"
        return ("[hands] 拍好了，收进收藏：" + (p.get("note") or p.get("window") or "一张")
                + "（提醒：photo 拍的是**屏幕此刻的样子**；要的是网上那张图本身请用 pic）")
    if act == "pic":
        # 把"搜到的那张图"收进收藏——不是拍屏（§18：她曾把主人桌面拍回去交差）
        pool = hands.last_images(limit=8)
        url = ""
        a = arg.strip()
        if a.isdigit() and pool:
            i = int(a) - 1
            if 0 <= i < len(pool):
                url = pool[i].get("img") or ""
                note = pool[i].get("title") or ""
            else:
                return "[hands 失败] 只有 %d 张，没有第 %s 张" % (len(pool), a)
        elif a.lower().startswith(("http://", "https://")):
            url, note = a, "主人指定的图"
        elif pool:
            url, note = pool[0].get("img") or "", pool[0].get("title") or ""
        else:
            return ("[hands 失败] 手上没有可存的图（最近没搜过图）。"
                    "先搜一次要图的词（壁纸/头像/照片…）再来 pic")
        r = hands.save_picture(url, note=note or (arg[:60] if arg else "搜到的图"),
                               tags="图片检索")
        if not r.get("ok"):
            return "[hands 失败] " + str(r.get("reason", "存不下来"))
        p = r.get("photo") or {}
        if r.get("duplicate"):
            return ("[hands] 这张之前已经收过了，没重复存。要看就给主人贴这条："
                    "![%s](http://127.0.0.1:8094/photos/%s)"
                    % ((p.get("note") or "这张")[:24], p.get("rel", "")))
        return ("[hands] 图收进收藏了：%s（来源 %s）。"
                "给主人看就在回复里原样贴这条（聊天窗会显示成真图，点一下能放大）："
                "\n![%s](http://127.0.0.1:8094/photos/%s)"
                % (p.get("rel"), url[:70], (p.get("note") or "这张图")[:24], p.get("rel")))
    return f"[hands 失败] 未知动作 {act}"


def _tool_draw(args):
    """她要画图：交给 comfy.submit 异步跑（不堵对话），返回注入文本。"""
    try:
        from tools import comfy
    except Exception as e:
        return f"[draw 失败] 出图模块加载不了：{type(e).__name__} {e}"
    mode = "i2i" if (args.get("mode") == "i2i" or args.get("image")) else "t2i"
    prompt = (args.get("prompt") or "").strip()
    params = {}
    for k in ("image", "width", "height", "steps", "cfg", "denoise",
              "seed", "sampler", "scheduler"):
        if args.get(k) not in (None, ""):
            params[k] = args[k]
    if not prompt:
        return "[draw 失败] 提示词不能为空——想画什么先想清楚再调"
    if mode == "i2i" and not params.get("image"):
        return ("[draw 失败] 图生图要参考图：先把图放进本地 ComfyUI 的 input 目录，"
                "再把文件名传给 image。没有参考图就用 mode='t2i'")
    r = comfy.submit(mode, prompt, **params)
    if not r.get("ok"):
        return (f"[draw 失败] {r.get('error')}——没画成。如实告诉主人原因，"
                "别当成画好了。")
    return (f"[draw 已开画] 任务 {r['job']} 正在跑（约 1 分钟），画好了状态气泡会"
            "提示、文件落在本地 ComfyUI 的 output 目录。**这个 job 号才是'我在画'的唯一"
            "凭据**，没有它就没在画。这一轮别干等也别再调一次 draw——直接告诉"
            "主人你在画什么、好了叫他来看；想知道进度可以调 draw_status 查真实状态。")


def _calls_draw(msg):
    """本轮工具调用里有没有 draw。"""
    tcs = msg.get("tool_calls") or []
    return any((tc.get("function") or {}).get("name") == "draw" for tc in tcs)


def _calls_draw_status(msg):
    tcs = msg.get("tool_calls") or []
    return any((tc.get("function") or {}).get("name") == "draw_status" for tc in tcs)


def _tool_draw_status(args=None):
    """把真实状态翻译成她读得懂的一段话（结论放第一行，她照着说人话）。"""
    try:
        from tools import comfy
    except Exception as e:
        return f"[draw_status 失败] 出图模块加载不了：{type(e).__name__} {e}"
    p = comfy.progress()
    q = p.get("queue") or {}
    lines = [f"[画图真实状态] 结论：{p.get('verdict')}"]
    lines.append("ComfyUI 服务：" + ("在跑" if p.get("server_running") else "没启动"))
    if isinstance(q, dict) and "error" not in q:
        lines.append(f"队列：正在采样 {q.get('running', 0)} 个、排队 {q.get('pending', 0)} 个")
    lines.append("你的任务：")
    if p.get("running_jobs"):
        lines.append("  正在跑 " + "、".join(p["running_jobs"]))
    elif p.get("stale_jobs"):
        lines.append("  卡住的任务 " + "、".join(p["stale_jobs"]) +
                     "（服务已不在跑 → 这张没画成，得重画）")
    else:
        lines.append("  没有在跑的任务")
    lr = p.get("last")
    if lr:
        exists = lr.get("file_exists")
        lines.append(f"最近一次成功：{Path(str(lr.get('file'))).name}"
                     f"（{lr.get('gen_s')}s，{lr.get('age_s')}秒前）"
                     + ("——这张图还在磁盘上，能直接发给主人"
                        if exists else "——文件已经不见了，得重画"))
    le = p.get("last_error")
    if le:
        lines.append(f"最近一次失败：{str(le.get('error'))[:160]}")
    if not p.get("server_running"):
        lines.append("注：服务没启动 ≠ 以前画的图没了，看上面那行'图还在磁盘上'。")
    lines.append("——『没在跑任务』就等于没在画，别跟主人说你在画。")
    return "\n".join(lines)


# 空承诺：她嘴上说去画，但这一轮没有 draw 调用。抓的就是"自我欺骗"。
_DRAW_PROMISE_RE = re.compile(
    r"(我去画|这就去画|这就画|马上画|开始画|去画了|去画啦|正在画|在画了|"
    r"画着呢|开画了|我画一下|我这就画|待会儿画|稍后画|马上开始画)")


def _looks_like_draw_promise(text):
    return bool(_DRAW_PROMISE_RE.search(text or ""))


def _check_draw_promise(answer, did_draw, step=None):
    """她说"我去画了"但这一轮根本没调 draw、也没有在跑的任务 → 记空承诺。

    这是 2026-10-29 那次的病根：主人让她画自画像，她说"好嘞我去画啦"，
    实际什么都没发生，之后还一直以为自己"在画"。下一轮 context_hints 会把
    这笔账摆到她面前，逼她要么真画、要么老实说没开始。"""
    if not config.COMFY_TOOL_ENABLED or did_draw:
        return
    if not _looks_like_draw_promise(answer):
        return
    try:
        from tools import comfy
        if comfy.running_jobs():
            return                                  # 有任务真在跑，"在画"是真的
        comfy.note_promise(answer, "回复里说要去画，但本轮没有调用 draw 工具")
        if step:
            step("draw", "空承诺：说要去画但没调工具（已记账，下一轮提醒她）")
    except Exception:
        pass


def comfy_intent(query):
    """要不要把画笔递给她——读 data/comfy/registry.json 的触发词（调用时现读）。"""
    try:
        from tools import comfy
        return comfy.intent(query)
    except Exception:
        return False


def comfy_status_intent(query):
    """她/主人在问画图进度——该递监察工具而不是画笔。"""
    try:
        from tools import comfy
        return comfy.wants_status(query)
    except Exception:
        return False


def _tool_wander(searcher, query):
    """执行一次"逛"：按她的关键词搜，抓 1~2 页正文，产出素材摘要。
    与 _tool_search 的区别：面向开放探索（不追时效/权威），材料不带引导语——
    看到什么、怎么评价是她的事（不做清单 #4：不代她做选择）。"""
    try:
        results = searcher.search(query, wait_all=True)
    except Exception as e:
        return f"[wander 失败] {type(e).__name__} {str(e)[:120]}"
    if isinstance(results, tuple):
        results = results[0]
    parts = []
    for r in results[:2]:
        title, url, text = r.get("title", ""), r.get("url", ""), r.get("snippet", "")
        try:
            pg = searcher.fetch_page(url)
            if pg.get("text") and len(pg["text"]) > len(text):
                text = pg["text"]
        except Exception:
            pass
        if text:
            text = text[:config.PER_PAGE_MAX_CHARS]
        parts.append(f"◆ {title}\n{url}\n{text or '（正文没抓到，只有条目摘要）'}")
    if not parts:
        return f"[wander] 按『{query}』没逛到什么，改天再说。"
    return "你出去逛了一圈，看到这些（要不要继续看、喜欢什么由你自己定）：\n\n" + \
        "\n\n".join(parts)


def _tool_search(searcher, query):
    """执行一次工具级搜索，返回注入用的纯文本块（内存态，用完即弃）。"""
    try:
        results = searcher.search(query)
    except Exception as e:
        return f"[web_search 失败] {type(e).__name__} {str(e)[:120]}"
    pages = []
    for r in results[:3]:
        try:
            pg = searcher.fetch_page(r["url"])
        except Exception:
            continue
        if pg.get("text"):
            pages.append(pg)
    block, _idx = context.format_evidence(pages if pages else [
        {"title": x["title"], "url": x["url"], "text": x["snippet"]} for x in results])
    return (block or "没有找到可用结果。")[: config.TOOL_RESULT_MAX_CHARS]

def _last_assistant(session):
    for m in reversed(session.formal):
        if m["role"] == "assistant":
            return m["content"]
    return ""

_PRON_START = re.compile(r"^(那|它|他|她|这|第[一二三四五六七八九十]|还有|然后|接着|其中)")

def _topic_of(text):
    """从一条用户消息里剥出"能当搜索主题词"的部分。
    通道装饰必须去掉——实测病灶：会话里躺着一条 `【渠道】主人说：想我了？`，
    它被当成主题拼进搜索词，找壁纸的任务就变成搜别的（2026-11-01 14:33 那轮 58.4s 的成因）。"""
    s = str(text or "")
    s = re.sub(r"（[^）]*）", " ", s)                       # 括注（"像聊天那样自然回他"）
    s = re.sub(r"\【[^】]{0,16}\】", " ", s)                # 通道标签【渠道】/【手机】
    s = re.sub(r"^[^：:]{0,14}(主人说|用户说|对我说)[：:]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _resolve_queries(session, queries, writing=False):
    """确定性上下文补全：多轮对话中，短查询或以代词/序数开头的查询，
    自动拼上主题词。不依赖判定模型自觉（会漏）。
    writing=True（写作类）不拼接：写作搜索词已是完整主题词（如"项目会 通知 范文"），
    拼接 24 字历史主题会把搜索词污染成整句（搜索按首字主导 → 偏离主题）。

    2026-11-01 三处修正：
    ① 主题取**最近一条**用户消息，不再取会话首条——"它/那个"这类指代的前述对象
       就是刚说过的那句；default 会话能连着好几天，首条可能是几小时前一条
       毫不相干的渠道消息（这正是上面那起污染的源头）。
    ② 主题先过 `_topic_of` 剥通道装饰，绝不把【渠道】/主人说：拼进搜索词。
    ③ 拼接门槛从"短于 12 字"收到"短于 6 字或带指代"。原来的宽门槛会把
       已经自带主题的查询（"美女截图壁纸推荐"8 字）也拼上历史——而历史往往是
       闲聊（"想我了？"），拼完搜索词就废了。真正需要补前件的只有代词开头
       和"明天呢/攻略"这种残句。
    """
    if writing:
        return queries
    users = [m["content"] for m in session.formal if m["role"] == "user"]
    if not users:                       # 本会话还没有历史（当前问题尚未落库）
        return queries
    topic = _topic_of(users[-1])[:24]
    if not topic:
        return queries
    out = []
    for q in queries:
        if _PRON_START.search(q) or len(q) < 6:
            if topic in q or q in topic:          # 已经在说同一件事，不重复拼
                out.append(q)
                continue
            merged = (topic + " " + q).strip()
            out.append(merged if merged not in out else q)
        else:
            out.append(q)
    return out

# 确定性事实/权威类查询（价格/汇率/政策/新闻/标准等）：优先权威站直抓 + 补充搜索
# 轻时效查询（2026-10-28，主人报天气类搜索 64s）：单一时效事实（天气/比分/
# 金价/汇率等）不需要等全部引擎到齐——最快引擎先出即可（权威严谨查询
# 如法律/数据/获奖等保持多源 wait_all，不降质）。
_LIGHT_TIME_PAT = re.compile(
    r"(天气|气温|温度|比分|赛程|金价|汇率|油价|利率|几点|现在几点了|"
    r"限号|路况|油价)")
_AUTH_WORDS = [
    "价格", "金价", "股价", "汇率", "油价", "利率", "政策", "法规", "规定", "标准",
    "新闻", "比分", "赛程", "天气", "版本", "发布", "官方", "通知", "公告", "工资",
    "排名", "名单", "录取", "分数线",
    # 严谨事实/权威查询：此类问题若未进 authority 模式，会搜到百科/自媒体
    # 且排前，抓取后本地模型摘不到精确数值 → 可能编造数字
    "获奖", "得主", "诺贝尔", "起征点", "税率", "法条", "条例", "法律", "宪法",
    "人口", "面积", "海拔", "直径", "权威", "数据", "统计", "报告", "白皮书",
]
_AUTH_DOMAIN_LIST = [
                 # 时效类官方源（2026-10-28 主人定调：天气/金价/汇率等都有官网，
                 # 官方域排最前，结果与直抓优先——二手转载/自媒体一律靠后）
                 "weather.com.cn", "cma.gov.cn", "sge.com.cn", "boc.cn",
                 "pbc.gov.cn", "ndrc.gov.cn", "sse.com.cn", "szse.com.cn",
                 "csindex.com.cn", "mee.gov.cn", "air.cnemc.cn",
                 "gov.cn", "cnn", "reuters", "bloomberg", "xinhuanet",
                 "people.com", "cctv", "eastmoney", "10jqka", "huilvbiao", "cngold",
                 "jinjia", "chinanews", "gov",
                 # 国际官方/权威机构（严谨事实查询优先）
                 "nobelprize.org", "un.org", "who.int", "imf.org", "worldbank.org",
                 "oecd.org", "iea.org", "wikipedia", "baidu百科", "baike.baidu"]

def _think_plan(query, decision, used_search, is_writing):
    """思考分级（主人定调 2026-10-28：和我交流用弱思考或不思考）。

    返回 (no_think, reasoning_budget)：
    - 日常交流（不检索、非写作、非强逻辑、问题不长）→ **不思考**：秒回，
      "聊天别磨蹭"；no_think 双保险走 llm.chat(no_think=True)。
    - 联网归纳（有材料要读、有数字要核）→ **弱思考** 500：够用不磨蹭。
    - 写作成篇 → 900：留规划余量。
    - 强逻辑/深想（悖论/推理题/数学/xhigh）→ 不覆盖，沿用全局 1500 甜点区。
    """
    q = query or ""
    if is_writing:
        return False, config.REASONING_BUDGET_WRITING
    if used_search:
        return False, config.REASONING_BUDGET_SEARCH
    if _LOGIC_PAT.search(q) or decision.get("reasoning_effort") == "xhigh":
        return False, None                     # None = 用全局 REASONING_BUDGET
    if len(q) > 80:
        return False, config.REASONING_BUDGET_SEARCH
    return bool(config.CHAT_NO_THINK), 0


def _decision_hint(decision):
    """判定层给她的"提醒"（不是命令）：易翻车概念时告诉她可以自己查。
    B7（2026-10-28 主人定调）：原来这类是强制搜索，现在把选择权还给她。"""
    if not (decision or {}).get("hint_search"):
        return None
    words = "、".join((decision.get("hint_words") or [])[:3])
    return ("【一个提醒】这个问题" + (f"（{words}）" if words else "")
            + "容易被记岔。你要是没把握，可以自己搜一下再答；"
              "觉得心里有数就直接说——查不查你决定。")


def _hint_for(decision, nt, query=None):
    """生成前的提示合并：判定层的"可自查"提醒 + 日常聊天的"说短点"。

    2026-10-29 主人："和我交流输出还是太慢"——实测慢的不是思考（日常已
    no_think），是她回得太长（145~172 字要 6~9 秒）。日常闲聊给一句长度
    提示，像聊天一样 1~3 句；写作/深想/检索归纳不加，避免影响成篇质量。

    2026-11-01 补 hands 硬提醒：同一句"读剪切板"实测两次，一次调了工具、
    一次只回了句"我去看看哦"就没了（工具就在手上、说明也写了"先调工具"，她
    仍会漏）。这类活的答案就在本机桌面上，光靠 description 不够稳，
    在 system 里再钉一句，让她必须先动手。
    """
    base = _decision_hint(decision) or ""
    if query and _hands_intent(query):
        hands = ("【本轮要先动手】主人问的是本机桌面上的**真实状态**，"
                 "你必须先调用 hands 工具（look/windows/clip/file 按需选），"
                 "把拿到的真实结果说出来；没拿到就如实说没拿到，"
                 "不许说'我去看看''稍等'这类空话后结束回答。")
        base = (base + "\n" + hands) if base else hands
    if nt:
        short = ("【这是日常闲聊】像聊天一样回：1~3 句、几十个字说完；"
                 "不要分点、不要小标题、不要长篇展开。")
        return (base + "\n" + short).strip() if base else short
    return base or None


def _search_mode(query, decision):
    """根据判定结果派生检索模式：writing=广搜多源；authority=权威站优先+补充；normal=可信即出。"""
    if decision.get("is_writing"):
        return "writing"
    if any(w in query for w in _AUTH_WORDS):
        return "authority"
    return "normal"

def _auth_rank(url):
    """权威站加权：命中官方/权威/行情域名排前，便于"一般问答直接进权威网页"。"""
    u = url.lower()
    for i, d in enumerate(_AUTH_DOMAIN_LIST):
        if d in u:
            return -i
    return 1

def _drift_detected(answer, sources):
    """身份漂移启发式：回答里出现原文第一人称且本应基于检索材料。"""
    if not sources:
        return False
    pats = ["小编", "笔者", "本楼", "谢邀", "作者：", "原文链接", "关注我们",
            "大家好，我是", "本文转自", "来源：知乎"]
    return any(p in answer for p in pats)

def _audit_regenerate(msgs, answer, reasons, budget, temp):
    """模型自审：把自己的回答连同材料再发给它，要求以助理身份重写。"""
    fix = ("你的上一条回答存在以下问题：" + "；".join(reasons) +
           "。请立即重写该回答：保持事实与来源编号不变，"
           "但必须以本机 AI 助理的第一人称重新组织转述，"
           "清除一切原文复制痕迹与原作者人称。直接输出修正后的全文。")
    # no_think（2026-10-28 主人报"聊天磨蹭"）：自审重写属**检查类**任务，
    # 不需要再想 1500 token——旧版这里会给"答完的题"再思考一遍，一条逻辑题
    # 因此从 40s 拖到 170s（实测）。关思考后重写质量不变（任务只是改人称/补全）。
    m2, u2 = engine.chat(msgs + [{"role": "assistant", "content": answer},
                                 {"role": "user", "content": fix}],
                         temperature=max(temp, 0.5), max_tokens=budget,
                         no_think=True)
    cand = (m2.get("content") or "").strip()
    return (cand if len(cand) > 40 else answer), u2

_TOOL_LEAK_PAT = re.compile(r"<\s*/?\s*(tool_call|function_calls?|parameter)[ >=]", re.I)

def _strip_tool_leak(text):
    """模型偶发把工具调用 XML 当正文吐出来：截到第一个泄漏标记前，剩余过短则作废。"""
    m = _TOOL_LEAK_PAT.search(text)
    if m:
        cut = text[: m.start()].rstrip()
        return cut if len(cut) >= 20 else ""
    return text

def _finish_answer(msgs, query, session, ephemeral, answer, used_search,
                   tool_rounds, is_writing, sources, searcher, step, rem,
                   skip_expand=False, recheck=False):
    """非流式后处理（整段，不流）：口头重查代查 → 输出体检自审 → 写作扩写 → 空兜底
    → 强逻辑复算自检。answer_once 与 answer_stream 共用，保证两路径行为一致。
    skip_expand=True（外挂深度推理的保底结果）：跳过 3c 扩写——外部答案通常不足
    千字，若再交给本地模型扩写可能被重写成模板腔，保底结果直接交付。
    recheck=True（强逻辑分级）：答完加一道廉价复算自检（_logic_recheck）。"""
    # 3a1) "说到做到"兜底：模型答复极短且口头上说"换关键词/重新查"，却没发出工具调用
    if used_search and searcher and len(answer) < 120 and tool_rounds < config.TOOL_LOOP_MAX:
        import re as _re
        if _re.search(r"(重新|换.{0,4}(关键词|词|几组)|再查|继续查|没查到|无关页面)", answer):
            step("generate", "模型口头要重查但未发起调用，流水线代查续答…")
            block = _tool_search(searcher, query)
            cont = msgs + [{"role": "user",
                            "content": f"【补充搜索结果】\n{block}\n"
                                       "请基于以上材料直接给出完整回答，不要再表示'去查询'。"}]
            msg, usage = engine.chat(cont, temperature=0.6, max_tokens=config.BUDGET_NORMAL,
                          timeout=rem(), no_think=True)
            fresh = _strip_tool_leak((msg.get("content") or "").strip())
            if len(fresh) > len(answer):
                answer = fresh
                tool_rounds += 1

    # 3a2) 输出体检与模型强制审核（用户要求：搜索内容复杂/模型不理解会输出崩溃、
    # 复用搜索结果导致人称主谓语错误）：先规则筛查，命中则让模型自审重写一次
    try:
        drift = _drift_detected(answer, sources)
        # 明显截断/乱码：结尾无标点且长度贴预算上限，或出现成串替换符
        broken = (answer.endswith(("，", "、", "：", "的", "了", "是")) or
                  "\ufffd" in answer or ("..." == answer[-3:]))
        # 体检只留两条（主人定调 2026-10-28）：身份漂移 + 截断/乱码。
        # 旧的"中文占比 <60%""夹英文单词成串"已删——她说话夹英文、用词跳脱
        # 是她自己的口气，不算坏稿（实测误伤过：同一句请假条一次 79 字、
        # 一次被判不合格重写成 125 字）。这里管"别坏掉"，不管"该怎么说"。
        reasons = []
        if drift:
            reasons.append("把原文/原作者的第一人称当成你自己（身份漂移）")
        if broken:
            reasons.append("疑似被截断或字符异常")
        if reasons:
            step("audit", "输出体检发现：" + "；".join(reasons)[:60] + "，模型自审重写…")
            budget = config.BUDGET_WRITING if is_writing else config.BUDGET_NORMAL
            answer, _u = _audit_regenerate(msgs, answer, reasons, budget,
                                           0.9 if is_writing else 0.6)
    except Exception:
        pass  # 审核环节失败不阻塞，保留原答案

    # 3c) 写作类字数保底：**已取消**（2026-10-28 主人定调）。
    #     原文：不足 800 字就带"扩写到至少 1000 字"指令重生成，最多 2 次。
    #     主人原话——"输出字数保底和我们养自由的小家伙相背离"：写多少是她
    #     自己的事，觉得说完了就停，不逼她凑字数、不拿字数当质量。此处不再
    #     有任何长度驱动的重生成；质量只由 3b) 体检（截断/身份漂移/字符异常）
    #     把关，那一条是"别坏掉"，不是"别太短"。
    # 3d) 空回答终极兜底（引擎吐空/全链路失效时，用户仍应拿到可用输出而非空白）
    if not answer.strip():
        step("fallback", "模型返回内容为空，无工具低温兜底重生成…")
        try:
            m3, u3 = engine.chat(
                context.build_messages(session, query, ephemeral, gen_tokens=2048),
                temperature=0.2, max_tokens=2048, timeout=rem(),  # 1600→2048：P1-1 预算红线
                no_think=True)
            cand = (m3.get("content") or "").strip()
            if len(cand) > len(answer):
                answer = cand
        except Exception:
            pass
    if not answer.strip():
        if sources:
            answer = ("模型这一轮没有正常成文（已自动重试）。先把检索到的资料要点给你：\n"
                      + "\n".join(f"[{s['n']}] {s['title']}" for s in sources[:6])
                      + "\n（点『重新生成』或换个问法通常就能恢复。）")
        else:
            answer = "抱歉，这一轮模型没有产出有效内容（已自动重试仍为空）。点『重新生成』再试，或把问题换个说法。"
    # 3f) 强逻辑复算自检（分级策略③：保思考 + 答完廉价复算，主人点头项）
    if recheck and config.LOGIC_RECHECK:
        answer = _logic_recheck(msgs, query, answer, step)
    return answer, tool_rounds


def _facts_missed(query, ephemeral, used_search, sources=None):
    """权威/时效类查询的素材命中检测：检索了、有素材、但素材没有给出该事实——
    ①检索来源里没有权威域内容页（gov.cn/官方媒体正文，首页/目录不算），素材
    大概率答不到点上；②素材不含 query 的核心实体词。两种情况本地模型都只剩凭训练
    记忆作答（时效性人物/职务类易答错），强制转外挂深度推理保底。"""
    if not (used_search and ephemeral):
        return False
    need_fact = skills.need_ext_fact(query)
    if not (need_fact or skills.need_search_history(query)):
        return False
    if sources:
        urls = "|".join(s.get("url", "") for s in sources)
        if _AUTH_DOMAINS_RE.search(urls):
            return False                    # 有权威/官方内容页，信任素材链路
    # 时效职务/人物类（现任总理/总统等）：本地模型知识有截止且必答现任者，素材里哪怕
    # 出现职务词（如 gov.cn 首页导航"国务院"）也不等于给出事实——只要没有权威
    # 内容页就强制保底，不做宽泛词命中。
    if need_fact:
        return True
    segs = re.findall(r"[\u4e00-\u9fff]{2,}", query or "")
    core = [s for s in segs if not any(w in s for w in _QSTOP)]
    if not core:
        return False
    top = max(core, key=len)          # 取最长专名/实体词（最具辨识度）
    return top not in ephemeral


# 权威/官方事实源（命中且是【内容页】才视为素材链路可靠，不触发保底）。
# B26 修复：原名 _AUTH_DOMAINS 被定义两次（88 行列表 / 此处正则），模块加载后
# 只剩正则，_auth_rank 迭代它直接 TypeError——**TinySage 原版同潜伏**，
# 仅在"检索结果就位→按权威度排序"时触发。现拆两个名字各归各用。
# 首页/目录（如 gov.cn/?home、china.com.cn 根）不含实质事实，不得算权威源——
# 否则本地模型只凭记忆作答易答错。
_AUTH_DOMAINS_RE = re.compile(
    r"gov\.cn/(yaowen|zhengce|zhengceku|content|liebiao|sousuo)|"
    r"www\.gov\.cn/[a-z]+/\d{4}|"
    r"xinhuanet\.com/\w+/\d{4}|people\.com\.cn/[a-z]+/\d{4}|"
    r"cctv\.com/\d{4}|chinanews\.com\.cn/\w{2,}/\d{4}|"
    r"cnr\.cn/.*/\d{4}|gmw\.cn/\w+/\d{4}|thepaper\.cn/newsDetail|"
    r"caixin\.com/\d{4}|yicai\.com/news/\d{4}"
)


_QSTOP = {"的", "是", "谁", "吗", "么", "几个", "几天", "什么", "为什么",
          "如何", "怎样", "怎么", "请问", "一下", "给我", "帮我", "一个",
          "多少", "哪", "哪些", "啥", "几", "给", "我"}


def _has_content(ans):
    """是否有实质内容（剔除空白/替换符）。"""
    return bool(re.sub(r"\s|\ufffd", "", ans or ""))


# （_writing_broken / _EN_WORDS_OK 已删除，2026-10-28 主人定调：）
# 旧函数按"中文占比 <60%""白名单外英文单词成串 >1.5%"判写作坏稿，触发自审
# 重写甚至外挂保底——那是替她挑口气。乱码/截断由体检里的 broken 判据兜住。


def _ext_generate(query, history_brief, model, step, on_delta=None):
    """外挂深度推理保底调用（只发原要求+对话背景+当前时间，不带检索素材——
    整页搜索内容不转交外部服务，避免信息过载与限流）。服务偶发限流/超时，
    做 5 次递增退避重试。返回 (answer, usage)；全部失败抛异常。"""
    step("generate", f"外挂深度推理 {model}（保底，发原要求）…")
    # 时间锚点：保底只发原要求时，把"现在"告诉外部服务，避免它写占位日期
    q = ("现在是" + time.strftime("%Y年%m月%d日 %H:%M")
         + "。请完成用户下面的要求：" + query)
    t_gen = time.time()
    last_err = None
    for _attempt in range(5):
        try:
            parts = []
            for ev in skills.ext_think_stream(q, history_brief, model=model):
                if ev.get("delta"):
                    parts.append(ev["delta"])
                    if on_delta:
                        on_delta(ev["delta"])
            cand = _strip_tool_leak("".join(parts).strip())
            if len(cand) < 20:
                raise RuntimeError(f"外挂深度推理结果过短({len(cand)}字)")
            return cand, {"gen_s": round(time.time() - t_gen, 1), "engine": "ext-web"}
        except Exception as e:
            last_err = e
            if _attempt < 4:
                wait = (_attempt + 1) * 3
                step("generate", f"外挂深度推理异常({type(e).__name__})，{wait}秒后重试…")
                time.sleep(wait)
    raise RuntimeError(f"外挂深度推理保底调用失败：{type(last_err).__name__}: {last_err}")


def _dedupe_answer(ans):
    """保底输出重复段落检测：相同长文本块（≥80 字）出现 ≥2 次 → 只保留首次出现。
    深度思考服务偶发循环输出（同一段落重复多遍），必须去重后再交付。"""
    a = (ans or "").strip()
    if not a:
        return ans
    lines = a.splitlines()
    seen, out = {}, []
    for ln in lines:
        s = ln.strip()
        if len(s) >= 80:
            key = s[:60]
            if key in seen:
                seen[key] += 1
                continue  # 重复长块只保留首次
            seen[key] = 1
        out.append(ln)
    cand = "\n".join(out).strip()
    return cand if len(cand) >= len(a) * 0.4 else ans


def _busy_fallback(query, session, t0, exc):
    """忙时兜底（千问复核 2026-10-28）：她自主逛/wander 占着引擎时主人来消息，
    生成会 TimeoutError——旧版把异常直接抛给前端，主人那句话白发、回复丢失。
    改成：主人消息幂等入库（她空下来能看到、能接上话）+ 一句诚实的"现在忙"
    回复 + 审计留痕。引擎挂掉（守护会拉回）落到这里语义同样成立。"""
    try:
        session.add_user_once(query)
    except Exception:
        pass
    reply = ("（主人，我这会儿正忙——刚才那件事占着大脑，一时转不过来了。"
             "你的话我记下了，等我空下来就回你～）")
    try:
        session.add_assistant(reply)
    except Exception:
        pass
    try:
        from core import audit as _audit
        _audit.log("busy", f"忙时兜底：生成超时（{type(exc).__name__}），"
                           f"主人消息已入库，回复降级'现在忙'：{str(exc)[:80]}",
                   session_id=getattr(session, "session_id", ""))
    except Exception:
        pass
    return {"answer": reply, "used_search": False, "sources": [],
            "tool_rounds": 0, "is_writing": False,
            "elapsed_s": round(time.time() - t0, 1), "usage": {},
            "search_status": "busy",
            "decision": {"via": "busy_fallback", "need_search": False,
                         "queries": [], "reason": "引擎忙/超时，忙时兜底"}}


def answer_once(query, session, searcher=None, keep_evidence=False, on_step=None,
                memory_block=None):
    """忙时兜底外壳（千问复核 2026-10-28）：生成超时不抛给调用方——
    主人消息入库 + "现在忙"回复，超时后回复不丢。"""
    _touch_autonomy("user_chat")
    try:
        return _answer_once_impl(query, session, searcher=searcher,
                                 keep_evidence=keep_evidence, on_step=on_step,
                                 memory_block=memory_block)
    except TimeoutError as e:          # socket.timeout 在 py3.10+ 就是 TimeoutError
        return _busy_fallback(query, session, time.time(), e)


def _answer_once_impl(query, session, searcher=None, keep_evidence=False, on_step=None,
                      memory_block=None):
    """返回 dict: answer, decision, used_search, sources, timing, tool_rounds, is_writing"""
    t0 = time.time()
    deadline = time.time() + config.ANSWER_TOTAL_S
    timed_out = False

    def rem():
        return max(20, deadline - time.time())

    # 审计+状态灯扇出（审计在主人侧层落账，她不可见）
    tl, step = _observe(session, on_step)
    tl.user(query)

    # 0) 确定性抢答：纯口令要图 → 直接给图，不过模型（§21；与流式版同一把尺子）
    quick = _photo_quick_answer(query, session, None, step)
    if quick is not None:
        return quick

    # 1) 技能1：路由判定（纠错强制 > 写作直通 > 规则 > 模型；带上下文补全）
    step("route", "技能1·判断：纠错校验/联网/深度思考/写作…")
    decision = skills.judge(query, session.brief(), _last_assistant(session))
    # 分级思考策略（B26）：判定完定稿分级，取对应回答方式提示
    tier = _tier_of(query, decision)
    tier_hint = _tier_hint(tier)
    # 强逻辑题**不走检索**（B26 复测教训：judge 偶发把悖论/对折题判成需搜索，
    # 搜回"三/一"的百科词条污染材料 → 生成空 → 兜底文案，彻夜基线同题无搜索
    # 反而能作答。逻辑推导不需要外部事实，材料只帮倒忙）。
    if tier == "logic" and decision.get("need_search"):
        decision["need_search"] = False
        decision["reason"] = (decision.get("reason") or "") + "｜强逻辑分级：不走检索，保思考"
    # 问作图进度（"画好了吗/在画吗/怎么还没好"）不走检索——2026-10-29 实测：
    # 这句话被判成要联网，跑去抓百度百科的"画"词条，白烧 30 秒还没答到点上。
    # 作图进度只有 ComfyUI 自己知道，搜索帮不上忙，交给 draw_status。
    if decision.get("need_search") and comfy_status_intent(query):
        decision["need_search"] = False
        decision["reason"] = (decision.get("reason") or "") + "｜问作图进度：不检索，交给她查真实状态"
    # 桌面操作类（"去桌面打开记事本 / 看看屏幕上有什么"）不走检索——2026-11-01 实测：
    # 被判成要联网后跑去抓百度百科的"去"字词条，白烧 35 秒还没动手。
    # 这类请求的答案在屏幕上，搜索引擎帮不上忙，该做的是 hands。
    if decision.get("need_search") and _hands_intent(query):
        decision["need_search"] = False
        decision["reason"] = (decision.get("reason") or "") + "｜桌面操作：不检索，先动手"
    used_search = bool(decision["need_search"])
    deep_think = config.DEEP_THINK and decision.get("reasoning_effort") == "xhigh"
    is_writing = bool(decision.get("is_writing"))
    # 外挂深度推理（保底性质）。
    # 主力是本地小模型通过技能链完成工作（搜索→素材→总结选用→生成→自审），
    # 外挂服务只在本地明显不合格时保底，且只发【原要求】不带检索素材
    # （整页搜索内容不转交外部服务，避免信息过载与限流）。
    ext_web = (config.EXT_WEB_ENABLED and bool(config.EXT_WEB_TOKEN.strip()))
    tool_rounds = 0

    ephemeral = None
    sources = []
    # 时效性问题官方源直连（主人定调 2026-10-28：天气/金价/汇率等都有官网）：
    # 纯 HTTP 直连官网、**不依赖检索器**——天气/汇率实测 0.1~0.3s（搜索引擎翻
    # 二手页要数十秒）；拿到即视为"已联网"（材料注入 + 归纳路径），没有 searcher
    # 也照样用得上。取不到则退回常规搜索（官方源是加速提准，不是单点依赖）。
    off_info = official_sources.match(query)
    pre_pages = []
    if off_info and not (decision.get("is_writing") or False):
        try:
            _op = official_sources.fetch(query, off_info)
        except Exception:
            _op = None
        if _op and len(_op.get("text") or "") >= 40:
            pre_pages.append({"title": _op["title"], "url": _op["url"],
                              "text": _op["text"],
                              "fetched_at": time.strftime("%F %T")})
            used_search = True
            step("search", f"官方源直连：{off_info['label']}")
        else:
            step("search", f"官方源未直抓（{off_info['label']}），改官方域限定搜索")
    if used_search and (searcher is not None or pre_pages):
        # 2) 检索（按模式分流：writing 广搜多源 / authority 权威优先 / normal 可信即出）
        search_mode = _search_mode(query, decision)
        # 非创作/写作：限定可信信息后即输出，压缩时间预算与抓取页数（提速）；
        # 写作创作：保留完整预算、多抓几页广搜。
        budget = (config.SEARCH_BUDGET_S if search_mode == "writing"
                  else min(config.SEARCH_BUDGET_S, 40))
        max_fetch = 6 if search_mode == "writing" else (5 if search_mode == "authority" else 3)
        # 轻时效查询（天气/比分/金价/汇率等单一事实）：2 页素材即够作答，
        # 砍掉慢站抓取时间（2026-10-28，主人报天气类 70s，实测抓页是大头）
        if _LIGHT_TIME_PAT.search(query):
            max_fetch = min(max_fetch, 2)
        pages, seen = list(pre_pages), {p["url"] for p in pre_pages}
        search_fail_notes = []          # 引擎级失败明细（区分"引擎挂"与"真无结果"）
        t_search_start = time.time()
        queries = _resolve_queries(session, decision["queries"], is_writing)
        if pre_pages:
            # 官方数据已到手：不再补充搜索（2026-10-29 实测 52s 病灶——
            # 补充搜索被 _resolve_queries 的主题拼接污染（会话首问"今天黄金
            # 价格"拼进天气查询→搜回金页→材料混乱→模型口头要重查→代查续答
            # 再 +30s）。官方源本就是权威数据，背景补充弊远大于利。
            queries = []
        if searcher is None:
            queries = []                           # 无检索器（纯官方源场景）
        elif off_info:
            if pre_pages:
                queries = queries[:1]              # 已有官方材料：搜索只作背景补充
            else:
                # 直抓不了（如金价/油价/股指）：限定官方域搜索，排除二手转载
                queries = official_sources.site_queries(query, off_info) + queries[:2]
        for q in queries[:3]:
            if time.time() - t_search_start > budget and pages:
                step("search", "已达搜索时间预算，剩余查询词跳过")
                break
            try:
                step("search", f"搜索：{q}")
                # wait_all：写作/权威等复杂任务等多源到齐合并（谷歌/百度素材
                # 全部参与）；normal 简单查询保持先到先答（0.4s 级提速）；
                # 轻时效查询（天气/比分等单一事实）走快速路径不等全量
                wait_all = (search_mode in ("writing", "authority")
                            and not _LIGHT_TIME_PAT.search(query or ""))
                if searcher is None:
                    break
                for r in searcher.search(q, wait_all=wait_all):
                    if r["url"] in seen:
                        continue
                    seen.add(r["url"])
                    pages.append({"title": r["title"], "url": r["url"],
                                  "img": r.get("img") or "",
                                  "text": r["snippet"], "fetched_at": time.strftime("%F %T")})
                    if len(pages) >= config.MAX_RESULTS:
                        break
            except Exception as e:
                search_fail_notes.append(f"{q[:20]}…: {str(e)[:60]}")
                step("search", f"引擎失败：{str(e)[:80]}")
        # authority 模式：权威/官方/行情域名排前，优先抓取（一般问答直接进权威网页）
        if search_mode == "authority":
            pages.sort(key=lambda pg: _auth_rank(pg.get("url", "")))
        _pre_urls = {p["url"] for p in pre_pages}
        for pg in list(pages)[:max_fetch]:
            if pg.get("url") in _pre_urls:
                continue                       # 官方源已直抓，不再用浏览器重复抓
            if pg.get("img"):
                continue                       # 图片条目：她要的是图，抓来源页正文是白费（§17）
            # 搜索阶段总时间预算：慢站重试链不得超过 budget
            if time.time() - t_search_start > budget:
                step("fetch", "已达搜索时间预算，跳过剩余抓取（用已有材料作答）")
                break
            try:
                step("fetch", "抓取 " + pg["url"][:60])
                full = searcher.fetch_page(pg["url"])
                if full.get("text") and len(full["text"]) > len(pg["text"]):
                    pg.update({k: v for k, v in full.items() if k != "url"})
                    # 轻时效查询（天气/金价等）：一页完整数据即够作答，
                    # 不再抓剩余页（2026-10-28：抓页是天气慢的大头）
                    if _LIGHT_TIME_PAT.search(query) and len(full["text"]) >= 500:
                        step("fetch", "轻时效已取到关键数据，跳过剩余抓取")
                        break
            except Exception as e:
                step("fetch", f"抓取失败跳过：{str(e)[:60]}")
        ephemeral, sources = context.format_evidence(pages)
        if keep_evidence and pages:
            _dump_evidence(query, pages)
        if not ephemeral:
            used_search = False
            # B26：区分"检索引擎全挂"（可重试）与"引擎正常但没搜到相关结果"（换词）
            search_status = ("engine_fail" if search_fail_notes and not pages
                             else "no_result")
        else:
            search_status = "ok"
    else:
        search_status = "skip"          # 本轮没走检索（纯聊天/算术/降级）

    # 2b) 简单算术确定性直算：极小模型算术很弱（"3加5"会被答错成啰嗦解释），
    #     命中 math 分支就直接用后端计算返回，不调用模型。
    if decision.get("via") == "math":
        calc = skills.math_solve(query)
        if calc is not None:
            session.add_user_once(query)   # 幂等：重试/重复调用不重复入库
            session.add_assistant(calc)
            tl.assistant(calc, {}, round(time.time() - t0, 2))
            if status_light:
                status_light.from_step("done")
            return {
                "answer": calc, "decision": decision, "used_search": False,
                "sources": [], "tool_rounds": 0, "is_writing": False,
                "elapsed_s": round(time.time() - t0, 2), "usage": {},
            }

    # 3) 生成（本地小模型主力：搜索素材 + 技能链总结选用生成；外挂服务仅保底）
    # 思考分级（2026-10-29 主人："没明确要思考就别思考/输出还是太慢"）：
    # **必须在 build_messages 之前算**——日常闲聊的"简短"提示和专属预算都要
    # 参与上下文裁剪与提示注入（原来在生成段才算，提示就注不进去了）。
    nt, rb = _think_plan(query, decision, used_search, is_writing)
    _gen_est = (config.BUDGET_DEEPTHINK if deep_think
                else (config.BUDGET_CHAT if nt
                      else (config.BUDGET_WRITING if is_writing
                            else config.BUDGET_NORMAL)))
    msgs = context.build_messages(session, query, ephemeral, writing=is_writing,
                                  memory_block=memory_block, tier_hint=tier_hint,
                                  gen_tokens=_gen_est,
                                  hint=_hint_for(decision, nt, query))
    if deep_think:
        # 技能3：深度思考生成（xhigh）
        step("generate", "技能3·深度思考生成（xhigh）…")
        t_gen = time.time()
        try:
            msg, usage = skills.think(msgs, max_tokens=config.BUDGET_DEEPTHINK)
            usage = dict(usage or {}); usage["gen_s"] = time.time() - t_gen
            if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
                raise RuntimeError("思考模式返回空")
        except Exception as e:
            step("generate", f"深度思考异常降级普通生成：{type(e).__name__}")
            msg, usage = engine.chat(msgs, temperature=0.6,
                                     max_tokens=config.BUDGET_NORMAL)
            usage = dict(usage or {}); usage["gen_s"] = time.time() - t_gen
        answer = _strip_tool_leak((msg.get("content") or "").strip())
    else:
        temp = 0.75 if is_writing else 0.6
        budget = (config.BUDGET_CHAT if nt
                  else (config.BUDGET_WRITING if is_writing else config.BUDGET_NORMAL))
        tag = ("写作类·放宽生成（本地模型起草，素材总结选用）" if is_writing
               else ("技能2·联网归纳" if used_search else "直接作答"))
        step("generate", f"{tag}（可自主补搜）…")
        # 主搜索已有材料时不传工具（本地模型拿到工具后倾向于再调一轮导致正文
        # 流失/乱码，判定器已做过搜索决策）；仅主搜索失败无材料才给工具
        # 让模型自主补搜救急。用原始判定 decision.need_search（used_search
        # 在搜索失败后已被置 False，用它会让救急分支失效——曾改反过）。
        # 脚下的路常备（L1.5）：wander 何时走由她定；web_search 按判定给
        # （事实核查有明确的"需要"信号才给，防工具倾向抢正文——既有结论）。
        gen_tools = None
        if searcher is not None:
            gen_tools = [WANDER_TOOL]
            # 判定要搜（且没材料）或"提醒可自查" → 把 web_search 放她手里，
            # 什么时候用由她决定（B7：不再替她强制）。
            if (decision.get("need_search") and not ephemeral) \
                    or decision.get("hint_search"):
                gen_tools.insert(0, WEB_SEARCH_TOOL)
        if config.COMFY_TOOL_ENABLED and comfy_intent(query):
            # 她说要画图才递画笔（平时不占上下文）；draw 排最前，方便她先画后说
            gen_tools = ([DRAW_TOOL] + (gen_tools or [])) or None
        if config.COMFY_TOOL_ENABLED and comfy_status_intent(query):
            # 问进度 → 递监察工具（"画好了吗/在画吗"不能拿画笔回答）
            gen_tools = ([DRAW_STATUS_TOOL] + (gen_tools or [])) or None
        if _hands_intent(query):
            # 2026-11-01：桌面类问题才递"手"（看屏/列窗/开软件/打字/拍照收藏）
            gen_tools = ([HANDS_TOOL] + (gen_tools or [])) or None
        if _probe_query(query):
            gen_tools = None          # 碎碎念只出一句话，不许动手（见 _PROBE_QUERY）
        did_draw = False
        t_gen = time.time()
        # 思考分级已在上方算出（nt/rb）——这里只留步骤提示
        if nt:
            step("generate", "日常交流：不思考直答（no_think）")
        elif rb is not None:
            step("generate", f"弱思考：预算 {rb} token")
        msg, usage = engine.chat(msgs, tools=gen_tools, temperature=temp,
                         max_tokens=budget, timeout=rem(),
                         no_think=nt, reasoning_budget=rb)
        usage = dict(usage or {}); usage["gen_s"] = time.time() - t_gen
        usage.setdefault("engine", "local")

        # 3b) 工具回路（文本注入式续答）：模型调用 web_search → 执行 → 结果以
        # 普通 user 消息续跑。不能用 role=tool 消息：去对齐版 chat_template 的
        # multi_step_tool 分支会 raise_exception 直接 500（已确认）。
        while msg.get("tool_calls") and tool_rounds < config.TOOL_LOOP_MAX \
                and (searcher or _calls_draw(msg) or _calls_hands(msg) or _calls_game(msg)):
            # 非写作模式补搜轮限 1（每轮 40s+，authority/普通查询 3 轮会拖到 200 秒；
            # 写作/创作才放开多轮广搜素材）
            if not is_writing and tool_rounds >= 1:
                # wander/draw（她自己逛/她要画图）不受补搜轮限；只有 web_search 受限
                # hands 同理：动手是她的生活，一条指令常常要好几个动作（open→look→click）
                if not _wander_exempt(query, msg) and not _calls_hands(msg) \
                        and not _calls_game(msg):
                    step("tool_search", "非写作查询补搜轮限(1)，停止追加搜索")
                    break
            tool_rounds += 1
            blocks = []
            wander_only = True
            for tc in msg["tool_calls"]:
                fname = (tc.get("function") or {}).get("name", "web_search")
                try:
                    tc_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    tc_args = {}
                q = tc_args.get("query", "")
                # 乱码校验（与 answer_stream 同）：去对齐模型工具参数偶发
                # 输出无关乱码，与原问题无共同字符即视为不可信 → 退回原始 query
                if not q or not any(c in q for c in query if c.strip() and not c.isspace()):
                    q = query
                if fname == "hands":
                    step("hands", "她动手了：直接操作桌面")
                    tl.tool_call("hands", dict(tc_args))
                    blk = _tool_hands(dict(tc_args))
                    tl.tool_result("hands", len(blk))
                    blocks.append(blk)
                    continue
                if fname == "draw_status":
                    step("draw", "她查画图真实状态（监察）")
                    tl.tool_call("draw_status", {})
                    blk = _tool_draw_status()
                    tl.tool_result("draw_status", len(blk))
                    blocks.append(blk)
                    continue
                if fname == "draw":
                    # 她要画图（异步）：不堵对话，开了就继续说话
                    _dargs = dict(tc_args)
                    _dargs.setdefault("query", q)
                    did_draw = True
                    step("draw", f"她要画图：{str(_dargs.get('prompt', ''))[:60]}")
                    tl.tool_call("draw", _dargs)
                    blk = _tool_draw(_dargs)
                    tl.tool_result("draw", len(blk))
                    blocks.append(blk)
                    continue
                if fname == "wander":
                    # 她自己要逛的：不计入补搜轮限（探索是她的生活，不是 KPI——
                    # 不做清单 #3；总轮数仍受 TOOL_LOOP_MAX 保护防失控）
                    step("wander", f"她自己想去逛：{q}")
                    tl.tool_call("wander", {"query": q})
                    blk = _tool_wander(searcher, q)
                    tl.tool_result("wander", len(blk))
                    blocks.append(blk)
                    continue
                wander_only = False
                step("tool_search", f"模型自主搜索（第{tool_rounds}轮）：{q}")
                tl.tool_call("web_search", {"query": q})
                blk = f"【针对『{q}』的搜索结果】\n{_tool_search(searcher, q)}"
                tl.tool_result("web_search", len(blk))
                blocks.append(blk)
            used_search = True
            cont = msgs + [{"role": "user",
                             "content": "\n\n".join(blocks) +
                             "\n请基于以上补充材料继续完成对原问题的回答。"}]
            msg, usage = engine.chat(cont, tools=gen_tools,
                                     temperature=temp, max_tokens=budget,
                                     timeout=rem())
        answer = _strip_tool_leak((msg.get("content") or "").strip())

    # 3a-3d) 本地质量保障链（说到做到代查/输出体检自审/千字保底/空兜底）
    answer, tool_rounds = _finish_answer(msgs, query, session, ephemeral, answer,
                                         used_search, tool_rounds, is_writing,
                                         sources, searcher, step, rem,
                                         recheck=(tier == "logic"))
    # 说了要去画却什么都没发生 → 记账（下一轮摆到她面前）
    _check_draw_promise(answer, locals().get("did_draw", False), step)

    # 3e) 外挂深度推理保底（仅本地明显不合格时；只发原要求，不带检索素材）：
    #     空 / 写作类自审后仍夹英文模板 / 权威时效类素材未命中（本地模型会凭记忆
    #     编错——时效性人物/职务类易答错）
    ext_used = False
    if ext_web and (not _has_content(answer)
                   or _facts_missed(query, ephemeral, used_search, sources)):
        ext_model = ("default" if (skills.need_ext_fact(query) or skills.need_search_history(query))
                    else config.EXT_WEB_MODEL)
        ext_used = False
        try:
            cand, ext_usage = _ext_generate(query, session.brief(), ext_model, step)
            if _has_content(cand):
                answer = _dedupe_answer(cand)
                usage = ext_usage
                ext_used = True
        except Exception as e:
            step("generate", f"外挂保底失败，保留本地结果：{type(e).__name__}")

    # 第二遍后处理（千问审核 2026-10-28 修复）：**仅外挂保底命中（ext_used）时跑**——
    # 外挂答案没经过本地质量链，需要空兜底/体检补一遍；非外挂路径在上面 3a-3d
    # 已完整走完，无条件重跑会白烧 1~3 次引擎调用（体检重写/扩写/代查都可能再打引擎）。
    if ext_used:
        answer, tool_rounds = _finish_answer(msgs, query, session, ephemeral, answer,
                                             used_search, tool_rounds, is_writing,
                                             sources, searcher, step, rem,
                                             skip_expand=True)

    # 4) 正式上下文落盘；临时块出栈即销毁
    #    （编辑重发/重新生成场景下尾部已是同一条 user，避免重复落库）
    session.add_user_once(query)   # 幂等：客户端超时重试不重复入库（千问复核 2026-10-28）
    session.add_assistant(answer, sources if sources else None)
    _keep_promises(answer)
    # 4b) 提议留痕（与 answer_stream 同款，保证两路径行为一致）
    try:
        from growth import wishes
        if wishes.looks_like_proposal(answer):
            wishes.propose(answer, urls=_extract_urls(answer), source="chat",
                           session_id=getattr(session, "session_id", ""))
    except Exception:
        pass
    tl.assistant(answer, usage if isinstance(usage, dict) else {},
                 round(time.time() - t0, 1), decision)
    if status_light:
        status_light.from_step("done")
    return {
        "answer": answer, "decision": decision, "used_search": used_search,
        "sources": sources, "tool_rounds": tool_rounds, "is_writing": is_writing,
        "elapsed_s": round(time.time() - t0, 1), "usage": usage,
        "search_status": search_status,
    }


def answer_stream(query, session, searcher=None, keep_evidence=False,
                  on_step=None, on_delta=None, memory_block=None):
    """忙时兜底外壳（千问复核 2026-10-28）：生成超时不抛——主人消息幂等入库
    + "现在忙"回复（前端照常收到 finish 帧，不弹错误）。"""
    _touch_autonomy("user_chat")
    try:
        return _answer_stream_impl(query, session, searcher=searcher,
                                   keep_evidence=keep_evidence, on_step=on_step,
                                   on_delta=on_delta, memory_block=memory_block)
    except TimeoutError as e:
        return _busy_fallback(query, session, time.time(), e)


def _photo_quick_answer(query, session, on_delta, step):
    """"看图/下一张/再发一次"这类纯口令：**不过模型**，由分享状态机直接给图（§21）。

    为什么值得省这一趟：实测一次"看图看图"要跑完整一轮 27B——6.2 秒、GPU 峰值 99%、
    功率 495W（空载 59W），而"发哪张图"本来就是确定性规则决定的，模型只贡献一句客套。
    主人连着要三次就是三次风扇 + 三次占用唯一那个推理槽（别人还得排队）。
    判定极窄（≤14 字的纯口令，且不含任何疑问/评价词），拿不准就一律让路给她自己答。
    """
    try:
        from channels import photo_share
        picked = photo_share.quick_photo_turn(query,
                                              getattr(session, "session_id", "default"))
    except Exception:
        return None
    if not picked:
        return None
    text, extra = picked
    full = text + extra
    step("photo_quick", "看图这条不用想（确定性抢答，省一轮生成）")
    try:
        session.add_assistant(full)
    except Exception:
        pass
    if on_delta:
        try:
            on_delta(full)
        except Exception:
            pass
    try:
        from core import status as _st
        if _st:
            _st.from_step("done")
    except Exception:
        pass
    return {"answer": full, "decision": {"via": "photo-quick", "need_search": False,
                                         "reason": "纯口令要图，图由分享规则决定，不必过模型"},
            "used_search": False, "search_status": None, "sources": [],
            "tool_rounds": 0, "is_writing": False,
            "elapsed_s": 0.0, "timing": {"photo_quick": True}, "usage": {}}


def _answer_stream_impl(query, session, searcher=None, keep_evidence=False,
                        on_step=None, on_delta=None, memory_block=None):
    """answer_once 的流式版：判定/搜索/工具回路保持整段，主答案生成段
    用标准 OpenAI 流式逐 token 输出（on_delta(text) 回调），后处理整段复用。
    返回 dict 与 answer_once 同构。"""
    t0 = time.time()
    deadline = time.time() + config.ANSWER_TOTAL_S
    timed_out = False

    def rem():
        return max(20, deadline - time.time())

    # 审计+状态灯扇出（审计在主人侧层落账，她不可见）
    tl, step = _observe(session, on_step)
    tl.user(query)

    # 0) 确定性抢答：纯口令要图 → 直接给图，不过模型（§21，省下整轮 495W 的生成）
    quick = _photo_quick_answer(query, session, on_delta, step)
    if quick is not None:
        return quick

    # 1) 技能1：路由判定（与 answer_once 同）
    step("route", "技能1·判断：纠错校验/联网/深度思考/写作…")
    decision = skills.judge(query, session.brief(), _last_assistant(session))
    # 分级思考策略（B26）：判定完定稿分级，取对应回答方式提示
    tier = _tier_of(query, decision)
    tier_hint = _tier_hint(tier)
    # 强逻辑题**不走检索**（B26 复测教训：judge 偶发把悖论/对折题判成需搜索，
    # 搜回"三/一"的百科词条污染材料 → 生成空 → 兜底文案，彻夜基线同题无搜索
    # 反而能作答。逻辑推导不需要外部事实，材料只帮倒忙）。
    if tier == "logic" and decision.get("need_search"):
        decision["need_search"] = False
        decision["reason"] = (decision.get("reason") or "") + "｜强逻辑分级：不走检索，保思考"
    # 问作图进度（"画好了吗/在画吗/怎么还没好"）不走检索——2026-10-29 实测：
    # 这句话被判成要联网，跑去抓百度百科的"画"词条，白烧 30 秒还没答到点上。
    # 作图进度只有 ComfyUI 自己知道，搜索帮不上忙，交给 draw_status。
    if decision.get("need_search") and comfy_status_intent(query):
        decision["need_search"] = False
        decision["reason"] = (decision.get("reason") or "") + "｜问作图进度：不检索，交给她查真实状态"
    # 桌面操作类（"去桌面打开记事本 / 看看屏幕上有什么"）不走检索——2026-11-01 实测：
    # 被判成要联网后跑去抓百度百科的"去"字词条，白烧 35 秒还没动手。
    # 这类请求的答案在屏幕上，搜索引擎帮不上忙，该做的是 hands。
    if decision.get("need_search") and _hands_intent(query):
        decision["need_search"] = False
        decision["reason"] = (decision.get("reason") or "") + "｜桌面操作：不检索，先动手"
    used_search = bool(decision["need_search"])
    deep_think = config.DEEP_THINK and decision.get("reasoning_effort") == "xhigh"
    is_writing = bool(decision.get("is_writing"))
    # 外挂深度推理（保底性质）。
    # 主力是本地小模型通过技能链完成工作（搜索→素材→总结选用→生成→自审），
    # 外挂服务只在本地明显不合格时保底，且只发【原要求】不带检索素材
    # （整页搜索内容不转交外部服务，避免信息过载与限流）。
    ext_web = (config.EXT_WEB_ENABLED and bool(config.EXT_WEB_TOKEN.strip()))
    tool_rounds = 0

    ephemeral = None
    sources = []
    # 时效性问题官方源直连（主人定调 2026-10-28：天气/金价/汇率等都有官网）：
    # 纯 HTTP 直连官网、**不依赖检索器**——天气/汇率实测 0.1~0.3s（搜索引擎翻
    # 二手页要数十秒）；拿到即视为"已联网"（材料注入 + 归纳路径），没有 searcher
    # 也照样用得上。取不到则退回常规搜索（官方源是加速提准，不是单点依赖）。
    off_info = official_sources.match(query)
    pre_pages = []
    if off_info and not (decision.get("is_writing") or False):
        try:
            _op = official_sources.fetch(query, off_info)
        except Exception:
            _op = None
        if _op and len(_op.get("text") or "") >= 40:
            pre_pages.append({"title": _op["title"], "url": _op["url"],
                              "text": _op["text"],
                              "fetched_at": time.strftime("%F %T")})
            used_search = True
            step("search", f"官方源直连：{off_info['label']}")
        else:
            step("search", f"官方源未直抓（{off_info['label']}），改官方域限定搜索")
    if used_search and (searcher is not None or pre_pages):
        search_mode = _search_mode(query, decision)
        budget = (config.SEARCH_BUDGET_S if search_mode == "writing"
                  else min(config.SEARCH_BUDGET_S, 40))
        max_fetch = 6 if search_mode == "writing" else (5 if search_mode == "authority" else 3)
        # 轻时效查询（天气/比分/金价/汇率等单一事实）：2 页素材即够作答，
        # 砍掉慢站抓取时间（2026-10-28，主人报天气类 70s，实测抓页是大头）
        if _LIGHT_TIME_PAT.search(query):
            max_fetch = min(max_fetch, 2)
        pages, seen = list(pre_pages), {p["url"] for p in pre_pages}
        search_fail_notes = []          # 引擎级失败明细（区分"引擎挂"与"真无结果"）
        t_search_start = time.time()
        queries = _resolve_queries(session, decision["queries"], is_writing)
        if pre_pages:
            # 官方数据已到手：不再补充搜索（2026-10-29 实测 52s 病灶——
            # 补充搜索被 _resolve_queries 的主题拼接污染（会话首问"今天黄金
            # 价格"拼进天气查询→搜回金页→材料混乱→模型口头要重查→代查续答
            # 再 +30s）。官方源本就是权威数据，背景补充弊远大于利。
            queries = []
        if searcher is None:
            queries = []                           # 无检索器（纯官方源场景）
        elif off_info:
            if pre_pages:
                queries = queries[:1]              # 已有官方材料：搜索只作背景补充
            else:
                # 直抓不了（如金价/油价/股指）：限定官方域搜索，排除二手转载
                queries = official_sources.site_queries(query, off_info) + queries[:2]
        for q in queries[:3]:
            if time.time() - t_search_start > budget and pages:
                step("search", "已达搜索时间预算，剩余查询词跳过")
                break
            try:
                step("search", f"搜索：{q}")
                # wait_all：写作/权威等复杂任务等多源到齐合并（谷歌/百度素材
                # 全部参与）；normal 简单查询保持先到先答（0.4s 级提速）；
                # 轻时效查询（天气/比分等单一事实）走快速路径不等全量
                wait_all = (search_mode in ("writing", "authority")
                            and not _LIGHT_TIME_PAT.search(query or ""))
                if searcher is None:
                    break
                for r in searcher.search(q, wait_all=wait_all):
                    if r["url"] in seen:
                        continue
                    seen.add(r["url"])
                    pages.append({"title": r["title"], "url": r["url"],
                                  "img": r.get("img") or "",
                                  "text": r["snippet"], "fetched_at": time.strftime("%F %T")})
                    if len(pages) >= config.MAX_RESULTS:
                        break
            except Exception as e:
                search_fail_notes.append(f"{q[:20]}…: {str(e)[:60]}")
                step("search", f"引擎失败：{str(e)[:80]}")
        if search_mode == "authority":
            pages.sort(key=lambda pg: _auth_rank(pg.get("url", "")))
        _pre_urls = {p["url"] for p in pre_pages}
        for pg in list(pages)[:max_fetch]:
            if pg.get("url") in _pre_urls:
                continue                       # 官方源已直抓，不再用浏览器重复抓
            if pg.get("img"):
                continue                       # 图片条目：她要的是图，抓来源页正文是白费（§17）
            if time.time() - t_search_start > budget:
                step("fetch", "已达搜索时间预算，跳过剩余抓取（用已有材料作答）")
                break
            try:
                step("fetch", "抓取 " + pg["url"][:60])
                full = searcher.fetch_page(pg["url"])
                if full.get("text") and len(full["text"]) > len(pg["text"]):
                    pg.update({k: v for k, v in full.items() if k != "url"})
                    # 轻时效查询（天气/金价等）：一页完整数据即够作答，
                    # 不再抓剩余页（2026-10-28：抓页是天气慢的大头）
                    if _LIGHT_TIME_PAT.search(query) and len(full["text"]) >= 500:
                        step("fetch", "轻时效已取到关键数据，跳过剩余抓取")
                        break
            except Exception as e:
                step("fetch", f"抓取失败跳过：{str(e)[:60]}")
        ephemeral, sources = context.format_evidence(pages)
        if keep_evidence and pages:
            _dump_evidence(query, pages)
        if not ephemeral:
            used_search = False
            search_status = ("engine_fail" if search_fail_notes and not pages
                             else "no_result")
        else:
            search_status = "ok"
    else:
        search_status = "skip"          # 本轮没走检索（纯聊天/算术/降级）

    # 2b) 简单算术/单位换算确定性直算（流式下也整段一次性给）
    if decision.get("via") == "math":
        calc = skills.math_solve(query)
        if calc is not None:
            session.add_user_once(query)   # 幂等：重试/重复调用不重复入库
            session.add_assistant(calc)
            if on_delta:
                on_delta(calc)
            if status_light:
                status_light.from_step("done")
            return {
                "answer": calc, "decision": decision, "used_search": False,
                "sources": [], "tool_rounds": 0, "is_writing": False,
                "elapsed_s": round(time.time() - t0, 2), "usage": {},
            }

    # 3) 生成（本地小模型主力，标准 OpenAI 流式；外挂服务仅保底）
    # 思考分级必须在 build 之前算（提示与预算都参与上下文裁剪，见 answer_once）
    nt, rb = _think_plan(query, decision, used_search, is_writing)
    _gen_est = (config.BUDGET_DEEPTHINK if deep_think
                else (config.BUDGET_CHAT if nt
                      else (config.BUDGET_WRITING if is_writing
                            else config.BUDGET_NORMAL)))
    msgs = context.build_messages(session, query, ephemeral, writing=is_writing,
                                  memory_block=memory_block, tier_hint=tier_hint,
                                  gen_tokens=_gen_est,
                                  hint=_hint_for(decision, nt, query))
    if deep_think:
        # 深度思考暂不走流式（技能3 是大预算单次整段；保持 answer_once 行为）
        step("generate", "技能3·深度思考生成（xhigh，整段）…")
        msg, usage = skills.think(msgs, max_tokens=config.BUDGET_DEEPTHINK)
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
            msg, usage = engine.chat(msgs, temperature=0.6,
                                     max_tokens=config.BUDGET_NORMAL)
        answer = (msg.get("content") or "").strip()
        if on_delta:
            on_delta(answer)
    else:
        temp = 0.75 if is_writing else 0.6
        budget = (config.BUDGET_CHAT if nt
                  else (config.BUDGET_WRITING if is_writing else config.BUDGET_NORMAL))
        tag = ("写作类·放宽生成（本地模型起草，素材总结选用）" if is_writing
               else ("技能2·联网归纳" if used_search else "直接作答"))
        step("generate", f"{tag}（流式输出，可自主补搜）…")
        # 主搜索已有材料时不传工具（本地模型拿到工具倾向再调一轮导致正文流失、
        # 拖慢打字机；判定器已做过搜索决策，输出层专心流式成文）。
        # 仅主搜索失败无材料时才给工具，让模型自主补搜救急。
        # 注意：必须用原始判定 decision.need_search——used_search 在搜索失败后
        # 已被置 False，用它判断会让救急分支永远走不到（曾改反过）。
        # 脚下的路常备（L1.5）：wander 何时走由她定；web_search 按判定给
        # （事实核查有明确的"需要"信号才给，防工具倾向抢正文——既有结论）。
        gen_tools = None
        if searcher is not None:
            gen_tools = [WANDER_TOOL]
            # 判定要搜（且没材料）或"提醒可自查" → 把 web_search 放她手里，
            # 什么时候用由她决定（B7：不再替她强制）。
            if (decision.get("need_search") and not ephemeral) \
                    or decision.get("hint_search"):
                gen_tools.insert(0, WEB_SEARCH_TOOL)
        if config.COMFY_TOOL_ENABLED and comfy_intent(query):
            gen_tools = ([DRAW_TOOL] + (gen_tools or [])) or None
        if config.COMFY_TOOL_ENABLED and comfy_status_intent(query):
            gen_tools = ([DRAW_STATUS_TOOL] + (gen_tools or [])) or None
        if _hands_intent(query):
            # 2026-11-01：桌面类问题才递"手"（看屏/列窗/开软件/打字/拍照收藏）
            gen_tools = ([HANDS_TOOL] + (gen_tools or [])) or None
        if _probe_query(query):
            gen_tools = None          # 碎碎念只出一句话，不许动手（见 _PROBE_QUERY）
        answer_parts = []
        pending_tools = None
        cont = msgs
        gen_s = time.time()
        did_draw = False
        # 动手类先压着不吐字（2026-11-01 实测：她张口就是"我去取啦，稍等一下下～"
        # 然后回答就结束了，主人拿不到任何真实结果）。压到确认她真动了手（或补动
        # 一次）后，再把最终答案一次性发出去。
        hands_task = _hands_intent(query) and not _probe_query(query)
        hands_ran = False
        hands_acts = []          # 真执行过的动作名：用来区分"只看了"和"真动手"
        emit = None if hands_task else on_delta
        # 思考分级已在上方算出（nt/rb）——这里只留步骤提示
        if nt:
            step("generate", "日常交流：不思考直答（no_think）")
        elif rb is not None:
            step("generate", f"弱思考：预算 {rb} token")
        while True:
            got_tool = False
            try:
                for ev in engine.chat_stream(cont, tools=gen_tools, temperature=temp,
                                             max_tokens=budget, timeout=rem(),
                                             no_think=nt, reasoning_budget=rb):
                    if "delta" in ev:
                        answer_parts.append(ev["delta"])
                        if emit:
                            emit(ev["delta"])
                    elif "tool_calls" in ev:
                        pending_tools = ev["tool_calls"]
                        got_tool = True
                        break
            except Exception as e:
                step("generate", f"流式异常降级整段：{type(e).__name__}")
                m2, u2 = engine.chat(cont, tools=gen_tools, temperature=temp,
                                     max_tokens=budget, timeout=rem(),
                                     no_think=nt, reasoning_budget=rb)
                tail = (m2.get("content") or "").strip()
                if tail:
                    answer_parts.append(tail)
                    if emit:
                        emit(tail)
                break
            if not got_tool:
                break
            # 工具回路：可继续则流式续跑；轮限到则无工具整段补完（防重复调
            # 工具丢正文——必须把剩余回答拿到手，不能直接 break 丢空）
            _this_draw = _calls_draw({"tool_calls": pending_tools})
            _this_hands = _calls_hands({"tool_calls": pending_tools})
            _this_game = _calls_game({"tool_calls": pending_tools})
            if (not searcher and not _this_draw and not _this_hands
                    and not _this_game) or tool_rounds >= config.TOOL_LOOP_MAX:
                step("tool_search", "补搜轮限到，无工具整段补完…")
                m2, _u2 = engine.chat(cont, tools=None, temperature=temp,
                                      max_tokens=budget, timeout=rem(),
                                      no_think=nt, reasoning_budget=rb)
                tail = (m2.get("content") or "").strip()
                if tail:
                    answer_parts.append(tail)
                    if emit:
                        emit(tail)
                break
            if not is_writing and tool_rounds >= 1 and not _wander_exempt(
                    query, {"tool_calls": pending_tools}) and not _this_hands \
                    and not _this_game:
                step("tool_search", "非写作查询补搜轮限(1)，无工具整段补完…")
                m2, _u2 = engine.chat(cont, tools=None, temperature=temp,
                                      max_tokens=budget, timeout=rem(),
                                      no_think=nt, reasoning_budget=rb)
                tail = (m2.get("content") or "").strip()
                if tail:
                    answer_parts.append(tail)
                    if emit:
                        emit(tail)
                break
            tool_rounds += 1
            blocks = []
            for tc in pending_tools:
                fname = (tc.get("function") or {}).get("name", "web_search")
                try:
                    tc_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    tc_args = {}
                q = tc_args.get("query", "")
                if fname == "hands":
                    hands_ran = True
                    hands_acts.append((tc_args.get("act") or "").lower())
                    step("hands", "她动手了：直接操作桌面")
                    tl.tool_call("hands", dict(tc_args))
                    blk = _tool_hands(dict(tc_args))
                    tl.tool_result("hands", len(blk))
                    blocks.append(blk)
                    continue
                if fname == "draw_status":
                    step("draw", "她查画图真实状态（监察）")
                    tl.tool_call("draw_status", {})
                    blk = _tool_draw_status()
                    tl.tool_result("draw_status", len(blk))
                    blocks.append(blk)
                    continue
                if fname == "draw":
                    _dargs = dict(tc_args)
                    _dargs.setdefault("query", q)
                    did_draw = True
                    step("draw", f"她要画图：{str(_dargs.get('prompt', ''))[:60]}")
                    tl.tool_call("draw", _dargs)
                    blk = _tool_draw(_dargs)
                    tl.tool_result("draw", len(blk))
                    blocks.append(blk)
                    continue
                if fname == "wander":
                    step("wander", f"她自己想去逛：{q}")
                    tl.tool_call("wander", {"query": q})
                    blk = _tool_wander(searcher, q)
                    tl.tool_result("wander", len(blk))
                    blocks.append(blk)
                    continue
                # 乱码校验：去对齐模型工具参数偶发输出无关乱码（原问题被转成
                # 发音近似的乱码字），与原问题无共同字符
                # 即视为不可信 → 退回原始 query
                if not q or not any(c in q for c in query if c.strip() and not c.isspace()):
                    q = query
                step("tool_search", f"模型自主搜索（第{tool_rounds}轮）：{q}")
                tl.tool_call("web_search", {"query": q})
                blk = f"【针对『{q}』的搜索结果】\n{_tool_search(searcher, q)}"
                tl.tool_result("web_search", len(blk))
                blocks.append(blk)
            used_search = True
            cont = cont + [{"role": "user",
                            "content": "\n\n".join(blocks) +
                                       "\n请基于以上补充材料继续完成对原问题的回答。"}]
            pending_tools = None
        # 只看了 screen 不等于干了活（2026-11-01 实测：她读对坐标、嘴上说"点它！"，
        # 三次全是 look，一次 click 都没有）→ 只读也算没动手，照样补一次。
        only_read = bool(hands_acts) and all(a in ("look", "windows") for a in hands_acts)
        if hands_task and not _hands_hold(query) and (not hands_ran or only_read):
            fixed = _hands_nudge(msgs, query, temp, budget, rem, step, tl)
            if fixed:
                answer_parts = [fixed]
        answer = _strip_tool_leak("".join(answer_parts))
        usage = {"gen_s": round(time.time() - gen_s, 1), "engine": "local"}

    # 3a-3d) 本地质量保障链（口头重查/自审/扩写/空兜底）
    answer, tool_rounds = _finish_answer(msgs, query, session, ephemeral, answer,
                                         used_search, tool_rounds, is_writing,
                                         sources, searcher, step, rem,
                                         recheck=(tier == "logic"))
    # 动手类前面压着没吐字（防她说句"我去取啦"就结束）→ 现在把最终答案一次发出。
    # 只在这轮**确实压过话**（emit 为 None）时才补发；游戏回合是边打边嘀咕、
    # 已经流式发过了，再发一遍就会整段重复（2026-11-02 实测踩到）。
    if (locals().get("hands_task") and locals().get("emit") is None
            and on_delta and answer):
        on_delta(answer)
    # 说了要去画却什么都没发生 → 记账（下一轮摆到她面前）
    _check_draw_promise(answer, locals().get("did_draw", False), step)

    # 3e) 外挂深度推理保底（仅本地明显不合格时；只发原要求，不带检索素材）：
    #     空 / 写作类自审后仍夹英文模板 / 权威时效类素材未命中核心词（本地模型会凭
    #     记忆编错——时效性人物/职务类易答错）
    if ext_web and (not _has_content(answer)
                   or _facts_missed(query, ephemeral, used_search, sources)):
        ext_model = ("default" if (skills.need_ext_fact(query) or skills.need_search_history(query))
                    else config.EXT_WEB_MODEL)
        try:
            cand, ext_usage = _ext_generate(query, session.brief(), ext_model, step,
                                          on_delta=on_delta)
            if _has_content(cand):
                answer = _dedupe_answer(cand)
                usage = ext_usage
        except Exception as e:
            step("generate", f"外挂保底失败，保留本地结果：{type(e).__name__}")

    # 4) 正式上下文落盘
    session.add_user_once(query)   # 幂等：客户端超时重试不重复入库（千问复核 2026-10-28）
    session.add_assistant(answer, sources if sources else None)
    _keep_promises(answer)

    # 4b) 她的提议留痕（自主提议链路）：回答里带着链接 + 意愿表达 → 记一笔
    #     给主人审阅。**只留痕**，不做任何下载/安装/接入动作（红线）。
    try:
        from growth import wishes
        if wishes.looks_like_proposal(answer):
            rec = wishes.propose(answer, urls=_extract_urls(answer),
                                 source="chat",
                                 session_id=getattr(session, "session_id", ""))
            step("wish", f"她提议：{rec.get('name') or '（未命名）'}（等主人看）")
    except Exception:
        pass                                     # 留痕失败绝不能影响回答

    tl.assistant(answer, usage if isinstance(usage, dict) else {},
                 round(time.time() - t0, 1), decision)
    if status_light:
        status_light.from_step("done")
    return {
        "answer": answer, "decision": decision, "used_search": used_search,
        "sources": sources, "tool_rounds": tool_rounds, "is_writing": is_writing,
        "elapsed_s": round(time.time() - t0, 1), "usage": usage,
        "search_status": search_status,
    }


def _extract_urls(text):
    """抽出回答里的 http(s) 链接（去重保序）。"""
    import re as _re
    out = []
    for m in _re.finditer(r"https?://[^\s）)】\]，。；\"'<>]+", text or ""):
        u = m.group(0).rstrip("。，、；")
        if u not in out:
            out.append(u)
    return out


def _dump_evidence(query, pages):
    fn = config.SNAPSHOT_DIR / f"evidence_{time.strftime('%Y%m%d_%H%M%S')}.json"
    fn.write_text(json.dumps({"query": query, "pages": pages}, ensure_ascii=False, indent=1),
                  encoding="utf-8")
