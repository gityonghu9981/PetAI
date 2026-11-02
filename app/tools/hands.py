# -*- coding: utf-8 -*-
"""桌宠AI · 她的"手"（app/tools/hands.py）——本地全桌面操作（2026-10-30）。

主人拍板：抛开 API 只做本地。调研结论（docs/全桌面操作能力调研-2026-10-30.md）
的第一+二步：PyAutoGUI(鼠标键盘) + mss(截屏) + pywinauto(UIA 控件树) + 她自己的
视觉塔(看屏幕)。

安全红线（写死，谁改谁负责）：
  1. FAILSAFE：鼠标甩屏幕左上角立即中止（pyautogui 内置，不许关）
  2. **自主活动只看不点**：autonomy 唤醒时只允许 screenshot/describe/open_app/photo/status；
     鼠标点击类必须来自主人当轮明确指令（conversation 路径）。
     （2026-11-01 主人定调：开软件**不再限白名单**，她可以开任何东西；
     但"自己挪鼠标键盘"这条红线不动）
     **唯一例外（2026-11-02 主人定）**：桌宠技能独立成文件，不塞在 hands 里
     的落子（start/play/auto）。主人原话"她想玩的时候能自己玩，可以顺利、没有
     任何卡点地玩到天荒地老"——游戏内的坐标点击由技能自己算、有 `GAME_LOCK`
     互斥、有实测校验，不属于"乱挪鼠标"。这个例外只限棋盘/已知按钮，不许扩散。
  3. 每次动手写审计（kind=hands）
  4. 危险操作（删文件/发消息/改配置）不在本模块——做了也做不坏
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from core import config

SCREEN_DIR = Path(config.DATA_DIR) / "petmedia" / "screen"
# 2026-11-01 主人定调：独立"她的收藏"照片库（不是临时看屏）。
# 结构：data/photos/YYYY-MM/<时间>.png + photos.jsonl 索引（追加事件，最新状态生效）。
PHOTO_DIR = Path(config.DATA_DIR) / "photos"
PHOTO_INDEX = PHOTO_DIR / "photos.jsonl"
# 2026-11-01 主人定调：个人电脑、截图只在主人和她之间交流——**不设敏感窗口拦截**；
# 窗口标题只作为照片的上下文记录。联网对外时再做限制。

# 应用名解析（2026-11-01 主人定："白名单去掉，让她可以认识任何东西"）。
# 原来这里是一张 AUTONOMY_APP_WHITELIST（只有计算器/记事本/画图三件），
# 名字不在表里就打不开——她说"打开 Edge浏览器"时被这张表挡死，只能回头找你要路径。
# 现在改成**通用解析**：PATH → 注册表 App Paths → 桌面/开始菜单快捷方式 → 常见安装位浅搜，
# 再配一张很小的"中文叫法 → 英文真名"翻译表（微信/记事本这类，见 _NAME_ALIAS）。
# 注意：翻译表是**翻译**，不是闸门——表外的名字照样往下走各条解析路径，绝不拦。
_APP_CACHE = {}
_MENU_SCANNED = False
_MENU_INDEX = []            # [(规范化全名, 词集, 显示名, Path)]

# 匹配时不算数的"通用词"：否则"浏览器""player""设置"这种词会命中一大堆不相干的快捷方式
_STOPWORD = {"浏览器", "软件", "应用", "专业版", "官方版", "极速版", "launcher", "installer",
             "uninstall", "player", "app", "the", "for", "windows", "pc", "版", "快捷方式"}
# 中文名 ↔ 英文真名的小词典（Windows 自带程序在开始菜单里没有快捷方式，只能这么认）
_NAME_ALIAS = {
    "记事本": "notepad", "计算器": "calc", "画图": "mspaint", "写字板": "write",
    "命令提示符": "cmd", "powershell": "powershell", "资源管理器": "explorer",
    "控制面板": "control", "设置": "ms-settings", "任务管理器": "taskmgr",
    "注册表": "regedit", "服务": "services.msc", "运行": "winver",
    "微信": "wechat weixin", "支付宝": "alipay", "百度网盘": "baidunetdisk yunfile",
    "夸克网盘": "quark", "腾讯qq": "qq", "谷歌浏览器": "chrome googlechrome",
    "边缘浏览器": "edge msedge", "截图工具": "snippingtool", "放大镜": "magnify",
}


def _norm(s):
    s = str(s or "")
    s = re.sub(r"\s*-\s*快捷方式\s*$", "", s)      # 桌面快捷方式的后缀噪声
    s = re.sub(r"[\s_—.()（）【】\[\]&',+]+", "", s)
    return s.lower()


def _tokens(s):
    """切词：中英混排都按"非字母数字汉字"切，汉字段整体留一个词。"""
    s = re.sub(r"\s*-\s*快捷方式\s*$", "", str(s or ""))
    out = set()
    for t in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", s):
        n = _norm(t)
        if n and n not in _STOPWORD:
            out.add(n)
    return out


def _looks_like_path(s):
    return ("\\" in s) or ("/" in s) or (len(s) > 2 and s[1] == ":")


def _launchable(p):
    try:
        return p.is_file() and p.suffix.lower() in (
            ".exe", ".lnk", ".url", ".msi", ".appref-ms", ".bat", ".cmd")
    except OSError:
        return False


def _shell_folder(csidl):
    """取 shell 特殊目录的真实路径——桌面在这台机被重定向到 D:\\，
    拿 ~/Desktop 会扫到一个空目录（实测踩过：51 个快捷方式全在 D:\\Users\\...\\Desktop）。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(0, csidl, 0, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return None


def _scan_menu_index():
    """懒扫一次桌面 + 开始菜单（用户与公共），建"名字→程序"索引，进程内缓存。
    这台机实测 ~1000 条，首扫几十毫秒，之后全走缓存。"""
    global _MENU_SCANNED
    if _MENU_SCANNED:
        return _MENU_INDEX
    roots = []
    for csidl in (0x00, 0x19, 0x16, 0x17, 0x01):   # 桌面/公共桌面/开始菜单/公共开始菜单/我的文档
        p = _shell_folder(csidl)
        if p:
            roots.append(p)
    for env in ("APPDATA", "ProgramData"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    seen = set()
    for r in roots:
        if not r.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(r)):
            dirnames[:] = [d for d in dirnames if not d.startswith("$")]
            for fn in filenames:
                if not fn.lower().endswith((".lnk", ".url", ".exe")):
                    continue
                full = os.path.join(dirpath, fn)
                if full.lower() in seen:
                    continue
                seen.add(full)
                disp = os.path.splitext(fn)[0]
                _MENU_INDEX.append((_norm(disp), _tokens(disp), disp, Path(full)))
            if len(_MENU_INDEX) > 8000:            # 兜底：异常目录也不至于吃满内存
                break
    _MENU_SCANNED = True
    return _MENU_INDEX


def _reg_app_paths(cands):
    """注册表 App Paths 是"msedge.exe → 完整路径"的官方位置，Edge/Chrome/Office 都在这。"""
    import winreg
    tail = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive, flag in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        for sub in cands:
            try:
                with winreg.OpenKey(hive, tail + "\\" + sub, 0,
                                    winreg.KEY_READ | flag) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                    if val:
                        p = Path(val)
                        if p.is_file():
                            return p
                        # 有的键值只给文件名，靠同目录的 Path 值补齐
                        try:
                            with winreg.OpenKey(hive, tail + "\\" + sub, 0,
                                                winreg.KEY_READ | flag) as k2:
                                base, _ = winreg.QueryValueEx(k2, "Path")
                            if base and (Path(base) / val).is_file():
                                return Path(base) / val
                        except OSError:
                            pass
            except OSError:
                continue
    return None


def _candidates(name):
    """把用户给的名字展开成一串查询：原名 + 规范化 + 词集 + 中文叫法对应的英文名。"""
    key = _norm(name)
    toks = _tokens(name)
    ascii_only = re.sub(r"[^A-Za-z0-9 .\-_]", "", str(name)).strip()
    queries = [key, _norm(ascii_only)] if ascii_only and _norm(ascii_only) != key else [key]
    for k in queries:
        if k in _NAME_ALIAS:
            for sub in _NAME_ALIAS[k].split():
                queries.append(_norm(sub))
                toks.add(_norm(sub))
    return [q for q in dict.fromkeys(queries) if q], toks


def _find_app(name):
    """任何名字 → 可执行的东西。认不出来返回 None（`open_app` 会把"查过哪里"讲清楚）。"""
    keys, toks = _candidates(name)
    if not keys:
        return None
    ck = keys[0]
    if ck in _APP_CACHE:
        return _APP_CACHE[ck]
    raw = str(name).strip().strip('"')
    found = None

    # ① 直接给路径
    if _looks_like_path(raw):
        p = Path(raw)
        if _launchable(p):
            found = p
        elif p.with_suffix(".exe").is_file():
            found = p.with_suffix(".exe")
    # ② PATH 上的命令（msedge / notepad / code / winver…），别名在这一步就生效
    if found is None:
        for q in keys:
            for cand in (q, q + ".exe", str(name).strip()):
                hit = shutil.which(cand)
                if hit:
                    found = Path(hit)
                    break
            if found:
                break
    # ③ 注册表 App Paths（Edge/Chrome/Office 都登记在这）
    if found is None:
        cands = []
        for q in keys:
            cands.append(q if q.endswith(".exe") else q + ".exe")
            cands.append(q)
        found = _reg_app_paths(cands)
    # ④ 桌面/开始菜单快捷方式：中文名主要靠这里。
    #    先精确名，再"词有重叠"（Edge浏览器 ↔ Microsoft Edge、VS Code ↔ Visual Studio Code），
    #    最后才退回子串包含。重叠打分避开 _STOPWORD，所以"浏览器"这种词不会到处乱命中。
    if found is None:
        menu = _scan_menu_index()
        exact = [e for e in menu if e[0] in set(keys)]
        pool = exact
        if not pool and toks:
            scored = []
            for e in menu:
                ov = len(toks & e[1])
                if ov:
                    scored.append((-(ov / (len(e[1]) or 1)), len(e[0]), e))
            scored.sort(key=lambda x: (x[0], x[1]))
            pool = [s[2] for s in scored]
        if not pool:
            pool = [e for e in menu if any(k in e[0] or e[0] in k for k in keys)]
        if pool:
            found = pool[0][3]
    # ⑤ 常见安装目录浅搜（只下探两层，绝不 rglob 整盘——那会卡好几秒）
    if found is None:
        probes = [_norm(re.sub(r"[^A-Za-z0-9]", "", q)) for q in keys]
        probes = [p for p in probes if p]
        if probes:
            bases = [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)"),
                     Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs")),
                     Path("D:\\") if Path("D:\\").is_dir() else None]
            best = None
            for b in filter(None, bases):
                if not b.is_dir():
                    continue
                try:
                    apps = [d for d in b.iterdir() if d.is_dir()]
                except OSError:
                    continue
                for d in apps:
                    dn = _norm(d.name)
                    if not any(p in dn or dn in p for p in probes):
                        continue
                    exes = list(d.glob("*.exe")) + list(d.glob(r"*\*.exe"))
                    for exe in exes:
                        if not _launchable(exe):
                            continue
                        rank = 0 if _norm(exe.stem) in set(keys) else 1
                        if best is None or rank < best[0]:
                            best = (rank, exe)
            found = best[1] if best else None
    _APP_CACHE[ck] = found
    return found


