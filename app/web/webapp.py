# -*- coding: utf-8 -*-
"""桌宠AI · L4 接口层（webapp.py）

只做"接口"：路由、SSE 组帧、状态聚合。业务不在这里——
对话记忆在 memory.py、主动搭话在 proactive.py、显存预算在 vram.py、
引擎进程在 engine.py、参数与校验在 config.py（L0/L1/L2 分层见各模块头）。

路由一览：
  GET  /                       网页（聊天 / 流式显示 / 状态条）
  POST /api/chat               桌宠对话（SSE；召回注入 → 引擎流式 → 会话捕获）
  GET  /api/status             引擎 + 显存 + 记忆 的聚合状态
  GET  /api/memory             长期记忆列表（active）
  POST /api/memory/remember    手动教她一条记忆
  POST /api/memory/forget      失效一条记忆（失效而非删除）
  POST /api/proactive/tick     桌宠壳心跳：要不要主动开口（本地掷骰，零算力）
  GET/POST /api/settings       采样参数读写（schema 校验，改 ctx 需重启引擎）
  GET  /v1/models              OpenAI 兼容
  POST /v1/chat/completions    OpenAI 兼容（stream=true 真 SSE）

红线遵守：max_tokens 默认 4096（≥2048，Qwen3.8 推理占预算）；不关思考模式；
只绑回环；纯本地，无任何在线依赖。
"""
import sys
from pathlib import Path

# 入口自举（2026-10-29 分层）：本文件在 app/web/，而各层子包（core/brain/…）
# 都挂在 app/ 下——把 app 根插进 sys.path，`from core import config` 才能解析。
# 只有入口需要这段；被导入的模块不再自己动 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import re
import threading
import time
import urllib.request

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)

from web import api_line
from core import audit
from growth import autonomy
from core import config
from brain import context
from tools import doctor
from core import engine
from brain import memory
from brain import message_memory
from brain import memory_consolidate as memcon   # 2026-10-30：主人状态类消息即时沉淀（C7 治本）
from brain import media_in
from brain import pipeline
from growth import proactive
from growth import scheduler
from brain import searcher
from growth import skillbook
from core import status as status_light
from core import vram
from growth import wishes
from channels import waiter

config.apply_settings()                        # 启动即加载用户设置

app = FastAPI(title="桌宠AI", docs_url=None, redoc_url=None)
_UI = Path(__file__).resolve().parent / "ui.html"    # 前端与入口同目录（分层后）
_sid_locks, _sid_guard = {}, threading.Lock()
MAX_TOKENS_FLOOR = config.MAX_TOKENS_FLOOR     # 红线真源已下沉 config（P1-2）：
                                               # llm.build() 统一保护，此处保兼容


def _sid_lock(sid):
    with _sid_guard:
        if sid not in _sid_locks:
            _sid_locks[sid] = threading.Lock()
        return _sid_locks[sid]


# ---------------------------------------------------------------- 会话与检索器
_sessions = {}
_sessions_guard = threading.Lock()


def get_session(sid):
    """会话对象（context.Session：JSONL 落盘、历史预算裁剪）。
    pipeline 独占会话写路径；memory.py 独占长期记忆——两库分工见任务单 L2 红线。"""
    with _sessions_guard:
        sess = _sessions.get(sid)
        if sess is None:
            sess = context.Session(sid)
            _sessions[sid] = sess
        return sess


_searcher = None
_searcher_guard = threading.Lock()


def get_searcher():
    """检索器懒加载单例。构造失败返回 None——pipeline 对 searcher=None 有
    优雅降级（本地直接答 + 说明没搜到），检索是增强不是依赖。"""
    global _searcher
    with _searcher_guard:
        if _searcher is None:
            try:
                _searcher = searcher.Searcher()
            except Exception:
                _searcher = False          # False = 尝试过且失败，不再反复构造
    return _searcher or None


