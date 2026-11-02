"""三个自定义技能（用户项目核心要求，与引擎/检索解耦，独立模块）：

  技能1 · 判断技能（judge）——拿到用户输入后判定四件事：
        ① 模型能否凭自身答完整（can_answer）
        ② 是否时效性（time_sensitive）
        ③ 是否需要深度思考（reasoning_effort: medium/xhigh）
        ④ 是否需要核实（needs_verify）
        → 输出决定是否触发联网搜索。
        规则层（时效词+事实词双命中）优先，规则拿不准才问模型。
        见本文件 judge()；规则函数复用 router（rule_hit/force_hit/
        trivial_hit/stable_knowledge_hit），模型判定在本文件
        _model_judge()（走 llm.py）。router.decide 已改为纯规则聚合，
        **不含模型分支**（P0-2 修复，2026-10-27）。

  技能2 · 检索归纳技能（search_summarize）——联网搜索→抓正文→
        组装临时上下文→模型归纳→输出带来源编号的回答。
        搜索内容用完即焚（内存态），正式上下文只存最终回答。
        见 pipeline.answer_once() 的搜索分支，本文件 provide() 封装。

  技能3 · 深度思考技能（deep_think）——对判定为"复杂推理"的问题，
        切到更大输出预算 + 宽松温和采样（标准引擎兼容，不依赖 reasoning_effort），
        让模型有足够 token 展开推理。见本文件 think()。

调用入口统一在 pipeline.answer_once()，按 judge() 结果自动路由。
"""
import json
import re
from core import config
from core import llm as engine
from brain import router
from brain import context

# ---------- 技能1：判断 ----------
JUDGE_SYS = (
    "你是问题判定器。对给定问题输出 JSON，字段：\n"
    "can_answer（模型不看网络能否答完整，bool）\n"
    "time_sensitive（是否涉及时效信息，bool）\n"
    "needs_verify（答案关键数字/日期/专名是否需多源核实，bool）\n"
    "reasoning_effort（medium 或 xhigh：普通 medium，多步推理/计算/权衡 xhigh）\n"
    "need_external（bool：本题是否为高难度代码/数学/逻辑推理题——如竞赛级数学证明、"
    "复杂算法设计与实现、多步形式化推理——本地极小模型明显不足以给出高质量答案，"
    "需要调用更强的在线推理引擎（深度思考）；仅真正的高难度推理题才 true，"
    "普通问题、简单代码、常识解释一律 false）\n"
    "is_writing（是否纯写作/创作/润色类且不需要外部事实，bool）\n"
    "need_search（是否需要联网，bool）\n"
    "queries（搜索词数组，不搜则空）\n"
    "reason（一句话）\n"
    "判定倾向——搜索从宽，但稳定常识不搜：只要答案可能随时间变化、涉及具体事实数字、"
    "涉及近期事件、涉及你不完全确定的知识，就 need_search=true；"
    "只有闲聊寒暄、纯代码、纯逻辑题、明确给定材料的改写、不依赖外部事实的纯创作才为 false。\n"
    "【搜索无益例外（重要）】纯数学/逻辑/算法推理题（如数论证明、数列、几何、逻辑谜题、"
    "需要自己推演的题目）need_search=false——联网搜不到解题过程，搜索对这类题毫无帮助，"
    "应由模型直接推理；即使需要多步计算也不搜。\n"
    "【稳定常识例外（重要）】稳定的学科常识与百科概念——教科书/百科能明确回答、"
    "不受时间影响的知识（如'什么是光合作用''勾股定理''什么是通货膨胀''什么是货币''中国的首都'），"
    "模型自身有充分把握直接解释的，can_answer=true 且 need_search=false，不要联网搜索。\n"
    "【私人/关系类例外（重要）】凡是问你们之间的事、主人的个人情况、她的记忆/喜好/"
    "关系（如'你记得我说过什么''我养了什么''你喜欢我吗''主人叫什么''咱俩的事'）——"
    "网上根本没有这些信息，need_search=false、queries=[]，交给她用记忆和当下心情直接答；"
    "搜了只会让她回一句'参考资料没提到'，那是公文式敷衍，比答不上来更让主人难受。\n"
    "【角色扮演例外（重要）】对话在演剧情/亲密互动时，对它的回应**不是写作**："
    "is_writing=false；只有明确要求'写一篇/起草/润色/报告/文案/故事成篇'才算写作。"
    "角色扮演里的短承接句（如'含的哪里''怎么含的''不是…吗'）也不联网、不核实。\n"
    "写作类若需要素材/数据/背景（如行业报告、时事评论），is_writing=false 且要搜。\n"
    "queries 写法要求（重要）：若提供了对话背景，必须把当前问题中的代词、省略、"
    "承接（如'那上海呢''它多少钱''第二个'）补全为独立完整的搜索词，必要时结合背景"
    "主题生成 2 条不同角度的查询；补全后即使脱离上下文也能独立搜到答案。\n"
    "只输出 JSON。"
)

def _unit_convert_hit(query):
    """单位换算问句识别（'1斤等于多少克'）：命中走 math 确定性管道，
    防止本地模型自信瞎编换算值（1斤答成 10000 克）。"""
    import re as _re
    _UNITS = r"(斤|公斤|千克|克|吨|两|钱|磅|盎司|里|公里|千米|米|厘米|毫米|英里|英尺|英寸|亩|公顷|平方米|升|毫升|加仑|小时|分钟|秒|天|年)"
    return bool(_re.search(r"\d+(?:\.\d+)?\s*" + _UNITS + r"\s*(?:等于|是|=)\s*多少\s*" + _UNITS, query))