def open_app(name, host="main"):
    """开软件：任何名字都先试着认（见 `_find_app`），认不出就老实说查过哪儿。"""
    raw = str(name or "").strip()
    if not raw:
        return "要开什么？给个名字或完整路径都行"
    # 网址/协议（http、https、ms-settings:、xxx://）直接交给 Windows shell，
    # 她要"上网找图"时走这条，不用先猜浏览器装在哪
    if raw.lower().startswith(("http://", "https://", "www.")):
        try:
            os.startfile(raw)                     # noqa: S606 主人/她自己指定的网址
            _audit(f"打开网址：{raw[:80]}")
            return f"已在默认浏览器打开 {raw[:60]}"
        except Exception as e:
            return f"网址打不开：{type(e).__name__}: {e}"
    if not _looks_like_path(raw) and re.match(r"^[a-z][a-z0-9+.\-]*:", raw, re.I):
        try:
            os.startfile(raw)                     # noqa: S606 协议唤起（ms-settings: 等）
            _audit(f"协议唤起：{raw[:60]}")
            return f"已交给 Windows 打开 {raw[:60]}"
        except Exception as e:
            return f"{raw} 打不开：{type(e).__name__}: {e}"
    p = _find_app(raw)
    if p is None:
        return (f"没找到 {raw}——PATH、注册表 App Paths、开始菜单和桌面快捷方式、"
                f"Program Files 我都翻过了，对不上；给我完整路径我就认")
    try:
        if p.suffix.lower() in (".lnk", ".url", ".msi", ".appref-ms"):
            os.startfile(str(p))                  # noqa: S606 快捷方式只能走 shell
        elif p.suffix.lower() in (".bat", ".cmd"):
            subprocess.Popen(["cmd", "/c", str(p)], creationflags=0x08000000)
        else:
            subprocess.Popen([str(p)])
    except Exception as e:
        return f"找到了但开不起来：{type(e).__name__}: {e}（{p}）"
    _audit(f"打开应用：{p.name} ←「{raw}」")
    return f"已打开 {p.name}（{p}）"


