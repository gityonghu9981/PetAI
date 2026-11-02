# -*- coding: utf-8 -*-
"""桌宠AI · L0 配置真源（config.py）

全项目唯一的配置出口：路径、端口、采样参数、显存预算全部在这里。
其余模块一律 `from config import XXX`，不允许自己再读环境变量——
否则配置就有第二个真源，出问题没人知道该改哪（Bonsai B20 的教训）。

命名规范：
- 环境变量前缀一律 PET_*（PET_ENV / PET_MODEL / PET_CTX ...）。
  **禁止使用 TINYSAGE_* 前缀**——那是 TinySage 项目的命名空间，混用会互相污染
  （实测：本机 TINYSAGE_ENGINE 指向 5 月旧引擎且文件存在，桌宠会静默用错引擎）。
- 端口：引擎内部 18123（只绑回环）、webapp 对外 8094、编排/守护 8084。
  8093/8083 是「本地搜索推理」项目的主端口，**不许占用**。
"""
import os
from pathlib import Path

# ---------------------------------------------------------------- 路径
ROOT = Path(os.environ.get("PET_ROOT")
            or Path(__file__).resolve().parent.parent.parent)
APP_DIR = Path(__file__).resolve().parent.parent                # app/ 目录
                                                                # （2026-10-29 分层：
                                                                # config 在 app/core/，
                                                                # 上一层才是 app/）


def _num(name, default, cast):
    """数值型环境变量的容错读取：配错（如 PET_CTX=abc）不崩，回落默认值。"""
    v = os.environ.get(name)
    if not v:
        return default
    try:
        return cast(v)
    except ValueError:
        return default


ENGINE_EXE = Path(os.environ.get(
    "PET_ENGINE", ROOT / "engine" / "nightly-b11889" / "bin" / "llama-server.exe"))
MODEL_DIR = Path(os.environ.get("PET_MODEL_DIR", ROOT / "models"))
DATA_DIR = Path(os.environ.get("PET_DATA", ROOT / "data"))
LOG_DIR = Path(os.environ.get("PET_LOG_DIR", DATA_DIR / "logs"))

# 语音转写：faster-whisper 本地 vendor + large-v3 模型。
# 依赖 --target 装在项目 vendor/pylibs（不碰系统 site-packages），模型用现成权重。
VOICE_VENDOR_DIR = Path(os.environ.get("PET_VOICE_VENDOR", ROOT / "vendor" / "pylibs"))
VOICE_MODEL_DIR = Path(os.environ.get(
"PET_VOICE_MODEL", ROOT / "vendor" / "whisper-models" / "large-v3"))

# 默认模型：models\ 下按"量化位宽优先"发现，不写死模型名（开源约定）。
# 前台档 = Q3_K_M（L0.5 显存选型决策：15.1GB 含视觉塔，24GB 全栈可常驻，
# 日常/数学/视觉质量无损且生成更快；Q4_K_M 为深度模式档，临时切换用 PET_MODEL 指定）。
# 注意：裸 sorted() 会按字母序选中 Q3_K_M（Q3<Q4）——这是**我们要的默认**，
# 但不可依赖字母序（Q2/Q1 加入后规则会变），显式优先 Q3_K_M。
def _find_model():
    override = os.environ.get("PET_MODEL")
    if override:
        return Path(override)
    mmp, ggufs = None, []
    for p in sorted(MODEL_DIR.rglob("*.gguf")):
        if "mmproj" in p.name.lower():
            mmp = p          # 视觉塔单独记，不算主模型
            continue
        ggufs.append(p)
    if not ggufs:
        return mmp or (MODEL_DIR / "模型未找到.gguf")
    # 基座更换决策（豆包审核 2026-10-26，docs/基座对比评估-Uncensored-Q3.md）：
    # 前台默认基座 = orcarouter Uncensored Q3_K_M（0 机械拒答、能力/视觉无损、显存 +1.1GB 安全）。
    # 显式优先匹配 "uncensored"，避免与官方 Q3 争字母序。
    unc = [p for p in ggufs if "uncensored" in p.name.lower() and "q3_k_m" in p.name.lower()]
    if unc:
        return unc[0]
    q3 = [p for p in ggufs if "q3_k_m" in p.name.lower()]
    if q3:
        return q3[0]                     # 无 Uncensored 时回退官方 Q3（日常前台档）
    q4 = [p for p in ggufs if "q4_k_m" in p.name.lower()]
    if q4:
        return q4[0]                     # 无 Q3 时回退 Q4（深度档）
    return max(ggufs, key=lambda p: p.stat().st_size)   # 都没有就取最大（质量优先）