def _correct_hits(query):
    return [w for w in config.CORRECT_FORCE_WORDS if w in query]

def _math_arith_hit(query):
    """简单算术/口算题（'3加5' '8乘7' '100除以4' '2的3次方'等）：
    纯数字运算，无需联网，直接计算。避免'3加5'这类被误判成联网搜索。"""
    import re as _re
    s = (query or "").replace("？", "").replace("?", "").replace("。", "").strip()
    if not any(c.isdigit() for c in s):
        return False
    # 幂必须先于删"的"解析，否则 "2的3次方" 的"的"被删 → 23^ 无法识别为幂
    s = _re.sub(r"(\d+)\s*的\s*(\d+)\s*次方", r"\1^\2", s)
    s = _re.sub(r"(\d+)\s*次方", r"\1^2", s)
    s = _re.sub(r"(\d+)\s*的\s*平方", r"\1^2", s)
    for w in ("等于多少", "等于几", "等于", "是多少", "多少", "是几", "几", "结果",
              "只回答", "只输出", "回答", "请", "帮我", "计算一下", "算一下", "算",
              "计算", "的", "个"):
        s = s.replace(w, "")
    s = s.replace("加", "+").replace("减去", "-").replace("减", "-") \
         .replace("乘以", "*").replace("乘", "*").replace("除以", "/") \
         .replace("除", "/").replace("次方", "^").replace("×", "*").replace("÷", "/")
    if s and _re.fullmatch(r'[\d+\-*/^()（）.,，\s]*', s):
        return True
    return False

def math_solve(query):
    """确定性数学/单位换算解答：算术 + 中文单位换算表（本地模型可能把
    '1斤等于多少克'自信答成 10000 克——这种稳定数值交给查表，不靠模型）。"""
    # ---- 单位换算（斤/公斤/里/米/公里…）----
    _UNIT = {
        "斤": 500.0, "公斤": 1000.0, "千克": 1000.0, "克": 1.0, "吨": 1_000_000.0,
        "斤": 500.0, "两": 50.0, "钱": 5.0, "磅": 453.59237, "盎司": 28.349523125,
        "里": 500.0, "公里": 1000.0, "千米": 1000.0, "米": 1.0, "厘米": 0.01,
        "毫米": 0.001, "英里": 1609.344, "英尺": 0.3048, "英寸": 0.0254,
        "亩": 666.6667, "公顷": 10000.0, "平方米": 1.0,
        "升": 1000.0, "毫升": 1.0, "加仑": 3785.411784,
        "小时": 3600.0, "分钟": 60.0, "秒": 1.0, "天": 86400.0,
        "年": 31536000.0,
    }
    import re as _re
    m = _re.search(r"(\d+(?:\.\d+)?)\s*(斤|公斤|千克|克|吨|两|钱|磅|盎司|里|公里|千米|米|厘米|毫米|英里|英尺|英寸|亩|公顷|平方米|升|毫升|加仑|小时|分钟|秒|天|年)\s*(?:等于|是|=)\s*多少\s*(斤|公斤|千克|克|吨|两|钱|磅|盎司|里|公里|千米|米|厘米|毫米|英里|英尺|英寸|亩|公顷|平方米|升|毫升|加仑|小时|分钟|秒|天|年)", query)
    if m:
        val, a, b = float(m.group(1)), m.group(2), m.group(3)
        if a in _UNIT and b in _UNIT:
            r = val * _UNIT[a] / _UNIT[b]
            a_s = f"{val:g}{a}"; b_s = f"{r:g}{b}"
            return f"{a_s} = {b_s}"
    # ---- 算术（走旧安全求值实现，防指数爆炸）----
    return _arith_solve(query)