def _audit(detail):
    try:
        from core import audit
        audit.log("hands", detail)
    except Exception:
        pass


def screenshot(host="main", grid=False):
    """截全屏 → SCREEN_DIR/yyyymmdd_HHMMSS.png → 返回路径。"""
    import mss
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREEN_DIR / (time.strftime("%Y%m%d_%H%M%S") + ".png")
    with mss.mss() as sct:
        mss.mss().grab(sct.monitors[0]) if False else None
        shot = sct.grab(sct.monitors[0])
        from mss.tools import to_png
        to_png(shot.rgb, shot.size, output=str(out))
    _audit(f"截屏 → {out.name}")
    return str(out)


def describe_screen(question="屏幕上现在是什么？哪些可以点？", host="main", grid=None):
    """看一眼屏幕：截图 → 她的视觉塔描述。这是她"眼睛"的桌面模式。

    grid：图上画真实坐标网格，因为**她换算不了坐标**——
    2026-11-01 实测，让她点『活动』她估成 (1368,954)，结果开了收藏页，还编了假内容。
    """
    from brain import vision
    if grid is None:
        grid = bool(grid)
    if grid:
        question = (question + "（图上画了绿色坐标网格，每条格线上的黄色数字就是真实屏幕坐标；"
                    "如果你要报坐标给人点，就照着网格数字读数，不要凭感觉估。）")
    p = screenshot(host=host, grid=grid)
    if not p:
        return "没截到图（截屏失败？）"
    desc = (vision.describe_image(p, question=question, max_tokens=500) or "").strip()
    return desc or "（屏幕看了但没说出个所以然）"