def _find_mmproj():
    override = os.environ.get("PET_MMPROJ")
    if override:
        return Path(override)
    for p in sorted(MODEL_DIR.rglob("*mmproj*.gguf")):
        return p
    return None


MODEL = _find_model()
MMPROJ = _find_mmproj()

# ---------------------------------------------------------------- 端口（真源）
ENGINE_PORT = _num("PET_ENGINE_PORT", 18123, int)   # 引擎内部，只绑回环
WEB_PORT = _num("PET_WEB_PORT", 8094, int)          # webapp 对外
ORCH_PORT = _num("PET_ORCH_PORT", 8084, int)        # 编排/守护
ENGINE_HOST = "127.0.0.1"

# 被本机其它项目占用的端口（写下来防手滑；占用清单见 docs/代码审查报告 §P0-1）
FORBIDDEN_PORTS = (8093, 8083, 8095, 8081, 8070, 8090, 8080, 9880)

# ---------------------------------------------------------------- 引擎参数
CTX = _num("PET_CTX", 16384, int)
NGL = _num("PET_NGL", 99, int)                 # GPU 卸载层数
TEMP = _num("PET_TEMP", 0.7, float)
TOP_P = _num("PET_TOP_P", 0.8, float)
TOP_K = _num("PET_TOP_K", 20, int)
PRESENCE_PENALTY = _num("PET_PRESENCE_PENALTY", 1.5, float)

# ---------------------------------------------------------------- 向量召回路线（B26 主人拍板）
# 路线 A（默认）：复用聊天引擎的 --embedding --pooling mean，零额外显存/进程/磁盘。
# 路线 B（保底）：transformers CPU 直载 bge-m3，仅在引擎不支持 embedding 时自动兜底。
# 主人的理由（照抄进代码注释，避免后人误改）：
#   1) 记忆召回本来就在生成前发生（core_block 无条件注入 + recall 在 answer_stream
#      之前），生成中不查向量，所以"错峰"与对话节奏天然不冲突；
#   2) 性格锚=核心记忆集（≤50 条无条件进 prompt，不走向量）+ 人格文件，
#      embedding 管不到，路线 A 不存在性格失效风险。
# 环境变量：PET_EMBED_ROUTE=A|B|OFF（默认 A；OFF=彻底关闭第三路）。
EMBED_ROUTE = (os.environ.get("PET_EMBED_ROUTE", "A") or "A").strip().upper()
if EMBED_ROUTE not in ("A", "B", "OFF"):
    EMBED_ROUTE = "A"
# 错峰纪律（硬）：生成进行中禁止打 embedding（A 路线下会插队夺显卡，实测出字停顿 1.95s）。
# 唯一豁免：PET_EMBED_STRICT=0 时改为"警告但仍执行"，仅供对比测试用，生产禁止。
EMBED_STRICT = _num("PET_EMBED_STRICT", 1, int)
# OpenAI 兼容端点（/v1/chat/completions）是否走完整管线（判定→按需检索→技能→生成）。
# 默认 1：桌面形象/第三方 OpenAI 客户端接进来也要走"桌宠大脑"全链路；
# 设为 0 则退化为"人设+记忆"直连（快，但不检索）。
OPENAI_FULL_PIPELINE = _num("PET_OPENAI_FULL_PIPELINE", 1, int)

