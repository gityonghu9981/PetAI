"""桌宠AI · L1 引擎对话客户端（llm.py）——调用本地 llama-server 的 OpenAI 兼容接口。

与 engine.py（进程管理）分离：一个管"进程活着"，一个管"怎么说话"（学 DeepSeek
Harness 的一能力一模块）。移植自 TinySage 的自愈设计（溢出裁剪/空回答降温重试/
瞬断重试/工具 grammar 降级），ENGINE_BASE_URL 指向桌宠引擎 18123。

自愈设计（修复"大材料导致 400 Bad Request / 空回答崩溃"）：
- 请求超引擎实际上下文时，自动按顺序裁剪：旧历史 → 检索材料尾部 → 重试；
- 返回空 content 且无 tool_calls 时自动降温重试一次。
"""
import json
import time
import urllib.request
import urllib.error
from core import config

# 引擎永远在回环地址（127.0.0.1），**任何代理都不该碰它**。
# B26 实测：开发机工具链 shell 注入 HTTP_PROXY 后，urllib 默认把回环请求也
# 交给代理 → 502/空响应，表现为"材料就位后主生成空回答"。用户本机没这个
# 变量所以"本地没问题"。这里用无代理 opener 根治（回环 + 免配置）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------------------------------------------------------- 生成状态灯（错峰纪律的执行件）
# 用途：向量召回（vector.py）在生成中要"让路"——路线 A 复用同一引擎进程，
# --parallel 1 下 embedding 与生成串行，实测生成中插一条会让出字停顿 1.95s
# （docs/向量召回双路线对比.md）。这盏灯就是"她正在说话，别插队"的信号。
# 放 L1（llm）：只有这里知道生成什么时候开始/结束；L3 memory/vector 只读不写。
import threading as _threading

_GEN_LOCK = _threading.Lock()
_GEN = {"depth": 0, "since": 0.0}


def is_generating():
    """当前是否有生成在跑（含流式未结束）。供上层做错峰判断。"""
    with _GEN_LOCK:
        return _GEN["depth"] > 0


class _Generating:
    """上下文管理器：进入置灯、退出灭灯（支持嵌套/并发请求计数）。"""

    def __enter__(self):
        with _GEN_LOCK:
            if _GEN["depth"] == 0:
                _GEN["since"] = time.time()
            _GEN["depth"] += 1
        return self

    def __exit__(self, *exc):
        with _GEN_LOCK:
            _GEN["depth"] = max(0, _GEN["depth"] - 1)
        return False


def _post(body, timeout=600):
    req = urllib.request.Request(
        config.ENGINE_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _Generating():                 # 非流式：整段请求期间都算"生成中"
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 把响应体带进异常文本：引擎超窗时报错正文才有 "context/exceed"，
        # 否则只剩 "HTTP Error 500"，裁剪自愈无法触发（已确认的报错根因）
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:400]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}") from None


def _is_overflow(err):
    s = str(err).lower()
    return "exceed" in s or "context" in s or "too long" in s or "400" in s


def _is_transient(err):
    s = str(err)
    if isinstance(err, urllib.error.URLError):
        return True
    return any(k in s for k in ("HTTP 500", "HTTP 502", "HTTP 503", "Connection reset",
                               "timed out", "Temporary failure"))


# ---------------------------------------------------------------- 空转检测兜底（B26-L1.5）
# Uncensored 基座思考更深：难题上 reasoning 可达 1.6 万字，与正文共享
# max_tokens 预算 → 预算耗尽时"思考超长、正文 0"（基座评估报告 §二.2，
# 两版基座共有，非拒答）。兜底：正文 0 且 reasoning 超长 → 自动**关思考**
# 重试一次（评估实测关思考 34s 正常答）。
#
# 外部审核 P1-1 修复（2026-10-27）：判据从绝对值 6000 改为**相对预算**——
# 旧判据下，预算给得小的调用点（判定 1200/巩固 1024）reasoning 顶多写到
# 预算就被截断，永远够不到 6000，恰恰是最需要兜底的地方兜底失效。
# 相对判据：reasoning > max(600, 预算×0.8) → 视为"思考吃光预算"。
SPIN_REASONING_CHARS = 6000            # 保留常量：相对判据的绝对下限参考