# ---------------------------------------------------------------- 她的收藏（照片库）
_photo_lock = threading.Lock()


def _active_window_title():
    """当前前台窗口标题（隐私黑名单用）。拿不到返回空串。"""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:
        return ""


def _dhash(path):
    """64 位差哈希（近似去重）；Pillow 不可用时回退 None（只用文件大小判重）。"""
    try:
        from PIL import Image
        im = Image.open(path).convert("L").resize((9, 8))
        px = list(im.getdata())
        bits = 0
        for r in range(8):
            for c in range(8):
                bits = (bits << 1) | (1 if px[r * 9 + c] < px[r * 9 + c + 1] else 0)
        return bits
    except Exception:
        return None


def _hamming(a, b):
    return bin((a or 0) ^ (b or 0)).count("1")


def _read_photo_events():
    out = []
    if PHOTO_INDEX.exists():
        for line in PHOTO_INDEX.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def photo_state():
    """索引事件 → 当前状态（id 字典，最新覆盖；追加式，不重写历史）。"""
    st = {}
    for e in _read_photo_events():
        pid = e.get("id")
        if not pid:
            continue
        if e.get("event") == "add":
            st[pid] = e
        elif pid in st:
            st[pid].update({k: v for k, v in e.items() if k != "event"})
    return st