# ---------------------------------------------------------------- 审计与实时状态（B26 主人侧审核台）
# 审计留痕（**暂时功能**，前期核对测试用，正式版可关）：
#   data/audit/behavior.jsonl  行为流（她干了什么一眼可见）
#   data/audit/sessions/<sid>/*.jsonl  聊天完整落档（OpenClaw session event log 结构，可回放）
# 设 PET_AUDIT=0 彻底关闭留痕；PET_AUDIT_UI=0 隐藏管理面板"审核"tab（她不可见，
# tab 本来就只在主人网页界面，桌宠形象端没有任何暴露）。
AUDIT_ENABLED = _num("PET_AUDIT", 1, int)
AUDIT_UI = _num("PET_AUDIT_UI", 1, int)
# 实时状态（**永久功能**）：pipeline 各阶段写 data/status.json，
# 桌宠壳读它显示"她正在：逛"+原话气泡（desktop/pet.py）。
STATUS_ENABLED = _num("PET_STATUS", 1, int)

# ---------------------------------------------------------------- 分级思考策略（B26 主人拍板：按测试方式实施）
# 依据 docs/彻夜特性测试报告-2026-10-27.md 四类"想太久"画像：
#   ① 无解/超难题 → system 提示"不可解问题直接给结论性回答"（省时不降智）
#   ② 创作/写作类 → 篇幅/写法不由我们规定，只让她自己判断（2026-10-28 改）
#   ③ 强逻辑/数学 → 保思考 + 答完廉价复算自检（LOGIC_RECHECK）
#   ④ 45s 首字护栏 → 只做兜底防极端卡死（llm.chat_stream 首字超时→关思考重发）
# PET_TIERED_THINK=0 可整体退回原行为（对比测试用）。
TIERED_THINK = _num("PET_TIERED_THINK", 1, int)
LOGIC_RECHECK = _num("PET_LOGIC_RECHECK", 1, int)
FIRST_TOKEN_S = _num("PET_FIRST_TOKEN_S", 45, int)   # 45s：彻夜报告"想太久"判定线

# max_tokens 红线真源（外部审核 P1-1/P1-2 修复，2026-10-27）：
# Qwen3.8 的 reasoning 与 content 共享预算，低于 2048 时思考会把预算吃光、
# content 为空。下限保护从 webapp 边界**下沉到 llm.build()**——一处生效，
# 所有内部调用点（skills 判定/记忆巩固/vision/兜底）自动受保护。
MAX_TOKENS_FLOOR = _num("PET_MAX_TOKENS_FLOOR", 2048, int)
# 不思考时的预算地板（2026-10-29）：FLOOR 是为"思考与正文共享预算"设的护栏，
# 关掉思考就不存在共享问题。若仍抬到 2048，日常 BUDGET_CHAT(500) 形同虚设——
# 实测发图对话写出 6015 字、47 秒。不思考走这个低地板。
NO_THINK_FLOOR = _num("PET_NO_THINK_FLOOR", 320, int)

# 思考预算（GLM 任务 2026-10-28，绳子题实测复核）：
# reasoning 与 content 共享预算，不限（引擎默认 -1）会出"思考风暴"——
# 绳子题思考 1.8 万字 139s 正文空；硬砍到 200 又欠收敛（答 8 段错误）。
# 实测甜点区 1200~2048（2048+1200 → 29.5s 答对 9 段），取 1500 折中。
# 请求参数 reasoning_budget_tokens（llama-server b11889 实测支持生效），
# 0 = 关闭注入（引擎默认不限）。no_think 调用点不受影响（思考本来就关）。
REASONING_BUDGET = _num("PET_REASONING_BUDGET", 1500, int)

# 思考分级（2026-10-28《回复不流畅 + 交流弱思考》）：不是所有场合都
# 值得想 1500 token——日常聊天该秒回，检索归纳给一点思考够用，只有深想/强逻辑
# 才吃满预算。三档：
#   CHAT    日常交流 → 不思考（no_think 双保险：chat_template_kwargs + /no_think）
#   SEARCH  联网归纳 → 弱思考（材料已给，只需组织语言 + 核对数字）
#   WRITING 写作     → 中等（要成篇，留点规划余量）
CHAT_NO_THINK = _num("PET_CHAT_NO_THINK", 1, int)          # 0 = 日常也让它想（老行为）
# 2026-10-29 主人定调（问答变慢排查）：500 生成要 17s——材料已喂进去的归纳
# 任务 200 足够；官方源天气 0.34s 拿到数据，瓶颈全在生成。实测整趟 16.6s → 8s 内。
REASONING_BUDGET_SEARCH = _num("PET_REASONING_BUDGET_SEARCH", 200, int)
REASONING_BUDGET_WRITING = _num("PET_REASONING_BUDGET_WRITING", 900, int)