def _engine_post(body, timeout=600):
    req = urllib.request.Request(
        f"http://{config.ENGINE_HOST}:{config.ENGINE_PORT}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    # 无代理 opener：同 llm.py——回环请求绝不过代理（B26 实测 502/空响应根因）
    return _NOPROXY.open(req, timeout=timeout)


def _sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ---------------------------------------------------------------- 网页
@app.get("/", response_class=HTMLResponse)
def index():
    return _UI.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 桌宠对话（SSE）

# ---------------------------------------------------------------- L1.5 技能学习确认流
_RE_LEARN = re.compile(
    r"(?:教你|我想让你学|让你学会|学一下)(?:会|用|怎么用|如何|操作)?\s*"
    r"([\u4e00-\u9fffA-Za-z0-9+/]{2,20})")


def _learn_intent(query):
    """识别主人的"教学"意图，返回技能名或 None。
    她**自己发起**的学习不走这里（那是对话，模型自己提）——这里处理的是
    主人主动教她的场景；草稿状态主人不点头就不生效（skillbook 闸门）。"""
    m = _RE_LEARN.search(query or "")
    return m.group(1).strip() if m else None


@app.post("/api/chat")
async def api_chat(req: Request):
    """桌宠对话：pipeline.answer_stream 全链路（判定→按需检索→生成→后处理）。

    记忆两挂钩（OpenViking 模式）：对话前 recall 注入（memory_block，不占检索
    预算）；对话后 capture 进会话缓冲。检索是增强不是依赖——纯聊天 pipeline
    判定后直接本地作答，searcher 失败时优雅降级。"""
    body = await req.json()
    raw_query = body.get("query")
    # 图片（2026-10-29 修"两次发图两次异常降级"）：query 可能是 OpenAI
    # content 数组（含 image_url），统一走 media_in 拆文本 + 视觉描述。
    query, img_meta = media_in.compose_query(raw_query, body.get("text") or "")
    if img_meta.get("count"):
        audit.log("vision", f"收到主人图片 {img_meta['count']} 张，"
                            f"理解成功 {img_meta.get('ok', 0)} 张："
                            f"{','.join(Path(p).name for p in img_meta.get('paths') or [])}",
                  session_id=(body.get("session_id") or "default"))
    sid = (body.get("session_id") or "default").strip() or "default"
    if not query:
        return JSONResponse({"error": "内容不能为空"}, status_code=400)

    # 等回复机制（B26）：主人在聊天页回话 = 解锁信号——她在 waiting_master
    # 时，这条回复就是"主人接话了"，清等待帧+审计留痕；回复本身照常走
    # pipeline 进上下文（她想起来自己刚才说了什么、主人怎么接的）。
    try:
        waiter.master_replied(query, "bubble")
    except Exception:
        pass                                   # 解锁检查坏了不影响正常聊天

    # L1.5 技能学习：主人教她 → 建草稿（不发布），并在注入里提示她走
    # "问清用途→记步骤→请主人点头发布"的确认流程。学什么由她发起或主人教，
    # 系统不预置（不做清单 #6）。
    learn_name = _learn_intent(query)
    learn_hint = ""
    if learn_name:
        try:
            # 触发词从技能名提炼（"怎么写周报"→"周报"）：学习流建的技能必须
            # 能被触发，否则发布了也是死档（B26 端到端实测抓出）。
            trigger = re.sub(r"^(怎么写|怎么|学会?写|学会?用|写|用)", "", learn_name)
            triggers = [trigger] if len(trigger) >= 2 else [learn_name]
            skill_md = (f"# {learn_name}\n\n## 主人教的（{time.strftime('%F')}）\n\n"
                        f"- 原话：{query}\n"
                        f"- （后续对话确认的细节往这里补）\n")
            skillbook.create(learn_name, title=learn_name, triggers=triggers,
                             skill_md=skill_md)
            learn_hint = (f"【主人正在教你『{learn_name}』】先自然地问清楚：它"
                          f"是干嘛的、怎么用、什么时候用；把要点记成步骤，"
                          f"已为你建好技能草稿，学会后提醒主人点头发布。")
        except Exception:
            pass

    # 记忆注入（L3）：核心记忆集（≤50 条无条件进 prompt，独立预算，
    # 检索砍不到——红线）+ 与本次输入相关的召回块，合并去重
    # 2026-10-29 二审修正：这一整段（SQLite 召回 + 技能读档 + 会话文件加载）
    # 全是同步 IO，原来直接跑在事件循环上——自主唤醒/消息轮询并发时会把
    # 整个 webapp（含 SSE 心跳）卡住。挪进线程池。
    def _build_block():
        core, _cu = memory.core_block(budget_tokens=600)
        rel, _ru = memory.recall(query, budget_tokens=1600)
        seen_lines = set()
        parts = []
        for seg in (core, rel):
            for ln in (seg or "").splitlines():
                ln = ln.strip()
                if ln and ln not in seen_lines:
                    seen_lines.add(ln)
                    parts.append(ln)
        if learn_hint:
            parts.append(learn_hint)
        block_ = "\n".join(parts)
        # L5 技能按任务加载：命中触发词才读档（平时 0 占用），≤配额截断
        skill_block, _sk_u, _sk_names = skillbook.load_for(query)
        if skill_block:
            block_ = (block_ + "\n\n" + skill_block).strip()
        # ComfyUI：三件事都要她知道——刚画好的图 / 上次画失败 / 上轮说要去画
        # 其实没动（空承诺）。2026-10-29 主人抓的那次"以为自己在画"就靠这个纠正。
        try:
            from tools import comfy as _comfy
            _hint = _comfy.context_hints()
            if _hint:
                block_ = (block_ + "\n" + _hint).strip()
        except Exception as e:
            try:
                from core import audit as _audit
                _audit.log("comfy", f"提示注入失败：{type(e).__name__} {e}")
            except Exception:
                pass
        # 2026-11-01：照片收藏提示（让她知道收藏里有什么，别答"我没有收藏夹"）
        try:
            from channels import photo_share as _ps
            _h = _ps.context_hint(query)
            if _h:
                block_ = (block_ + "\n\n" + _h).strip()
        except Exception:
            pass
        return block_.strip()

    block = await run_in_threadpool(_build_block)
    session = await run_in_threadpool(get_session, sid)
    lk = _sid_lock("pet:" + sid)
    if not lk.acquire(blocking=False):
        return JSONResponse({"error": "上一条还在生成中"}, status_code=429)

    q = __import__("queue").Queue()

    def worker():
        try:
            def on_delta(t):
                q.put(("tok", t))

            def on_step(name, detail=""):
                q.put(("step", f"{name} {detail}".strip()))
                # 状态灯：她正在做什么 → data/status.json（桌宠形象读它显示）。
                # 2026-10-29：这里之前只往前端推，状态文件从头到尾是 idle，
                # 所以主人看到的形象永远"发呆"——漏的就是这一行。
                try:
                    status_light.from_step(name, detail)
                except Exception:
                    pass

            r = pipeline.answer_stream(query, session, searcher=get_searcher(),
                                       on_delta=on_delta, on_step=on_step,
                                       memory_block=block or None)
            q.put(("done", r))
        except Exception as e:
            try:
                audit.log("error", f"{type(e).__name__}: {str(e)[:200]}",
                          session_id=sid)
            except Exception:
                pass
            try:
                status_light.set_state("error", "这轮出了点状况")
            except Exception:
                pass
            q.put(("err", f"{type(e).__name__}: {str(e)[:200]}"))
        finally:
            # 锁无条件先释放（B26 教训：finally 里后续动作抛异常会锁死会话）
            lk.release()
            try:
                memory.capture("user", query)
                message_memory.maybe_remember_user_status(query)   # C7 即时沉淀
                memcon.capture_increment(sid, "user", query)        # 任务 2：增量入缓冲
                memcon.schedule_flush(sid, _extract_session_facts)  # 任务 2：5 分钟后排程
            except Exception:
                pass

    def gen():
        got = []
        while True:
            try:
                kind, payload = q.get(timeout=20)
            except __import__("queue").Empty:
                yield ": ping\n\n"                    # 检索/思考期心跳
                continue
            if kind == "tok":
                got.append(payload)
                yield _sse({"delta": payload})
            elif kind == "step":
                yield _sse({"step": payload})           # 前端可显示"搜索中…"
            elif kind == "done":
                r = payload or {}
                answer = "".join(got)
                memory.capture("pet", answer)
                memcon.capture_increment(sid, "assistant", answer)  # 任务 2
                proactive.note_real_chat()              # 真聊过 → 近聊折扣
                # 2026-11-01 照片分享（确定性层）：主人说"看/继续"发下一张；
                # 没说就等有空闲聊时问一句（一次只发一张，规则见 photo_share）。
                try:
                    from channels import photo_share as _photo_share
                    answer, _photo_extra = _photo_share.post_answer(query, answer, sid)
                    if _photo_extra:
                        yield _sse({"delta": _photo_extra})
                except Exception:
                    pass
                # B26：区分"引擎挂"与"真无结果"——用户该知道这次回答的底气
                st = r.get("search_status")
                if st == "engine_fail":
                    yield _sse({"step": "⚠ 检索引擎暂时都不可用，这条凭本地知识回答，可能不是最新"})
                elif st == "no_result":
                    yield _sse({"step": "没搜到相关资料，这条凭本地知识回答"})
                yield _sse({"finish": "stop",
                            "answer": answer,
                            "sources": r.get("sources", []),
                            "used_search": r.get("used_search", False),
                            "search_status": st,
                            "decision": r.get("decision", {}),
                            "timing": r.get("timing", {})})
                yield "data: [DONE]\n\n"
                return
            elif kind == "err":
                yield _sse({"error": payload})
                yield "data: [DONE]\n\n"
                return

    threading.Thread(target=worker, daemon=True).start()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 聊天上下文（只读）
@app.get("/api/chat/history")
def api_chat_history(sid: str = "default", limit: int = 30):
    """聊天窗上下文：返回会话最近 N 条 user/assistant 正文（只读，不进 LLM）。

    2026-11-01 主人定调：点桌宠主动搭话的气泡 → 进 8094 聊天界面，要能看到
    她刚才说了什么、上下文接得住。复用 context.Session 的已落盘历史。
    """
    sid = (sid or "default").strip() or "default"
    try:
        s = context.Session(sid)
        rows = s.formal[-max(1, min(int(limit), 200)):]
    except Exception:
        rows = []
    out = [{"role": m.get("role"), "content": m.get("content") or "", "ts": m.get("ts")}
           for m in rows if m.get("role") in ("user", "assistant")]
    return {"sid": sid, "messages": out}


# ---------------------------------------------------------------- 她的收藏（照片）
_PHOTO_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "avif"}   # 与 hands._PIC_EXT 同口径