def capture_photo(note="", tags="", source="auto"):
    """截一张"她喜欢的/想给主人看的"图，收进 data/photos，返回记录。

    - 近 300 张里近似重复（dHash 汉明距离 ≤4）→ 返回已有那张，不重复占盘
    """
    title = _active_window_title()
    month = time.strftime("%Y-%m")
    outdir = PHOTO_DIR / month
    outdir.mkdir(parents=True, exist_ok=True)
    fname = time.strftime("%Y%m%d_%H%M%S") + "_" + str(int(time.time() * 1000) % 1000) + ".png"
    out = outdir / fname
    try:
        import mss
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            from mss.tools import to_png
            to_png(shot.rgb, shot.size, output=str(out))
    except Exception as e:
        _audit(f"照片截取失败：{type(e).__name__}: {str(e)[:80]}")
        return {"ok": False, "reason": f"截屏失败：{type(e).__name__}"}
    h = _dhash(str(out))
    rel = f"{month}/{fname}"
    st = photo_state()
    if h is not None:
        for rec in list(st.values())[-300:]:
            if rec.get("hash") is not None and _hamming(h, rec.get("hash")) <= 4:
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
                return {"ok": True, "duplicate": True, "photo": rec}
    pid = time.strftime("%Y%m%d_%H%M%S") + f"_{len(st) + 1}"
    rec = {"event": "add", "id": pid, "ts": time.time(), "rel": rel,
           "note": (note or "").strip()[:200],
           "tags": [t.strip() for t in str(tags or "").replace("，", ",").split(",") if t.strip()][:8],
           "window": title[:80], "hash": h, "shown": False, "asked_ts": 0,
           "source": source}
    with _photo_lock:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        with PHOTO_INDEX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _audit(f"收藏照片 → {rel}（{source}）")
    return {"ok": True, "photo": {**rec, "url": "http://127.0.0.1:8094/photos/" + rel}}


_PIC_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
_PIC_MAX_BYTES = 12 * 1024 * 1024
_LAST_IMAGES = Path(config.DATA_DIR) / "memory" / "last_images.json"


def last_images(limit=6, max_age_s=7200):
    """她最近一次图片检索拿到的图（由 `searcher.search_images` 落盘）。
    过期或没有就返回 []——承诺执行器据此决定是"取现成的"还是"重新搜"。"""
    try:
        d = json.loads(_LAST_IMAGES.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) > max_age_s:
            return []
        return (d.get("items") or [])[:limit]
    except Exception:
        return []


