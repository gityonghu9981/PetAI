# -*- coding: utf-8 -*-
"""桌宠AI · 桌面气泡壳（desktop/pet.py）——白猫桌宠，气泡伴聊。

设计（刻意克制，零新依赖）：
- tkinter 透明无边框置顶窗口：tkinter 是 Python 自带，比 WebView/Electron
  轻两个数量级——桌宠常驻，省的就是这个。
- 立绘用 canvas 手绘白猫：没有立绘素材也能跑，动画只有眨眼/耳朵抖/尾巴摆
  三个循环，CPU 占用可忽略（after 定时器，无死循环）。
- 气泡：主动搭话 / 实时状态（data/status.json）/ 守护播报 / 等回复提醒，
  12s 自动收起；气泡可拖动，单击打开聊天页。
- 主动搭话：进程内直接调 app/growth/proactive.maybe_speak（static 台词模式，
  零模型调用——节约本地算力红线）。不依赖 webapp 存活：webapp 挂了
  气泡照常陪着你，只是点开聊天页会打不开。
- 右键菜单：打开聊天（网页 8094）/ 管理面板（网页 8094） / 让她说一句 /
  隐藏气泡 / 隐藏或显示状态气泡 / 退出。
- 实时状态气泡：每 0.9s 读 data/status.json（pipeline 各阶段写），她非空闲
  时显示"她正在：…"+她当前那句话。

运行：python desktop/pet.py（请用自带 tkinter 的系统 Python 跑）
"""
import json
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
except ImportError:
    # venv 的 Python（3.13 最小构建）不带 tkinter——桌宠壳请用系统 Python 跑
    sys.exit("本 Python 没带 tkinter（venv 最小构建常见）。"
             "请用系统 Python 运行：python desktop/pet.py")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from core import config              # noqa: E402
from growth import proactive         # noqa: E402
from channels import waiter          # noqa: E402  等回复机制：她开口 → 登记等待主人回话

TRANS = "#010101"          # 透明键色
W, H = 240, 180            # 画布（气泡时高度按内容自适应）
CHAT_URL = f"http://127.0.0.1:{config.WEB_PORT}/"
POS_FILE = ROOT / "data" / "pet_pos.json"
EVENTS_FILE = ROOT / "data" / "pet_guard_events.jsonl"   # 守护掉线事件（jsonl）
STATUS_FILE = ROOT / "data" / "status.json"              # 实时状态灯（pipeline 写）

# 台词气泡历史（右键"让她说一句"用）
_MOOD_LINES = None


def _lines():
    global _MOOD_LINES
    if _MOOD_LINES is None:
        f = config.APP_DIR / "prompts" / "proactive_lines.json"
        try:
            _MOOD_LINES = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            _MOOD_LINES = {"idle": ["……"]}
    return _MOOD_LINES


def _pick_idle():
    """随机抽一句闲置台词（% len 防越界——默认/加载失败时 idle 可能只有 1 条）。"""
    ls = _lines().get("idle") or ["……"]
    return ls[int(time.time()) % len(ls)]