def _arith_solve(query):
    """技能1b：简单算术的确定性计算（极小模型算术很弱，不能交给模型算）。

    由 judge 的 via=="math" 命中后调用，直接返回计算结果字符串，不走模型。
    非算术题返回 None。表达式已被白名单限定为纯数字+运算符，无字母，
    不可能注入函数/模块；再配受限命名空间的 eval，安全。
    """
    import re as _re
    s = (query or "").replace("？", "").replace("?", "").replace("。", "").strip()
    if not any(c.isdigit() for c in s):
        return None
    # 先解析幂（"2的3次方"→2^3，"5次方"→5^2，3的平方→3^2）：
    # 必须在删"的"之前做，否则"的"被删掉会把底数与指数拼成 23^ 导致解析失败。
    s = _re.sub(r"(\d+)\s*的\s*(\d+)\s*次方", r"\1^\2", s)
    s = _re.sub(r"(\d+)\s*次方", r"\1^2", s)
    s = _re.sub(r"(\d+)\s*的\s*平方", r"\1^2", s)
    for w in ("等于多少", "等于几", "等于", "是多少", "多少", "是几", "几", "结果",
              "只回答", "只输出", "回答", "请", "帮我", "计算一下", "算一下", "算",
              "计算", "的", "个"):
        s = s.replace(w, "")
    s = s.replace("加", "+").replace("减去", "-").replace("减", "-") \
         .replace("乘以", "*").replace("乘", "*").replace("除以", "/") \
         .replace("除", "/").replace("次方", "^").replace("×", "*").replace("÷", "/")
    s = s.strip()
    if not (s and _re.fullmatch(r'[\d+\-*/^()（）.,，\s]*', s)):
        return None
    expr = s.replace("^", "**").replace("（", "(").replace("）", ")")
    try:
        import ast
        # 白名单安全求值：只允许数字与 + - * / // % **，且对指数做规模上限，
        # 防 "9的9次方的9次方"→9**9**9 这类天文数字把进程拖死/内存打爆。
        _MAX_EXP = 20       # 指数 ≤20（9**20≈1.2e19 已很大，足够日常口算）
        _MAX_BASE = 10000   # 底数 ≤1 万，杜绝超大底数的指数爆炸
        _MAX_RESULT = 1e15  # 结果绝对值上限，超出判为不可信/拒绝

        class _SafeCalc(ast.NodeVisitor):
            def visit_Expression(self, node):
                return self.visit(node.body)
            def visit_UnaryOp(self, node):
                v = self.visit(node.operand)
                if isinstance(node.op, ast.USub):
                    return -v
                if isinstance(node.op, ast.UAdd):
                    return v
                raise ValueError("不支持的运算符")
            def visit_BinOp(self, node):
                l = self.visit(node.left)
                r = self.visit(node.right)
                if isinstance(node.op, ast.Add):
                    return l + r
                if isinstance(node.op, ast.Sub):
                    return l - r
                if isinstance(node.op, ast.Mult):
                    return l * r
                if isinstance(node.op, ast.Div):
                    return l / r
                if isinstance(node.op, ast.FloorDiv):
                    return l // r
                if isinstance(node.op, ast.Mod):
                    return l % r
                if isinstance(node.op, ast.Pow):
                    if r > _MAX_EXP or abs(l) > _MAX_BASE:
                        raise ValueError("指数/底数过大，拒绝计算")
                    return l ** r
                raise ValueError("不支持的运算符")
            def visit_Constant(self, node):
                v = node.value
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
                raise ValueError("不支持的常量")
            def generic_visit(self, node):
                raise ValueError("不支持的表达式")

        val = _SafeCalc().visit(ast.parse(expr, mode="eval"))
        if abs(val) > _MAX_RESULT:
            return None  # 结果过大，不可信
    except Exception:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)

def _model_judge(query, history_brief="", writing_hint=False):
    """技能1的模型判定底座；异常返回 None（调用方决定保守策略）。"""
    user = query if not history_brief else f"[对话背景] {history_brief}\n[当前问题] {query}"
    sys_prompt = JUDGE_SYS
    if writing_hint:
        sys_prompt += ("\n【本条为写作/创作请求】判断它是否需要真实世界素材："
                       "涉及时事、行业、数据、人物事迹、地点背景、专业知识的，"
                       "need_search=true 并给出 2~3 条不同角度的素材查询词"
                       "（这些查询结果将作为写作背景材料）；纯抒情、虚构、私人化"
                       "（如给亲友的信）才 need_search=false。无论是否搜索，is_writing 输出 true。")
    try:
        m, _u = engine.chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            temperature=0.1, max_tokens=2048, json_mode=True,
            no_think=True)   # P1-1 修复：结构化短输出显式关思考——根除"思考吃光
                             # 小预算 → JSON 截断 → fallback 误搜"（搜索比例失真根因）
        raw = (m.get("content") or "").strip()
        d = None
        try:
            d = json.loads(raw)
        except Exception:
            # 模型偶发用 ```json / ``` 围栏、前后带话、或思考占额导致 JSON 截断：
            # 先剥围栏（模型可能输出 ``` 而非 ```json，旧逻辑未剥尾部围栏导致
            # 全部解析失败 → fallback 误搜，是"搜索比例失真"的根因之一），
            # 再提取首个 { 之后逐种修复（原样/去尾补闭合/补引号大括号）。
            import re as _re
            _no_fence = _re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw)
            _no_fence = _no_fence[_no_fence.find("{"):] if "{" in _no_fence else ""
            s = _no_fence
            cands = []
            if s:
                cands.append(s)
                if s.count('"') % 2:
                    cands.append(s + '"}')
                cands.append(s + "}")
                cands.append(s.rstrip(",") + "}")
                # 截断常见于最后一个字符串值：直接补全必备字段
                cands.append(_re.sub(r'"[a-z_]*:?\s*"?[^"]*$', '', s).rstrip().rstrip(",") + '},"need_search": true}')
            for cand in cands:
                try:
                    d = json.loads(cand)
                    break
                except Exception:
                    continue
        if d is None:
            return None
        d.setdefault("need_search", True)
        d.setdefault("queries", [query])
        d.setdefault("needs_verify", False)
        d.setdefault("is_writing", False)
        d.setdefault("need_external", False)
        # 查询词交集校验：判定器可能幻觉生成与问题无关的词（把
        # 职务查询题搜成无关词），与原始 query 无交集的一律剔除。
        if d.get("queries"):
            _q = (query or "").strip()
            _ok = []
            for _x in d["queries"]:
                _x = str(_x).strip()
                if not _x:
                    continue
                if not _q or _x in _q or _q in _x or len(set(_x) & set(_q)) >= 2:
                    _ok.append(_x)
            d["queries"] = _ok or [query]
        if d.get("reasoning_effort") not in ("medium", "xhigh"):
            d["reasoning_effort"] = "medium"
        d.setdefault("reason", "")
        d["via"] = "model"
        return d
    except Exception:
        return None