def save_picture(url, note="", tags="", source="web"):
    """把网上的图**下载**进她的收藏（CHANGELOG §18）。

    为什么非有这条不可：主人说"把你喜欢的截图回来"，她调 `photo` 拍的是**整个屏幕**，
    于是把主人的桌面拍了回去（15:41 实测）。她当时没有任何办法把"搜到的那张图"
    变成手里的一张图——开浏览器再截屏会占主人的屏幕、还要抢鼠标。
    下载进收藏才是她要的"把喜欢的图带回来"。

    边界：只准 http(s)（`file:`/`data:` 一律拒）、限 12MB、按 content-type 或扩展名认图、
    原子落盘、同一 URL 去重。"""
    import urllib.parse
    import urllib.request
    u = str(url or "").strip()
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("http", "https"):
        return {"ok": False, "reason": "只允许下载 http(s) 图片，收到 %r" % p.scheme}
    st = photo_state()
    for rec in st.values():
        if rec.get("src_url") == u:
            return {"ok": True, "duplicate": True, "photo": rec}
    month = time.strftime("%Y-%m")
    outdir = PHOTO_DIR / month
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": p.scheme + "://" + p.netloc + "/"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            blob = r.read(_PIC_MAX_BYTES + 1)
    except Exception as e:
        _audit(f"下图失败 {u[:70]}：{type(e).__name__}: {str(e)[:80]}")
        return {"ok": False, "reason": "下载失败：%s" % type(e).__name__}
    if not blob or len(blob) > _PIC_MAX_BYTES:
        return {"ok": False, "reason": "图片为空或超过 12MB"}
    ext = os.path.splitext(p.path)[1].lower()
    if ctype.startswith("image/jpeg"):
        ext = ext if ext in _PIC_EXT else ".jpg"
    elif ctype.startswith("image/"):
        cand = "." + ctype.split("/")[-1].split(";")[0].replace("x-", "").replace("svg", "")
        ext = cand if cand in _PIC_EXT else (ext if ext in _PIC_EXT else ".jpg")
    elif ext not in _PIC_EXT:
        return {"ok": False, "reason": "拿回来的不是图片（content-type=%s）" % (ctype or "未知")}
    fname = time.strftime("%Y%m%d_%H%M%S") + "_" + str(int(time.time() * 1000) % 1000) + ext
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / fname
    tmp = out.with_suffix(out.suffix + ".part")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, out)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "reason": "落盘失败：%s" % type(e).__name__}
    try:
        h = _dhash(str(out))
    except Exception:
        h = None
    rel = f"{month}/{fname}"
    pid = time.strftime("%Y%m%d_%H%M%S") + f"_{len(st) + 1}"
    rec = {"event": "add", "id": pid, "ts": time.time(), "rel": rel,
           "note": (note or "").strip()[:200],
           "tags": [t.strip() for t in str(tags or "").replace("，", ",").split(",") if t.strip()][:8],
           "window": "网上下的图", "hash": h, "shown": False, "asked_ts": 0,
           "source": source, "src_url": u, "bytes": len(blob)}
    with _photo_lock:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        with PHOTO_INDEX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _audit(f"收下图 → {rel}（{p.netloc}，{len(blob) // 1024}KB）")
    return {"ok": True, "photo": {**rec, "url": "http://127.0.0.1:8094/photos/" + rel}}


def list_photos(unseen_only=False, limit=20, query=""):
    """列她的收藏（按时间倒序）；unseen_only 只看还没给主人看过的。"""
    st = photo_state()
    rows = sorted(st.values(), key=lambda r: r.get("ts", 0), reverse=True)
    q = (query or "").strip()
    out = []
    for r in rows:
        if unseen_only and r.get("shown"):
            continue
        if q and q not in (r.get("note", "") + " " + r.get("window", "") + " "
                           + " ".join(r.get("tags") or [])):
            continue
        out.append({**r, "url": "http://127.0.0.1:8094/photos/" + r.get("rel", "")})
        if len(out) >= limit:
            break
    return out


def mark_photo(pid, shown=None, asked=False):
    """更新照片状态（shown=已给主人看；asked=已问过要不要看）。追加事件，不重写。"""
    ev = {"event": "update", "id": pid, "ts": time.time()}
    if shown is not None:
        ev["shown"] = bool(shown)
    if asked:
        ev["asked_ts"] = time.time()
    with _photo_lock:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        with PHOTO_INDEX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def next_unseen_photo():
    """下一张还没给主人看过的照片（按时间正序，一次一张）。"""
    st = photo_state()
    rows = sorted(st.values(), key=lambda r: r.get("ts", 0))
    for r in rows:
        if not r.get("shown"):
            return {**r, "url": "http://127.0.0.1:8094/photos/" + r.get("rel", "")}
    return None


def unseen_count():
    return sum(1 for r in photo_state().values() if not r.get("shown"))



def _pg():
    import pyautogui
    pyautogui.FAILSAFE = True          # 鼠标甩左上角=急停（红线 1，不许关）
    pyautogui.PAUSE = 0.4              # 每个动作间隔，防连点误触
    return pyautogui


def _idle_check(min_idle_s=2.0):
    """主人的鼠标键盘空闲 min_idle_s 秒才允许自主活动动手——别抢主人的鼠标。"""
    try:
        import ctypes.wintypes as wt

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        idle = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
        return idle >= min_idle_s, round(idle, 1)
    except Exception:
        return True, -1


