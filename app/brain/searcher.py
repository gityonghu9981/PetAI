"""浏览器检索层：Playwright 驱动 Chromium（跨平台，模型无关）。

通用性设计：
- 默认用 playwright 自带 chromium 内核（`playwright install chromium` 一次即可），
  跨 Win/Mac/Linux 零配置；设 BROWSER_CHANNEL=chrome 可改用系统 Chrome。
- 使用项目内独立 profile（browser-profile），绝不复用用户日常浏览器的登录态；
- 检索结果只是"临时上下文"：本模块返回内存中的 dict，由调用方决定去留；
  快照落盘仅在 --keep-evidence 调试开关下发生。

线程模型（修复"重新生成经常卡住"）：
- Playwright sync API 的对象只能在其创建线程中使用，跨线程调用会挂死；
- 本类自建一个浏览器专属线程，所有操作经队列派发，调用方带超时等待，
  彻底避免跨线程阻塞导致界面卡死；超时后返回错误而不是悬挂。
"""
import re
import time
import queue
import threading
import concurrent.futures as _cf
import html as _html
import urllib.request, urllib.parse, ssl as _ssl
from core import audit
from core import config

OFFICIAL_PAT = re.compile(
    r"(官方|标准|规范|白皮书|政策|法规|条例|规定|公告|通知|文件|条款|指南|手册|法条"
    r"|起征点|税率|获奖|得主|现行|法律|宪法|立法"
    r"|总理|总统|首相|主席|部长|国务委员|人大委员长|央行行长|秘书长|总书记|元首)")

# 职务/人物查询：确定性抽取"国名+任期词+职务"作搜索词。
# 搜索引擎对长中文句联想常失败成国家首页，
# 必须把核心实体词直接喂给引擎，而不是整句。
_POSITION_PAT = re.compile(
    r"(中国|美国|英国|法国|德国|日本|俄罗斯|印度|韩国|朝鲜|越南|泰国|新加坡"
    r"|澳大利亚|加拿大|巴西|意大利|西班牙|荷兰|瑞典|瑞士|土耳其|伊朗|以色列"
    r"|沙特|印尼|马来西亚|菲律宾|巴基斯坦|蒙古|乌克兰|波兰|匈牙利|希腊|葡萄牙"
    r"|新西兰|秘鲁|智利|墨西哥|联合国|欧盟|世卫|国际奥委会)"
    r"(?:现任|目前|当前|新)?"
    r"(?:国务院总理|总理|总统|首相|主席|国家主席|外长|国防部长|财长|央行行长"
    r"|部长|元首|副总统|秘书长|总书记)")
_POSITION_TRAIL = re.compile(r"(说出姓名|告诉我|请告诉我|说一下|回答我|是谁|哪位|叫什么|姓什么|是哪个)")

# 合并层垃圾结果过滤：验证页/风控页/拦截页会以"正常结果"的形态混进
# 任一引擎的返回（B26 实测：bing/ddg 抓到百度安全验证页当结果条目）。
# 在合并处统一拦，比在每个引擎里各写一遍可靠——标题或 URL 命中即弃。
_JUNK_TITLE_PAT = re.compile(
    r"安全验证|验证码|请完成验证|访问验证|人机身份核验|滑动验证|"
    r"access denied|unusual traffic|are you a robot|verify you are human|"
    r"just a moment|attention required", re.I)
_JUNK_URL_PAT = re.compile(
    r"wappass\.baidu\.com|captcha|/verify\?|passport\.[a-z]+\.com/|"
    r"www\.google\.com/recaptcha", re.I)

# ---------------------------------------------------------------- 搜索白名单（GLM 任务 2026-10-28）
# 总开关 config.SEARCH_WHITELIST_ENABLED（默认 0/关闭，主人定策"限制保留但不启用"）：
# 关闭时她自由获取信息，两卡口全部放行、零额外限制；搜索/抓取行为仍逐条写
# audit（kind=whitelist，标注放行）——给主人观察依据，作为以后商量是否开限制的基础。
# 开关打开（PET_SEARCH_WHITELIST_ENABLED=1）：恢复"默认拒绝 + 白名单 + 审计"。
# 卡口两处：search() 出口过滤结果条目、fetch_page() 入口预检直连抓取——
# 覆盖并行/串行/全部引擎路径与 _tool_wander 的页面抓取，不依赖调用方自觉。
def _host_of(url):
    """取 URL 的小写主机名（去 userinfo 与端口）。解析失败返回空串（视为拒绝）。"""
    try:
        h = urllib.parse.urlsplit(url or "").netloc.lower()
        return h.split("@")[-1].split(":")[0]
    except Exception:
        return ""


def domain_allowed(url):
    """URL 域名是否放行。开关关闭（默认）→ 一律放行（限制不启用）；
    开关打开 → host 必须在 SEARCH_WHITELIST 内（host == 名单项 或 其子域）。"""
    if not config.SEARCH_WHITELIST_ENABLED:
        return True
    h = _host_of(url)
    if not h:
        return False
    for d in config.SEARCH_WHITELIST:
        if h == d or h.endswith("." + d):
            return True
    return False


def _wl_audit(url, allowed):
    """白名单审计（GLM 任务 2026-10-28）：url + 结果，两种开关状态都留痕。
    开关关闭时标注"放行(限制未启用)"，主人可据此观察她获取信息的去向。
    失败静默——审计不许弄断检索。"""
    try:
        if config.SEARCH_WHITELIST_ENABLED:
            tag = "allow: " if allowed else "deny: "
        else:
            tag = "allow(限制未启用): "
        audit.log("whitelist", tag + (url or "")[:200])
    except Exception:
        pass


# 要图的查询走图片检索（§17）。判据要窄：光有"图片"两个字不算要图——
# "图片怎么压缩""这个图片格式是什么意思"是要**说明**，塞一堆壁纸回去就是答非所问。
_IMAGE_WORDS = re.compile(r"(壁纸|头像|照片|美图|插画|写真|表情包|封面|海报|背景图|"
                          r"图片|配图)")
_IMAGE_TEXTISH = re.compile(r"(怎么|怎样|如何|为什么|为何|教程|方法|格式|压缩|转换|"
                            r"打印|翻译|尺寸|分辨率|定义|意思|是什么|大小|占用|违法|版权)")