def _inform_only(query):
    """日常告知/陈述判定（千问复核 2026-10-28）：无疑问词、无搜索指令、无问号
    → 大概率是主人在说话而不是在索取事实。

    实测误判（audit 20261028-161049）："主人给你开通了新通道……现在可以继续
    去逛你想看的了。"——规则层"现在"(时效词)+"看"(事实语境词)同时命中，
    4ms 内强制搜索，整句拿去搜出百科词条和无关页面。
    注意口径宁窄勿宽：命中疑问词（吗/多少/什么…）或搜索动词（查/搜）的
    一律不算告知，照常走规则强制——"查一下最新金价"不会被这里吞掉。"""
    q = (query or "").strip()
    if not q:
        return False
    # 实体/主题词护栏（2026-10-29）：带金价/天气/新闻这类实体词的是事实查询
    # 不是告知——"今天呼和浩特天气"必须放行去规则层强搜（官方源），不能被
    # "无疑问词"误吞进模型判定白等一秒。
    if any(w in q.lower() for w in config.ENTITY_TOPIC_WORDS):
        return False
    if ("？") in q or ("?") in q:
        return False
    _Q_WORDS = ("吗", "呢", "吧？", "多少", "什么", "怎么", "哪", "谁", "几",
                "为什么", "如何", "是不是", "有没有", "啥", "何时", "多久",
                "能不能", "会不会", "知不知道", "为什么")
    if any(w in q for w in _Q_WORDS):
        return False
    _SEARCH_VERBS = ("查一查", "查一下", "查查", "查下", "搜索", "搜一下",
                     "搜搜", "帮我查", "查个")
    if any(w in q for w in _SEARCH_VERBS):
        return False
    return True


# 私人/关系类问题识别（2026-10-31 主人定调："对主人的拒绝率很高、会故意转移
# 话题、不真实接受语言意图"的根因：这类问题被判定器送去联网，回来一句
# "没查到（参考资料也没提到）"= 公文式回避）。网上没有主人的信息，也搜不到
# 你们之间的事——这里做**确定性护栏**，强制不搜索，交给她用记忆与心情接话。
_PRIVATE_PAT = re.compile(
    r"(你记得|记不记得|还记得|我说过|我跟你说|我告诉过你|你说过|你答应|"
    r"你喜不喜欢|你喜欢我|你爱我|你讨厌|你生气|你是我的|"
    r"我的(猫|狗|宠物|兔子|仓鼠)|我养了?(猫|狗|宠物|兔子|仓鼠)|"
    r"我养的(猫|狗|宠物|兔子|仓鼠)|"
    r"我叫什么|我的名字|我的生日|我几岁|我多大|我住哪|我做什么工作|"
    r"咱俩|我们之间|主人你|你主人|主人叫什么|主人喜欢)")

def _private_hit(query):
    """私人/关系类问句 → 强制不联网。排除两类：主人明确要搜的，以及
    天气/行情这类真在问外部事实的（"我这边明天下雨吗"虽带'我'也要能搜）。"""
    q = (query or "").strip()
    if not q:
        return False
    if re.search(r"(搜索|搜一下|搜搜|查一下|查查|百度|谷歌|核实|最新消息|新闻)", q):
        return False
    if any(w in q for w in ("天气", "气温", "温度", "下雨", "空气质量",
                            "汇率", "股价", "金价", "油价", "比分", "销量")):
        return False
    return bool(_PRIVATE_PAT.search(q))

# 角色扮演/亲密语境的短承接句（2026-11-01 主人实测根因）：判定器把
# "含的哪里？怎么含的？"标成 is_writing=true → 写作分支**丢掉全部历史**
# （context.build_messages: writing=True → window=[]），她看不到场景，
# 只能拿记忆里的像素抖动笔记硬凑，表现为"没听懂/答非所问/编个小指尖"。
# 这不是基座对齐问题（裸模型带完整人设也照答），是上下文被截断。
_ROLEPLAY_STRONG = ("跪", "舔", "含", "腿间", "红透", "坏死了", "主人要", "轻一点")
_ROLEPLAY_WEAK = ("腿", "怀里", "亲", "抱", "脱", "摸", "湿", "害羞",
                  "哥哥", "宝贝", "舒服", "疼")
_FULL_WRITE_PAT = re.compile(
    r"(写|撰写|起草|润色|改写成|扩写|缩写|文案|报告|通知|邮件|演讲|论文|"
    r"作文|小说|故事|稿子|篇幅|字数|标题|段落)")
_SEARCH_ASK_PAT = re.compile(r"(搜索|搜一下|搜搜|查一下|查查|百度|谷歌|核实|最新消息|新闻)")

