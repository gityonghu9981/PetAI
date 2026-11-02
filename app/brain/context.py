"""正式上下文与临时上下文的隔离管理 + 多对话支持。

用户核心约束：
- 只有【用户输入 + 模型输出】才是正式上下文：检索结果/网页正文绝不写入对话记录，
  生成用完后随函数栈销毁（内存态，不落盘）；
- 正式上下文要够长：窗口大小由设置 budget_history 控制（默认 12000 token），
  按完整消息边界裁剪，长对话不轻易丢历史；
- 默认不留任何检索痕迹；--keep-evidence 仅调试时把快照写 snapshots/。

多对话（用户要求：新建/删除/编辑/重新生成）：
- data/index.json      会话索引 {id: {title, created, updated, n_msgs}}
- data/conv_{id}.jsonl 每个会话的正式消息
- 编辑=截断该消息之后全部记录再重发；重新生成=截断最后一条回答再重发。
"""
import json
import re
import time
from core import config

INDEX_LOCK = __import__("threading").Lock()

def _idx_path():
    return config.DATA_DIR / "index.json"

def _load_index():
    p = _idx_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}

def _save_index(idx):
    _idx_path().write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")

def list_sessions(include_hidden=False):
    """会话索引。hidden 的测试/诊断会话默认不出现（数据保留可追溯）。

    hidden 标记由 tools/clean_test_sessions.py 打（night_*/retest_*/stress_*/
    diag_*/probe/smoke 等测试会话）——不删原始 conv 文件，只是不进正式视图
    （外部代码审核前的污染清理，2026-10-27）。
    """
    with INDEX_LOCK:
        idx = _load_index()
    if include_hidden:
        return idx
    return {k: v for k, v in idx.items() if not v.get("hidden")}

def new_session(title=None):
    sid = time.strftime("%Y%m%d-%H%M%S-") + str(int(time.time() * 1000) % 1000)
    with INDEX_LOCK:
        idx = _load_index()
        idx[sid] = {"title": title or "新对话", "created": time.time(),
                    "updated": time.time(), "n_msgs": 0}
        _save_index(idx)
    return sid

def delete_session(sid):
    with INDEX_LOCK:
        idx = _load_index()
        idx.pop(sid, None)
        _save_index(idx)
    p = config.DATA_DIR / f"conv_{_safe(sid)}.jsonl"
    if p.exists():
        p.unlink()   # 对话连同文件彻底删除（用户"删除对话"意图明确）

def rename_session(sid, title):
    with INDEX_LOCK:
        idx = _load_index()
        if sid in idx:
            idx[sid]["title"] = (title or "新对话")[:60]
            _save_index(idx)