def _spin(msg, budget=4096):
    """是否空转：正文空、无工具调用、reasoning 超过预算的 80%（相对判据）。"""
    body = (msg.get("content") or "").strip()
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return (not body and not msg.get("tool_calls")
            and len(reasoning) > max(600, int(budget * 0.8)))


def _append_no_think(messages):
    """末条 user 追加 /no_think 软开关（Qwen3 原生），返回新列表不改原参。"""
    msgs = [dict(m) for m in messages]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            c = msgs[i].get("content") or ""
            if isinstance(c, str) and "/no_think" not in c:
                msgs[i]["content"] = c + " /no_think"
            break
    return msgs


def _no_think_body(body, messages):
    """构造关思考的重试请求：chat_template_kwargs（llama.cpp --jinja 支持）
    + 末条 user 追加 /no_think 软开关（Qwen3 原生，双保险）。"""
    b = dict(body)
    b["chat_template_kwargs"] = {"enable_thinking": False}
    # 关思考后预算全归正文，不需要再抬到 4096（2026-10-28 实测：抬升让"只要
    # 一行结果"的复算写满长推理，白烧 56s）；保底 1024 防正文被截断。
    b["max_tokens"] = max(body.get("max_tokens", 1024), 1024)
    msgs = [dict(m) for m in messages]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            c = msgs[i].get("content") or ""
            if isinstance(c, str) and "/no_think" not in c:
                msgs[i]["content"] = c + " /no_think"
            break
    b["messages"] = msgs
    return b


# 材料块定位（千问审核 2026-10-28 修复）：检索材料实际拼在**最后一条 user 消息**
# 的【参考资料】…\n\n【注意】段（context.build_messages），system 里没有材料——
# 旧版在 system 里找【临时检索材料】是空转，超窗砍材料两档（2/3 级）一直失效。
_MAT_MARK = "【参考资料】"
_MAT_TAIL = "\n\n【注意】"