class Pet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("桌宠·气泡")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANS)
        self.root.config(bg=TRANS)
        self.cv = tk.Canvas(self.root, width=W, height=H, bg=TRANS,
                            highlightthickness=0)
        self.cv.pack()
        self._restore_pos()

        # ---- 状态 ----
        self.bubble_until = 0            # >当前时间 = 气泡在显示
        self.bubble_text = ""
        self.blink_until = 0
        self.next_blink = time.time() + 3
        self.ear_flip = 0
        self.next_ear = time.time() + 5
        self.tail_phase = 0
        self.tail_until = 0
        self.drag_off = None
        self.moved = False
        self.status_hide = False         # 实时状态气泡开关（右键切换）

        self._ev_off = 0                 # 守护事件文件读偏移（须先于 _loop）

        self._menu()
        self._bind()
        self.root.withdraw()          # 没话说时整个窗口收起来，不挡桌面点击
        self._loop()
        self.root.after(5000, self._poll_guard_events)
        self.root.after(65000, self._poll_guard_events)    # 前 3 分钟密集听一次，
        self.root.after(125000, self._poll_guard_events)   # 之后由 _loop 每分钟续约
        self.root.after(185000, self._poll_guard_events)

    # ---------------- 白猫立绘 ----------------
    def _draw_cat(self):
        """canvas 手绘白猫：圆脸/耳朵/眼睛/胡须/尾巴，眨眼与摆尾循环。"""
        cv = self.cv
        cx, cy = W // 2, H // 2 + 14
        # 尾巴（慢摆）
        base = [(cx + 62, cy + 40), (cx + 84, cy + 26)]
        if self.tail_phase:
            base = [(cx + 62, cy + 40), (cx + 86, cy + 46)]
        cv.create_line(base[0][0], base[0][1], base[1][0], base[1][1],
                       fill="#e8e4da", width=7, capstyle="round")
        # 耳朵
        cv.create_polygon(cx - 52, cy - 30, cx - 26, cy - 66, cx - 8, cy - 34,
                          fill="#fffef8", outline="#d8d2c4")
        cv.create_polygon(cx + 52, cy - 30, cx + 26, cy - 66, cx + 8, cy - 34,
                          fill="#fffef8", outline="#d8d2c4")
        # 内耳
        cv.create_polygon(cx - 42, cy - 32, cx - 27, cy - 56, cx - 16, cy - 35,
                          fill="#f6c6c0", outline="")
        cv.create_polygon(cx + 42, cy - 32, cx + 27, cy - 56, cx + 16, cy - 35,
                          fill="#f6c6c0", outline="")
        # 脸
        cv.create_oval(cx - 55, cy - 42, cx + 55, cy + 52,
                       fill="#fffef8", outline="#d8d2c4", width=2)
        # 眼睛（眨眼=一条线）
        if time.time() < self.blink_until:
            cv.create_line(cx - 24, cy - 6, cx - 12, cy - 6, fill="#5a5248", width=3)
            cv.create_line(cx + 12, cy - 6, cx + 24, cy - 6, fill="#5a5248", width=3)
        else:
            cv.create_oval(cx - 25, cy - 10, cx - 11, cy + 2, fill="#5a5248")
            cv.create_oval(cx + 11, cy - 10, cx + 25, cy + 2, fill="#5a5248")
            cv.create_oval(cx - 22, cy - 7, cx - 17, cy - 3, fill="#fffef8")
            cv.create_oval(cx + 14, cy - 7, cx + 19, cy - 3, fill="#fffef8")
        # 鼻与嘴
        cv.create_polygon(cx - 6, cy + 8, cx + 6, cy + 8, cx, cy + 15,
                          fill="#f6c6c0", outline="")
        cv.create_line(cx, cy + 15, cx - 10, cy + 26, fill="#8a8070", width=1)
        cv.create_line(cx, cy + 15, cx + 10, cy + 26, fill="#8a8070", width=1)
        cv.create_line(cx, cy + 15, cx, cy + 20, fill="#8a8070", width=1)
        # 胡须
        for dx, sx in ((1, 1), (1, -1)):
            for dy in (-6, 0, 6):
                cv.create_line(cx + 52 * dx, cy + 6 + dy,
                               cx + 52 * dx + 20 * sx, cy + 2 + dy,
                               fill="#c8c0b2", width=1)
        # 前爪
        cv.create_oval(cx - 22, cy + 48, cx - 6, cy + 60, fill="#fffef8",
                       outline="#d8d2c4")
        cv.create_oval(cx + 6, cy + 48, cx + 22, cy + 60, fill="#fffef8",
                       outline="#d8d2c4")

    # ---------------- 气泡 ----------------
    def say(self, text, seconds=12):
        self.bubble_text = text
        self.bubble_until = time.time() + seconds
        self._redraw()

    def _draw_bubble(self, text):
        cv = self.cv
        lines = self._wrap(text, 18)
        w = min(W - 8, max(178, max(len(l) for l in lines) * 14 + 26))
        h = 20 * len(lines) + 22
        x1, y1 = (W - w) // 2, 4
        x2, y2 = x1 + w, y1 + h
        cx = W // 2
        cv.create_polygon(cx - 8, y2 - 1, cx + 8, y2 - 1, cx, y2 + 9,
                          fill="#fffef8", outline="#e2dccc")
        cv.create_rectangle(x1, y1, x2, y2, fill="#fffef8", outline="#e2dccc",
                            width=1)
        cv.create_rectangle(x1 + 1, y2 - 4, x2 - 1, y2 - 1, fill="#fffef8",
                            outline="")
        cv.create_text((x1 + x2) // 2, (y1 + y2) // 2 - 1, text="\n".join(lines),
                       justify="center", fill="#5a5248",
                       font=("Microsoft YaHei UI", 10))
        self._bubble_h = h + 14
        self._place_bubble(self._bubble_h)

    def _place_bubble(self, h):
        """气泡贴窗口当前位置；没摆过就右下角。"""
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        except Exception:
            x, y = sw - W - 40, sh - h - 90
        x = max(0, min(x, sw - W))
        y = max(0, min(y, sh - h))
        self.cv.config(width=W, height=h)
        self.root.geometry(f"{W}x{h}+{int(x)}+{int(y)}")
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

    @staticmethod
    def _wrap(text, width=16):
        """中文按字符折行（气泡宽度受限）。"""
        out, cur = [], ""
        for ch in text:
            cur += ch
            if len(cur) >= width:
                out.append(cur)
                cur = ""
        if cur:
            out.append(cur)
        return out or ["…"]

    # ---------------- 实时状态气泡 ----------------
    def _poll_status(self):
        """读 data/status.json（pipeline 各阶段写、原子替换），她非空闲时
        显示"她正在：…"+她当前那句话。轮询 0.9s 一次，文件几十字节，开销可忽略。
        status_hide=True（右键关闭）或 idle 时不显示。

        追问规则：**等回复的是那件事，不是她**——有提议挂起时
        （data/waiting.json 有等待帧），气泡末尾叠加一行
        "有件事在等主人回话"，她的状态灯照常显示，两不覆盖。"""
        try:
            pend = None
            try:
                pend = waiter.is_waiting()
            except Exception:
                pass
            # 她在等回话时主动弹气泡催：挂起超过 90s 气泡催一次，
            # 之后每 90s 一次、最多 3 次。
            if pend and not self.status_hide:
                ts = float(pend.get("ts") or 0)
                waited = time.time() - ts
                if waited > 90:
                    n = getattr(self, "_nudge_cnt", 0)
                    last_n = getattr(self, "_nudge_ts", 0)
                    if n < 3 and time.time() - last_n > 90:
                        self._nudge_cnt = n + 1
                        self._nudge_ts = time.time()
                        txt = (pend.get("text") or "")[:60]
                        self.say("主人～我刚才说的那个事还在等你回话呢："
                                 + (txt + "…" if txt else "要不要看看呀"),
                                 seconds=12)
            if not STATUS_FILE.exists():
                if pend and not self.status_hide:
                    self.say("有件事在等主人回话（她玩自己的呢）", seconds=14)
                return
            d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        now = time.time()
        st = (d or {}).get("state", "idle")
        line = (d.get("line") or "").strip()
        detail = (d.get("detail") or "").strip()
        # 僵尸状态自愈：pipeline 的收尾（写 idle）不一定跑得到，status.json
        # 会永远停在"思考"。她不可能连续活动 45 秒还一点不更新，过点当发呆。
        _ts = int(d.get("ts") or 0)
        if st in ("thinking", "searching", "wandering", "working", "writing",
                  "talking") and _ts and now - _ts > 45:
            st, line, detail = "idle", "我在发呆", ""
        # 非空闲 → 气泡显示她的真实状态（连发呆也显示）
        if pend and not getattr(self, "_pend_announced", False):
            self._pend_announced = True
            txt = (pend.get("text") or "有件事想跟主人说") + "\n（5 分钟没回应就再提醒你）"
            self.say(txt, seconds=14)
        elif not pend and getattr(self, "_pend_announced", False):
            self._pend_announced = False        # 帧清了，下次出现再提醒
        if self.status_hide or st in ("", "error"):
            return
        if st != "idle":
            body = line or detail
            if body:
                self.say(f"她正在：{body}", seconds=10)

    # ---------------- 守护事件播报（故障可见：摔了跤要说） ----------------
    def _poll_guard_events(self):
        """读守护事件 jsonl 的增量，气泡播报。事件由 pet_guard.ps1 写入，
        桌宠只读不写；文件不存在=守护没出过事，正常。"""
        try:
            if not EVENTS_FILE.exists():
                return
            with EVENTS_FILE.open("r", encoding="utf-8") as f:
                f.seek(getattr(self, "_ev_off", 0))
                news = [ln.strip() for ln in f if ln.strip()]
                self._ev_off = f.tell()
            if news:
                last = json.loads(news[-1])
                msg = ("刚才摔了一跤，自己爬起来了～（" + str(last.get("what", ""))
                       + "）现在一切正常。")
                self.say(msg)
        except Exception:
            pass

    # ---------------- 主动搭话 ----------------
    def _tick_proactive(self):
        """进程内直调 proactive（static 台词，零模型调用）。
        注意：不碰 webapp 的 tick 端点——那是网页 UI 的消费者，
        两边各自的冷却互不干扰（同一份台词包，不会对不上脾气）。"""
        try:
            r = proactive.maybe_speak(idle_seconds=999, mode="static")
            if r and r.get("line"):
                self.say(r["line"])
                # 等回复机制：主动开口=表达 → 登记等待主人回话
                # （状态灯 waiting_master、本地气泡升级、审计留痕都在 waiter 里）
                waiter.express(r["line"], session_id="default", channel="bubble")
        except Exception:
            pass

    def _say_and_wait(self, line):
        """右键"让她说一句"：说话 + 登记等待（与主动搭话同一套等回复纪律）。"""
        self.say(line)
        try:
            waiter.express(line, session_id="default", channel="bubble")
        except Exception:
            pass

    # ---------------- 动画主循环 ----------------
    def _loop(self):
        now = time.time()
        if now >= self.next_blink:              # 眨眼
            self.blink_until = now + 0.15
            self.next_blink = now + 2.5 + (now % 3)
        if now >= self.next_ear:                # 耳朵抖
            self.ear_flip = 1 - self.ear_flip
            self.next_ear = now + (0.2 if self.ear_flip else 4 + now % 4)
        if now >= self.tail_until:              # 尾巴慢摆
            self.tail_phase = (self.tail_phase + 1) % 2
            self.tail_until = now + 1.6
        if now >= getattr(self, "_next_status", 0):   # 实时状态（0.9s 一次：
            self._next_status = now + 0.9            # 她的活动常常只持续 1~2 秒，
            self._poll_status()                      # 1.5s 会整段错过）
        self._redraw()                    # bubble_until 过期后 _redraw 自然不再画
        self.root.after(200, self._loop)
        if not hasattr(self, "_next_ev") or time.time() >= self._next_ev:
            self._next_ev = time.time() + 60
            self.root.after(10, self._poll_guard_events)   # 每分钟听一次守护事件

    def _redraw(self):
        """有话说就画气泡并显示；没话说显示白猫（或收起窗口）。"""
        self.cv.delete("all")
        if time.time() < self.bubble_until:
            self._draw_bubble(self.bubble_text)
            return
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
        except Exception:
            pass
        self._draw_cat()

    # ---------------- 交互 ----------------
    def _bind(self):
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Button-3>", self._popup)

    def _press(self, e):
        self.drag_off = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.moved = False

    def _drag(self, e):
        """按住拖动：气泡窗口跟着鼠标走。"""
        if self.drag_off:
            dx = e.x_root - self.drag_off[0]
            dy = e.y_root - self.drag_off[1]
            self.root.geometry(f"+{self.drag_off[2] + dx}+{self.drag_off[3] + dy}")
            self.moved = True

    def _release(self, e):
        if self.drag_off and not self.moved:
            self._open_chat()                   # 单击 = 打开聊天页
        else:
            self._save_pos()
        self.drag_off = None

    def _open_chat(self):
        """单击气泡 → 打开聊天页（网页 8094）。"""
        try:
            webbrowser.open(CHAT_URL)
        except Exception:
            pass

    def _open_panel(self):
        """右键"管理面板"：完整网页 UI（记忆/技能/审计/状态都在那儿）。"""
        webbrowser.open(CHAT_URL)

    def _save_pos(self):
        try:
            POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            POS_FILE.write_text(json.dumps(
                {"x": self.root.winfo_x(), "y": self.root.winfo_y()}),
                encoding="utf-8")
        except Exception:
            pass

    def _restore_pos(self):
        try:
            d = json.loads(POS_FILE.read_text(encoding="utf-8"))
            self.root.geometry(f"+{int(d['x'])}+{int(d['y'])}")
        except Exception:
            self.root.geometry(f"+{1200}+{600}")

    def _menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="打开聊天", command=self._open_chat)
        m.add_command(label="管理面板（网页）", command=self._open_panel)
        m.add_command(label="让她说一句",
                      command=lambda: self._say_and_wait(_pick_idle()))
        m.add_command(label="隐藏气泡", command=lambda: setattr(self, "bubble_until", 0))
        m.add_command(label="隐藏/显示状态气泡",
                      command=lambda: setattr(self, "status_hide",
                                              not getattr(self, "status_hide", False)))
        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        self._popup_menu = m

    def _popup(self, e):
        try:
            self._popup_menu.tk_popup(e.x_root, e.y_root)
        finally:
            self._popup_menu.grab_release()


if __name__ == "__main__":
    pet = Pet()
    # 开场白：点开气泡窗，右下角先招呼一声
    pet.root.after(600, lambda: pet.say("我在这儿～点我跟我聊天", seconds=14))
    # 主动搭话：全天循环，说不说由 proactive 的场合判断定
    def _proactive_loop():
        try:
            pet._tick_proactive()
        except Exception:
            pass
        gap = max(60, int(config.PET_PROACTIVE_INTERVAL_S)) * 1000
        pet.root.after(gap, _proactive_loop)
    pet.root.after(90000, _proactive_loop)
    pet.root.mainloop()
