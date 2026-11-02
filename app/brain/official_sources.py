# -*- coding: utf-8 -*-
"""桌宠AI · 时效性问题的官方源直连（official_sources.py）。

主人定调（2026-10-28）：时效性问题（天气/金价/汇率/油价/股指…）**都有官网**，
不该把整句丢给搜索引擎再翻结果页——慢、且常搜到二手转载。本模块做两件事：

1. `match(query)`：识别时效类别 → 给出官方站点（label/domains/site）；
2. `fetch(query, info)`：**直连官网**取结构化数据（天气 / 外汇牌价），
   解析成一段干净文本给她当材料——比搜索快、比转载准。

实测（2026-10-28，本机）：
- 中国天气网城市码接口 toy1.weather.com.cn/search?cityname=城市 → ref 前 9 位即城市码；
  天气预报页 weather.com.cn/weather/{code}.shtml 是静态 UTF-8 HTML，可解析。
- 中国银行外汇牌价 boc.cn/sourcedb/whpj/ 是静态表格（货币名+买入/卖出/折算价+时间）。
- 上海黄金交易所 sge.com.cn 页面为 JS 渲染（无直抓价值）→ 用官方域限定搜索。
- 其余类别（油价/股指/空气质量）同样走官方域限定搜索，不做直抓（避免解析脆弱）。

失败一律返回 None，调用方退回常规搜索——官方源是加速与提准，不是单点依赖。
"""
import re
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

# 类别目录：pat 命中即归类；domains 用于结果加权/排序，site 用于搜索限定
CATS = [
    {"cat": "weather", "label": "中国天气网（中国气象局）",
     "pat": re.compile(r"(天气|气温|温度|下雨|降雨|降雪|有雨|有雪|雨吗|雪吗|"
                       r"台风|大风|雾霾|空气质量|多少度|几度|穿什么|带伞|冷不冷|热不热)"),
     "domains": ("weather.com.cn", "cma.gov.cn"), "site": "weather.com.cn",
     "direct": True},
    {"cat": "gold", "label": "上海黄金交易所",
     "pat": re.compile(r"(金价|黄金(价格|行情|多少钱|价)|银价|铂金|黄金t\+d|t\+d)"),
     "domains": ("sge.com.cn",), "site": "sge.com.cn", "direct": False},
    {"cat": "fx", "label": "中国银行外汇牌价 / 中国人民银行",
     "pat": re.compile(r"(汇率|外汇牌价|美元|欧元|日元|港币|英镑|澳元|加元|"
                       r"人民币(兑|换|汇率))"),
     "domains": ("boc.cn", "pbc.gov.cn", "safe.gov.cn"), "site": "boc.cn",
     "direct": True},
    {"cat": "oil", "label": "国家发展改革委（成品油价格）",
     "pat": re.compile(r"(油价|汽油|柴油|成品油|加油)"),
     "domains": ("ndrc.gov.cn",), "site": "ndrc.gov.cn", "direct": False},
    {"cat": "index", "label": "上交所 / 深交所 / 中证指数",
     "pat": re.compile(r"(上证指数|深证成指|创业板指|沪深300|大盘|A股(指数|行情)|"
                       r"股指|股市(今天|行情|怎么样))"),
     "domains": ("sse.com.cn", "szse.cn", "csindex.com.cn"),
     "site": "sse.com.cn", "direct": False},
    {"cat": "air", "label": "中国环境监测总站 / 生态环境部",
     "pat": re.compile(r"(AQI|PM2\.5|pm2\.5|空气污染|空气质量指数)"),
     "domains": ("air.cnemc.cn", "mee.gov.cn"), "site": "mee.gov.cn",
     "direct": False},
]