def _shrink_material(content, level):
    """裁 user 消息里的材料块：2=砍前半保后半；3=只留来源索引行。
    【注意】尾巴与查询原样保留（那是回答要求，不是材料）。返回 (新内容, 是否命中)。"""
    head, mark, block = content.partition(_MAT_MARK)
    if not mark:
        return content, False
    tail = ""
    tpos = block.find(_MAT_TAIL)
    if tpos >= 0:
        tail = block[tpos:]
        block = block[:tpos]
    if level >= 3:
        src = "来源["
        lines = block.splitlines()
        block = ("\n".join(ln for i, ln in enumerate(lines) if i < 3 or src in ln)
                 + "\n（材料仅剩来源索引，请基于摘要谨慎作答并说明依据有限）")
    else:
        block = "(前段已省略)\n" + block[len(block) // 2:]
    return head + _MAT_MARK + block + tail, True


def _shrink(messages, level):
    """按级裁剪：1=砍最旧历史 2=砍材料到一半 3=材料只留3条摘要级。
    2/3 级裁的是最后一条 user 消息的【参考资料】块（材料真实所在，
    千问审核 2026-10-28 修复——旧版找 system 里的【临时检索材料】永远落空）。"""
    msgs = [dict(m) for m in messages]
    sysm = msgs[0]
    rest = msgs[1:-1]
    last_user = msgs[-1]
    if level == 1:
        # 保留 system + 最近 4 条历史
        rest = rest[-4:]
    elif level >= 2:
        c = last_user.get("content")
        if isinstance(c, str):
            newc, hit = _shrink_material(c, level)
            if hit:
                last_user["content"] = newc
    return [sysm] + rest + [last_user]


def _stream_events(body, stats=None, timeout=600, first_token_s=0):
    """单次流式请求的底层事件泵（chat_stream 的实现件）。

    stats（可选）：{"content": int, "reasoning": int}——累计两类增量字符数，
    供上层做空转判定。reasoning 增量**不外发**（管线只吃正文）。

    first_token_s（B26 分级策略④，默认取 config.FIRST_TOKEN_S）：
    开场读超时——连接+等第一个增量若 45s 毫无动静（含思考流）视为极端卡死，
    抛 TimeoutError 交给 chat_stream 做关思考重发兜底。收到第一个增量后把
    读超时放宽回完整 timeout（正常思考会持续吐 reasoning 块，不会误伤）。
    """
    req = urllib.request.Request(
        config.ENGINE_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    open_to = min(timeout, first_token_s) if first_token_s else timeout
    # 流式：从请求发出到最后一个 token 收完都算"生成中"（生成器关闭也会灭灯）
    with _Generating(), _OPENER.open(req, timeout=open_to) as resp:
        if first_token_s and open_to < timeout:
            try:                     # 头进来后放宽读超时：45s 只守"开场没动静"
                resp.fp.raw._sock.settimeout(timeout)
            except Exception:
                pass                 # 放宽失败就整段 45s 节拍（仍可接受的兜底）
        tool_acc, tool_order = {}, []
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
                if stats is not None:
                    stats["content"] += len(delta["content"])
                yield {"delta": delta["content"]}
            if stats is not None and delta.get("reasoning_content"):
                stats["reasoning"] += len(delta["reasoning_content"])
            tc = delta.get("tool_calls")
            if tc:
                for t in tc:
                    idx = t.get("index", 0)
                    acc = tool_acc.setdefault(
                        idx, {"id": "", "type": "function",
                              "function": {"name": "", "arguments": ""}})
                    if idx not in tool_order:
                        tool_order.append(idx)
                    fn = t.get("function") or {}
                    if t.get("id"):
                        acc["id"] = t["id"]
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
        if tool_acc:
            yield {"tool_calls": [tool_acc[i] for i in sorted(tool_order)]}


def _rb(override=None):
    """思考预算取值：按调用覆盖优先（思考分级），否则 config.REASONING_BUDGET；
    0=不注入（引擎默认思考）。用 getattr 兜底——热加载/旧配置对象缺字段时不炸。

    分级（2026-10-28 主人定调"和我交流弱思考或不思考"）：
    - 日常对话 → no_think=True（完全不思考，1~3s 开口）
    - 检索归纳 → REASONING_BUDGET_SEARCH（弱思考，够用不磨蹭）
    - 深想/强逻辑 → REASONING_BUDGET（1500 甜点区）
    """
    if override is not None:
        try:
            return max(0, int(override))
        except Exception:
            pass
    try:
        return int(getattr(config, "REASONING_BUDGET", 0) or 0)
    except Exception:
        return 0


def chat_stream(messages, tools=None, temperature=0.6, max_tokens=2048,
                json_mode=False, timeout=600, no_think=False,
                reasoning_budget=None):
    """标准 OpenAI 流式（SSE）对话：yield 事件字典，逐 token 增量。

    事件形态（兼容 llama.cpp 系引擎的 OpenAI 流式响应）：
      {"delta": "文本增量"}            —— choices[0].delta.content
      {"tool_calls": [完整工具调用列表]} —— 流式 tool_calls 增量合并后的最终列表
    流结束即生成器自然退出（不 yield [DONE]）。
    不做裁剪重试（主生成是最后一环；上层捕获异常可降级非流式）。

    空转兜底（B26-L1.5）：首流正文 0 且 reasoning 超长（Uncensored 思考深、
    预算被思考吃光）→ 自动关思考重发一次（评估实测 34s 正常答）。

    no_think（2026-10-28，主人报日常问答慢）：日常直答关思考——闲聊/常识题
    不再想几百 token 才开口（实测"你好在干嘛"想 300 token / 7 秒）。
    双保险与 _no_think_body 同款：chat_template_kwargs enable_thinking=False
    + 末条 user 追加 /no_think（Qwen3 原生软开关）。
    """
    _msgs = [dict(m) for m in messages]
    # 2026-10-29 修正：地板只防"思考吃光预算导致 content 空"，**不思考时不需要**。
    # 之前无脑抬到 MAX_TOKENS_FLOOR(2048)，把日常 BUDGET_CHAT(500) 抬成 2048——
    # 实测发图对话写出 6015 字（47 秒）就是它干的。no_think 走低地板。
    _floor = (config.NO_THINK_FLOOR if no_think else config.MAX_TOKENS_FLOOR)
    body = {
        "model": config.ENGINE_MODEL, "messages": _msgs,
        # P1-2 修复（2026-10-27）：流式路径同样吃预算下限保护（红线真源在 config）
        "temperature": temperature,
        "max_tokens": max(int(max_tokens), _floor),
        "stream": True,
        "repeat_penalty": 1.15,  # 小模型长生成防退化循环（已确认的重复刷屏问题）
    }
    if no_think:
        # 日常直答：首请求就关思考（不再等空转兜底才关）
        body["chat_template_kwargs"] = {"enable_thinking": False}
        for i in range(len(_msgs) - 1, -1, -1):
            if _msgs[i].get("role") == "user":
                c = _msgs[i].get("content") or ""
                if isinstance(c, str) and "/no_think" not in c:
                    _msgs[i]["content"] = c + " /no_think"
                break
    elif _rb(reasoning_budget):
        # 思考预算（GLM 任务 2026-10-28）：流式主对话同款注入，非思考关闭路径。
        # reasoning_budget 按调用分级覆盖（检索归纳弱思考 vs 深想 1500）。
        body["reasoning_budget_tokens"] = _rb(reasoning_budget)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    stats = {"content": 0, "reasoning": 0}
    guard = max(0, int(getattr(config, "FIRST_TOKEN_S", 0) or 0))
    try:
        for ev in _stream_events(body, stats, timeout=timeout,
                                 first_token_s=guard):
            yield ev
    except TimeoutError:
        # 45s 兜底（分级策略④，主人拍板 2026-10-27）：开场极端卡死（没有任何
        # 增量）→ 关思考重发一次。已有正文后卡死不重发（重发会整段重来，
        # 交给上层既有的异常降级路径处理）。
        if guard and stats["content"] == 0 and not tools:
            try:
                from core import audit as _audit
                _audit.log("fallback", f"{guard}s 无任何增量，关思考重发（45s兜底）")
            except Exception:
                pass
            for ev in _stream_events(_no_think_body(body, messages),
                                     timeout=timeout):
                yield ev
        else:
            raise
    # 空转判定：无工具场景下正文 0 且思考超长 → 关思考重发一次
    # （P1-1 修复：相对判据——reasoning 超过预算 80% 即算吃光预算，
    #   低预算调用点也能进兜底，不再要求绝对 6000 字）
    if (stats["content"] == 0 and not tools
            and stats["reasoning"] > max(600, int(max_tokens * 0.8))):
        for ev in _stream_events(_no_think_body(body, messages), timeout=timeout):
            yield ev


def chat(messages, tools=None, temperature=0.6, max_tokens=2048, json_mode=False,
         timeout=600, no_think=False, reasoning_budget=None):
    """单次非流式对话，带溢出自愈。返回 (message dict, usage)。

    timeout：单次请求的最长等待秒数（含重试）。上层（pipeline 编排）可传
    "剩余预算"以实现端到端总时长硬上限，防止高难度问题+超大上下文把请求
    拖到 10 分钟（避免卡死/穷举）。默认 600 保持兼容。

    no_think（外部审核 P1-1 修复，2026-10-27）：显式关思考——判定/提炼这类
    **结构化短输出任务**不需要思考，关了又快又稳，也根除"思考吃光小预算
    导致 JSON 截断"（skills 判定 fallback、记忆巩固静默失效的共同根因）。
    双保险与 _no_think_body 同款：chat_template_kwargs + 末条 user 追加
    /no_think（Qwen3 原生软开关）。

    max_tokens 下限保护（P1-2 修复）：build() 里 max(传入, MAX_TOKENS_FLOOR)
    ——红线从 webapp 边界下沉到此处，**所有内部调用点自动受保护**，
    新增调用点传小值不再静默踩坑。

    说明：只使用标准 OpenAI 兼容字段（model/messages/temperature/max_tokens/
    stream/tools/tool_choice/response_format）+ 一个实测过的引擎扩展字段
    reasoning_budget_tokens（思考预算，llama-server b11889 实测支持生效，
    GLM 任务 2026-10-28；对不认识该字段的引擎通常忽略，无害）
    （chat_template_kwargs 是 llama.cpp --jinja 的标准支持项，no_think 才带）。
    """
    # 预算下限：reasoning 与 content 共享预算，低于 FLOOR 时 content 必空
    # （no_think 时不思考、不存在共享问题 → 用低地板，别把日常短回复抬成长篇）
    max_tokens = max(int(max_tokens),
                     config.NO_THINK_FLOOR if no_think else config.MAX_TOKENS_FLOOR)

    def build(msgs):
        body = {
            "model": config.ENGINE_MODEL, "messages": msgs,
            "temperature": temperature, "max_tokens": max_tokens, "stream": False,
            "repeat_penalty": 1.15,  # 小模型长生成防退化循环（已确认的重复刷屏问题）
        }
        if no_think:
            # 显式关思考（结构化短输出专用）——不再靠"给小预算顺便省时间"
            body["chat_template_kwargs"] = {"enable_thinking": False}
        elif _rb(reasoning_budget):
            # 思考预算（GLM 任务 2026-10-28，绳子题实测）：限制思考 token 数，
            # 剩余预算自动留给正文——根除"思考风暴挤光正文"（引擎默认不限，
            # 绳子题 139s 思考 1.8 万字正文空）。甜点区 1200~2048，默认 1500。
            # reasoning_budget 按调用分级覆盖（2026-10-28：检索=弱思考）。
            body["reasoning_budget_tokens"] = _rb(reasoning_budget)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    # no_think 软开关双保险：末条 user 追加 /no_think（Qwen3 原生）
    if no_think:
        messages = _append_no_think(messages)

    cur = messages
    data, err = None, None
    retried_transient = False
    tool_retried = False
    for level in (0, 1, 2, 3):
        if level:
            cur = _shrink(messages, level)
        while True:
            try:
                data = _post(build(cur), timeout=timeout)
                break
            except Exception as e:
                err = e
                # 去对齐模型 + 工具(grammar) 不兼容：模型想调工具但输出不匹配
                # llama.cpp 的 JSON/grammar 校验（"peg-native format"）→ 降级为
                # 无工具纯文本重试一次，避免"HTTP 500"直接崩给用户（已确认）。
                if tools and not tool_retried and any(
                        k in str(e) for k in ("peg-native", "grammar",
                                              "does not match the expected",
                                              "tool_choice")):
                    tool_retried = True
                    body2 = build(cur)
                    body2.pop("tools", None)
                    body2.pop("tool_choice", None)
                    try:
                        data = _post(body2, timeout=timeout)
                        break
                    except Exception as e2:
                        err = e2
                    continue
                if _is_transient(e) and not retried_transient:
                    # 引擎可能正在重启（用户刚点过"保存并重启引擎"）：等12秒原样重试一次
                    retried_transient = True
                    time.sleep(12)
                    continue
                if _is_overflow(e):
                    break  # 交给外层按级裁剪
                raise
        if data is not None:
            break
    if data is None:
        raise RuntimeError(f"引擎请求失败(裁剪{level}级仍溢出): {str(err)[:200]}")

    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})
    # 空转兜底（先于通用空回答重试）：正文 0 且 reasoning 超预算 → 关思考重试。
    # 通用降温重试对空转无效（思考照开、预算更小只会更快耗尽）。
    # （P1-1：相对判据 _spin(msg, budget)，低预算调用点也能触发）
    if _spin(msg, max_tokens):
        try:
            d2 = _post(_no_think_body(build(cur), cur), timeout=timeout)
            m2 = d2["choices"][0]["message"]
            if (m2.get("content") or "").strip():
                return m2, d2.get("usage", {})
        except Exception:
            pass
    # 空回答自愈：既无内容也无工具调用 → 降温重试一次
    # （P1-1 修复：不再把预算砍到 1500——reasoning 与 content 共享预算，
    #   砍预算只会更快耗尽、复发"content 空"；保持完整预算只降温重试）
    if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
        retry = build(cur)
        retry["temperature"] = min(temperature, 0.3)
        try:
            d2 = _post(retry, timeout=timeout)
            m2 = d2["choices"][0]["message"]
            if (m2.get("content") or "").strip():
                return m2, d2.get("usage", {})
        except Exception:
            pass
    return msg, usage