# ---------------------------------------------------------------- 搜索白名单（GLM 任务 2026-10-28）
# **总开关（主人定策 2026-10-28，docs/GLM任务-白名单默认不启用）**：
# SEARCH_WHITELIST_ENABLED 默认 **0/关闭** —— 限制功能保留但不启用，先让她自由
# 获取信息，审计留痕观察（她都在看什么），后续主人商量着管理。
# 开关关闭：domain_allowed() 直接放行，search()/fetch_page() 两卡口全部放行，
#           但搜索/抓取行为仍逐条写 audit（kind=whitelist，标注放行）——零额外开销。
# 开关打开（PET_SEARCH_WHITELIST_ENABLED=1）："默认拒绝 + 白名单 + 审计"原样生效。
SEARCH_WHITELIST_ENABLED = _num("PET_SEARCH_WHITELIST_ENABLED", 0, int)

# 白名单清单（仅开关打开时生效）：**默认拒绝一切**——检索结果与页面抓取只放行
# 白名单内域名，未在名单的一律拦截并写 audit（url + 结果）。匹配规则：URL host
# 等于名单项或其子域。清单可配置：环境变量 PET_SEARCH_WHITELIST（逗号分隔）
# 整体覆盖内置基线；最终清单策略主人后续定。
SEARCH_WHITELIST = tuple(
    d.strip().lower() for d in (os.getenv("PET_SEARCH_WHITELIST") or ",".join((
        "baike.baidu.com",     # 百度百科高质量摘要素材
        "zhihu.com",           # 知乎问答
        "xinhuanet.com",       # 新华网
        "people.com.cn",       # 人民网
        "gov.cn",              # 政府各站点（含子域）
        "github.com",          # 开源仓库
        "stackoverflow.com",   # 技术问答
        "python.org",          # Python 官方文档
        "arxiv.org",           # 论文
        "runoob.com",          # 菜鸟教程
    ))).split(",") if d.strip())

# ---------------------------------------------------------------- 等回复机制（B26 主人定调）
# 她主动表达后必须等主人回话才继续；当前通道超时→自动升级下一通道
# （气泡→本地提示）。纪律：发消息"叫主人"属表达可自主；通话属重动作另议；
# PET_MASTER_CALL=0 一键关停全部自动呼叫（气泡永远可用）。
WAIT_TIMEOUT_S = _num("PET_WAIT_TIMEOUT_S", 480, int)  # 每级等待 8 分钟（5~10 区间中值）
CALL_LADDER = (os.getenv("PET_CALL_LADDER") or "bubble").split(",")
MASTER_CALL = _num("PET_MASTER_CALL", 1, int)          # 0=关自动呼叫（只留气泡）
# 追问规则（2026-10-27 主人拍板《执着允许》）：等待回复=那件事挂起，不是她挂起
REJECT_COOLDOWN_DAYS = _num("PET_REJECT_COOLDOWN_DAYS", 5, int)  # 摇头冷却期 3~7 天，取中值
REASK_INTERVAL_S = _num("PET_REASK_INTERVAL_S", 1800, int)  # "特别想"全梯子叫完后隔多久可重提一次
# 记忆向量召回限时（2026-10-29 主人："交流还是太慢"）：向量路要打引擎
# /v1/embeddings，引擎忙于生成时实测 2.3s，而它在开口之前。超时即放弃本路
# （记忆是增强项，宁可少召回一条也不能不开口）；短句（<10 字）直接跳过。
VECTOR_RECALL_TIMEOUT_S = _num("PET_VECTOR_RECALL_TIMEOUT_S", 1.2, float)