# 主要城市 → 城市码（内置兜底，省一次网络往返；接口失败时用）
CITY_CODE = {
    "北京": "101010100", "上海": "101020100", "天津": "101030100", "重庆": "101040100",
    "广州": "101280101", "深圳": "101280601", "杭州": "101210101", "南京": "101190101",
    "武汉": "101200101", "成都": "101270101", "西安": "101110101", "郑州": "101180101",
    "济南": "101120101", "青岛": "101120201", "长沙": "101250101", "合肥": "101220101",
    "福州": "101230101", "厦门": "101230201", "南昌": "101240101", "太原": "101100101",
    "石家庄": "101090101", "沈阳": "101070101", "大连": "101070201", "长春": "101060101",
    "哈尔滨": "101050101", "昆明": "101290101", "贵阳": "101260101", "南宁": "101300101",
    "海口": "101310101", "兰州": "101160101", "西宁": "101150101", "银川": "101170101",
    "乌鲁木齐": "101130101", "呼和浩特": "101080101", "拉萨": "101140101",
    "苏州": "101190401", "无锡": "101190201", "宁波": "101210401", "东莞": "101281601",
    "佛山": "101280800", "珠海": "101280701", "温州": "101210701", "烟台": "101120501",
}

_CUR_NAMES = ("美元", "欧元", "日元", "港币", "英镑", "澳元", "加元", "新加坡元",
              "瑞士法郎", "澳门元", "韩元", "泰铢")


def match(query):
    """识别时效类别。返回 info dict 或 None（不在表内 → 常规检索）。"""
    q = (query or "").strip()
    if not q:
        return None
    for c in CATS:
        if c["pat"].search(q):
            return {"cat": c["cat"], "label": c["label"],
                    "domains": c["domains"], "site": c["site"],
                    "direct": c["direct"]}
    return None


def site_queries(query, info):
    """官方域限定搜索词（直抓失败/不支持直抓时用）：限定官方站，避免二手转载。"""
    if not info:
        return []
    return [f"{query} site:{info['site']}"]


# ---------------------------------------------------------------- 抓取底座
def _get(url, timeout=10, referer=None):
    h = dict(_UA)
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch(query, info):
    """直连官网取数据。返回 {"title","url","text"} 或 None（不支持/失败 → 搜索兜底）。"""
    if not info or not info.get("direct"):
        return None
    try:
        if info["cat"] == "weather":
            return _weather(query)
        if info["cat"] == "fx":
            return _fx(query)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- 天气
def _city_of(query):
    """从问句里认城市：先看内置表命中，再用"XX天气/XX气温"正则提，最后查官方接口。"""
    for name, code in CITY_CODE.items():
        if name in query:
            return name, code
    m = re.search(r"([\u4e00-\u9fa5]{2,6}?)(?:今天|明天|后天|现在)?的?"
                  r"(?:天气|气温|温度|下雨|降雨|多少度)", query)
    cand = m.group(1) if m else ""
    cand = re.sub(r"^(请问|帮我|查一下|看看|今天|明天|现在)", "", cand)
    if cand in CITY_CODE:
        return cand, CITY_CODE[cand]
    if cand:
        code = _city_code_online(cand)
        if code:
            return cand, code
    return None, None


def _city_code_online(name):
    """官方城市码接口（toy1.weather.com.cn）：返回 9 位码或 None。"""
    try:
        u = ("http://toy1.weather.com.cn/search?cityname="
             + urllib.parse.quote(name))
        raw = _get(u, timeout=8).decode("utf-8", "ignore")
        m = re.search(r'"ref":"(\d{9})', raw)
        return m.group(1) if m else None
    except Exception:
        return None


def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _weather(query):
    city, code = _city_of(query)
    if not code:
        return None                        # 认不出城市：交给搜索（不猜）
    url = f"https://www.weather.com.cn/weather/{code}.shtml"
    html = _get(url, referer="https://www.weather.com.cn/").decode("utf-8", "ignore")
    if 'class="t clearfix"' not in html:
        return None
    seg = html.split('class="t clearfix"')[-1].split("</ul>")[0]
    items = seg.split("<li")[1:4]          # 今天/明天/后天
    lines = []
    for it in items:
        day = _strip_tags(re.search(r"<h1>(.*?)</h1>", it).group(1)) if re.search(
            r"<h1>(.*?)</h1>", it) else ""
        wea = re.search(r'class="wea"[^>]*>(.*?)</p>', it, re.S)
        wea = _strip_tags(wea.group(1)) if wea else ""
        tem = re.search(r'class="tem">(.*?)</p>', it, re.S)
        tem = re.sub(r"\s+", "", _strip_tags(tem.group(1))) if tem else ""
        win = re.search(r'class="win">(.*?)</p>', it, re.S)
        win = re.sub(r"\s+", " ", _strip_tags(win.group(1))) if win else ""
        if day:
            lines.append(f"{day}：{wea} {tem} {win}".strip())
    if not lines:
        return None
    now = _weather_now(code)               # 实时观测 + 今日高低温（补 7 日页只有最低温的短板）
    text = (f"【中国天气网 · {city}】{time_now()}\n" +
            ("\n".join(now) + "\n" if now else "") + "\n".join(lines) +
            "\n（数据来源：中国气象局中国天气网 weather.com.cn，官方发布）")
    return {"title": f"{city}天气预报（中国天气网）", "url": url, "text": text}