def _wants_images(query):
    """这条查询是想要**图**，还是想要关于图的文字说明。"""
    q = str(query or "")
    return bool(_IMAGE_WORDS.search(q)) and not _IMAGE_TEXTISH.search(q)


class Searcher:
    def __init__(self, op_timeout=90):
        self._q = queue.Queue()
        self._timeout = op_timeout
        self._alive = True
        self._http_opener = None
        self._thread = threading.Thread(target=self._browser_loop, daemon=True, name="browser-thread")
        self._thread.start()
        # 等浏览器就绪（就绪前 op 会在 loop 里排队）
        for _ in range(120):
            if self._ready():
                return
            time.sleep(0.25)
        # 浏览器内核缺失/启动失败时【不 raise】：webapp.py 在模块级 new Searcher()，
        # 这里一 raise 整个 webapp import 崩溃、uvicorn 永远起不来（新用户按 README
        # 把内核装到 C 盘默认目录即 100% 踩中）。降级为"检索不可用"软故障：
        # _boot_error 由 /healthz 与运行台报告，页面仍可用本地直答。
        self._boot_error = getattr(self, "_boot_error", None) or (
            "浏览器启动超时(30s)：检索不可用（内核未安装或路径不对，"
            "请检查 README 的 PLAYWRIGHT_BROWSERS_PATH 安装说明）")

    def _ready(self):
        try:
            self._submit("ping", None, timeout=1.0)
            return True
        except Exception:
            return False

    # ---------- 专属线程：创建并独占 Playwright ----------
    def _browser_loop(self):
        try:
            from playwright.sync_api import sync_playwright
            self._p = sync_playwright().start()
            launch_kw = dict(
                user_data_dir=str(config.ROOT / "browser-profile"),
                headless=config.BROWSER_HEADLESS,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                # 真实 Chrome UA：默认 HeadlessChrome UA 指纹明显，
                # 搜索引擎对无头 UA 会直接弹验证页。
                user_agent=self._http_ua(),
            )
            # 默认用 playwright 自带 chromium（跨平台零配置，需先 `playwright install chromium`）；
            # 设 BROWSER_CHANNEL=chrome 时可改用系统 Chrome。
            if config.BROWSER_CHANNEL:
                launch_kw["channel"] = config.BROWSER_CHANNEL
            self._ctx = self._p.chromium.launch_persistent_context(**launch_kw)
        except Exception as e:
            self._ctx = None
            self._boot_error = f"{type(e).__name__}: {str(e)[:150]}"
        while True:
            try:
                kind, payload, sender = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if kind == "shutdown":
                break
            if self._ctx is None:
                sender.put(("err", self._boot_error or "浏览器未启动"))
                continue
            try:
                if kind == "ping":
                    sender.put(("ok", True))
                elif kind == "search":
                    # 检索放独立线程执行：_do_search 内部会经队列派发渲染引擎
                    # （search_engine），若本线程同步跑 _do_search 就没人处理
                    # search_engine 任务 → 死锁（渲染组全部超时）。
                    # 起线程后浏览器线程立即空闲，可轮转处理渲染引擎任务。
                    def _run_search():
                        try:
                            sender.put(("ok", self._do_search(*payload)))
                        except Exception as e:
                            sender.put(("err", f"{type(e).__name__}: {str(e)[:150]}"))
                    threading.Thread(target=_run_search, daemon=True).start()
                elif kind == "search_engine":
                    # 指定单引擎检索（渲染引擎并行入口）：在浏览器线程执行，
                    # 避免 ThreadPoolExecutor 工作线程跨线程碰 Playwright 对象
                    # （playwright sync API 只允许创建线程使用，跨线程必报 error）。
                    # payload 结构 = (引擎名, (query, n, [mode]))，args 取 payload[1]。
                    name = payload[0]
                    args = payload[1]
                    fn = getattr(self, "_search_" + name)
                    sender.put(("ok", fn(*args)))
                elif kind == "fetch":
                    sender.put(("ok", self._do_fetch(*payload)))
                else:
                    sender.put(("err", f"未知操作 {kind}"))
            except Exception as e:
                sender.put(("err", f"{type(e).__name__}: {str(e)[:150]}"))
        # 收尾
        try:
            if self._ctx: self._ctx.close()
            if self._p: self._p.stop()
        except Exception:
            pass

    def _submit(self, kind, payload, timeout=None):
        sender = queue.Queue()
        self._q.put((kind, payload, sender))
        try:
            tag, data = sender.get(timeout=timeout or self._timeout)
        except queue.Empty:
            raise TimeoutError(f"检索操作 {kind} 超时（>{self._timeout}s），浏览器可能卡在慢站")
        if tag == "err":
            raise RuntimeError(data)
        return data

    # ---------- 对外接口（可在任意线程调用） ----------
    def search(self, query, max_results=None, wait_all=False):
        """wait_all=False（默认）：先到先用，最快引擎结果即返回（简单查询提速）。
        wait_all=True：等所有引擎到齐合并去重再返回——写作/权威/严谨查询
        需要多源素材，不能因为必应先到就丢谷歌/百度的补充（用户明确要求）。
        白名单（GLM 任务 2026-10-28）：返回前按 SEARCH_WHITELIST 过滤。
        总开关关闭（默认）→ 全部放行仅留痕；打开 → 默认拒绝，未在名单的
        域名条目拦截。拦截/放行逐条写 audit。
        要图的查询（壁纸/头像/照片…）先走图片检索（§17，纯 HTTP，不开浏览器）；
        图片检索空手而归再退回网页检索——绝不因为加了这条路就把她挡在材料外。"""
        if _wants_images(query):
            imgs = self.search_images(query, max_results or config.MAX_RESULTS)
            if imgs:
                return self._whitelist_filter(imgs)
        return self._whitelist_filter(self._submit("search", (query, max_results, wait_all)))

    def search_images(self, query, n=8):
        """图片检索：必应图片 **HTTP 直抓**（不启动浏览器、零额外进程/显存，实测 <1s）。

        为什么要（CHANGELOG §17）：她说"去找几个美女壁纸回来"时只能走网页检索，
        `高颜值女生头像 壁纸` 被引擎联想成"高"字的字典页——她要的是图，
        网页搜索给不了，她就只能反复重试（在她嘴里就成了"浏览器又卡住了"）。

        返回形状与网页检索一致（title/url/snippet），**额外带 `img` 直链**：
        `url` 给的是来源页，这样 `fetch_page` 照常读得动；`img` 才是图本身，
        她想"把图摆到屏幕上"时 `hands open <img>` 或截屏都行。
        解析口径：必应把元数据 HTML 转义过（`&quot;`），必须先 `_html.unescape`
        再抓 `murl/purl/t` 三元组（实测三者数量一致、按序对齐）。
        任何一步失败都返回 []，由调用方退回网页检索。"""
        try:
            q = urllib.parse.quote(self._clean_query(query))
            page = self._http_get(
                "https://www.bing.com/images/search?q=" + q
                + "&form=HDRSC2&first=1&count=%d&setlang=zh-CN" % max(10, int(n) * 2),
                timeout=10)
            if not page:
                return []
            h = _html.unescape(page)
            murl = re.findall(r'"murl":"(http[^"]+)"', h)
            purl = re.findall(r'"purl":"(http[^"]+)"', h)
            titles = re.findall(r'"t":"([^"]{2,120})"', h)
            out, seen = [], set()
            for i, img in enumerate(murl):
                if img in seen:
                    continue
                seen.add(img)
                out.append({
                    "title": (titles[i][:120] if i < len(titles) else "图片结果"),
                    "url": (purl[i] if i < len(purl) else img),
                    "img": img,
                    "snippet": "图片直链（来源页见 url）"})
                if len(out) >= n:
                    break
            if out:
                audit.log("search", "图片检索 %r → %d 张（必应图片 HTTP 直抓）"
                          % (query[:40], len(out)))
                # 落一份"她最近看到的图"，好让"我截给你看"这类承诺有东西可落（§18）
                try:
                    import json as _json
                    p = config.DATA_DIR / "memory" / "last_images.json"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(_json.dumps(
                        {"ts": time.time(), "query": query[:60],
                         "items": [{"img": r["img"], "page": r["url"],
                                    "title": r["title"]} for r in out]},
                        ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
            return out
        except Exception:
            return []

    def _whitelist_filter(self, results):
        """结果条目域名白名单过滤（默认拒绝）。逐条审计 url+结果。"""
        out = []
        for r in (results or []):
            ok = domain_allowed(r.get("url") or "")
            _wl_audit(r.get("url") or "", ok)
            if ok:
                out.append(r)
        return out

    def fetch_page(self, url):
        # 白名单预检（GLM 任务 2026-10-28）：开关打开时未在名单的域名直接拦截，
        # 不进浏览器线程——返回结构同 _do_fetch（带 error 字段），调用方按失败处理。
        # 开关关闭（默认）→ 全部放行仅留痕，主链路零额外开销。
        ok = domain_allowed(url)
        _wl_audit(url, ok)
        if not ok:
            return {"url": url, "title": "", "text": "",
                    "error": "域名被搜索白名单拦截（默认拒绝，未在 SEARCH_WHITELIST）"}
        return self._submit("fetch", (url,))

    def close(self):
        self._alive = False
        self._q.put(("shutdown", None, None))

    # ---------- 具体实现（只在浏览器线程执行） ----------
    # 引擎池：国内（必应/百度/搜狗）+ 国际（谷歌/雅虎/DDG）混合，项目面向国内外开源。
    # 并行策略（调优结论）：bing 走 HTTP 直抓（调用方线程，不占浏览器队列，最快）；
    # 百度/谷歌等渲染引擎进浏览器线程组。二者通过 ThreadPoolExecutor 真并行：
    # 海外部署时谷歌/雅虎正常返回并参与合并；国内网络谷歌超时被短预算截断，
    # 空/失败结果不影响其他引擎（"空内容不影响速度和结果"）。
    # 失败冷却：某引擎连续 2 次失败 → 冷却 10 分钟不再浪费时间。
    _ENGINES = ("bing", "baidu", "google", "yahoo", "ddg", "sogou")
    # 渲染引擎组（需浏览器线程）：google 排前——国内会快速超时失败，海外正常，
    # 都不阻塞 bing/百度首批结果返回。
    _RENDER_ENGINES = ("google", "baidu", "yahoo", "ddg", "sogou")
    _rot = 0
    _fails = {}          # name -> [连续失败次数, 解禁时间戳]
    COOLDOWN_S = 600
    _PARALLEL = True     # 并行开关（True=bing HTTP 与渲染组并发；False=退回顺序轮换）

    @classmethod
    def _cooling(cls, name):
        rec = cls._fails.get(name)
        return bool(rec and rec[1] > time.time())

    @classmethod
    def _fail_mark(cls, name):
        rec = cls._fails.setdefault(name, [0, 0])
        rec[0] += 1
        if rec[0] >= 2:
            rec[1] = time.time() + cls.COOLDOWN_S

    @classmethod
    def _ok_mark(cls, name):
        cls._fails.pop(name, None)

    def _do_search(self, query, max_results, wait_all=False):
        max_results = max_results or config.MAX_RESULTS
        query = self._clean_query(query)
        errors = []
        order = [e for e in self._ENGINES if not self._cooling(e)]
        if not order:
            errors.append("全部引擎冷却中")
            order = list(self._ENGINES)

        # 官方/确定性查询：并行试 bing(HTTP) 与 google(渲染) 的 site:gov.cn 变体，
        # 先达即用（gov 变体结果同权威，官方查询本就是快路径）
        if config.OFFICIAL_SEARCH and OFFICIAL_PAT.search(query):
            gov = query + " site:gov.cn"
            ex = _cf.ThreadPoolExecutor(max_workers=2)
            try:
                f1 = ex.submit(self._http_engine_search, "bing", gov, max_results, "official")
                f2 = ex.submit(self._render_engine_search, "google", gov, max_results, "official")
                for fut in _cf.as_completed((f1, f2), timeout=config.FETCH_TIMEOUT_S + 3):
                    try:
                        res = fut.result()
                        if res:
                            Searcher._ok_mark("bing" if fut is f1 else "google")
                            return res
                    except Exception as e:
                        name = "bing" if fut is f1 else "google"
                        Searcher._fail_mark(name)
                        errors.append(f"gov-{name}: {type(e).__name__}")
            finally:
                ex.shutdown(wait=False)   # 不等待未完成任务（先到即用）
            Searcher._rot = 1

        if self._PARALLEL:
            return self._do_search_parallel(query, max_results, order, errors, wait_all)
        return self._do_search_serial(query, max_results, order, errors)

    def _do_search_parallel(self, query, max_results, order, errors, wait_all=False):
        """并行检索：bing HTTP 直抓（调用线程）+ 渲染组（浏览器线程队列）。
        wait_all=False（简单查询默认）：首批有效结果即返回（先到先用，0.4s 级）。
        wait_all=True（写作/权威等复杂任务）：等两组都完成再合并去重排序——
        必须让谷歌/百度等多源素材全部参与，不能因为必应先到就丢补充素材。
        某引擎失败/超时/空 → 标记冷却或忽略，不影响整体。"""
        http_engines = [e for e in order if e == "bing"]
        render_engines = [e for e in order if e in self._RENDER_ENGINES]
        if not wait_all:
            # 简单查询快速路径（2026-10-28，主人报天气类 73s）：渲染组串行
            # 跑 google/yahoo/sogou 在国内各拖 10-60s；非 wait_all 只留国内
            # 可达最快的 baidu+ddg，配合下方"3 条即返回"，天气/比分等
            # 轻时效查询几秒内出结果。严谨查询（wait_all=True）不受影响。
            render_engines = [e for e in render_engines if e in ("baidu", "ddg")]
        all_res = []                             # 千问审核 2026-10-28：两组都收，
        hits_ok = set()                          # 只留先完成的一组会丢另一组素材
        stop = threading.Event()                 # 协作式取消：首达即返回后叫停渲染组
        ex = _cf.ThreadPoolExecutor(max_workers=2)
        try:
            f_http = ex.submit(self._http_engine_group, query, max_results, http_engines)
            f_render = ex.submit(self._render_engine_group, query, max_results,
                                 render_engines, wait_all, stop)
            if wait_all:
                # 复杂任务：等两组都完成（渲染组内部各引擎也全部跑完），
                # 多源素材合并去重后再返回——先到先答会丢谷歌/百度补充素材
                for fut in (f_http, f_render):
                    try:
                        res, ok_names = fut.result()
                    except Exception as e:
                        errors.append(f"parallel: {type(e).__name__} {str(e)[:120]}")
                        continue
                    if res:
                        all_res.append(res)
                        hits_ok.update(ok_names)
                # 渲染组内的每引擎结果此时已全部汇总进各自分组；合并去重排序
                # （B26：统一走 _merge_clean，验证/拦截页在合并前被过滤）
                merged = self._merge_clean(all_res, query, max_results,
                                           hits_ok, "等全源合并")
                if merged:
                    return merged
            else:
                for fut in _cf.as_completed((f_http, f_render), timeout=config.FETCH_TIMEOUT_S + 8):
                    try:
                        res, ok_names = fut.result()
                    except Exception as e:
                        errors.append(f"parallel: {type(e).__name__} {str(e)[:120]}")
                        continue
                    if res:
                        all_res.append(res)
                        hits_ok.update(ok_names)
                        # 首批（必应/百度）已可作答：立即返回，不等渲染组剩余引擎；
                        # 够 3 条即出（2026-10-28：天气/比分等轻时效查询 bing 常不足
                        # 6 条，等满会拖到渲染组串行 60s+；3 条素材已够本地作答）
                        if len(res) >= min(3, max_results):
                            for n_ in ok_names:
                                Searcher._ok_mark(n_)
                            Searcher._rot = (Searcher._rot + 1)
                            print("[searcher] 并行引擎 %s 返回 %d 条（先到先用）" % (",".join(ok_names), len(res)), flush=True)
                            stop.set()                 # 叫停渲染组剩余引擎（省浏览器算力）
                            return res[:max_results]
        finally:
            ex.shutdown(wait=False)   # 首达即返回；后台未完成引擎自然结束，不阻塞调用方
        # 全部不足：合并两组结果去重，按相关性排序（同样过滤验证页）
        merged = self._merge_clean(all_res, query, max_results,
                                   hits_ok, "并行合并")
        if merged:
            return merged
        raise RuntimeError("所有引擎均失败: " + " | ".join(errors) or "无可用引擎")

    def _http_engine_group(self, query, max_results, engines):
        """HTTP 直抓组（调用方线程执行，与渲染组真并行）：目前 bing。"""
        res, ok = [], []
        for name in engines:
            try:
                r = getattr(self, "_search_" + name)(query, max_results)
                if r:
                    res.extend(r)
                    ok.append(name)
                    Searcher._ok_mark(name)
                else:
                    Searcher._fail_mark(name)
            except Exception as e:
                Searcher._fail_mark(name)
        return res, ok

    @staticmethod
    def _junk(r):
        """结果条目是否为验证/拦截页（合并前过滤，见模块头 _JUNK_* 注释）。"""
        t = (r.get("title") or "")
        u = (r.get("url") or "")
        return bool(_JUNK_TITLE_PAT.search(t) or _JUNK_URL_PAT.search(u))

    def _merge_clean(self, sources, query, max_results, ok_names, label):
        """合并 → 过滤验证页 → 去重 → 相关性排序 → 截断。并行路径两个合并点共用。"""
        merged, seen = [], set()
        for src in sources:
            for r in (src or []):
                if not r.get("url") or self._junk(r) or r["url"] in seen:
                    continue
                seen.add(r["url"])
                merged.append(r)
        for n_ in ok_names:
            Searcher._ok_mark(n_)
        if not merged:
            return merged
        kw = self._kw_words(query)
        if kw:
            merged.sort(key=lambda r: self._kw_hits(kw, r["title"] + r["url"]), reverse=True)
        Searcher._rot += 1
        print("[searcher] %s %d 条（引擎: %s）" % (label, len(merged), ",".join(ok_names) or "-"), flush=True)
        return merged[:max_results]

    def _render_engine_isolated(self, name, query, n):
        """独立浏览器实例的渲染引擎（wait_all 并行专用）。
        每线程各自 sync_playwright().start() + 独立 chromium/context：
        Playwright sync API 只允许创建线程使用，独立实例天然绕开
        "跨线程碰 _ctx"限制，多引擎真并行（总耗时 ≈ 最慢引擎）。
        用完即关，实例开销仅在写作/权威等低频 wait_all 场景产生。"""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # 与主浏览器线程（_browser_loop）同一通道策略：BROWSER_CHANNEL=chrome
            # 用系统 Chrome。B26 实测：这里漏了 channel → playwright 去找没下载的
            # 自带 chromium_headless_shell → 渲染组全失败且只报 "Error"（串行路径
            # 用共享 _ctx 带 channel 是好的，只有 wait_all 并行路径踩坑）。
            launch_kw = dict(
                headless=config.BROWSER_HEADLESS,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            if config.BROWSER_CHANNEL:
                launch_kw["channel"] = config.BROWSER_CHANNEL
            try:
                browser = p.chromium.launch(**launch_kw)
            except Exception:
                # 通道指定的浏览器不可用（如系统 Chrome 被卸载）→ 退回自带内核再试一次
                launch_kw.pop("channel", None)
                browser = p.chromium.launch(**launch_kw)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                user_agent=self._http_ua(),
            )
            # 上下文级超时兜底：组级 as_completed 超时后线程无法强杀——
            # 让每个页面操作都在预算附近自爆，线程才能及时退出关浏览器，
            # 不至于一个卡住的引擎把整个实例拖到天荒地老（发热+句柄泄漏）。
            _budget_ms = int((config.FETCH_TIMEOUT_S + 2) * 1000)
            try:
                ctx.set_default_timeout(_budget_ms)
                ctx.set_default_navigation_timeout(_budget_ms)
            except Exception:
                pass
            try:
                fn = getattr(self, "_search_" + name)
                return fn(query, n, _ctx=ctx) or []
            finally:
                try:
                    ctx.close()
                    browser.close()
                except Exception:
                    pass

    def _render_engine_group(self, query, max_results, engines, wait_all=False, stop=None):
        """渲染组。
        wait_all=False（简单查询）：按序经浏览器线程队列串行执行（先到先答
        场景不额外开实例）；某引擎失败/空不影响后续。
        wait_all=True（写作/权威等复杂任务）：每个引擎独立浏览器实例并行跑
        （总耗时≈最慢引擎），全部结果到齐后合并——多源素材必须全部参与；
        剔除 yahoo（对无头 UA 恒重定向/空，纯吃超时）。
        stop：协作式取消（2026-11-01）。HTTP 组先到先用返回后调用方置位，
        渲染组**剩余引擎不再开页面**——实测原来这一步会在答复之后继续拿
        无头 Chrome 撞 baidu/ddg 各 6~15s（结果没人用，还把浏览器队列占住，
        下一次真要渲染时排队等超时——她口中的"浏览器又卡住了"就是这么来的）。"""
        if stop is not None and stop.is_set():
            return [], []
        if wait_all:
            engines = [e for e in engines if e != "yahoo"]
            res, ok = [], []
            # 并行上限：每引擎=独立完整浏览器实例，5 路全开 + 生成同时跑会把
            # CPU 突发顶到 90°C（彻夜测试实测）。3 路封顶，慢引擎自然排队。
            workers = max(1, min(int(config.RENDER_WORKERS), len(engines)))
            ex = _cf.ThreadPoolExecutor(max_workers=workers)
            timed_out = []
            try:
                futs = {ex.submit(self._render_engine_isolated, name, query, max_results): name
                        for name in engines}
                try:
                    for fut in _cf.as_completed(futs, timeout=config.FETCH_TIMEOUT_S + 6):
                        name = futs[fut]
                        try:
                            r = fut.result()
                            if r:
                                res.extend(r)
                                ok.append(name)
                            else:
                                Searcher._fail_mark(name)
                        except Exception as e:
                            Searcher._fail_mark(name)
                            print("[searcher] 渲染引擎 %s 失败: %s: %s" % (name, type(e).__name__, str(e)[:120]), flush=True)
                except _cf.TimeoutError:
                    # 整体预算到点：**保住已完成引擎的结果**，未完成的取消并
                    # 记冷却（原实现让 TimeoutError 直接炸出去，素材全丢——
                    # 线程虽会自行结束，但浏览器要等卡住的页面自己超时才关）。
                    for fut, name in futs.items():
                        if not fut.done():
                            fut.cancel()
                            timed_out.append(name)
                            Searcher._fail_mark(name)
                    if timed_out:
                        print("[searcher] 渲染组整体超时，放弃: %s（已收 %s）"
                              % (",".join(timed_out), ",".join(ok) or "无"), flush=True)
            finally:
                ex.shutdown(wait=False)
            return res, ok
        res, ok = [], []
        for name in engines:
            if stop is not None and stop.is_set():
                print("[searcher] 先到先用已出结果，渲染组剩余引擎跳过：%s"
                      % ",".join(engines[engines.index(name):]), flush=True)
                break
            try:
                r = self._render_engine_search(name, query, max_results)
                if r:
                    res.extend(r)
                    ok.append(name)
                else:
                    Searcher._fail_mark(name)
            except Exception as e:
                Searcher._fail_mark(name)
                print("[searcher] 渲染引擎 %s 失败: %s: %s" % (name, type(e).__name__, str(e)[:120]), flush=True)
        return res, ok

    def _render_engine_search(self, name, query, n, _mode=None):
        """渲染引擎统一入口：经浏览器线程队列执行（防跨线程碰 Playwright）。
        带短超时——某引擎（如国内网络的谷歌）超时即快速失败，不阻塞并行整体。"""
        args = [query, n] + (["official"] if _mode else [])
        try:
            return self._submit("search_engine", (name, tuple(args)), timeout=config.FETCH_TIMEOUT_S + 3)
        except TimeoutError:
            Searcher._fail_mark(name)
            print("[searcher] 渲染引擎 %s 超时(>%ds)，跳过" % (name, config.FETCH_TIMEOUT_S + 3), flush=True)
            return []

    def _http_engine_search(self, name, query, n, _mode=None):
        fn = getattr(self, "_search_" + name)
        return fn(query, n, _mode)

    def _do_search_serial(self, query, max_results, order, errors):
        """顺序轮换兜底（并行模式关闭时），逻辑与原实现一致。"""
        start = Searcher._rot % len(order)
        order = order[start:] + order[:start]
        first_res = None
        for engine_name in order:
            try:
                fn = getattr(self, "_search_" + engine_name)
                res = fn(query, max_results)
                if res:
                    if first_res is None:
                        first_res = res
                    kw = self._kw_words(query)
                    if kw and len(kw) >= 2:
                        best = max(self._kw_hits(kw, r["title"] + r["url"]) for r in res)
                        if best < 4:
                            print("[searcher] 引擎 %s 结果低相关(最高%2d分)，轮换下一引擎" % (engine_name, best), flush=True)
                            Searcher._rot = (Searcher._rot + 1)
                            continue
                    Searcher._rot = (Searcher._rot + 1)
                    Searcher._ok_mark(engine_name)
                    print("[searcher] 引擎 %s 成功 %d 条" % (engine_name, len(res)), flush=True)
                    return res
                Searcher._fail_mark(engine_name)
                errors.append(f"{engine_name}: 0 条")
            except Exception as e:
                Searcher._fail_mark(engine_name)
                errors.append(f"{engine_name}: {type(e).__name__} {str(e)[:120]}")
        if first_res:
            print("[searcher] 各引擎均低相关/失败，兜底返回首批 %d 条" % len(first_res), flush=True)
            return first_res[:max_results]
        raise RuntimeError("所有引擎均失败: " + " | ".join(errors))

    def _wait_any(self, page, selector, timeout_s=8):
        end = time.time() + timeout_s
        while time.time() < end:
            els = page.query_selector_all(selector)
            if els:
                return els
            time.sleep(0.5)
        return []

    def _search_google(self, query, n, _mode=None, _ctx=None):
        page = (_ctx or self._ctx).new_page()
        out = []
        try:
            page.goto("https://www.google.com/search?hl=zh-CN&num=10&q=" + _q(query),
                      timeout=min(config.FETCH_TIMEOUT_S, 6) * 1000, wait_until="domcontentloaded")
            # GDPR 同意页：点击"接受全部"再继续（海外网络下常见的拦截点）
            for _ in range(2):
                if "consent.google.com" in page.url:
                    for sel in ("form[action*='consent'] button:has-text('全部接受')",
                                "form[action*='consent'] button:has-text('Accept all')",
                                "button:has-text('I agree')"):
                        try:
                            page.click(sel, timeout=2500)
                            page.wait_for_load_state("domcontentloaded", timeout=6000)
                            break
                        except Exception:
                            continue
                if "consent.google.com" not in page.url:
                    break
            # 验证页/风控：快速失败，不阻塞并行整体
            if "consent.google.com" in page.url or "captcha" in (page.url or "").lower():
                return []
            body = page.evaluate("document.body.innerText") or ""
            if "unusual traffic" in body.lower() or "captcha" in body.lower():
                return []
            # 谷歌 DOM 常变：多套选择器兜底；a[href^=http] 全量再过滤
            links = self._wait_any(page, "div.g a[href], div.YrXWZb a[href], a[href^='/url?'], a[href^='http']", 6)
            seen = set()
            for a in links:
                url = a.get_attribute("href") or ""
                url = _g_unwrap(url)
                if url in seen:
                    continue
                seen.add(url)
                title = re.sub(r"\s+", " ", (a.inner_text() or "").strip())
                if url.startswith("http") and _is_webpage(url):
                    out.append({"title": title[:120], "url": url, "snippet": ""})
                    if len(out) >= n:
                        break
        except Exception:
            return []      # 被拦/超时/解析失败一律视为该引擎空结果（不 raise 拖垮并行）
        finally:
            page.close()
        return out

    def _search_baidu(self, query, n, _mode=None, _ctx=None):
        """百度：Playwright 渲染抓取（无头模式可正常返回、无验证码）。
        容器：标题用 #content_left h3 a；摘要用 .c-abstract / .c-span-last。
        风控页（"百度安全验证"）→ 返回空，不影响并行整体。"""
        page = (_ctx or self._ctx).new_page()
        out = []
        try:
            page.goto("https://www.baidu.com/s?wd=" + _q(query),
                      timeout=min(config.FETCH_TIMEOUT_S, 6) * 1000, wait_until="domcontentloaded")
            title = page.title() or ""
            if "百度安全验证" in title or "验证码" in (title or "")[:80]:
                # 风控页：隔 1.5s 重试一次（百度风控常一过即放），仍拦截才放弃。
                # 放弃后由 _fail_mark 冷却 + 其它引擎补位（自动换源）。
                page.wait_for_timeout(1500)
                page.goto("https://www.baidu.com/s?wd=" + _q(query),
                          timeout=min(config.FETCH_TIMEOUT_S, 6) * 1000,
                          wait_until="domcontentloaded")
                title = page.title() or ""
                if "百度安全验证" in title or "验证码" in (title or "")[:80]:
                    print("[searcher] baidu 风控拦截（重试后仍验证页），本轮放弃", flush=True)
                    return []
            links = self._wait_any(page, "#content_left h3 a", 6)
            seen = set()
            for a in links[: n * 2]:
                url = a.get_attribute("href") or ""
                title = re.sub(r"\s+", " ", (a.inner_text() or "").strip())
                if not url.startswith("http"):
                    continue
                # 过滤百度站内导航/广告（保留百科：百科摘要是高质量素材）
                if re.search(r"baidu\.com/(s|link|baidu|web)", url, re.I) or "pos.baidu.com" in url:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                # 摘要：向上找结果卡片容器
                card = a.evaluate_handle("(el) => { let p = el; while (p && p.id !== 'content_left' && p.tagName !== 'BODY') { p = p.parentElement; if (p && (p.className || '').includes('result')) return p; } return null; }")
                sn = ""
                if card:
                    c = card.as_element()
                    s = c.query_selector(".c-abstract, .c-span-last p, [class*=abstract]")
                    if s:
                        sn = re.sub(r"\s+", " ", (s.inner_text() or "").strip())[:200]
                out.append({"title": title[:120], "url": url, "snippet": sn})
                if len(out) >= n:
                    break
        finally:
            page.close()
        return out

    def _search_yahoo(self, query, n, _mode=None, _ctx=None):
        page = (_ctx or self._ctx).new_page()
        out = []
        try:
            page.goto("https://search.yahoo.com/search?p=" + _q(query),
                      timeout=min(config.FETCH_TIMEOUT_S, 6) * 1000, wait_until="domcontentloaded")
            items = self._wait_any(page, "div.SearchResult, div.r__o0VQC0Ej8ub", 8)[: n * 2]
            for li in items:
                a = li.query_selector("h3 a")
                if a is None:
                    continue
                url = a.get_attribute("href") or ""
                title = re.sub(r"\s+", " ", (a.inner_text() or "").strip())
                if url.startswith("http") and _is_webpage(url):
                    sn = li.query_selector(".cr-1-srwL, p")
                    snippet = re.sub(r"\s+", " ", (sn.inner_text() or "").strip())[:200] if sn else ""
                    out.append({"title": title, "url": url, "snippet": snippet})
                    if len(out) >= n:
                        break
        finally:
            page.close()
        return out

    def _search_bing(self, query, n, _mode=None):
        """必应：HTTP 直抓（urllib 带 UA）解析 li.b_algo。
        bing 对普通 HTTP 请求返回完整结果页；playwright 渲染反而被访问限制/
        渲染失败（ElementHandle 报错）。HTTP 直抓更快、更稳，且不依赖浏览器线程。
        先对 query 做轻量规范化（去"今天/大概"等口语词），避免 bing 把"今天"当
        主导词返回黄历/万年历等不相关内容（"今天黄金价格" → "黄金价格"）。
        """
        sq = self._clean_query(query)
        html = self._http_get("https://www.bing.com/search?setlang=zh-CN&q=" + _q(sq))
        if not html:
            print("[searcher] bing http_get 失败返回空", flush=True)
            return []
        if html.count("b_algo") == 0:
            print("[searcher] bing 无 b_algo len=%d head=%s" % (len(html), html[:120].replace("\n", " ")), flush=True)
            return []
        out = []
        for b in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
            t = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not t:
                continue
            url = _html.unescape(t.group(1)).strip()
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t.group(2))).strip()
            m = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
            snippet = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:200] if m else ""
            if url.startswith("http") and _is_webpage(url):
                out.append({"title": title, "url": url, "snippet": snippet})
        # 按 query 关键词相关性排序：把含查询实义词的标题/链接排前，
        # 避免"今天黄金价格"被"万年历/黄历"等联想结果占位（bing 结果常混入不相关页）。
        kw = self._kw_words(query)
        out.sort(key=lambda r: self._kw_hits(kw, r["title"] + r["url"]), reverse=True)
        return out[:n]

    def _clean_query(self, q):
        """搜索前规范化：去掉口语/时间/疑问词，保留核心实体词。
        解决 bing 把"今天/多少"等当主导词、返回黄历/万年历等不相关内容的问题
        （"今天黄金价格大概多少" → "黄金价格"，可稳定搜到金价网站）。"""
        q2 = re.sub(r"多少钱|怎么样|为什么|什么时候|价格多少|我想知道|有没有"
                    r"|今天|今日|昨天|明天|现在|目前|最新|大概|大约"
                    r"|多少|什么|怎么|请问|一下|呢|吗|的|了|帮我|是"
                    r"|说出姓名|告诉我|请告诉我|说一下|回答我", "", q)
        # 贵金属/金价类查询：去掉单位与行情修饰词。bing 对
        # "国际金价每盎司美元"会联想成"国际新闻"首页，而"国际金价"
        # 稳定返回金价行情站（水贝金价网/今日金价/黄金走势图等）。
        if any(w in q for w in ("金价", "黄金", "铂金", "白银", "贵金属")):
            q2 = re.sub(r"每盎司|盎司|一克|每克|美元|人民币|港币|欧元|英镑"
                        r"|行情|走势|实时|报价|查询|最新价格", "", q2)
        # 绝对日期降级到"年/月"级：精确到"日"的搜索极易被引擎联想成节假日/日历页
        # （"2026年10月20日 国际黄金价格"→ bing 返回 2026 节假日日历），
        # 而行情类查询真正需要的是月份级的近期数据页 → "2026年10月 国际黄金价格"。
        q2 = re.sub(r"(\d{4}年\d{1,2}月)\d{1,2}日", r"\1", q2)
        # 历史/成语查询：提取引号内的成语/典故词作搜索词（"成语'围魏救赵'…"
        # 整句会被 bing 联想成"成语"列表页，来源全是成语站首页）
        mq = re.search(r"['\"“”‘’]([^'\"“”‘’]{2,12})['\"“”‘’]", q2 or q)
        if mq:
            _kw = mq.group(1)
            if any(w in (q2 or q) for w in ("战役", "典故", "背景", "来历", "关系", "事迹", "人物", "故事")):
                return _kw + " 战役 背景 人物"
            return _kw
        # 职务查询：直接抽取"国名+职务"作搜索词（整句联想失败成"中国"首页）。
        # 去掉"现任"等任期词：带任期词会命中"现任职务"类百科页而非人物页。
        m = _POSITION_PAT.search(q2 or q)
        if m:
            pos = re.sub(r"现任|目前|当前|新", "", m.group(0))
            return pos or m.group(0)
        q2 = re.sub(r"\s+", " ", q2).strip(" ，。？！")
        return q2 or q

    @staticmethod
    def _kw_words(q):
        """中文长句无空格，findall {2,} 会整块匹配导致核心词丢失
        （'国际金价每盎司美元' 被当成一个词）。改为：
        - 短中文块(≤4字)整块作为核心词；
        - 长中文块按 2 字滑动窗口切词（'金价''美元'等实体词可独立命中）。
        排除口语/时间虚词。"""
        _DROP = ("今天", "昨天", "明天", "现在", "目前", "最新", "大概", "大约",
                 "多少", "什么", "怎么", "请问", "这个", "一个", "还有", "可以",
                 "有没有", "是不是", "多少美元")
        words = []
        for block in re.findall(r"[\u4e00-\u9fff]{2,}", q):
            if len(block) <= 4:
                cand = [block]
            else:
                cand = [block[i:i + 2] for i in range(len(block) - 1)]
            for w in cand:
                if w not in _DROP and w not in words:
                    words.append(w)
        return words

    def _kw_hits(self, kw, text):
        """查询核心词对结果的相关性计数：整词命中记 2 分，两字片段命中记 1 分。
        用途：金价站标题多为"今日黄金价格/国际金价"等，不含完整查询词
        （如"国际黄金价格"），必须用片段匹配才不会漏判相关结果。"""
        n = 0
        for w in kw:
            if w in text:
                n += 2
                continue
            for i in range(len(w) - 1):
                if w[i:i + 2] in text:
                    n += 1
                    break
        return n

    def _http_get(self, url, timeout=None):
        """轻量 HTTP GET：带浏览器 UA + Cookie 会话（模拟真实浏览器连续搜索，
        降低引擎对无 cookie 高频请求的访问限制），返回页面文本；失败返回 None。"""
        try:
            if self._http_opener is None:
                self._http_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            req = urllib.request.Request(url, headers={
                "User-Agent": self._http_ua(),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.bing.com/"})
            with self._http_opener.open(req, timeout=timeout or 12) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            return None

    def _http_ua(self):
        # 轮换常见浏览器 UA，降低指纹识别概率
        _uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.2088.61",
        ]
        return _uas[int(time.time()) % len(_uas)]

    def _search_ddg(self, query, n, _mode=None, _ctx=None):
        """用 DDG 主站（js 渲染版）：lite/html 端点在部分网络环境返回空壳，主站可解析。"""
        page = (_ctx or self._ctx).new_page()
        out = []
        try:
            page.goto("https://duckduckgo.com/?q=" + _q(query) + "&ia=web",
                      timeout=min(config.FETCH_TIMEOUT_S, 6) * 1000, wait_until="domcontentloaded")
            time.sleep(2.2)  # 等结果渲染
            links = page.query_selector_all("a[href^='http']")
            seen = set()
            for a in links:
                url = a.get_attribute("href") or ""
                if url in seen or not _is_webpage(url):
                    continue
                if "duckduckgo" in url:      # 过滤 DDG 自身导航/商店/推广链接
                    continue
                title = re.sub(r"\s+", " ", (a.inner_text() or "").strip())
                if len(title) < 6:      # 过滤图标/短链接
                    continue
                seen.add(url)
                out.append({"title": title[:120], "url": url, "snippet": ""})
                if len(out) >= n:
                    break
        finally:
            page.close()
        return out

    def _search_sogou(self, query, n, _mode=None, _ctx=None):
        page = (_ctx or self._ctx).new_page()
        out = []
        try:
            page.goto("https://www.sogou.com/web?query=" + _q(query),
                      timeout=min(config.FETCH_TIMEOUT_S, 6) * 1000, wait_until="domcontentloaded")
            items = self._wait_any(page, "div.vrwrap, div.rb", 8)[: n * 2]
            for li in items:
                a = li.query_selector("h3 a")
                if not a:
                    continue
                url = a.get_attribute("href") or ""
                title = (a.inner_text() or "").strip()
                if url.startswith("/") or url.startswith("http://www.sogou.com"):
                    url = "https://www.sogou.com" + url if url.startswith("/") else url
                if url.startswith("http") and _is_webpage(url) and "sogou.com/link" not in url:
                    cap = li.query_selector("p")
                    snippet = (cap.inner_text() or "").strip() if cap else ""
                    out.append({"title": title, "url": url, "snippet": snippet})
                if len(out) >= n:
                    break
        finally:
            page.close()
        return out

    def _do_fetch(self, url):
        page = self._ctx.new_page()
        info = {"url": url, "title": "", "text": "", "fetched_at": time.strftime("%F %T")}
        try:
            page.goto(url, timeout=config.FETCH_TIMEOUT_S * 1000, wait_until="domcontentloaded")
            time.sleep(1.0)
            try:
                # networkidle 对国内站几乎必等满（轮询/广告请求不断）；
                # 2.5s 截断后直接抽取——domcontentloaded+1s 已足够拿到正文
                page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            info["title"] = (page.title() or "").strip()
            raw = page.evaluate(
                """() => {
                    const kill = ['script','style','noscript','nav','header','footer',
                                  'aside','form','iframe','svg','button'];
                    const doc = document.cloneNode(true);
                    kill.forEach(t => doc.querySelectorAll(t).forEach(e => e.remove()));
                    let best = null, bestLen = 0;
                    const cand = doc.querySelectorAll('article, main, [role=main], .content, #content, body');
                    cand.forEach(el => { const L = (el.innerText||'').length; if (L > bestLen) { bestLen=L; best=el; } });
                    return best ? best.innerText : '';
                }"""
            ) or ""
            text = _clean(raw)
            info["text"] = text[: config.PER_PAGE_MAX_CHARS]
            info["truncated"] = len(text) > config.PER_PAGE_MAX_CHARS
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        finally:
            page.close()
        return info


def _q(s):
    from urllib.parse import quote
    return quote(s)

def _ddg_unwrap(u):
    m = re.search(r"uddg=([^&]+)", u or "")
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return u or ""

def _g_unwrap(u):
    """谷歌重定向链接 /url?q=... → 真实 URL。"""
    u = u or ""
    if u.startswith("/url?"):
        u = "https://www.google.com" + u
    m = re.search(r"[?&]q=([^&]+)", u)
    if m and "google.com" in u:
        from urllib.parse import unquote
        return unquote(m.group(1))
    return u

_BLOCKED = re.compile(
    r"(^https?://(www\.)?(bing|duckduckgo|google|yahoo|search|duck)\.)|"
    r"(duck\.ai)|"
    r"(accounts\.google\.com)|(consent\.google\.)|(\.(cn|com|net)/url\?)|(#)|"
    r"(bing\.com/ck/)|(search\.yahoo\.com)", re.I)

def _is_webpage(url):
    if _BLOCKED.search(url):
        return False
    if url.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".exe", ".png", ".jpg")):
        return False
    return True

_WS = re.compile(r"[ \t\u3000]+")
_MANYNL = re.compile(r"\n{3,}")

def _clean(t):
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = _WS.sub(" ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    t = _MANYNL.sub("\n\n", t)
    return t.strip()
