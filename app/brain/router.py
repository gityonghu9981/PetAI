"""路由判断层：规则前置（纯规则，无模型分支）。

用户约束落地：
- 小模型自估知识截止不可靠 → 时效词命中直接强制搜索，不给模型否决权；
- 判断结果只影响本轮，不写入正式上下文。

外部审核修复（2026-10-27，P0-2，主人确认）：**删除模型判定分支**。
旧代码 `import engine`（engine.py 是纯进程管理模块，没有 chat），
`decide()` 的模型分支必然抛 AttributeError 被 except 吞掉、伪装成
"保守转搜索"——从未成功执行过一次（死代码会骗人）。真正在用的判定
在 skills._model_judge（走 llm.py，主链路一直正常）。
本模块现在只保留真被 skills.py 调用的规则函数：
rule_hit / force_hit / trivial_hit / stable_knowledge_hit；
decide() 保留为纯规则聚合（规则未命中 → 保守转搜索，不再假装问过模型）。
"""
from core import config

# 极简寒暄/自我介绍类：无需联网，直接由模型回答（避免"你好"这类也触发检索）
TRIVIAL_WORDS = [
    "你好", "您好", "嗨", "hello", "hi", "在吗", "谢谢", "感谢", "再见", "拜拜",
    "辛苦了", "你是谁", "你叫什么", "介绍一下你自己", "你能做什么", "你会什么",
    "你能干什么", "有什么功能", "怎么用", "你是谁做的",
]

def trivial_hit(query):
    q = (query or "").strip().lower()
    # 仅"空/单字"判为寒暄。注意不能用 len<=2：两字事实查询（如"金价""油价"）
    # 会被误判成寒暄而不联网搜索（模型凭权重瞎编金价）。
    # 真正的寒暄词都在 TRIVIAL_WORDS 里走下方独立匹配。
    if not q or len(q) <= 1:
        return True
    if len(q) <= 26:
        for w in TRIVIAL_WORDS:
            if w in q:
                return True
    return False

# 稳定常识/百科解释类：解释型问句且无时效/行情风险词 → 直接答（避免被拖去联网搜索）
_KNOW_TERMS = ["什么是", "是什么", "介绍一下", "解释", "原理", "含义", "用途",
               "作用", "组成", "定义", "区别", "概念", "怎么算", "怎么理解"]
_TIME_RISK = ["价格", "金价", "股价", "汇率", "油价", "利率", "新闻", "最新", "今天",
              "今年", "昨天", "目前", "政策", "法规", "比分", "天气", "版本", "发布",
              "出台", "通知", "公告", "行情", "工资", "录取", "分数线"]
# 不稳定知识词：命中则禁止"稳定常识直通"（改走模型判定）。
# 这类对象对本地小模型属于知识截止内不完整/易过时的领域——公司产品动态、
# AI/新兴科技概念、政策金融术语、具体榜单名单——一刀切"直接答"会系统性输出旧信息，
# 正是"搜索比例偏低、直接瞎答"的主要来源。
_UNSTABLE_TERMS = [
    "ChatGPT", "GPT", "Gemini", "Claude", "大模型", "AI", "人工智能",
    "iPhone", "华为", "小米", "苹果", "特斯拉", "比亚迪", "蔚来", "小鹏", "理想",
    "6G", "5G", "量子霸权", "人形机器人", "具身智能", "AIGC", "文生视频", "元宇宙",
    "区块链", "加密货币", "比特币", "央行", "降准", "加息", "监管", "新规", "标准",
    "新能源车企", "排行榜", "榜单", "排名", "名单", "市值", "销量", "财报", "融资",
    "政策", "补贴", "国家战略", "十四五", "碳中和", "碳达峰", "中国特色", "制度",
    "税务", "社保", "公积金", "房贷", "个税", "ESG", "独角兽", "纳斯达克",
    # 多义词/同名概念（本地模型可能自信答错方向，如把物理"三体问题"答成社会学概念）
    "三体",
]

# 强制搜索词：命中直接 need_search=true，不给模型否决权。
# 来源：已知判定器也判"稳定常识"放行、但直接答翻车的概念
#（多义词、前沿概念——模型不知道自己不知道）。
_FORCE_TERMS = ["三体", "量子霸权"]