class Session:
    # 检索来源默认不落盘（用户"不留痕迹"）；需要时 set_persist_sources(True)
    persist_sources = False

    def __init__(self, session_id="default"):
        self.session_id = session_id
        self.path = config.DATA_DIR / f"conv_{_safe(session_id)}.jsonl"
        self.formal = []
        self._load()
        self._touch_index(len(self.formal))

    def _load(self):
        self.formal = []
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self.formal.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

    def _touch_index(self, n=None):
        with INDEX_LOCK:
            idx = _load_index()
            e = idx.setdefault(self.session_id, {"title": "新对话",
                                 "created": time.time()})
            e["updated"] = time.time()
            e["n_msgs"] = len(self.formal) if n is None else n
            # 用首条用户消息自动命名
            if e["title"] == "新对话":
                first = next((m["content"] for m in self.formal if m["role"] == "user"), "")
                if first:
                    e["title"] = first[:30]
            _save_index(idx)

    def _flush(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for m in self.formal:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        self._touch_index()

    def add_user(self, content):
        self.formal.append({"role": "user", "content": content, "ts": time.time()})
        self._flush()

    def add_user_once(self, content, max_back=3):
        """幂等写入（千问复核 2026-10-28）：同一条消息近期已作为 user 入库则跳过。

        场景：生成超时/500 后客户端重试同一句话——旧版只在 formal[-1] 恰好是
        同内容 user 时去重，中间隔一条忙时兜底回复就漏。这里回看最近 max_back
        条，命中同内容 user（120s 内，防误吞主人刻意连发的相同消息）不重复写。
        返回 True=新写入，False=已存在跳过。"""
        now = time.time()
        for m in self.formal[-max_back:]:
            if (m.get("role") == "user" and (m.get("content") or "") == content
                    and now - m.get("ts", 0) <= 120):
                return False
        self.add_user(content)
        return True

    def add_assistant(self, content, sources=None):
        rec = {"role": "assistant", "content": content, "ts": time.time()}
        if sources and Session.persist_sources:
            rec["source_index"] = sources
        self.formal.append(rec)
        self._flush()

    # ---- 编辑 / 重新生成 ----
    def truncate_after(self, index):
        """删除第 index 条之后的所有消息（index 从 0 计）。"""
        if 0 <= index < len(self.formal):
            self.formal = self.formal[: index + 1]
            self._flush()

    def pop_last_assistant(self):
        """重新生成用：去掉最后一条 assistant，返回去掉前的 user 文本。"""
        while self.formal and self.formal[-1]["role"] == "assistant":
            self.formal.pop()
        self._flush()
        last_user = next((m["content"] for m in reversed(self.formal)
                          if m["role"] == "user"), None)
        return last_user

    def messages_raw(self):
        return list(self.formal)

    def brief(self, max_chars=400):
        tail = self.formal[-6:]
        txt = " / ".join(m["content"][:80] for m in tail if m["role"] in ("user", "assistant"))
        return txt[-max_chars:]

# 写作类的"成篇特征"（2026-11-01 主人实测）：只有成篇请求才丢弃全部历史；
# 短承接句即使被误判成写作，也必须带上文（否则"含的哪里"会答成无关记忆）。
_WRITING_FULL_PAT = re.compile(
    r"(写|撰写|起草|润色|改写成|扩写|缩写|文案|报告|通知|邮件|演讲|论文|"
    r"作文|小说|故事|稿子|篇幅|字数|标题|段落|正文)")

# 主人明确要"再说一遍"时，不许拿"别重复"去挡（2026-11-02 加反复读提示时配的护栏）
_ASK_REPEAT = re.compile(r"(再说一遍|再说一次|重复一遍|重复一下|刚才说|刚才讲|"
                         r"你说了什么|我说了什么|原话|照原样)")

_ACTIVITY_KINDS = {"hands": 0, "tool_call": 0, "comfy": 0, "autonomy": 1,
                   "wish": 1, "notify": 2, "error": 2, "generate": 3,
                   "wait": 3, "message": 4, "memory": 4}


def _recent_activity(minutes=12, limit=5, behavior_path=None):
    """她"最近几分钟真实做过的事"——一小段回灌进提示的观察依据。

    为什么需要（CHANGELOG §15 实测）：主人让她去截图，她回"刚才那个浏览器又卡住了"，
    而那一轮审计只有 `inform-short` 直答，全程没有任何搜索/抓取动作——**她把猜测讲成了进度汇报**。
    社区成熟做法就是给 agent 回灌自己的真实轨迹（trajectory/scratchpad grounding）：
    有据可依，没做过就说不出口。
    纪律：只读 `behavior.jsonl` 尾部几十 KB，不开线程不发推理；任何异常返回空串
    （这块没内容宁可没有，绝不能拖垮回答）。"""
    try:
        p = behavior_path or (config.DATA_DIR / "audit" / "behavior.jsonl")
        import os
        size = os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(max(0, size - 65536))
            raw = f.read().decode("utf-8", "replace")
        cut = time.time() - minutes * 60
        rows, seen = [], set()
        for line in raw.split("\n")[1:]:          # 丢掉可能被截断的首行
            if '"ts"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            kind = d.get("kind")
            ts = d.get("ts", 0)
            if kind not in _ACTIVITY_KINDS or ts < cut:
                continue
            detail = (d.get("detail") or "").strip().replace("\n", " ")
            if not detail:
                continue
            key = detail[:24]
            if key in seen:
                continue
            seen.add(key)
            rows.append((_ACTIVITY_KINDS[kind], ts, kind, detail[:60]))
        rows.sort(key=lambda r: (r[0], -r[1]))
        picked = sorted(rows[:limit], key=lambda r: r[1])
        return "\n".join("- %s [%s] %s"
                         % (time.strftime("%H:%M", time.localtime(r[1])), r[2], r[3])
                         for r in picked)
    except Exception:
        return ""


def build_messages(session, query, ephemeral_block=None, writing=False,
                   memory_block=None, tier_hint=None, gen_tokens=None, hint=None):
    """组装发给模型的完整消息。ephemeral_block 仅存在于返回值，不进 formal。
    memory_block：长期记忆召回块（L3）——拼在 persona/时间锚点之后、检索材料
    之前，**不占 ephemeral 预算**（桌宠红线：记忆/人格块永不因检索被砍）。
    tier_hint：分级思考策略的回答方式提示（pipeline._tier_hint 生成）——
    拼在 system 末段，告诉模型这题用什么"工作方式"作答（B26 主人拍板）。
    writing=True（写作/创作类）：素材按"参考吸收"提示注入，不要求逐条溯源编号，
    允许吸收结构/观点/案例后用自己的语言成文（事实与数据仍须以素材为准）。
    gen_tokens：本轮给生成留多少 token——用于按实际占用算历史预算（A6）。
    hint：一句话提醒（如"这题易翻车，可以自己查"），拼在 system 末段，不是命令。"""
    msgs = []
    base = _persona_prompt()
    # 时效锚点：把"现在"和"对话时间轴"告诉模型，避免把检索页里的日期当今天
    base += _time_anchor(getattr(session, "formal", []) or [])
    # 自述的观察依据：她最近几分钟真实做过的事（防"把猜测讲成进度"，见 _recent_activity）
    act = _recent_activity()
    if act:
        base += ("\n\n【你最近几分钟真实做过的事】（要跟自己刚干过什么有关就照这个说；"
                 "这里面没有的别说自己做过，也没必要为进度找理由）\n" + act)
    if tier_hint:
        base += "\n\n【本轮回答方式】" + tier_hint
    if memory_block:
        base += ("\n\n【你记得的事】（长期记忆，自然使用即可；这是你的回忆，"
                 "不要当系统提示复读）\n" + memory_block)
    if ephemeral_block:
        if writing:
            base += (
                "\n\n【写作要求】（必须遵守）：\n"
                "1. 你正被要求写一篇文稿。已检索到参考素材（见下方【参考资料】）。\n"
                "2. 你的角色是【起草者/编辑】：素材只是原料，由你总结、选用、转述后"
                "用自己的话成文；不得照抄整段原文，也不要把素材里的『我』『本站』"
                "『笔者』当成你自己。\n"
                "3. 文中的【具体事实与数字】（年份、公司名、人名、百分比、金额、规模等）"
                "必须来自素材，并在数字后标注来源编号如 [1]、[2]（编号对应上方"
                "【参考资料】的来源顺序）；素材中没有的具体数字【严禁编造】——"
                "写『据公开资料/根据相关行业信息』概述趋势即可，不要给虚构数值。\n"
                "4. 全文用简体中文（专有名词如公司名/人名/产品名可保留原文），"
                "正文不得夹杂英文句子或英文单词（技术缩写如 AI、API、CPU、GPU 除外）。\n"
                "5. 结构完整、层次清晰，按公文/报告应有的格式写，结尾自然收束。\n\n"
            )
        else:
            base += (
                "\n\n【回答要求】（必须遵守）：\n"
                "1. 具体事实与数字必须来自下方【参考资料】，不得编造；"
                "素材中没有的精确数据写『未查到该精确数值』或概述，不给虚构数字。\n"
                "2. 用自己的话转述归纳，不得照抄整段原文；素材中『我』『本站』『笔者』指原作者。\n"
                "3. 多来源冲突时指出分歧；过时/可疑信息先质疑后作答。\n"
                "4. 全文用简体中文（专有名词可保留原文）。\n\n"
            )
    else:
        base += ("\n\n【本轮无检索材料】本次没有获得任何网页资料，回答中不要输出 "
                 "[1][2] 这类来源编号标注，也不要声称引用了某个网站；"
                 "如果主人问的是你们之间的事/私人话题，就用你的记忆和当时的感受"
                 "直接接话——网上本来就没有，不许说'没查到/参考资料没提到'。")
    if hint:
        base += "\n\n" + hint
    # 反复读提示（2026-11-02，A/B 实测 17%→2%）：同一句闲聊连问，上一轮回答就躺在
    # 历史里，小模型会照抄自己的开头和句式。把上一条原文当**负面提示**摆出来，
    # 比只在人设里写"别重复"有效得多（人设那条+6 个点，这条+15 个点）。
    # 只在日常对话里加；写作不加（会干扰创作），"再说一遍"类明确要求也不算。
    if not writing:
        try:
            # 2026-11-02 主人抓到她"习惯性重复"：同一句"刚打完那局…"
            # 换个问题又问一遍，原样照抄（sessions 836/842 两条一模一样）。
            # 只摆"上一轮"不够——她的复读经常跳一轮出现，所以把**最近 3 条**都列成
            # 负面清单，并明确点名不许复用开头。
            prevs = [m.get("content") for m in reversed(getattr(session, "formal", []) or [])
                     if m.get("role") == "assistant" and m.get("content")]
            prevs = [str(x).strip() for x in prevs if str(x).strip()][:3]
            if prevs and not _ASK_REPEAT.search(query or ""):
                lines = ["\n\n【你最近刚说过的原话（这一轮禁止复用）】"]
                for i, p in enumerate(prevs, 1):
                    lines.append("%d. 「%s」" % (i, p[:70]))
                lines.append("这一轮**不许**用上面任何一条的开头、句式或收尾；"
                             "意思可以一样，说法必须换（换个开头、换个比喻、换个落点）。")
                base += "\n".join(lines)
        except Exception:
            pass
    msgs.append({"role": "system", "content": base})
    # 写作/创作类：不注入历史。写作是独立创作任务，历史注入会让
    # 本地模型被上一话题带跑（答非所问串话题）；延续/润色类需求由用户在问题中
    # 提供前文即可，不为它牺牲创作独立性。
    if writing:
        q = (query or "").strip()
        # 2026-11-01 修复：写作请求本身不需要历史，但**短承接句**必须有上文——
        # 实测"含的哪里？怎么含的？"被判定器误标成写作，旧逻辑 window=[] 把
        # 场景删光，她只能拿记忆里的无关笔记硬凑（表现为"没听懂/答非所问"）。
        # 短句（≤25 字、无成篇特征）保留最近 8 条；成篇请求照旧不注入历史。
        if len(q) <= 25 and not _WRITING_FULL_PAT.search(q):
            window = session.formal[-8:]
        else:
            window = []
    else:
        # 曾用 [-40:] 硬截断：50 轮会话开局锚点（名字/约定/偏好）直接消失，
        # _trim 根本看不到（GLM 任务 2026-10-28 实测抓到"记忆召回失败"根因）。
        # 120 → 300 条（A6 2026-10-28 实测又踩一次）：会话超过 120 条后，
        # 开局那条"我叫小陈，最喜欢蓝色"落在窗口外，_trim 的摘要也救不回来
        # （她答"你没跟我说过颜色呀"）。放宽到 300 条让锚点进窗口，
        # 真正的裁剪仍由 _trim 按 token 预算 + 抽取式摘要完成。
        window = session.formal[-300:]
    if window and window[-1]["role"] == "user" and window[-1]["content"] == query:
        window = window[:-1]  # 当前问题刚写入过，避免重复
    # 历史预算（A6，主人定调 2026-10-28：上限 8000）：配置只是上限，实际按
    # 本轮真实占用算——system（人格+记忆）+ 材料 + 生成预算 全从 ctx 里扣，
    # 剩多少给历史。放得下就 8000，深想/长材料时自动降，绝不超引擎 ctx
    # （超了会 400，或触发 _shrink 把历史砍到几条 = 突然失忆）。
    hist_budget = _hist_budget(base, ephemeral_block, gen_tokens)
    trimmed = _trim(window, hist_budget, hard_cap=hist_budget)
    sys_parts = [m["content"] for m in trimmed if m.get("role") == "system"]
    if sys_parts:
        # 摘要块（_trim 生成的【早期对话摘要】）若留在历史中间，Qwen3.8
        # 模板会 500 "System message must be at the beginning"（system 必须
        # 连续在开头）。合并进首位 system，语义不变且模板安全。
        msgs[0]["content"] = (msgs[0]["content"] + "\n\n"
                              + "\n\n".join(sys_parts))
        trimmed = [m for m in trimmed if m.get("role") != "system"]
    msgs.extend({"role": m["role"], "content": m["content"]} for m in trimmed)
    if ephemeral_block:
        # 素材 + 当前问题合并进【同一条】 user 消息（生成紧跟素材之后，本地模型
        # 必须先读素材才能写；拆成两条 user 时模型把素材当无关输入直接无视）。
        # 素材截断到 1200 字：本地模型对超长材料注意力稀释，长材料会全盘无视；
        # 只留最相关部分，其余细节宁可不要也不能淹没要点。
        mat = ephemeral_block
        # 1200 → 2000 字（主人定调 2026-10-28：让她看得全材料）。旧阈值是为
        # "本地模型注意力稀释"设的折中，硬上限仍受 BUDGET_EPHEMERAL 约束。
        if len(mat) > 2000:
            mat = mat[:2000] + "…（其余资料略）"
        msgs.append({"role": "user",
                     "content": "【参考资料】" + mat +
                     "\n\n【注意】以上【参考资料】与之前的历史对话仅作素材参考，"
                     "历史对话已结束，现在只回答用户【当前】提出的问题：\n"
                     "回答必须【全中文】（专有名词如公司名/人名/产品名除外）；"
                     "文中事实与数字必须来自资料，资料没有的【严禁编造】；"
                     "用自己的话成文，不得照抄原文：" + query})
    else:
        msgs.append({"role": "user", "content": query})
    return msgs

def _time_anchor(formal=None):
    """时效锚点：把"现在"和"对话时间轴"告诉模型，避免把检索页里的日期当今天。"""
    now = time.time()
    last_ts = None
    for m in reversed(formal or []):
        if m.get("role") in ("user", "assistant") and m.get("ts"):
            last_ts = m["ts"]
            break
    if last_ts is None:
        last_ts = now
    wd = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[time.localtime(now).tm_wday]
    return ("\n\n【当前时间】现在：" + time.strftime("%Y年%m月%d日 %H:%M", time.localtime(now))
            + " " + wd + "（本机时区）。"
            + "\n对话时间轴：本轮最新一条消息的时间戳是 "
            + time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts))
            + "；早于它的内容属于历史，晚于它的属于本轮。"
            + "\n判断材料是否过期、回答时效性问题（今天/昨天/上周/最新价格）时，"
              "以这两个时间为准；检索到的页面里出现的其它日期是发布或抓取时间，"
              "不等于用户提问的时间。")