@app.get("/photos/{rel_path:path}")
def api_photo_file(rel_path: str):
    """给聊天窗/图库回照片文件：`photos/YYYY-MM/文件名.<图片后缀>` 的相对路径。

    后缀不能只认 .png（2026-11-01 实测的坑）：她的收藏里有两条来源——
    `capture_photo` 拍屏是 .png，而 `save_picture` 从网上收图是 .jpg/.webp（§18）。
    只认 .png 时那些 jpg/webp 的地址一律 404，主人看到的就是"只有地址、图打不开"。
    安全口径不变：必须正好两段、末段取 `Path().name`（吃掉任何 `../`）、只读该目录下的文件。
    """
    from tools import hands as _hands
    parts = [p for p in (rel_path or "").split("/") if p]
    if len(parts) != 2 or Path(parts[1]).name.lower().rsplit(".", 1)[-1] not in _PHOTO_EXTS:
        return JSONResponse({"error": "not found"}, status_code=404)
    # 第一段必须是收藏的月份目录（YYYY-MM）。2026-11-01 审核实测：只校验"两段"是不够的——
    # 传 `../zz_probe.png` 会拼成 data/photos/../zz_probe.png 并被 200 读出来（越出收藏目录）。
    if not re.fullmatch(r"\d{4}-\d{2}", parts[0]):
        return JSONResponse({"error": "not found"}, status_code=404)
    p = _hands.PHOTO_DIR / parts[0] / Path(parts[1]).name
    if not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


@app.get("/api/photos")
def api_photos(unseen_only: bool = False, limit: int = 30):
    """她的收藏清单（主人看图库 / 她回看）。"""
    from tools import hands as _hands
    return {"unseen": _hands.unseen_count(),
            "photos": _hands.list_photos(unseen_only=unseen_only,
                                         limit=max(1, min(limit, 100)))}


@app.post("/api/photos/capture")
async def api_photo_capture(req: Request):
    """主人当轮指令：现在拍一张收进收藏（自主路径是 autonomy_act("photo")）。"""
    from tools import hands as _hands
    try:
        body = await req.json()
    except Exception:
        body = {}
    r = _hands.capture_photo(note=(body or {}).get("note", ""),
                             tags=(body or {}).get("tags", ""),
                             source="master")
    return r


# ---------------------------------------------------------------- 图片静态服务
# 她给主人发的图（ComfyUI 产物等）放 data/petmedia/，走 /files/{name}。
# 网页和手机 App 的聊天窗都能直接渲染这个地址的 markdown 图片。
_PETMEDIA = Path(config.DATA_DIR) / "petmedia"


@app.get("/files/{name}")
def api_files(name: str):
    safe = Path(name).name                     # 防目录穿越：只认文件名
    p = _PETMEDIA / safe
    if not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


# ---------------------------------------------------------------- 状态与记忆
@app.get("/api/status")
def api_status():
    e = {"health": engine.is_running(), "ctx": config.CTX}
    g = vram.query_gpu() or {}
    m = memory.stats()
    return {"engine": e, "vram": g, "memory": m,
            "ports": f"engine={config.ENGINE_PORT} web={config.WEB_PORT}",
            "vram_report": vram.report()}


@app.get("/api/memory")
def api_memory():
    return {"facts": memory.list_facts(100), "stats": memory.stats()}


@app.post("/api/memory/remember")
async def api_remember(req: Request):
    d = await req.json()
    fid, merged = memory.remember((d.get("content") or "").strip(),
                                  kind=d.get("kind") or "fact",
                                  polarity=int(d.get("polarity") or 0))
    if fid is None:
        return JSONResponse({"error": "内容不能为空"}, status_code=400)
    return {"id": fid, "merged": merged, "stats": memory.stats()}


@app.post("/api/memory/forget")
async def api_forget(req: Request):
    d = await req.json()
    n = memory.expire((d.get("content_substr") or "").strip())
    return {"expired": n}


@app.post("/api/memory/promote")
async def api_promote(req: Request):
    """核心记忆集开关：promote_state=1 的事实无条件注入 system prompt。"""
    d = await req.json()
    fid = int(d.get("id") or 0)
    if not fid:
        return JSONResponse({"error": "缺少事实 id"}, status_code=400)
    memory.promote(fid, bool(d.get("on", True)))
    return {"id": fid, "core_set": memory.stats()["core_set"]}


# ---------------------------------------------------------------- 技能（L5）
@app.get("/api/skills")
def api_skills():
    return {"stats": skillbook.stats(),
            "skills": skillbook._load_index()["skills"]}


@app.post("/api/skills/create")
async def api_skill_create(req: Request):
    d = await req.json()
    d_path = skillbook.create(d.get("name") or "", title=d.get("title"),
                              triggers=d.get("triggers"),
                              files=d.get("files"), quota=d.get("quota_tokens"),
                              skill_md=d.get("skill_md") or "")
    return {"dir": str(d_path), "stats": skillbook.stats()}


@app.post("/api/skills/{name}/publish")
async def api_skill_publish(name: str, req: Request):
    """人工闸门：发布/撤回。draft 不可被触发加载——主人没点头的技能不用。"""
    d = await req.json()
    st = skillbook.publish(name, bool(d.get("on", True)))
    return {"name": name, "status": st}


@app.post("/api/notify")
async def api_notify(req: Request):
    """有事找主人 → 弹本地气泡（面板/桌宠壳都能调）。"""
    d = await req.json()
    from channels import notify as notify_mod
    r = notify_mod.tell_master(d.get("text") or "", why=d.get("why") or "manual",
                               force=bool(d.get("force")))
    return r


@app.post("/api/skills/{name}/feedback")
async def api_skill_feedback(name: str, req: Request):
    d = await req.json()
    skillbook.feedback(name, int(d.get("score", 0)), d.get("note") or "")
    return {"ok": True}


# ---------------------------------------------------------------- ComfyUI 出图
@app.get("/api/comfy/status")
def api_comfy_status():
    """服务状态/显存/可用技能（面板用；桌宠出图走工具，不经这里）。"""
    from tools import comfy as _comfy
    return comfy.status()


@app.post("/api/comfy/generate")
async def api_comfy_generate(req: Request):
    """同步出图（面板/自测用）：{"skill":"t2i","prompt":"...", ...参数}。"""
    from tools import comfy
    d = await req.json()
    skill = d.get("skill") or "t2i"
    prompt = d.get("prompt") or ""
    params = {k: v for k, v in d.items()
              if k not in ("skill", "prompt")}
    return JSONResponse(comfy.generate(skill, prompt, **params))


@app.post("/api/comfy/start")
def api_comfy_start():
    from tools import comfy
    return comfy.start()


@app.post("/api/comfy/stop")
def api_comfy_stop():
    """手动关服务，把显存还给大模型/直播。"""
    from tools import comfy
    return comfy.stop()


@app.post("/api/memory/consolidate")
def api_consolidate():
    """手动触发每日巩固（04:00 由后台线程自动跑，这个是立刻跑一次）。"""
    return memory.consolidate(_extract_facts)