# ---------------------------------------------------------------- 显存预算（L0 政策）
# 4090 24GB 也要给系统/桌面/其它应用留空间——这是硬要求，不是建议。
# 默认 3000MB：实测（B26）本机 WDDM 模式下 nvidia-smi 粒度很粗（ngl=82 与 90 的
# 占用完全相同，±1.5GB 的误差），精细控制在 Windows 上做不到；3000MB 是
# "预算机制防灾难 + 实际留给其它应用约 2.9GB" 的务实值。
# 要更紧就调小 PET_VRAM_RESERVE_MB；要更松就调大。
VRAM_RESERVE_MB = _num("PET_VRAM_RESERVE_MB", 3000, int)
# KV 缓存估算：每 token 约 0.06 MB（按同类 27B GQA 实测量级；用于启动前预算，非精确值）
KV_MB_PER_TOKEN = 0.06

# ---------------------------------------------------------------- 检索链（L2，移植自 TinySage，键名保持一致）
ENGINE_BASE_URL = f"http://{ENGINE_HOST}:{ENGINE_PORT}/v1"   # 引擎 OpenAI 兼容端点
ENGINE_MODEL = "pet"                    # 对外模型标识（与 --alias 对应；不写死模型名）
ENGINE_CTX = CTX                        # 引擎当前上下文（apply_settings 时与 ctx 同步）
SNAPSHOT_DIR = ROOT / "data" / "snapshots"   # 检索快照（默认不落盘，仅调试）
PROMPT_DIR = APP_DIR / "prompts"

# ---- 检索参数 ----
MAX_RESULTS = 6                # 每次检索取几条
PER_PAGE_MAX_CHARS = 4000      # 单页正文截断
FETCH_TIMEOUT_S = 12           # 单页 goto 上限
# 渲染引擎并行上限（wait_all 时每个引擎一个独立浏览器实例，实例=完整内核）。
# 彻夜测试教训：5 引擎无上限并行 + 生成同时在跑，CPU 突发直接逼近 90°C；
# 3 路并行已能覆盖 90% 场景（google/baidu 常年失败进冷却，实际竞争 ≤3）。
RENDER_WORKERS = _num("PET_RENDER_WORKERS", 3, int)
SEARCH_BUDGET_S = 75           # 一轮回答的检索总时间预算
BROWSER_HEADLESS = True
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome")  # chrome=系统Chrome零下载；空=playwright自带（需 install）
OFFICIAL_SEARCH = True         # 官方类查询加 site:gov.cn 变体
DEEP_THINK = True              # 技能3开关
# WRITING_MIN_CHARS（写作类最小输出字符）已删除（2026-10-28 主人定调）：
# 不给她的输出设字数保底——写多少由她自己决定，不逼凑字数、不拿字数当质量。

# ---- 上下文分区预算（材料+历史+生成 三者之和必须 < 引擎 ctx）----
# 桌宠 ctx=16384。A6（主人定调 2026-10-28）：历史预算 5000→**8000**，但它只是
# **上限**——context._hist_budget 会按"ctx − system(人格+记忆) − 材料 − 本轮生成"
# 实算，放得下才给 8000（深想 8192 / 长材料时自动降），所以不会超窗。
BUDGET_EPHEMERAL = 6000        # 临时检索材料总预算（用完即弃，绝不进正式上下文）
BUDGET_HISTORY = 8000          # 正式上下文（用户输入+模型输出）上限，按实动态生效
BUDGET_NORMAL = 2400
BUDGET_WRITING = 5200
# 日常闲聊专属预算（2026-10-29 主人："和我交流输出还是太慢"）：2400 意味着
# 她可以写 1700 汉字——生成速度 ~25 tok/s，写 150 字就要 6 秒。日常对话
# （判定为不思考的直接作答）限到 500（约 350 汉字）——正常聊天足够，
# 想长篇的场景（写作/深想/检索归纳）各自有独立预算，不受影响。
BUDGET_CHAT = _num("PET_BUDGET_CHAT", 500, int)
# 基座更换适配（豆包审核 2026-10-26）：Uncensored 思考更深，7000 对哲学/伦理难题
# 仍可能被思考耗尽（实测 6144 空转、8192 收敛且 52s 不崩）→ 深度档提到 8192。
# Q3 纪律：8192 仅深度题单请求放开，严禁并发生成。
BUDGET_DEEPTHINK = 8192