def compat_system(query=None, budget_tokens=1600):   # C10：800→1600
    """OpenAI 兼容端点（/v1/chat/completions）的 system：人设 + 时间锚点 + 记忆块。

    为什么单独有这个函数：外部客户端（早先的桌面形象走 opencode 子进程）
    会自带一大段 agent system，让她变成"工具调用机器"。桌宠端点的规矩是——
    **谁来调都只认她自己的人设**，外来 system 一律丢掉，只保留 user/assistant，
    并照样注入核心记忆集与召回（所以形象里说话的仍然是桌宠本体，有记忆有性格）。
    """
    base = _persona_prompt() + _time_anchor()
    try:
        import memory                                    # noqa: PLC0415 避免顶层循环依赖
        core, _cu = memory.core_block(budget_tokens=600)
        rel, _ru = memory.recall(query or "", budget_tokens=budget_tokens)
        seen, parts = set(), []
        for seg in (core, rel):
            for ln in (seg or "").splitlines():
                ln = ln.strip()
                if ln and ln not in seen:
                    seen.add(ln)
                    parts.append(ln)
        if parts:
            base += ("\n\n【你记得的事】（长期记忆，自然使用即可；这是你的回忆，"
                     "不要当系统提示复读）\n" + "\n".join(parts))
    except Exception:
        pass                                             # 记忆不可用也不能挡住回答
    return base