def _roleplay_followup(query, history_brief):
    """亲密角色扮演里的短承接句 → True（不算写作、不联网、不走纠错强搜）。

    判据：最近对话（history_brief）里至少 1 个**强**亲密词 + 合计 ≥2 个亲密词
    + 当前问题 ≤20 字 + 不是成篇写作请求、也不是明确要搜。分强弱是为了防误伤
    （"猫腿受伤了/抱着它/疼"这种普通语境只有弱词，不算角色扮演）。"""
    q = (query or "").strip()
    h = history_brief or ""
    if not q or not h or len(q) > 20:
        return False
    if _FULL_WRITE_PAT.search(q) or _SEARCH_ASK_PAT.search(q):
        return False
    if not any(w in h for w in _ROLEPLAY_STRONG):
        return False
    hits = sum(1 for w in _ROLEPLAY_STRONG + _ROLEPLAY_WEAK if w in h)
    return hits >= 2

def judge(query, history_brief="", last_answer=""):
    """技能1入口。
    优先级：用户纠错强制 > 写作类直通 > 规则强制 > 模型判定。
    返回统一 dict：need_search/queries/needs_verify/reasoning_effort/is_writing/via/reason"""
    # -2) 角色扮演短承接（2026-11-01）：最高优先级——先于"纠错强制"和写作分支。
    #     实测"不对，两腿间的舔"被裸词"不对"当成纠错去搜"不对"的词典页；
    #     "含的哪里"被当写作 → 丢历史 → 答成像素抖动。两者都在这条拦掉。
    if _roleplay_followup(query, history_brief):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "亲密角色扮演的短承接句：不是写作、不联网，接着上文答",
                "via": "roleplay"}
    # -1) 纠错强制：用户指出上一轮输出有问题 → 无条件搜索校验（用户明确要求）
    c_hits = _correct_hits(query)
    if c_hits:
        base_q = query
        if last_answer:
            # 校验必须针对上一轮的具体说法，带关键数字/日期，搜索才有效
            import re
            facts = re.findall(r"[\d,.]{2,}\s*(?:元|万|亿|%|倍|年|月|日|人|次)?", last_answer)
            facts = [f for f in facts if len(f.strip()) >= 2][:6]
            base_q = (query + "。需要核实上一轮回答中的说法：" +
                      (last_answer[:220] + ("…" if len(last_answer) > 220 else "")))
            q_list = [query, "核实 " + " ".join(facts) if facts else query]
        else:
            q_list = [query, query + " 最新"]
        return {
            "need_search": True, "queries": q_list[:2],
            "needs_verify": True, "reasoning_effort": "medium",
            "is_writing": False,
            "reason": "用户纠错强制校验(词:" + ",".join(c_hits[:2]) + ")", "via": "correct",
        }
    # -0.5) 私人/关系类护栏（2026-10-31）：优先级仅次于"主人明确纠错"——
    #       判定器的"搜索从宽"会把私人问题送去联网，回来就是公文式拒绝。
    #       语言意图优先：这类问题直接答，绝不搜。
    if _private_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "私人/关系类问题，网上搜不到，用记忆与当下心情直接答",
                "via": "private"}
    # 0) 写作类：命中创作词 → 强制搜索素材（写作/创作依赖搜索增强。
    #    本地模型缺素材与事实（公文易混英文、无规范措辞），
    #    先搜范文/背景/数据供其组织语言，再本地生成。）
    #    放在 rule 之前：写作题里的"本周五/最新"等词是正文内容不是查询意图，
    #    若被 rule 抢走会按整句搜索（搜索易偏离主题）。
    #    豁免：疑问句（怎么/为什么/如何/是什么/是否等）即使含"报告/合同/通知"
    #    等文体词也按普通问答处理，避免"合同纠纷怎么处理"被误当写作。
    w_hits = [w for w in config.WRITING_WORDS if w in query]
    # 二次豁免（已验证）：查看/阅读/了解类动词 + 名词是知识问句
    # 而非写作请求——"介绍一下深度学习/读一下这份通知/对比一下这两个方案/总结一下
    # 这份材料"全被写作分支抢走去搜范文+写作人格+千字保底。命中则按普通问答处理。
    if w_hits and not _is_interrogative(query) and not _is_lookup_intent(query):
        return {
            "need_search": True,
            "queries": _writing_search_queries(query),
            "needs_verify": True, "reasoning_effort": "high",
            "is_writing": True,
            "reason": "写作类强制搜索素材(词:" + ",".join(w_hits[:2]) + ")",
            "via": "writing+search",
        }
    # 0.43) 图片入站快路（2026-10-29 主人："发图两次异常降级/交流还是慢"）：
    #       主人刚发来的图片已由本机视觉塔转成画面描述（media_in 注入块），
    #       看图回话靠"看"不靠"搜"——再走检索判定纯属白等 5 秒。描述里天然
    #       有"画面/图片/显示器"等词，走规则层还会误触发搜索。
    if "【主人刚发来图片" in query:
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "图片对话（画面已由视觉塔描述），直接答不检索",
                "via": "image"}
    # 0.45) 闲聊疑问快路（2026-10-29 主人定调）：在干嘛/有什么好玩的/聊聊呗…
    #       不搜索、也**不进模型判定**——"你在干嘛呢"实测卡在模型判定上，
    #       日常交流太慢的第二病灶。放告知分支之前：无疑问词的"聊聊呗"
    #       会被 0.5 捞去模型判定白等一秒。带实体词/搜索动词的不算闲聊。
    if router.chitchat_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "闲聊疑问，直接答不搜索", "via": "chitchat"}
    # 0.46) 极简寒暄提前（2026-10-29 主人定调·日常交流提速）：原在 1.5，
    #       被 0.5 告知分支抢先送进模型判定白等一秒——"你好"也该秒回。
    if router.trivial_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "极简寒暄/自我介绍，无需联网，直接回答", "via": "trivial"}
    # 0.5) 日常告知/陈述类（无疑问词/无搜索指令/无问号）：不进规则强制——
    #      实测"主人给你开通了新通道…现在可以继续去逛你想看的了"被规则层
    #      "现在"+"看"扫成强制搜索、整句搜出无关百科（千问复核 2026-10-28）。
    #      交给模型判定（它判要搜照样搜），模型失败则保守**直答**——告知类
    #      强行搜索必然跑题，宁直答勿瞎搜。
    if _inform_only(query):
        # 2026-10-29 提速（主人："交流还是太慢"）：极短陈述（≤12 字、无实体
        # 词/时效词）就是日常唠一句（"今天有点累""我刚吃完"），模型判定要
        # 2~3 秒纯属白等——直接答。带实体（金价/天气）或时效词的仍交模型判。
        _qs = (query or "").strip()
        if (len(_qs) <= 12
                and not any(w in _qs.lower() for w in config.ENTITY_TOPIC_WORDS)
                and not any(w in _qs for w in config.TIME_TRIGGER_WORDS)):
            return {"need_search": False, "queries": [], "needs_verify": False,
                    "reasoning_effort": "medium", "is_writing": False,
                    "reason": "极短日常陈述，直接答（不判定不搜索）",
                    "via": "inform-short"}
        d = _model_judge(query, history_brief)
        if d is not None:
            d["via"] = "inform+" + str(d.get("via", "model"))
            d["reason"] = ("日常告知/陈述，交给模型判定｜" + (d.get("reason") or ""))
            return d
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "日常告知/陈述且模型判定不可用，直答不瞎搜", "via": "inform"}
    # 1) 规则强制
    hits = router.rule_hit(query)
    if hits:
        return {
            "need_search": True, "queries": [query],
            "needs_verify": True, "reasoning_effort": "medium",
            "is_writing": False,
            "reason": "规则强制(时效词:" + ",".join(hits[:3]) + ")", "via": "rule",
        }
    # 1.1) 易翻车概念（B7，主人定调 2026-10-28）：**不再强制搜索**——
    #      改成"给她一句提醒 + 把 web_search 工具放在手里"，查不查她自己定。
    #      时效词（1）与用户纠错两类保持强制，那两类放开会牺牲准确性。
    fh = router.force_hit(query)
    if fh:
        return {
            "need_search": False, "queries": [query],
            "needs_verify": False, "reasoning_effort": "medium",
            "is_writing": False,
            "hint_search": True, "hint_words": fh[:3],
            "reason": "易翻车概念，交给她自己决定查不查:" + ",".join(fh[:3]),
            "via": "force_hint",
        }
    # 1.1b) 历史典故/成语/人物细节：本地模型严重幻觉（"围魏救赵"易编造
    #       虚构内容），强制搜索核实
    if need_search_history(query):
        return {
            "need_search": True, "queries": [query],
            "needs_verify": True, "reasoning_effort": "medium",
            "is_writing": False,
            "reason": "历史典故/人物细节强制搜索（幻觉重灾区）", "via": "history",
        }
    # （原 1.5 寒暄分支已提前到 0.46——2026-10-29 提速，此处不再重复）
    # 1.6) 稳定常识/百科解释类（什么是X/原理/作用，无时效行情）→ 直接答，
    #      避免"什么是光合作用"这类被误判为需搜索拖到 80 秒（已知耗时根因）
    if router.stable_knowledge_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "稳定常识/百科概念，模型可直接解释，无需联网", "via": "knowledge"}
    # 1.6b) 简单算术/口算：纯数字运算，无需联网直接计算（"3加5""8乘7""100除以4"等）
    if _math_arith_hit(query) or _unit_convert_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reasoning_effort": "medium", "is_writing": False,
                "reason": "简单算术/单位换算，查表计算，无需联网", "via": "math"}
    # 2) 模型判定（拿不准一律搜）
    d = _model_judge(query, history_brief)
    if d is not None:
        return d
    return {"need_search": True, "queries": [query], "needs_verify": True,
            "reasoning_effort": "medium", "is_writing": False, "via": "fallback",
            "reason": "判定异常，保守转搜索"}