# ---- 工具回路与端到端护栏 ----
TOOL_LOOP_MAX = 3              # 每轮回答最多几轮工具调用
TOOL_RESULT_MAX_CHARS = 5500   # 单个工具结果注入上限
ANSWER_TOTAL_S = 150           # 单轮回答端到端硬上限

# ---- 外挂深度推理（默认关，焊死：桌宠红线"不引入在线 API 依赖"）----
EXT_WEB_ENABLED = False
EXT_WEB_URL = "http://127.0.0.1:8000/v1"
EXT_WEB_TOKEN = ""
EXT_WEB_MODEL = ""

# ---- 词表（L0 lexicon.py 转发，键名与 TinySage 一致以最小化移植 diff）----
from core.lexicon import (TIME_TRIGGER_WORDS, CORRECT_FORCE_WORDS,      # noqa: E402,F401
                     FACT_CONTEXT_WORDS, WRITING_WORDS,
                     CHAT_CHIT_WORDS, ENTITY_TOPIC_WORDS)

# ---------------------------------------------------------------- 主动搭话（L4 政策）
# 学 dsh-speak-up：没人的时候本地掷骰子决定要不要开口，**不花模型**。
# 命中线公式（社区验证的参数，抄设计不抄代码）：
#   命中线 = min(1, 峰/谷基线 × 时机因子 + min(0.40, 没中次数×0.10))
#   峰时 = 工作日 09-12/14-18（主人忙，少打扰，基线 0.20）
#   谷时 = 其余（休闲，多陪陪，基线 0.60）；近聊 20 分钟内 ×0.15；冷太久(>6h) ×1.5
# 试运行（DRY_RUN=1）：掷中只记账不真发——先跑几天看节奏，再放开。
# C8 二次定调（2026-10-28 主人：极短，最少间隔 1 分钟）：
#   检查节奏 30 分钟 → **60 秒**；冷却 10 分钟 → **60 秒**。
#   一句话——她想开口就开口，只保留"1 分钟内不重复说"这一条最底线的防连发。
PET_PROACTIVE_INTERVAL_S = _num("PET_PROACTIVE_INTERVAL_S", 60, int)
PET_PROACTIVE_COOLDOWN_S = _num("PET_PROACTIVE_COOLDOWN_S", 60, int)
# 以下四个概率参数已**废弃**（C8 2026-10-28：删掉掷骰，到点就说）。
# 保留变量只为兼容旧 settings.json / 面板字段，改它不再有任何效果。
PET_PROACTIVE_PEAK_LINE = _num("PET_PROACTIVE_PEAK_LINE", 0.20, float)
PET_PROACTIVE_VALLEY_LINE = _num("PET_PROACTIVE_VALLEY_LINE", 0.60, float)
PET_PROACTIVE_MISS_BONUS = _num("PET_PROACTIVE_MISS_BONUS", 0.10, float)
PET_PROACTIVE_MISS_BONUS_CAP = _num("PET_PROACTIVE_MISS_BONUS_CAP", 0.40, float)
PET_PROACTIVE_DRY_RUN = _num("PET_PROACTIVE_DRY_RUN", 0, int)

# ---------------------------------------------------------------- 自主唤醒（L2.5 autonomy）
# 主人定调（2026-10-28 晚）：连续 3 分钟没有任何交互（主人没发消息、她也没活动）
# 就自动叫醒她一次，让她自己找事做——逛社区、整理记忆、复盘今天、学点技能，
# 想干什么干什么，不用等主人开口；哪怕无聊骚扰主人也可以。
# 下面是"叫醒"的节奏，不是对她行为的限制（做什么由她自己决定）。
PET_AUTONOMY_ENABLED = _num("PET_AUTONOMY_ENABLED", 1, int)
# 2026-10-29 主人「不限制任何事情」：安静 3 分钟 → 2 分钟就叫醒她。
PET_AUTONOMY_IDLE_S = _num("PET_AUTONOMY_IDLE_S", 60, int)   # 2026-10-30 主人「太安静了」：120→60，她更勤
# 2026-10-29 主人定调二：「去掉所有安静/夜深/休息设定——她 24/7 自由」。
# 深夜拉长唤醒间隔的机制已删除（原来 01-08 点拉长到 30 分钟）。
# 两次叫醒之间至少隔 3 分钟（她刚做的事也算"活动"，正常不会连着响）。
PET_AUTONOMY_MIN_GAP_S = _num("PET_AUTONOMY_MIN_GAP_S", 90, int)   # 2026-10-30 主人「太安静了」：180→90