def _persona_prompt():
    p = config.PROMPT_DIR / "persona.md"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return "你是用户的本地智能助理，用简体中文自然回答。"

_TOKEN_PER_CHAR = 1.15          # 中文≈1.15 字/token（与 _trim 内同一口径）


def _est_tokens(text):
    """粗估 token 数（中文按 1.15 字/token；够用，宁可估高不估低）。"""
    return int(len(text or "") / _TOKEN_PER_CHAR) + 1


def _hist_budget(system_text, material=None, gen_tokens=None):
    """本轮能分给历史的 token 数（A6）：ctx − system − 材料 − 生成 − 余量。

    config.BUDGET_HISTORY 是**上限**不是固定值：主人要 8000，但若本轮是深想
    （生成 8192）或材料很长，硬塞 8000 会超 ctx → 引擎 400 或 _shrink 把历史
    砍到几条。所以按真实占用算，能放多少放多少。
    """
    ctx = max(2048, int(getattr(config, "ENGINE_CTX", 16384)))
    gen = int(gen_tokens if gen_tokens is not None else config.BUDGET_NORMAL)
    avail = ctx - _est_tokens(system_text) - _est_tokens(material) - gen - 400
    cap = int(getattr(config, "BUDGET_HISTORY", 8000))
    return max(1024, min(cap, avail))