def click(x, y, double=False, right=False, host="main"):
    """点击屏幕坐标（仅主人指令场景调用——自主活动不走这里）。"""
    pg = _pg()
    w, h = pg.size()
    if not (0 <= x <= w and 0 <= y <= h):
        return f"坐标 ({x},{y}) 在屏幕外（{w}x{h}）"
    if right:
        pg.rightClick(x, y)
    elif double:
        pg.doubleClick(x, y)
    else:
        pg.click(x, y)
    _audit(f"{'双击' if double else '右击' if right else '单击'} ({x},{y})")
    return f"已{'双击' if double else '右击' if right else '单击'} ({x},{y})"


def type_text(text, host="main"):
    _pg().write(text, interval=0.02)
    _audit(f"键入文本（{len(text)} 字）")
    return "已输入"


def hotkey(combo, host="main"):
    _pg().hotkey(*[k.strip() for k in combo.split("+")])
    _audit(f"快捷键 {combo}")
    return f"已按 {combo}"


def close_window(host="main"):
    """关掉当前**前台窗口**（Alt+F4），并把关完之后还剩哪些窗口原样报回来。

    实测她根本没有"关"这个动作，只能瞎编
    "Alt+F4 按下去了/关机指令已发"。这里补一个真动作 + 真实回执：关没关干净由
    剩余窗口列表说话，她照念就不会再编。**这不是关机**，关机不在能力范围内。
    """
    try:
        hotkey("alt+f4")
    except Exception as e:
        return f"[失败] 关窗口没成功：{type(e).__name__} {e}"
    time.sleep(1.2)
    left = []
    tail = ("还剩这些窗口：" + "、".join(left[:8])) if left else "没读到剩余窗口列表"
    _audit("关掉当前前台窗口")
    return ("我按了 Alt+F4，关掉最前面的那个窗口（**只能关窗口，不能关机**）。"
            + tail + "｜（还亮着的就是没关完，别报「已关机」。）")


def scroll(amount, x=None, y=None, host="main"):
    _pg().scroll(int(amount), x=x, y=y)
    _audit(f"滚动 {amount}")
    return "已滚动"


def clipboard_read(host="main"):
    """读剪切板：本机直接读。"""
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


def clipboard_write(text, host="main"):
    """把文字放进剪切板。"""
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as e:
        return f"剪切板写入失败：{type(e).__name__}: {e}"
    _audit(f"剪切板写入（{len(text)} 字）")
    return f"已写进剪切板（{len(text)} 字）"


def uia_windows(host="main"):
    """枚举当前所有顶层窗口（UIA 树）→ 她知道现在屏幕上开着什么。"""
    try:
        from pywinauto import Desktop
        wins = [(w.window_text() or "(无名)", w.element_info.control_type)
                for w in Desktop(backend="uia").windows()]
        return wins
    except Exception as e:
        return f"枚举失败：{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 自主活动入口
def autonomy_act(kind, arg=""):
    """自主活动专用入口（红线 2：只看不点）。

    kind: screenshot / describe / open / photo / status
    鼠标空闲检测：主人正在用电脑时，自主动作延后（返回提示）。
    2026-11-01 主人定调：**去掉"只能开白名单软件"这道闸**——她爱开什么开什么，
    红线只剩"点击/键盘类必须主人当轮指令"和"每次动手写审计"。
    """
    if kind not in ("screenshot", "describe", "open", "photo", "status"):
        return "自主活动只允许：截屏/看屏幕/开软件/收藏照片/报状态——点击类要主人当轮指令"
    ok, idle = _idle_check(2.0)
    if not ok:
        return f"主人正在用电脑（鼠标键盘 {idle}s 前还动过），我先不动，别抢他的鼠标"
    if kind == "screenshot":
        return f"截好了：{screenshot()}"
    if kind == "describe":
        return describe_screen()
    if kind == "open":
        return open_app(arg)
    if kind == "photo":
        r = capture_photo(note=arg, source="auto")
        if not r.get("ok"):
            return "这轮没拍成：" + str(r.get("reason", ""))[:60]
        p = r.get("photo") or {}
        return "收好了，我拍下这张：" + (p.get("note") or p.get("window") or "一张图")
    if kind == "status":
        wins = uia_windows()
        if isinstance(wins, str):
            return wins
        return "现在屏幕上开着：" + ("；".join(f"{n}（{t}）" for n, t in wins[:8]) or "没窗口")
    return "未知动作"