def stable_knowledge_hit(query):
    """解释类常识问句（什么是X/介绍X/原理）且无时效/行情风险词 → 稳定常识，无需联网。
    命中不稳定知识词（AI/产品/新概念/政策金融/榜单）则不放行，交给模型判定。"""
    if not any(w in query for w in _KNOW_TERMS):
        return False
    if any(w in query for w in _TIME_RISK):
        return False
    if any(w in query for w in _UNSTABLE_TERMS):
        return False
    return True

def rule_hit(query):
    """规则层：闲聊疑问先排除 → 时效词 +（事实查询词 或 实体主题词）→ 强制搜索。

    2026-10-29 主人定调（问答变慢排查）：
    - 旧 FACT_CONTEXT 里"什么/吗/呢/？"是纯语气词，"今天有什么好玩的事吗"
      靠它们凑成双命中强搜，64.7s 搜回字典站垃圾——已从词表删除；
    - 事实索取看"多少/查/价格"这类真查询词，或"金价/天气"这类实体词
      （"今天金价""今天呼和浩特天气"没有查询动词也该强搜）；
    - 闲聊疑问（有什么好玩的/在干嘛）明确排除出强制搜索，不搜直接答。
    返回命中的时效词列表（空则不强制）。
    """
    q = (query or "").strip()
    if not q:
        return []
    # 闲聊排除（带实体词或搜索动词的不算闲聊："聊聊今天金价"仍强搜）
    if any(w in q for w in config.CHAT_CHIT_WORDS):
        has_entity = any(w in q.lower() for w in config.ENTITY_TOPIC_WORDS)
        if not has_entity and ("查" not in q) and ("搜" not in q):
            return []
    time_hits = [w for w in config.TIME_TRIGGER_WORDS if w in q]
    if not time_hits:
        return []
    fact_hits = [w for w in config.FACT_CONTEXT_WORDS if w in q]
    ent_hits = [w for w in config.ENTITY_TOPIC_WORDS if w in q.lower()]
    return time_hits if (fact_hits or ent_hits) else []


def chitchat_hit(query):
    """闲聊疑问快路（2026-10-29 主人定调）：在干嘛/有什么好玩的/聊聊天…
    直接答，不搜索、也不进模型判定——"你在干嘛呢"实测卡在模型判定上，
    日常交流太慢的第二病灶。带实体词/搜索动词的不算闲聊。"""
    q = (query or "").strip()
    if not q:
        return False
    if any(w in q for w in config.ENTITY_TOPIC_WORDS):
        return False
    if ("查" in q) or ("搜" in q):
        return False
    return any(w in q for w in config.CHAT_CHIT_WORDS)

def force_hit(query):
    """强制搜索词：命中即强制搜索（易翻车概念，模型判定不可靠）。"""
    return [w for w in _FORCE_TERMS if w in query]

def decide(query, history_brief=""):
    """纯规则聚合判定。返回 dict: need_search, queries, needs_verify, reason, via。

    （P0-2 修复：模型判定分支已删——旧分支调用了不存在的 engine.chat，
    从未成功执行过，只会在日志里伪装成"判定异常，保守转搜索"。）
    优先级：强制词 > 时效规则 > 寒暄 > 稳定常识 > 保守兜底（转搜索）。
    """
    fh = force_hit(query)
    if fh:
        # B7（主人定调 2026-10-28）：易翻车概念**不再强制搜索**——改成给她一句
        # 提醒 + 把搜索工具放手里，查不查她自己定（词表只有"三体""量子霸权"，
        # 风险很小）。时效词 / 纠错两类仍走强制，那两类放开会牺牲准确性。
        return {
            "need_search": False,
            "queries": [query],
            "needs_verify": False,
            "hint_search": True,
            "hint_words": fh[:3],
            "reason": "易翻车概念，交给她自己决定查不查: " + ",".join(fh[:3]),
            "via": "force_hint",
        }
    hits = rule_hit(query)
    if hits:
        return {
            "need_search": True,
            "queries": [query],
            "needs_verify": True,
            "reason": "规则层命中时效词: " + ",".join(hits[:4]),
            "via": "rule",
        }
    if trivial_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reason": "极简寒暄/自我介绍，无需联网，直接回答", "via": "trivial"}
    if stable_knowledge_hit(query):
        return {"need_search": False, "queries": [], "needs_verify": False,
                "reason": "稳定常识解释类，直接回答", "via": "knowledge"}
    # 规则未命中：保守转搜索（这是明确的业务兜底，不是异常降级）
    return {
        "need_search": True, "queries": [query], "needs_verify": True,
        "reason": "规则层未命中，保守转搜索", "via": "fallback_rule",
    }