def _trim(msgs, token_budget, hard_cap=None):
    """按 token 预算裁剪历史消息——锚点保留 + 摘要版（GLM 任务 2026-10-28）。

    旧版丢弃式（B26）：预算外旧消息整条消失，长会话里"开场说过的重要约定"
    （名字/偏好/上下文约定）最先被丢。新版三段式：
      ① 头部锚点：开头的 system 消息（人设/注入块）无条件保留；
      ② 尾部：从最新往回收，预算内全保（最近对话永远完整）；
      ③ 中段丢弃的消息不蒸发：抽取式摘要（每条截首句拼"角色: 内容"），
         以 system 块【早期对话摘要】注入头部之后——不调引擎、不阻塞，
         摘要预算 ≤ min(500, 总预算 20%)，超预算丢最新保最旧（开局锚点最值钱）。
    预算可被用户在设置页调大，但【实际生效上限】受引擎 ctx 约束：
    history + ephemeral + normal 之和超过 ctx 时引擎会 400，触发 _shrink
    把历史砍到只剩几条（"多轮后突然失忆"）。这里钳制到 ctx 内，
    既尊重用户"无上限自由选择"，又保证运行时不会自我击穿。"""
    if hard_cap is None:
        ctx = max(1024, int(config.ENGINE_CTX))
        hard_cap = ctx - int(config.BUDGET_EPHEMERAL) - int(config.BUDGET_NORMAL)
    hard_cap = max(1024, int(hard_cap))   # 至少保住一轮对话
    token_budget = max(1024, min(int(token_budget), hard_cap))

    # ① 头部锚点：开头连续的 system 消息必保（正常窗口没有 system，
    #    人设由 build_messages 拼——这里是防御性保留，将来有人把 system
    #    混进历史窗口也不至于砍掉人设）
    head = []
    for m in msgs:
        if m.get("role") == "system":
            head.append(m)
        else:
            break
    rest = msgs[len(head):]
    if not rest:
        return head
    head_est = sum(engine_est(m["content"]) for m in head)
    if head_est + sum(engine_est(m["content"]) for m in rest) <= token_budget:
        return list(msgs)                     # 没超预算：原样全保

    # ③ 摘要预算：头部锚点必保（先从预算里扣除），剩余给"摘要+尾部原文"。
    #    锚点本身超预算时 avail 兜底 1024——锚点仍保，其余尽力。
    avail = max(1024, token_budget - head_est)
    sum_budget = min(500, max(200, avail // 5))
    keep_budget = avail - sum_budget

    # ② 尾部：从最新往回收
    acc, tail = 0, []
    for m in reversed(rest):
        t = engine_est(m["content"])
        if acc + t > keep_budget and tail:
            break
        acc += t
        tail.insert(0, m)
    kept_ids = {id(m) for m in tail}
    dropped = [m for m in rest if id(m) not in kept_ids]
    if not dropped:
        return head + tail

    summary = _digest(dropped, sum_budget)
    if summary:
        return head + [{"role": "system", "content": summary}] + tail
    return head + tail

def _digest(dropped, budget):
    """抽取式摘要（GLM 任务 2026-10-28）：不调引擎、零阻塞——每条丢弃消息
    压成"角色: 首句"，超预算从【最新】开始丢：开局锚点（名字/约定/偏好）是
    摘要里最值钱的，必须保；被丢的较新内容贴近尾部原文，连续性由尾部承担。"""
    role_name = {"user": "主人", "assistant": "桌宠"}
    lines = []
    for m in dropped:
        txt = re.sub(r"\s+", " ", (m.get("content") or "")).strip()
        if not txt:
            continue
        lines.append(f"{role_name.get(m.get('role'), m.get('role') or '？')}: {txt[:60]}")
    if not lines:
        return ""
    head_txt = "【早期对话摘要】（更早的对话已压缩成要点，细节以最近对话为准）：\n"
    out = head_txt + "\n".join(lines)
    while lines and engine_est(out) > budget:
        lines.pop()                           # 丢最新的摘要行（保开局锚点）
        out = head_txt + "\n".join(lines)
    return out

def engine_est(text):
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return int(cjk * 1.1 + (len(text) - cjk) / 3.2)

def format_evidence(pages):
    """把抓到的页面组装成带编号的临时上下文块（总量受 BUDGET_EPHEMERAL 硬约束，
    超预算按页截断，修复材料过多导致的输出崩溃）。返回 (block, source_index)。"""
    parts, idx, used = [], [], 0
    budget = config.BUDGET_EPHEMERAL
    for i, pg in enumerate(pages, 1):
        if pg.get("error"):
            continue
        if pg.get("img"):
            # 图片条目（§17）：给她能直接用的两条链，不占正文预算
            parts.append(f"[图{len(idx) + 1}] {pg.get('title','无标题')}"
                         f" | 图直链 {pg['img']} | 来源页 {pg['url']}")
            idx.append({"n": len(idx) + 1, "url": pg["url"],
                        "title": pg.get("title", ""), "img": pg["img"]})
            continue
        if not pg.get("text"):
            continue
        t = pg["text"]
        est = engine_est(t)
        if used + est > budget:
            remain = int((budget - used) * 1.15)  # 中文≈1.15字/token（原写反，摘要只吃到 87%）
            if remain < 300:
                break
            t = t[:remain] + "\n[…本条材料超长已截断，其余部分未注入]"
            est = budget - used
        used += est
        parts.append(f"=== 来源[{len(idx) + 1}] {pg.get('title','无标题')} | {pg['url']} ===\n{t}")
        idx.append({"n": len(idx) + 1, "url": pg["url"], "title": pg.get("title", "")})
    return "\n\n".join(parts), idx

def _safe(s):
    return re.sub(r"[^\w\-]", "_", s)[:60]