def _first_json(s):
    """从字符串里抠第一个完整 JSON 对象（官方 JS 数据文件常是 `var x={...};var y=...`，
    贪婪正则会把后面对象一起吞掉导致解析失败——用括号配平取第一个）。"""
    import json
    i = s.find("{")
    if i < 0:
        return {}
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    return {}
    return {}


def _weather_now(code):
    """实时观测（sk_2d）+ 今日高低温（dingzhi）：官方 JS 数据文件，实测可用。"""
    out = []
    try:
        raw = _get(f"http://d1.weather.com.cn/sk_2d/{code}.html",
                   referer="http://www.weather.com.cn/", timeout=6).decode("utf-8", "ignore")
        d = _first_json(raw)
        if d.get("temp"):
            out.append("实时：%s℃　%s%s　湿度%s　气压%shPa（%s 观测）" % (
                d.get("temp"), d.get("WD", ""), d.get("WS", ""),
                d.get("SD", ""), d.get("qy", ""), d.get("time", "")))
    except Exception:
        pass
    try:
        raw = _get(f"http://d1.weather.com.cn/dingzhi/{code}.html",
                   referer="http://www.weather.com.cn/", timeout=6).decode("utf-8", "ignore")
        d = (_first_json(raw) or {}).get("weatherinfo", {})
        if d.get("weather"):
            out.append("今日：%s　%s/%s　%s%s" % (
                d.get("weather"), d.get("temp", ""), d.get("tempn", ""),
                d.get("wd", ""), d.get("ws", "")))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 外汇牌价
def _fx(query):
    url = "https://www.boc.cn/sourcedb/whpj/"
    html = _get(url, referer="https://www.boc.cn/").decode("utf-8", "ignore")
    cells = [re.sub(r"<[^>]+>|&nbsp;|\s+", "", c)
             for c in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)]
    # 实测（中行 2026-10-28）：每币种占 8 格——
    # 名称 / 现汇买入 / 现钞买入 / 现汇卖出 / 现钞卖出 / 中行折算价 / 发布时间 / 时间
    rows, i = [], 0
    while i + 6 < len(cells):
        name = cells[i]
        if name in _CUR_NAMES:
            rows.append((name, cells[i + 1], cells[i + 3], cells[i + 5], cells[i + 6]))
            i += 8
        else:
            i += 1
    if not rows:
        return None
    want = [c for c in _CUR_NAMES if c in query]
    picked = [r for r in rows if r[0] in want] if want else rows[:8]
    lines = [f"{n}：现汇买入 {b} / 现汇卖出 {s} / 折算价 {z}（{t}）"
             for n, b, s, z, t in picked]
    text = ("【中国银行外汇牌价】" + time_now() + "\n" + "\n".join(lines) +
            "\n（数据来源：中国银行官网 boc.cn/sourcedb/whpj，100 外币=人民币元）")
    return {"title": "中国银行外汇牌价（官网）", "url": url, "text": text}


def time_now():
    import time
    return time.strftime("%Y-%m-%d %H:%M")


def rank(url, info):
    """官方域加权：命中官方域排前（与 pipeline._auth_rank 协同）。"""
    u = (url or "").lower()
    if info:
        for d in info.get("domains", ()):
            if d in u:
                return -100
    return 1