def _extract_facts(dialog):
    """巩固的提炼函数：本地引擎把对话浓缩成事实列表（记忆模块不 import llm，
    分层单向——这里把 llm 的能力注入给 memory.consolidate）。"""
    from core import llm as _llm
    prompt = (
        # C9（主人定调 2026-10-28）：8 条 → 30 条，让她一天经历的事记得住；
        # 输出预算同步 2048 → 4096（30 条长句在 2048 里会被截断 → JSON 解析
        # 失败 → 当天一条都存不下，这才是"删上限"真正会撞的墙）。
        "从下面的对话里提炼值得长期记住的事实（主人的习惯/偏好/约定/重要事"
        "件、你自己经历和想法）。只输出 JSON 数组，每条一句完整的话，最多 50 条；"
        "没有值得记的就输出 []。"
        "不要编造，只提炼对话中明确表达的。"
        # C11（主人定调 2026-11-02）：按《记忆事实句式规范》标准句式生成，
        # 同一类型同一句式，杜绝同类事实反复变体堆叠（声线包曾堆 112 条）。
        "按标准句式写：能力=我会「名」：用途。怎么用：步骤。边界：红线；"
        "技能玩法=「名」的玩法：规则。红线：不能做；约定=约定：条件时，行为；"
        "承诺=我承诺：事项；主人指令=主人要求我：行为；主人期望=主人希望我：行为；"
        "偏好=主人喜欢/不喜欢…或我偏好…；状态=主人告诉桌宠：原文；"
        "事件=时间，谁做了什么；观点=主人认为：观点；愿望=我希望：愿望；"
        "学习=我学了「主题」：一句话要点。\n\n对话：\n" + dialog)
    m, _u = _llm.chat([{"role": "user", "content": prompt}],
                      temperature=0.3, max_tokens=4096,
                      no_think=True)   # P1-1 修复：结构化提炼关思考——旧值 1024
                                       # 被思考吃光 → content 空 → 记忆巩固静默失效
    txt = (m.get("content") or "").strip()
    i, j = txt.find("["), txt.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        return json.loads(txt[i:j + 1])
    except Exception:
        return []


def _extract_session_facts(dialog):
    """任务 2（豆包交接 2026-10-30）：5 分钟增量巩固用的会话级提炼——
    只针对本会话一小段对话，上限 20 条（_extract_facts 是全日版上限 50）。"""
    from core import llm as _llm
    prompt = ("从这段短对话里提炼值得长期记住的事实（主人的状态/习惯/约定/"
              "重要事件、你自己经历的）。只输出 JSON 数组，每条一句完整的话，"
              "最多 20 条；没有值得记的就输出 []。不要编造。"
              # C11（主人定调 2026-11-02）：与 _extract_facts 同款句式规范
              "按标准句式写：能力=我会「名」：…怎么用…边界…；技能玩法=「名」的玩法…红线…；"
              "约定=约定：条件时，行为；承诺=我承诺：…；主人指令=主人要求我：…；"
              "主人期望=主人希望我：…；偏好=主人喜欢/不喜欢…；状态=主人告诉桌宠：…；"
              "事件=时间，谁做了什么；观点=主人认为：…；愿望=我希望：…；"
              "学习=我学了「主题」：一句话要点。\n\n对话：\n" + dialog)
    m, _u = _llm.chat([{"role": "user", "content": prompt}],
                      temperature=0.3, max_tokens=2048, no_think=True)
    txt = (m.get("content") or "").strip()
    i, j = txt.find("["), txt.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        return json.loads(txt[i:j + 1])
    except Exception:
        return []


_CONSOL_STATE = {"last_run": ""}


def _consolidator_loop():
    """每日巩固守护：每天 04:00-04:59 窗口跑一次（引擎活着才跑，
    没活就跳过等明天——宁可漏一天也不把聊天引擎挤下卡）。"""
    while True:
        try:
            now = time.localtime()
            key = time.strftime("%F")
            if now.tm_hour == 4 and _CONSOL_STATE["last_run"] != key:
                if engine.is_running():
                    r = memory.consolidate(_extract_facts)
                    _CONSOL_STATE["last_run"] = key
                    print(f"[consolidate] {key}: {r.get('status')} "
                          f"facts={len(r.get('facts', []))}", flush=True)
                else:
                    # 引擎没跑：记个"尝试过"，避免这个小时内每 10 分钟反复试；
                    # 任务 3（豆包交接 2026-10-30）：同时写待补跑标记，引擎恢复即补跑
                    _CONSOL_STATE["last_run"] = key + ":engine_down"
                    memcon.mark_pending(key)
            elif _CONSOL_STATE["last_run"].startswith(key + ":engine_down") and now.tm_hour != 4:
                _CONSOL_STATE["last_run"] = ""     # 出窗口了，重置等明天
            # 任务 3：每轮（600s）检查待补跑标记——引擎恢复健康就补跑当日巩固
            if memcon.pending_exists() and engine.is_running():
                memcon.run_pending_consolidate(
                    lambda: memory.consolidate(_extract_facts))
        except Exception:
            # 千问审核 2026-10-28：巩固是长任务（引擎调用/DB/JSON 解析都可能抛），
            # 循环体裸奔会让线程整体死掉、之后每天 04:00 静默不再巩固。
            # 吞掉本轮异常，10 分钟后照常轮询。
            import traceback
            traceback.print_exc()
        time.sleep(600)


def _autonomy_loop():
    """自主唤醒守护（主人定调 2026-10-28 晚）：安静满 3 分钟 → 叫醒她一次，
    让她自己决定做什么。每 20 秒看一次表，真正的判断都在 autonomy.tick 里
    （引擎忙/主人在聊 → 自动让路，不抢话）。"""
    while True:
        try:
            autonomy.tick()
        except Exception:
            # 千问审核同款教训：守护线程裸奔一次异常就整条死掉，之后静默失效
            import traceback
            traceback.print_exc()
        time.sleep(20)


def _chat_busy():
    """调度器抢占探针：任一会话还在生成 = 主人在等她 → 长任务让路
    （红线"不能听不见主人喊她"的机制化，见 scheduler.set_busy_check）。"""
    return any(lk.locked() for lk in _sid_locks.values())


@app.post("/api/jobs")
async def api_job_submit(req: Request):
    """提交后台长任务（EXPLORE/IDLE 类）：切片执行、checkpoint 可恢复、
    聊天优先。slice_fn 无法跨 HTTP 传——这里支持 kind=consolidate 这类
    内置任务；自定义切片走进程内 scheduler.submit。"""
    d = await req.json()
    kind = (d.get("kind") or "").strip()
    if kind == "consolidate":
        jid = scheduler.submit("consolidate", priority=scheduler.P2_IDLE,
                               note="手动触发的记忆巩固")
        return {"id": jid, "note": "内置任务需在 webapp 进程内注册切片，"
                                   "当前请用 POST /api/memory/consolidate 直接触发"}
    jid = scheduler.submit(kind, payload=d.get("payload") or {},
                           priority=int(d.get("priority", scheduler.P3_EXPLORE)),
                           note=d.get("note") or "")
    return {"id": jid, "stats": scheduler.stats()}


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": scheduler.list_jobs(), "stats": scheduler.stats()}


@app.post("/api/jobs/{jid}/{action}")
def api_job_action(jid: str, action: str):
    fn = {"pause": scheduler.pause, "resume": scheduler.resume,
          "cancel": scheduler.cancel}.get(action)
    if fn is None:
        return JSONResponse({"error": "action 须为 pause/resume/cancel"}, status_code=400)
    return {"id": jid, "ok": fn(jid)}