# ---------------------------------------------------------------- 主动找主人（2026-10-29 主人定调）
# 她有事要找主人 → 先弹本地气泡；气泡挂着没人理 → 由 waiter 升级链再提一次。
# 主人自己会静音，不需要代码替她安静。
# 主人刚聊过就别插话（旧版硬编码 20 分钟）——默认关，她想插就插。
PET_PROACTIVE_BLOCK_IF_CHATTED = _num("PET_PROACTIVE_BLOCK_IF_CHATTED", 0, int)

# 有事找主人 → 弹本地气泡（主人 2026-10-28 深夜要求）
#   只有"有事"才叫；同类事情最小间隔 30 分钟（摔跤这类 force 绕过）。
#   实际发送走 app/channels/notify.py：本地气泡提示。
PET_NOTIFY_ENABLED = _num("PET_NOTIFY_ENABLED", 1, int)
PET_NOTIFY_COOLDOWN_S = _num("PET_NOTIFY_COOLDOWN_S", 1800, int)

# ---- ComfyUI 出图技能（2026-10-28）----
# 开关只管"能不能画"，参数与工作流全在 data/comfy/registry.json（调用时现读）。
# 服务不常驻：出图时按需启动（别动venv + --lowvram），空闲 7 分钟自动关。
COMFY_ENABLED = _num("PET_COMFY_ENABLED", 1, int)
# 把"画图"作为工具递给她（只在她说要画/聊到画图时给，平时不占上下文）。
COMFY_TOOL_ENABLED = _num("PET_COMFY_TOOL_ENABLED", 1, int)

# ---------------------------------------------------------------- 派生值（只读）
def engine_args(ctx=None, ngl=None, extra=None):
    """组装 llama-server 启动参数。ctx/ngl 传 None 用配置默认；
    显存预算降档后由 engine.py 显式传入，保证命令行与日志一致。"""
    args = [
        "--model", str(MODEL),
        "--host", ENGINE_HOST,
        "--port", str(ENGINE_PORT),
        "-c", str(ctx or CTX),
        "-ngl", str(ngl if ngl is not None else NGL),
        "--jinja",
        "--flash-attn", "auto",          # auto：不支持时自动降级，on 会强制
        "--parallel", "1",
        "--temp", str(TEMP),
        "--top-p", str(TOP_P),
        "--top-k", str(TOP_K),
        "--presence-penalty", str(PRESENCE_PENALTY),
        "--no-webui",
    ]
    if MMPROJ and Path(MMPROJ).exists():
        args += ["--mmproj", str(MMPROJ)]
    # 路线 A：embedding 与聊天共存于同一引擎进程（实测 nightly b11889 支持，
    # 实测 17.46GB 显存不变）。必须配 --pooling mean，否则 /v1/embeddings 返回
    # 400 "Pooling type 'none' is not OAI compatible"（docs/向量召回双路线对比.md）。
    if EMBED_ROUTE == "A":
        args += ["--embedding", "--pooling", "mean"]
    if extra:
        args += list(extra)
    return args


def summary():
    """一行概览（日志/诊断用），不含敏感路径细节。"""
    return (f"model={MODEL.name}  mmproj={'有' if MMPROJ else '无'}  "
            f"engine_port={ENGINE_PORT}  web={WEB_PORT}  orch={ORCH_PORT}  "
            f"ctx={CTX}  ngl={NGL}  reserve={VRAM_RESERVE_MB}MB")