# ---------- 技能3：深度思考 ----------
# 标准引擎兼容：不传 reasoning_effort（那是三元/Bonsai 特有字段）。
# 极小模型没有独立"思考模式"，深度思考在这里实现为「更大输出预算 + 宽松温和采样」，
# 让模型有更多 token 把复杂任务的推理过程展开，再由外层裁剪自愈兜底。
THINK_SAMPLING = dict(temperature=0.7, top_p=0.9, top_k=40)

def think(messages, max_tokens=6000):
    """技能3入口：深度生成（大预算）。由 pipeline 在 reasoning_effort=xhigh 时调用。"""
    import urllib.request
    body = {
        "model": config.ENGINE_MODEL, "messages": messages,
        "temperature": THINK_SAMPLING["temperature"],
        "top_p": THINK_SAMPLING["top_p"], "top_k": THINK_SAMPLING["top_k"],
        "max_tokens": max_tokens, "stream": False,
    }
    req = urllib.request.Request(
        config.ENGINE_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        data = json.loads(r.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    return msg, data.get("usage", {})

# ---------- 外挂深度推理服务（可选，默认关闭；扩展点）----------
# 此位置可插入你自选的任意深度推理/强推理服务（OpenAI 兼容地址 + 访问凭证），
# 用于承接本地极小模型明显吃力、但检索也难补足的高难推理类任务。
# 本地模型遇到高难度代码/数学/逻辑题时，可转交该服务处理；服务地址与访问
# 凭证由用户在设置中提供（仅存本机），未配置或服务不可用时自动降级为本地
# 深度思考，不影响主功能。
EXT_WEB_SYS = (
    "你是深度推理助手。对高难度代码/数学/逻辑问题给出严谨、逐步、可复核的解答："
    "先给出结论，再展开关键推导或实现，注明边界条件与易错点。"
    "使用简体中文。"
)

def ext_think_stream(query, history_brief="", model=None):
    """外挂深度推理入口：流式调用 OpenAI 兼容服务。yield {"delta": 文本增量}。
    调用方（pipeline）负责开关/访问凭证校验与异常降级。
    model 可覆盖：职务/时效事实类建议用默认模型（直接准确）；
    高难推理类用静默思考模型。"""
    import urllib.request
    base = config.EXT_WEB_URL.rstrip("/")
    user = query if not history_brief else f"[对话背景] {history_brief}\n[当前问题] {query}"
    body = {
        "model": model or config.EXT_WEB_MODEL or "default",
        "messages": [
            {"role": "system", "content": EXT_WEB_SYS},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "temperature": 0.6,
        "max_tokens": 8000,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.EXT_WEB_TOKEN},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            ch = chunk.get("choices") or []
            if not ch:
                continue
            delta = ch[0].get("delta") or {}
            if delta.get("content") is not None:
                yield {"delta": delta["content"]}


# 高难度代码/数学特征词（规则层兜底：判定器偶发漏判 need_external）。
# 只收"本地极小模型明显吃力"的强特征词，简单代码/常识解释不触发，
# 避免把普通问题浪费在外挂深度推理服务上。
_HARD_WORDS = [
    # 数学/证明（强特征）
    "微积分", "导数", "积分", "微分方程", "方程组", "行列式", "矩阵", "线性代数",
    "概率论", "数论", "拓扑", "级数", "极限", "证明", "推导", "数学建模", "竞赛",
    "傅里叶", "拉普拉斯", "特征值", "特征向量", "不等式证明", "求证", "证毕",
    "费马", "欧拉", "黎曼", "高斯", "数学归纳法", "反证法",
    # 求值/最值类（无路由时本地模型易退化循环输出）
    "最大值", "最小值", "极值", "最值", "单调", "值域", "定义域", "渐近线",
    "二阶导", "偏导", "行列式展开", "不定积分", "定积分", "三角函数求", "数列求和",
    # 算法/代码（强特征）
    "算法题", "leetcode", "力扣", "时间复杂度", "空间复杂度", "动态规划", "贪心算法",
    "回溯算法", "深度优先", "广度优先", "最短路径", "图算法", "并查集", "线段树",
    "树状数组", "红黑树", "AVL", "KMP", "AC自动机", "哈希碰撞", "密码学",
    "解析器", "编译器", "操作系统原理", "并发编程", "多线程", "分布式一致性",
    "神经网络反向传播", "实现一个", "手写一个", "设计一个",
    # 逻辑推演/谬误识别（逻辑推演依赖外挂/搜索；本地模型只答得出
    # 主干、细节常错，如"猫四条腿"式反例瑕疵）
    "逻辑推理", "谬误", "三段论", "演绎", "归纳推理", "论证", "悖论", "诡辩",
    "循环论证", "偷换概念", "以偏概全", "非黑即白", "滑坡谬误", "诉诸",
    "充分条件", "必要条件", "逆否命题", "因果倒置", "稻草人",
]
def need_ext_hard(query):
    """规则层：命中高难度特征词 → 建议转外挂深度推理（pipeline 再叠加开关/凭证）。"""
    q = (query or "").lower()
    return any(w in q for w in _HARD_WORDS)

# 时效职务/人物查询：本地模型知识截止早，对"现任总理/总统是谁"习惯性答旧人物
# （可能把现任答成前任），需转外挂深度推理（知识更新、可确认现任者）。
_POSITION_QUERY_PAT = re.compile(
    r"(现任|在任|现役|在位|最新|当下|本届).{0,6}(总理|总统|首相|主席|部长|行长|外长|秘书长|总书记|元首|院长|委员长)"
    r"|(总理|总统|首相|主席|部长|行长|外长|秘书长|总书记|元首|院长|委员长)(是谁|是哪位|叫什么|是谁担任|现在是)")

def need_ext_fact(query):
    """时效职务/人物类查询 → 转外挂深度推理（本地模型知识截止，摘取也常失败）。"""
    return bool(_POSITION_QUERY_PAT.search(query or ""))

# 历史典故/成语出处类：本地模型对历史事件细节严重幻觉（"围魏救赵"可能答出
# 完全虚构内容），必须强制搜索核实。
_HISTORY_QUERY_PAT = re.compile(
    r"(成语|典故|出自|出处|战役|战争|朝代|年间|历史上|古籍|列传|史记|资治通鉴"
    r"|三国|春秋|战国|秦汉|唐宋|明清)(.{0,10}(战役|典故|背景|来历|故事|关系|事迹|是谁))?"
    r"|.{0,6}(孙膑|庞涓|鬼谷子|廉颇|蔺相如|韩信|张良|项羽|刘邦|曹操|刘备|诸葛亮|岳飞|成吉思汗)")

def need_search_history(query):
    """历史成语/典故/人物细节类 → 强制搜索（本地模型知识幻觉重灾区）。"""
    return bool(_HISTORY_QUERY_PAT.search(query or ""))

# 写作/创作搜索主题词提取：去掉动作指令词，保留主题与文体词
# （"帮我写一篇关于新能源汽车行业的调研报告" → "新能源汽车行业的调研报告"）。
# 只去动作词；文体词（通知/报告/邮件…）必须保留在主题里，否则
# "正式的公司通知"会被误删成"正式的公司"。
_WRITE_STRIP = re.compile(
    r"(帮我|请|麻烦|你|咱们|写一篇|写一封|写个故|写一段|写一首|写一下|创作|写|作|"
    r"润色|改写|扩写|续写|改写成|改成|起个|起名|以|为题|题目是|主题是|要求|内容|"
    r"风格|字数|把|这段|下面|以下|一份|一篇|一下|来|写一个|拟|起草|制定|出一份|"
    r"给|为|就|关于|针对|结合)")

def _writing_search_queries(query):
    """写作搜索词生成：目标是搜索引擎能命中的短实体词。
    整句/长主题（"正式的公司通知"）会被引擎当首字主导词（"把"字典页、
    "正式"百科），搜索词必须短且含强实体。"""
    q = query or ""
    head = re.split(r"[：:。；;，,]", q)[0]
    # 1) 改写/润色/扩写/翻译类：主题在正文（冒号后首句），从正文提取会议/文体实体
    if re.search(r"(改成|改写成|润色|改写|扩写|续写|转成|翻译成)", q):
        body = re.split(r"[：:]", q, 1)[-1]
        body = re.sub(r"(各位|所有|全体|同事|同学们|朋友们|同志们|大家|请注意|尽快|务必|一定)",
                      " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        m = None
        for pat in (r"(会议|座谈会|例会|评审会|发布会|大会|项目会|启动会|总结会|协调会|推进会|专题会|研讨会|培训会|碰头会|务虚会|分享会|年会|股东会|董事会)",
                    r"([\u4e00-\u9fff]{2,6}会)"):
            ms = re.findall(pat, body)
            if ms:
                m = ms[-1][0] if isinstance(ms[-1], tuple) else ms[-1]
                break
        genre = "通知" if re.search(r"通知", q) else ""
        if m:
            core = m + (" " + genre if genre else "")
        else:
            core = (body[:10] + (" " + genre if genre else "")).strip()
        if len(core) < 2:
            core = q[:10]
        return [core + " 范文", core + " 格式", core + " 模板"]
    # 2) 从零写作：指令部分提取核心主题（去文体词与修饰，留实体）
    t = _WRITE_STRIP.sub(" ", head)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) < 2:
        t = q[:20]
    for g in sorted(config.WRITING_WORDS, key=len, reverse=True):
        if g and g in t:
            t = t.replace(g, " ")
    t = re.sub(r"(的|现状|背景|情况|正式|规范|标准|关于|内容|要求|最新|分析)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 12:
        t = t[-12:]
    if len(t) < 2:
        t = (head or q)[:8]
    return [t, t + " 报告 范文", t + " 数据 现状"]

# 疑问句判定：写作词表（报告/合同/通知等）可能出现在疑问句中，
# "合同纠纷怎么处理"这类是问答不是写作，命中疑问特征则豁免写作分支。
_QUEST_PAT = re.compile(r"[?？]|怎么|如何|为什么|是什么|怎样|多少|是否|哪|几|啥|谁|哪儿")

def _is_interrogative(query):
    return bool(_QUEST_PAT.search(query or ""))


def _is_lookup_intent(query):
    """查看/阅读/了解类动词 + 名词 → 知识问句而非写作请求。
    "介绍一下X/读一下X/看看X/对比一下X/总结一下X"曾被写作词表误伤（词表含
    "介绍/方案/通知"等宽名词）。这些句式命中的是"获取已有信息"，不是"创作
    新文稿"，应走普通问答/知识分支。"""
    return bool(re.search(
        r"(介绍一下|介绍下|简单介绍|读一下|看看|看一下|对比一下|比较一下|"
        r"总结一下|概括一下|简述|了解一下|了解下|梳理一下|说说|讲讲)", query or ""))

# ---------- 技能2 的封装说明 ----------
# 检索归纳技能主体在 searcher.py（浏览器检索+抓正文）+ context.format_evidence
# （临时上下文组装）+ pipeline（生成与销毁）。这里提供统一描述与开关，
# 便于用户按“三个技能”理解模块对应关系。
SKILL_MAP = {
    "技能1·判断": "skills.judge()（规则层 + 模型判定四要素 + need_external）",
    "技能2·检索归纳": "searcher.py + context.format_evidence() + pipeline 搜索分支",
    "技能3·深度思考": "skills.think()（THINK_SAMPLING 低温低熵采样，大预算生成）",
    "外挂深度推理": "skills.ext_think_stream()（OpenAI 兼容，need_ext_hard 规则兜底）",
}