# ---------------------------------------------------------------- 审计与实时状态（B26 主人侧审核台）
@app.get("/api/state")
def api_state():
    """实时状态（永久功能）：她正在做什么 + 当前那句话（桌宠壳/网页都能读）。"""
    return status_light.read()


@app.get("/api/audit/config")
def api_audit_config():
    """审核功能开关（暂时功能）：ui=0 前端隐藏 tab；enabled=0 后端停止留痕。"""
    return {"enabled": bool(config.AUDIT_ENABLED), "ui": bool(config.AUDIT_UI)}


@app.get("/api/audit")
def api_audit(kind: str = "", session_id: str = "", limit: int = 300,
              since: float = 0.0):
    """行为流统一查询：按类型/会话/起始时间过滤；kind=wish 合并提议清单。
    她不可见——这只是主人网页界面的数据源，桌宠形象端没有任何暴露。"""
    if not config.AUDIT_ENABLED:
        return {"events": [], "enabled": False}
    evs = audit.query(kind=kind or None, session_id=session_id or None,
                      limit=max(1, min(limit, 2000)), t_start=since or 0.0)
    return {"events": evs, "enabled": True, "kinds": audit.kinds_count()}


@app.get("/api/audit/sessions")
def api_audit_sessions():
    """会话清单（聊天回放页的入口列表）。"""
    return {"sessions": audit.sessions() if config.AUDIT_ENABLED else []}


@app.get("/api/audit/session")
def api_audit_session(sid: str, limit: int = 50):
    """一个会话的逐轮回放（OpenClaw session event log 结构的事件流）。"""
    if not config.AUDIT_ENABLED:
        return {"sid": sid, "turns": []}
    return {"sid": sid, "turns": audit.session_turns(sid, limit=min(limit, 200))}


# ---------------- 全通道对话（桌宠右键"设置"打开的就是这个页面） ----------------
_CHANNEL_LABELS = {
    "default": "主对话",
    "openai:default": "桌宠对话框",
    "openai:whisper": "定时碎碎念",
    "notify": "主动提醒",
}


def _channel_of(sid):
    """会话 → 通道名。手机和桌面网页现在共用 default 会话（由手机通道的
    config.session 决定）；想让手机单独成一路，把它改成 "phone" 即可。"""
    if sid in _CHANNEL_LABELS:
        return _CHANNEL_LABELS[sid]
    if sid.startswith("openai:"):
        return "客户端 · " + sid.split(":", 1)[1]
    return "其它 · " + sid


@app.get("/api/allchat")
def api_allchat(limit: int = 150, channel: str = "", q: str = "", activity: int = 40):
    """把所有通道的对话合成一条时间线，外加她这段时间做了什么摘要。

    只读，数据源就是她自己已有的两份留痕：`data/conv_*.jsonl`（逐轮对话）和
    `data/audit/behavior.jsonl`（行为流）。不新增存储、不写任何文件。"""
    limit = max(1, min(int(limit or 150), 500))
    activity = max(0, min(int(activity or 40), 200))
    try:
        idx = context._load_index() or {}
    except Exception:
        idx = {}
    msgs, channels = [], []
    for sid, meta in sorted(idx.items(),
                            key=lambda kv: -(kv[1].get("updated") or 0)):
        meta = meta if isinstance(meta, dict) else {}
        path = Path(config.DATA_DIR) / f"conv_{context._safe(sid)}.jsonl"
        rows = []
        if path.exists():
            try:
                for ln in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except json.JSONDecodeError:
                        pass
            except Exception:
                rows = []
        ch = _channel_of(sid)
        channels.append({"sid": sid, "channel": ch, "title": meta.get("title", ""),
                         "n_msgs": meta.get("n_msgs", len(rows)),
                         "last": time.strftime(
                             "%m-%d %H:%M",
                             time.localtime(meta.get("updated") or 0))})
        for m in rows:
            if m.get("role") not in ("user", "assistant"):
                continue
            text = (m.get("content") or "").strip()
            if not text:
                continue
            msgs.append({"sid": sid, "channel": ch, "role": m["role"],
                         "content": text[:1200], "ts": m.get("ts") or 0,
                         "time": time.strftime("%m-%d %H:%M",
                                               time.localtime(m.get("ts") or 0))})
    kw = (q or "").strip()
    if kw:
        msgs = [m for m in msgs if kw in m["content"]]
    if channel:
        msgs = [m for m in msgs if m["channel"] == channel]
    msgs.sort(key=lambda x: x["ts"], reverse=True)
    acts = []
    if activity and config.AUDIT_ENABLED:
        try:
            for e in audit.query(limit=activity):
                acts.append({"kind": e.get("kind", ""),
                             "detail": (e.get("detail") or "")[:200],
                             "time": (e.get("time") or "")[5:16],
                             "sid": e.get("session_id", "")})
        except Exception:
            acts = []
    return {"messages": msgs[:limit], "channels": channels,
            "activity": acts, "state": status_light.read()}


@app.get("/api/wishes")
def api_wishes():
    """她的提议（心愿）列表——留痕供主人审阅；pending=等主人看。

    注意：list_wishes 第一个位置参数是 status，之前误传 limit=50 当
    status（过滤 status==50 恒空），审核台提议清单一直空白——已修。
    """
    return {"wishes": wishes.list_wishes(limit=50), "stats": wishes.stats()}


@app.post("/api/wishes/{wish_id}/{action}")
async def api_wish_decide(wish_id: str, action: str, req: Request):
    """主人拍板（approve/reject）。

    红线：这里**只改状态并留痕**，不触发任何下载/安装/接入动作——
    接入永远由主人侧人工执行（她无对齐无约束时不许自动带东西回来）。
    """
    if action not in ("approve", "reject"):
        return JSONResponse({"error": "action 只能是 approve/reject"}, status_code=400)
    # 主人可带审阅意见（review_note）——审核台提议清单可见
    note = None
    try:
        body = await req.json()
        note = (body or {}).get("note") or None
    except Exception:
        pass
    r = (wishes.approve(wish_id, note=note) if action == "approve"
         else wishes.reject(wish_id, note=note))
    if not r:
        return JSONResponse({"error": "没找到这条提议"}, status_code=404)
    return {"id": wish_id, "status": r["status"],
            "note": "接入由主人侧人工执行（系统不代劳）"}


def _waiter_loop():
    """等回复机制守护线程（B26）：升级链 + 气泡重提。

    - escalate_check：每 15s 看一眼——她还在等主人回话且当前通道超时，
      就换下一通道再叫一次（气泡→本地提示，每通道只叫一次不轰炸）。
    """
    while True:
        try:
            waiter.escalate_check()
        except Exception:
            pass                               # 守护线程自身不许死
        time.sleep(15)