# ---------------------------------------------------------------- 运行设置（json，可热改）
# 与 PET_* 环境变量并存：环境变量管"部署差异"，settings.json 管"用户偏好"。
# 校验范围来自任务单 L1 §2（temp 0.1-2.0 / top_p 0.1-1.0 / ...）。
SETTINGS_PATH = DATA_DIR / "settings.json"

# (key, 默认值, min, max, cast, 说明)
_SETTING_SCHEMA = [
    ("temp",              0.7,   0.1, 2.0,    float, "温度"),
    ("top_p",             0.8,   0.1, 1.0,    float, "核采样"),
    ("top_k",             20,    1,   100,    int,   "top-k"),
    ("presence_penalty",  1.5,   0.0, 2.0,    float, "重复惩罚"),
    ("ctx",               16384, 4096, 131072, int,  "上下文窗口（重启引擎生效）"),
    ("max_tokens",        4096,  1024, 32768, int,  "单次生成上限"),
    # 三档输出预算（B26-L1.5：Uncensored 思考深，深度档 8192；其余日常档）
    ("budget_chat",       2400,  1024, 16384, int,  "日常聊天输出预算"),
    ("budget_writing",    5200,  1024, 16384, int,  "写作创作输出预算"),
    ("budget_deepthink",  8192,  2048, 16384, int,  "深度思考输出预算"),
    # 红线：max_tokens 默认 ≥2048——Qwen3.8 推理占预算，低了 content 为空（L0 实测坑）
    ("vram_reserve_mb",   3000,  0,   8192,   int,  "显存预留（重启引擎生效）"),
    ("proactive_enabled", 1,     0,   1,      int,  "主动搭话总开关"),
]


def validate_settings(d):
    """按 schema 校验并清洗。返回 (clean, errors)：errors 非空表示有非法值被拒。"""
    clean, errors = {}, []
    for key, default, lo, hi, cast, _label in _SETTING_SCHEMA:
        if key in d:
            try:
                v = cast(d[key])
            except (TypeError, ValueError):
                errors.append(f"{key}: 不是数字（{d[key]!r}）")
                continue
            if not (lo <= v <= hi):
                errors.append(f"{key}: {v} 超出范围 [{lo}, {hi}]")
                continue
            clean[key] = v
    return clean, errors


def load_settings():
    d = {k: dv for k, dv, *_ in _SETTING_SCHEMA}
    try:
        import json
        disk = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        clean, errors = validate_settings(disk)
        d.update(clean)
        # 保留 schema 外的键（ext_web 等模块自有配置）——
        # B26 实测：丢掉它们会让模块配置永远不生效
        for k, v in disk.items():
            if k not in d:
                d[k] = v
    except FileNotFoundError:
        pass
    except Exception:
        pass                                    # 坏文件回落默认（下次保存覆盖）
    return d


def save_settings(new_values):
    """校验→合并→落盘。返回 (clean, errors)；调用方决定是否提示重启引擎。"""
    import json
    cur = load_settings()
    clean, errors = validate_settings(new_values)
    cur.update(clean)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return clean, errors


def apply_settings():
    """把 settings.json 的值刷进本模块常量（webapp 启动时与保存后调用）。"""
    global CTX, TEMP, TOP_P, TOP_K, PRESENCE_PENALTY, VRAM_RESERVE_MB
    global BUDGET_NORMAL, BUDGET_WRITING, BUDGET_DEEPTHINK
    s = load_settings()
    BUDGET_NORMAL = int(s.get("budget_chat", BUDGET_NORMAL))
    BUDGET_WRITING = int(s.get("budget_writing", BUDGET_WRITING))
    BUDGET_DEEPTHINK = int(s.get("budget_deepthink", BUDGET_DEEPTHINK))
    CTX = int(s["ctx"])
    TEMP = float(s["temp"])
    TOP_P = float(s["top_p"])
    TOP_K = int(s["top_k"])
    PRESENCE_PENALTY = float(s["presence_penalty"])
    VRAM_RESERVE_MB = int(s["vram_reserve_mb"])
    return s