# ---------------------------------------------------------------- 守护互保心跳
def _guard_heartbeat_loop():
    """webapp 内嵌守护心跳（GLM 任务 2026-10-28）：每 30 秒查一次 pet_guard
    进程（按 CommandLine 匹配 pet_guard.ps1，不是端口——守护不监听任何端口），
    死了就按 pet_guard 同款 StartHidden 拉起，形成"webapp 拉守护、守护拉
    引擎/webapp"的互守护闭环。事实 2（全家消失时守护日志无记录）说明守护
    会先于引擎死——只有让活着的一方把死了的守护拉回来才能破局。
    线程自身任何异常都吞掉（守护线程不许死），拉起失败下一轮再试。"""
    import subprocess
    root = str(config.ROOT)
    ps_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    while True:
        try:
            # 主判据（GLM 2026-10-28 定案）：守护每轮把 $PID 写进 data\pet_guard.pid，
            # 这里按文件里的 PID 探活——命令行匹配会自我命中（探测 powershell 自己
            # 的命令行就含过滤串，-like 通配连模式文本里的 * 都能桥过去，实测踩死）。
            alive = False
            pid_txt = ""
            try:
                pid_txt = (config.ROOT / "data" / "pet_guard.pid").read_text().strip()
            except Exception:
                pid_txt = ""
            if pid_txt.isdigit():
                r = subprocess.run(
                    [ps_exe, "-NoProfile", "-Command",
                     f"$p = Get-Process -Id {pid_txt} -EA SilentlyContinue; "
                     "if ($p -and $p.ProcessName -eq 'powershell') { 'alive' }"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=0x08000000)
                alive = "alive" in (r.stdout or "")
            else:
                # 兜底：无 PID 文件（旧版守护）才走命令行匹配——-File 是真守护的
                # 拉起方式，-Command 是探测自身，$PID 排除自己防自我匹配。
                ps = ("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                      "Where-Object { $_.ProcessId -ne $PID -and "
                      "$_.CommandLine -like '*-File*pet_guard.ps1*' } | "
                      "Select-Object -First 1 ProcessId | ForEach-Object { $_.ProcessId }")
                r = subprocess.run([ps_exe, "-NoProfile", "-Command", ps],
                                   capture_output=True, text=True, timeout=20,
                                   creationflags=0x08000000)
                alive = bool((r.stdout or "").strip())
            if not alive:
                # CREATE_NO_WINDOW（0x08000000）只能单用——与 DETACHED_PROCESS
                # （0x08）互斥，组合是非法标志，powershell 启动即崩（实测：
                # 拉起的守护秒死且不写任何日志）。
                psi = subprocess.Popen(
                    [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-WindowStyle", "Hidden", "-File", str(config.ROOT / "pet_guard.ps1")],
                    cwd=root, creationflags=0x08000000)
                try:
                    audit.log("guard", "守护心跳: pet_guard 进程消失，webapp 已拉起 "
                              f"(新守护 PID {psi.pid})")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(30)


@app.get("/api/wait")
def api_wait():
    """等回复机制现状（面板用）：她在不在等、等了多久、叫过哪些通道、
    本地气泡状态。主人可见——等回复全程留痕，这里汇总。"""
    return {"wait": waiter.state_view()}






# ---------------------------------------------------------------- 框架自检
@app.get("/api/framework")
def api_framework():
    """整体框架就绪度（doctor 的机读版）：模块清单/实现状态/运行时探测。"""
    return doctor.framework_report()


def _status_watchdog_loop(interval=20, stale_s=180):
    """状态灯兜底：卡住的 transient 状态超时自动回发呆。

    正常收尾由 pipeline 的 `from_step("done")` 写 idle。但那一帧依赖"进程活到答完"——
    中途被重启/异常掐断（2026-11-01 实测 webapp 在 08:51:24 重启过一次，正撞在一轮
    对话中间），status.json 就永远停在"思考/日常交流"，形象上一直挂着不消失。
    `waiting_master` 是她真的在等主人回话，不参与超时。"""
    while True:
        time.sleep(interval)
        try:
            st = status_light.read()
            state = st.get("state")
            if state in (None, "idle", "waiting_master"):
                continue
            if time.time() - float(st.get("ts") or 0) <= stale_s:
                continue
            status_light.set_state("idle", "刚忙完一轮，在想事情")
        except Exception:
            pass                       # 兜底线程绝不能弄崩 webapp


@app.on_event("startup")
def _start_consolidator():
    threading.Thread(target=_consolidator_loop, daemon=True).start()
    threading.Thread(target=_waiter_loop, daemon=True).start()   # 等回复守护
    threading.Thread(target=_status_watchdog_loop, daemon=True).start()  # 状态灯兜底
    threading.Thread(target=_guard_heartbeat_loop, daemon=True).start()  # 守护互保心跳（GLM 2026-10-28）
    # 自主唤醒（2026-10-28 晚）：能力注入 + 守护线程（tick 每 20 秒看一次）
    autonomy.init(searcher_getter=get_searcher, extract_fn=_extract_facts,
                  busy_check=_chat_busy)
    threading.Thread(target=_autonomy_loop, daemon=True).start()

    # 任务 3（豆包交接 2026-10-30）：启动时检查待补跑标记（凌晨引擎没跑的场景）
    def _run_pending_once():
        try:
            if memcon.pending_exists() and engine.is_running():
                memcon.run_pending_consolidate(lambda: memory.consolidate(_extract_facts))
        except Exception:
            pass
    threading.Thread(target=_run_pending_once, daemon=True).start()
    # ComfyUI 空闲自动关（2026-10-28）：要用才启动 + 空闲 7 分钟自动还显存
    def _comfy_idle_loop():
        while True:
            try:
                from tools import comfy
                comfy.autostop_tick()
            except Exception:
                pass
            time.sleep(60)
    threading.Thread(target=_comfy_idle_loop, daemon=True).start()
    skillbook.ensure_template()
    scheduler.set_busy_check(_chat_busy)
    scheduler.start(bg=True)
    # 状态灯归位：上次进程退出时停在哪一帧，重启后不该继续装作在忙
    try:
        status_light.set_state("idle", "刚醒")
    except Exception:
        pass


@app.post("/api/proactive/tick")
def api_proactive_tick():
    """桌宠壳/前端心跳：本地掷骰，要不要主动开口（不花模型）。"""
    s = config.load_settings()
    if not int(s.get("proactive_enabled", 1)):
        return {"speak": False, "reason": "主动搭话已关闭"}
    r = proactive.maybe_speak(idle_seconds=int(time.time() - _last_active()))
    if r is None:
        return {"speak": False}
    if r.get("dry") or not r.get("line"):
        # 2026-10-30 深夜（主人定调）：静态台词组已退役（"要不要陪我聊两句"
        # 这类中性表达无意义、烧提醒额度）——到点让她现场想一句有内容的，
        # 想不出有内容的就沉默，绝不硬凑。
        line = proactive.gen_line()
        if not line:
            return {"speak": False, "reason": "没想出值得说的话，先闭嘴"}
        r["line"] = line
    # 2026-10-29 主人定调：她主动开口 → 先弹气泡；气泡挂着没人理，
    # 由 waiter 的升级链再提醒一次。不再一开口就多通道同时炸。
    try:
        from channels import waiter as _waiter
        _waiter.express(r["line"], session_id="default",
                        channel="bubble", mood="patient")
    except Exception as e:
        try:
            from core import audit as _a
            _a.log("proactive", f"登记等待失败：{type(e).__name__} {e}", session_id="proactive")
        except Exception:
            pass
    return {"speak": True, "dry": False, **r}


def _last_active():
    st = proactive._load_state()
    return st.get("last_real_chat_ts", time.time() - 600)


# ---------------------------------------------------------------- 自主唤醒（2026-10-28 晚）
@app.get("/api/autonomy/status")
def api_autonomy_status():
    """她现在安静多久、什么时候醒过、上次做了什么（面板/主人可见）。"""
    return autonomy.status_view()


@app.post("/api/autonomy/wake")
def api_autonomy_wake():
    """主人点名/测试：立刻叫醒她一次（不等 3 分钟），她自己决定做什么。"""
    if not int(config.PET_AUTONOMY_ENABLED):
        return {"status": "disabled"}
    return autonomy.wake_once()


# ---------------------------------------------------------------- 设置
@app.get("/api/settings")
def api_settings_get():
    return {"settings": config.load_settings(),
            "engine_ctx_live": config.CTX,
            "note": "改 ctx / vram_reserve_mb 需重启引擎（POST /api/engine/restart）"}


@app.post("/api/settings")
async def api_settings_post(req: Request):
    d = await req.json()
    clean, errors = config.save_settings(d)
    config.apply_settings()
    return {"saved": clean, "errors": errors,
            "need_engine_restart": "ctx" in clean or "vram_reserve_mb" in clean}


@app.post("/api/engine/restart")
def api_engine_restart():
    ok = engine.restart()
    return {"ok": ok, "summary": config.summary()}


# ---------------------------------------------------------------- OpenAI 兼容
@app.get("/v1/models")
def v1_models():
    return {"object": "list", "data": [
        {"id": "pet", "object": "model", "owned_by": "local"}]}


def _memory_block(query):
    """核心记忆集 + 相关召回（与 /api/chat 同款注入；记忆永不因检索被砍）。"""
    try:
        core, _cu = memory.core_block(budget_tokens=600)
        rel, _ru = memory.recall(query, budget_tokens=1600)
        seen, parts = set(), []
        for seg in (core, rel):
            for ln in (seg or "").splitlines():
                ln = ln.strip()
                if ln and ln not in seen:
                    seen.add(ln)
                    parts.append(ln)
        try:
            from tools import comfy
            _hint = comfy.context_hints()      # 曾写成 _comfy 被 except 吞掉：从没生效过
            if _hint:
                parts.append(_hint)
        except Exception as e:
            try:
                from core import audit as _audit2
                _audit2.log("comfy", f"提示注入失败(2)：{type(e).__name__} {e}")
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return None


def _collect_pipeline(query, sess, blk):
    """非流式跑一遍 pipeline 全链路，返回正文（answer 字段优先，其次增量拼接）。"""
    buf = []
    r = pipeline.answer_stream(query, sess, searcher=get_searcher(),
                               on_delta=lambda t: buf.append(t),
                               memory_block=blk or None)
    return (r or {}).get("answer") or "".join(buf)


@app.post("/v1/chat/completions")
async def v1_chat(req: Request):
    """OpenAI 兼容：stream=false 一次性 JSON；stream=true 标准 SSE（含 [DONE]）。"""
    body = await req.json()
    msgs = body.get("messages") or []
    settings = config.load_settings()

    # 桌宠端点的规矩：外来 system 一律丢弃（外部客户端/agent 框架会注入自己的
    # "工具调用"人设，会把她变成机器），只保留对话内容，再套上她自己的人设
    # + 时间锚 + 记忆块——无论谁调这个端点，答话的都是桌宠本体。
    keep = [m for m in msgs if m.get("role") in ("user", "assistant", "tool")]
    # content 规范化（2026-10-29 修"两次发图两次异常降级"）：OpenAI content
    # 可以是 [{type:text},{type:image_url}]，旧代码 str(c) 把字典里的 'type'
    # 键当成了用户问题去搜索。现在：文本单独取、图片走媒体入站模块。
    last_user, images, last_idx = "", [], -1
    for i, m in enumerate(keep):
        text, imgs = media_in.split_content(m.get("content"))
        m["content"] = text                      # 历史里的数组也文本化（防上下文污染）
        if m.get("role") == "user":
            last_user, images, last_idx = text, imgs, i
    # 解锁信号（2026-10-30 补）：主人在**桌面聊天窗**回话走的就是这条端点——
    # 之前解锁只挂在 /api/chat，主人在气泡里回话，她的"等回话"状态
    # 永远不清（主人 09:41 抓到）。有新 user 文本 = 主人接话了。
    if last_user:
        try:
            waiter.master_replied(last_user, "bubble")
        except Exception:
            pass
    img_meta = {"count": 0, "ok": 0, "paths": []}
    if images:
        block, img_meta = media_in.describe_images(images, last_user)
        if block:
            last_user = (last_user + "\n\n" + block).strip()
        else:
            last_user = (last_user + "\n\n【主人刚发来图片，但这次我没看清画面，"
                                    "可以说一句没看清】").strip()
        if last_idx >= 0:
            keep[last_idx]["content"] = last_user
        audit.log("vision", f"图片入站 {img_meta['count']} 张，理解成功 "
                            f"{img_meta.get('ok', 0)} 张："
                            f"{','.join(Path(p).name for p in img_meta.get('paths') or [])}",
                  session_id="openai:" + str(body.get("user") or "default"))
    # 视频入口（2026-10-30 补）：主人从聊天页发的本地视频路径在这条端点上
    # 也要接——之前只接了 /api/chat，主人 00:20 发视频她说"看了 120 帧"全是
    # 脑补（这条端点没走视频链路），被主人抓到。
    last_user, vmeta = media_in.watch_video_if_any(last_user)
    if vmeta:
        if last_idx >= 0:
            keep[last_idx]["content"] = last_user
        audit.log("vision", f"视频入站（openai 端点）：{vmeta.get('video')}｜"
                            f"{'成功' if vmeta.get('video_ok') else '失败'}",
                  session_id="openai:" + str(body.get("user") or "default"))
    msgs = [{"role": "system", "content": context.compat_system(last_user)}] + keep
    mt = max(int(body.get("max_tokens") or settings["max_tokens"]), MAX_TOKENS_FLOOR)
    payload = {"messages": msgs, "stream": bool(body.get("stream")),
               "max_tokens": mt, "temperature": settings["temp"],
               "top_p": settings["top_p"], "top_k": settings["top_k"],
               "presence_penalty": settings["presence_penalty"]}
    want_stream = payload["stream"]

    sid = str(body.get("user") or "default")
    # 定时碎碎念（形象侧 whisper）不是主人说话：走独立会话、不写记忆缓冲。
    # 否则主对话历史会被"随便说一句日常碎碎念…"这类指令淹没（2026-10-31 修：
    # 实测最近 300 条上下文里 134 条是这类探针，导致她记不住真实对话、语义漂移）。
    _probe = last_user.strip().startswith("随便说一句日常碎碎念")
    sess_sid = "whisper" if _probe else sid
    lk = _sid_lock("openai:" + sid)
    if not lk.acquire(blocking=False):
        return JSONResponse({"error": {"message": "上一条仍在生成", "type": "rate_limit"}},
                            status_code=429)

    # 全链路优先：走 pipeline（判定→按需检索→技能→生成）——形象/第三方客户端
    # 接进来也是"桌宠大脑"；pipeline 出问题才静默退回直连（人设+记忆）。
    if config.OPENAI_FULL_PIPELINE and last_user:
        try:
            sess = context.Session("openai:" + sess_sid)
            if _probe and sess.formal:
                # 碎碎念是"随口一句"，不该拿自己上一句当范本（2026-11-02 实测）：
                # 同一个 prompt 每 6 分钟跑一次，历史里全是他自己那句"主人在外面忙了一天吧～"，
                # 模型就照着抄 —— 日志里同一条被复读几十次，且和"会不会说话"无关
                # （直接拿同一 prompt 热调引擎，三次输出分别是三种，说明模型本身不钝）。
                # 办法：碎碎念轮**不带历史**，并把上一句当作"别重复"的负面提示。
                prev = [m.get("content") for m in sess.formal
                        if m.get("role") == "assistant" and m.get("content")]
                sess.formal = []
                if prev:
                    last_user += ("\n（上一句你嘀咕的是：「" + str(prev[-1])[:40] +
                                  "」。这次换一句不一样的，别重复。）")
            blk = _memory_block(last_user)
            if not want_stream:
                ans = _collect_pipeline(last_user, sess, blk)
                if not _probe:
                    memcon.capture_increment(sid, "user", last_user)     # 任务 2
                    memcon.capture_increment(sid, "assistant", ans)
                    memcon.schedule_flush(sid, _extract_session_facts)
                lk.release()
                return {"id": "chatcmpl-pet", "object": "chat.completion",
                        "created": int(time.time()), "model": "pet",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": ans}}],
                        "usage": {}}
            # 流式：pipeline 的 on_delta 增量包成 OpenAI delta 帧
            q = __import__("queue").Queue()

            def pworker():
                try:
                    r = pipeline.answer_stream(
                        last_user, sess, searcher=get_searcher(),
                        on_delta=lambda t: q.put(("tok", t)),
                        memory_block=blk or None)
                    ans = (r or {}).get("answer") or ""
                    if ans and not _probe:
                        memcon.capture_increment(sid, "assistant", ans)  # 任务 2
                        memcon.schedule_flush(sid, _extract_session_facts)
                    q.put(("done", None))
                except Exception as e:                  # noqa: BLE001
                    q.put(("err", f"{type(e).__name__}: {str(e)[:160]}"))
                finally:
                    lk.release()

            def pgen():
                yield _sse({"choices": [{"index": 0, "delta": {"role": "assistant"},
                                         "finish_reason": None}], "model": "pet"})
                while True:
                    try:
                        kind, p = q.get(timeout=15)
                    except __import__("queue").Empty:
                        yield ": ping\n\n"
                        continue
                    if kind == "tok":
                        yield _sse({"choices": [{"index": 0,
                                                 "delta": {"content": p},
                                                 "finish_reason": None}], "model": "pet"})
                    else:
                        yield _sse({"choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "stop"}], "model": "pet"})
                        yield "data: [DONE]\n\n"
                        return

            threading.Thread(target=pworker, daemon=True).start()
            return StreamingResponse(pgen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})
        except Exception:                               # noqa: BLE001
            if lk.locked():
                lk.release()                            # 异常退出也要放锁，别锁死会话
            # 2026-10-30 修"release unlocked lock"：这里 release 后**必须 return**，
            # 否则落穿到下面的直连分支再 release 一次（双释放 500）。
            return JSONResponse({"error": {"message": "pipeline 这轮没跑成，已回退直连，重试一次通常就好",
                                           "type": "pipeline_error"}},
                                status_code=502)

    if not want_stream:
        try:
            try:
                with _engine_post(payload) as r:
                    d = json.loads(r.read().decode())
            finally:
                lk.release()
            msg = d["choices"][0]["message"]
            return {"id": "chatcmpl-pet", "object": "chat.completion",
                    "created": int(time.time()), "model": "pet",
                    "choices": [{"index": 0, "message": msg,
                                 "finish_reason": d["choices"][0].get("finish_reason")}],
                    "usage": d.get("usage", {})}
        except Exception as e:
            return JSONResponse({"error": str(e)[:300]}, status_code=500)

    q = __import__("queue").Queue()

    def worker():
        try:
            with _engine_post(payload) as r:
                buf = b""
                while True:
                    chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        s = line.decode("utf-8", "replace").strip()
                        if not s.startswith("data:"):
                            continue
                        p = s[5:].strip()
                        if p == "[DONE]":
                            continue
                        try:
                            q.put(("up", p))
                        except Exception:
                            pass
            q.put(("done", None))
        except Exception as e:
            q.put(("err", str(e)[:200]))
        finally:
            lk.release()

    def gen():
        yield _sse({"choices": [{"index": 0, "delta": {"role": "assistant"},
                                 "finish_reason": None}], "model": "pet"})
        while True:
            try:
                kind, p = q.get(timeout=15)
            except __import__("queue").Empty:
                yield ": ping\n\n"
                continue
            if kind == "up":
                yield "data: " + p + "\n\n"          # 上游帧原样透传（OpenAI 格式）
            elif kind == "done":
                yield "data: [DONE]\n\n"
                return
            elif kind == "err":
                yield _sse({"error": {"message": p}})
                yield "data: [DONE]\n\n"
                return

    threading.Thread(target=worker, daemon=True).start()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "engine": engine.is_running()}


# ---------------------------------------------------------------- 单实例锁
# 2026-10-28 修复（主人确认，审单：B27"形象报引擎离线"+双 webapp 竞态）：
# 1) 端口预检：8094 已被监听（已有实例或他程序占用）→ 干净退出，不叠实例；
# 2) 锁文件 O_EXCL 原子互斥：两个 webapp 同时拉起时只有一方能拿锁，另一方退出，
#    根治"同一秒拉起两个 webapp.py、一个闲置"的双开竞态；
# 3) 崩溃残留自愈：锁文件在但 PID 已死 → 删锁接管，不卡死后续启动。
def _acquire_singleton():
    import os
    import socket
    import sys

    # ① 端口预检：能被连上说明已有服务在 8094（本实例或其它程序），退出不叠加
    try:
        s = socket.create_connection((config.ENGINE_HOST, config.WEB_PORT), timeout=1)
        s.close()
        print(f"[webapp] 8094 已被监听（可能已有实例或其它程序占用），本进程退出")
        sys.exit(0)
    except OSError:
        pass                                   # 端口空闲，继续

    # ② 锁文件原子互斥（O_CREAT|O_EXCL：已存在即失败）
    lock = config.DATA_DIR / "webapp.lock"
    flag = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    import time
    for attempt in range(4):                     # 最多尝试 4 轮（含空窗等待）
        try:
            fd = os.open(lock, flag)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return                             # 拿到锁，正常启动
        except FileExistsError:
            # 锁文件在：读 PID。空/非法 = 持有者正在写入的空窗，等 0.5s 重读，
            # 绝不删锁（删了会把正在启动的实例的锁抢走 → 双开竞态）。
            old = 0
            for _ in range(3):
                try:
                    old = int(lock.read_text(encoding="utf-8").strip() or 0)
                    if old:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            if old and _pid_alive(old):
                print(f"[webapp] 已有实例 PID={old} 在跑，本进程退出（防双开）")
                sys.exit(0)
            if old:
                try:
                    lock.unlink()              # 残留死锁 → 删掉重试（仅当 PID 明确存在过）
                except OSError:
                    pass
            else:
                time.sleep(0.5)                # 一直读不到有效 PID：再等一轮后重试
    print("[webapp] 锁文件无法接管，本进程退出")
    sys.exit(1)


def _pid_alive(pid):
    """PID 是否存活（Windows：OpenProcess 查询，进程存在即 True）。"""
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


if __name__ == "__main__":
    import uvicorn
    _acquire_singleton()
    uvicorn.run(app, host="127.0.0.1", port=config.WEB_PORT, log_level="warning")
