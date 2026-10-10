# -*- coding: utf-8 -*-
"""
gh-latency —— GitHub 延迟监视器（tkinter 深色，单文件，纯标准库）

架构（v3：数据驱动 + 渐进上屏 + 自适应双速探测）：
  PROBE_PLAN          声明式目标清单
  全量模式(>=5s)      每目标所有 DNS IP 并发探测，选出最优 IP（端口压力 ~5.6 连接/s）
  快速模式(<5s)       只测上一轮各目标的最优 IP（1s 间隔 = 7 连接/s，端口安全）
                      每 30s 自动插入一次全量扫描纠偏
  渐进上屏            结果一行到达即刷新对应行/历史/走势

安全依据（检索实测换算）：
  Windows 动态端口 49152-65535 共 16384 个，TIME_WAIT 默认 240s
  → 无耗尽安全线 ≈ 16384/240 ≈ 68 连接/秒
  → 快速模式 7 连接/s（占 10%），全量 5.6 连接/s（占 8%），均远低于红线
"""
from __future__ import annotations
import ctypes
import os
import re
import socket
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import ttk
import concurrent.futures as cf

try:  # 必须在 import tkinter 之前：声明 DPI 感知（高分屏位图拉伸→模糊+拖动卡顿）
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
import tkinter as tk
import winreg
from tkinter import ttk

SCALE = 1.0
try:
    SCALE = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 96.0
except Exception:
    pass

TITLE = "GitHub 延迟监视器 · gh-latency"
TIMEOUT_FULL = 2.5      # 全量模式单 IP 超时
TIMEOUT_FAST = 1.0      # 快速模式超时（只测已知最优 IP）
FAST_BELOW = 5          # 间隔小于该秒数 → 快速模式
FULL_EVERY = 30         # 快速模式下每隔多少秒强制全量扫描
HISTORY_MAX = 180
CONN_BUDGET_SAFE = 68   # 16384 端口 / 240s TIME_WAIT（检索换算）

SERIES_COLOR = {"github.com": "#4caf7d", "api.github.com": "#5b8dd9",
                "raw.githubusercontent.com": "#e2b45a",
                "codeload.github.com": "#af9eec",
                "avatars.githubusercontent.com": "#ed93b1",
                "ssh.github.com:443": "#5dcaa5"}

BG, PANEL, PANEL2 = "#1e1f24", "#26272e", "#2c2d35"
FG, MUT, BORDER = "#e8e8ea", "#9a9aa3", "#3a3b44"
GREEN, AMBER, RED, BLUE = "#4caf7d", "#e2b45a", "#e26d5a", "#5b8dd9"

PROBE_PLAN = [
    ("直连", "github.com"),
    ("直连", "api.github.com"),
    ("直连", "raw.githubusercontent.com"),
    ("直连", "codeload.github.com"),
    ("直连", "avatars.githubusercontent.com"),
    ("SSH 通道", "ssh.github.com:443"),
    ("基线", "www.baidu.com"),
]
ROW_ORDER = {t: i for i, (_, t) in enumerate(PROBE_PLAN)}


# ---------------------------------------------------------------- 探测原语
def tcp_latency(ip, port=443, timeout=TIMEOUT_FULL):
    try:
        t0 = time.time()
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return round((time.time() - t0) * 1000, 1)
    except Exception:
        return None


def resolve_ips(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return sorted({i[4][0] for i in infos})
    except Exception:
        return []


def proxy_port():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        sv = winreg.QueryValueEx(k, "ProxyServer")[0]
        if winreg.QueryValueEx(k, "ProxyEnable")[0]:
            m = re.search(r"127\.0\.0\.1:(\d+)", sv)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def via_proxy_latency(port, host, timeout=TIMEOUT_FULL):
    try:
        t0 = time.time()
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.settimeout(timeout)
        s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = s.recv(1)
            if not c:
                return None
            head += c
        s.close()
        if b" 200" not in head.split(b"\r\n")[0]:
            return None
        return round((time.time() - t0) * 1000, 1)
    except Exception:
        return None


# ---------------------------------------------------------------- 轮次构建
def build_jobs(port, known_best, fast):
    """返回 jobs=[(cat,tgt,fn)]。fast=True 只测已知最优 IP（连接数=目标数）。"""
    jobs = []
    for cat, tgt in PROBE_PLAN:
        if fast:
            ip = known_best.get(tgt)
            if not ip:
                continue
            to = TIMEOUT_FAST
            jobs.append((cat, tgt, lambda ip=ip, to=to: (tcp_latency(ip, timeout=to), ip)))
            continue
        if tgt == "ssh.github.com:443":
            jobs.append((cat, tgt, lambda: (tcp_latency("ssh.github.com", 443),
                                            "ssh.github.com")))
            continue
        ips = resolve_ips(tgt) or [None]
        for ip in ips:
            if ip is None:
                jobs.append((cat, tgt, lambda: (None, None)))
            else:
                jobs.append((cat, tgt,
                             lambda ip=ip: (tcp_latency(ip), ip)))
    if port:
        for cat, tgt in PROBE_PLAN:
            if cat == "直连":
                jobs.append(("经代理加速", tgt,
                             lambda t=tgt: (via_proxy_latency(port, t), f"proxy:{t}")))
    return jobs


def run_round(port, known_best, fast, on_result=None):
    """并发跑一轮。返回 (rows, best_ips, conn_count)。
    rows=[(cat,tgt,ms,note)]；best_ips={tgt: 最优IP}；conn_count=本轮连接数"""
    jobs = build_jobs(port, known_best, fast)
    agg = {}
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(fn): (cat, tgt) for cat, tgt, fn in jobs}
        done, total = 0, len(futs)
        for fu in cf.as_completed(futs):
            cat, tgt = futs[fu]
            done += 1
            try:
                ms, ip = fu.result()
            except Exception:
                ms, ip = None, None
            a = agg.setdefault((cat, tgt), {"best": None, "ip": None,
                                            "total": 0, "dead": 0})
            if ip and not str(ip).startswith("proxy:"):
                a["total"] += 1
                if ms is None:
                    a["dead"] += 1
                elif a["best"] is None or ms < a["best"]:
                    a["best"] = ms
                    a["ip"] = ip
            else:
                if ms is not None and (a["best"] is None or ms < a["best"]):
                    a["best"] = ms
            if on_result:
                on_result(cat, tgt, a["best"], ip, done, total, fast)
    rows = []
    best_ips = {}
    for (cat, tgt), a in agg.items():
        if cat == "基线":
            note = "本地网络正常" if a["best"] is not None else "本机断网?"
        elif a["best"] is None:
            note = "超时"
        elif a["dead"]:
            note = f"{a['total'] - a['dead']}/{a['total']} IP 活"
        elif fast:
            note = "快速探测"
        else:
            note = "通"
        if a["ip"]:
            best_ips[tgt] = a["ip"]
        rows.append((cat, tgt, a["best"], note))
    rows.sort(key=lambda r: ROW_ORDER.get(r[1], 99))
    return rows, best_ips, total


# ---------------------------------------------------------------- UI
class App:
    def __init__(self, root):
        self.root = root
        root.title(TITLE)
        root.geometry(f"{int(720*SCALE)}x{int(660*SCALE)}")
        root.minsize(int(640*SCALE), int(600*SCALE))
        root.configure(bg=BG)
        self.known_best = {}     # target -> 最优 IP（全量轮更新）
        self.last_full = 0.0
        self._build_style()
        self._build_ui()
        self.running = False
        self._task = None
        self.refresh()
        self.schedule()

    def _build_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                    bordercolor=BORDER, font=("Microsoft YaHei UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Muted.TLabel", foreground=MUT)
        s.configure("TButton", background=PANEL2, foreground=FG,
                    bordercolor=BORDER, padding=(14, 6))
        s.map("TButton", background=[("active", "#3a3b45")])
        s.configure("Accent.TButton", background="#2e5c46", foreground="#dff5ea")
        s.map("Accent.TButton", background=[("active", "#376e54")])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=int(30*SCALE), bordercolor=BORDER)
        s.configure("Treeview.Heading", background=PANEL2, foreground=MUT,
                    relief="flat", font=("Microsoft YaHei UI", 9))
        s.map("Treeview", background=[("selected", "#35414d")])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                    foreground=FG, arrowcolor=FG, bordercolor=BORDER,
                    lightcolor=PANEL2, darkcolor=PANEL2)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL2)],
              foreground=[("readonly", FG)])
        for opt, val in (("*TCombobox*Listbox.background", PANEL2),
                         ("*TCombobox*Listbox.foreground", FG),
                         ("*TCombobox*Listbox.selectBackground", "#35414d"),
                         ("*TCombobox*Listbox.selectForeground", FG)):
            self.root.option_add(opt, val)
        self.root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))

    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(top, text="GitHub 延迟监视器",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        self.auto_var = tk.BooleanVar(value=True)
        self.interval = tk.StringVar(value="1s")
        ttk.Label(top, text="间隔(秒,可输入)", style="Muted.TLabel").pack(side="right")
        box = ttk.Combobox(top, textvariable=self.interval, width=8,
                           values=("1s", "5s", "10s", "30s", "60s"), state="normal")
        box.pack(side="right", padx=(4, 0))
        box.bind("<Return>", lambda e: self.schedule())
        box.bind("<<ComboboxSelected>>", lambda e: self.schedule())
        ttk.Checkbutton(top, text="自动刷新",
                        variable=self.auto_var).pack(side="right", padx=(0, 8))

        self.table = ttk.Treeview(self.root, columns=("cat", "tgt", "ms", "st"),
                                  show="headings", height=7)
        for col, w, txt in (("cat", 110, "通道"), ("tgt", 260, "目标"),
                            ("ms", 110, "延迟"), ("st", 180, "说明")):
            self.table.heading(col, text=txt)
            self.table.column(col, width=w, anchor="w")
        self.table.pack(fill="x", padx=16, pady=6)
        for tag, c in (("fast", GREEN), ("mid", AMBER), ("slow", BLUE),
                       ("dead", RED)):
            self.table.tag_configure(tag, foreground=c)
        self.table.bind("<<TreeviewSelect>>", self.on_select)
        self.iids = {}

        self.history = {}
        self.chart_target = "github.com"
        ttk.Label(self.root, text="延迟走势（点击表格行切换目标）",
                  style="Muted.TLabel").pack(fill="x", padx=16)
        self.chart = tk.Canvas(self.root, bg=PANEL, height=150,
                               highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=16, pady=(2, 4))
        self.chart.bind("<Configure>", lambda e: self.draw_chart())

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=16, pady=(0, 12))
        self.btn = ttk.Button(bar, text="刷新", style="Accent.TButton",
                              command=self.refresh)
        self.btn.pack(side="left")
        self.status = ttk.Label(bar, text="就绪", style="Muted.TLabel")
        self.status.pack(side="left", padx=12)

    # ---- 调度
    def parse_interval(self):
        raw = self.interval.get().strip().rstrip("sS秒")
        try:
            v = int(float(raw))
        except ValueError:
            return None
        return max(1, min(3600, v))

    def schedule(self):
        if self._task:
            self.root.after_cancel(self._task)
        iv = self.parse_interval()
        if iv is None:
            self.status.configure(text="间隔需为 1-3600 的数字")
            return
        self.interval.set(f"{iv}s")
        if self.auto_var.get():
            self._task = self.root.after(iv * 1000, self._auto)

    def _auto(self):
        if not self.running:
            self.refresh()
        self.schedule()

    def refresh(self):
        if self.running:
            return
        iv = self.parse_interval() or 1
        fast = iv < FAST_BELOW and bool(self.known_best)
        full_due = (time.time() - self.last_full) >= FULL_EVERY
        fast = fast and not full_due
        self.running = True
        self.btn.configure(state="disabled")
        mode = "快速" if fast else "全量"
        self.status.configure(text=f"[{mode}] 测量中 (0/?)……")
        port = proxy_port()
        threading.Thread(target=self._work, args=(port, fast), daemon=True).start()

    def _work(self, port, fast):
        def on_result(cat, tgt, best, ip, done, total, is_fast):
            self.root.after(0, lambda: self._on_row(
                cat, tgt, best, ip, done, total, is_fast))
        rows, best_ips, total = run_round(
            port, self.known_best, fast, on_result=on_result)
        if not fast:
            self.known_best.update(best_ips)
            self.last_full = time.time()
        stamp = time.strftime("%H:%M:%S")
        self.root.after(0, lambda: self._finish(rows, stamp, fast))

    # ---- 渐进更新
    def _on_row(self, cat, tgt, best, ip, done, total, is_fast):
        self.status.configure(text=f"[{'快速' if is_fast else '全量'}] "
                                   f"测量中 ({done}/{total})……")
        if best is not None and ip and not str(ip).startswith("proxy:"):
            self.known_best[tgt] = ip
        self._upsert_row(cat, tgt, best)
        if cat in ("经代理加速", "基线"):
            return
        h = self.history.setdefault(tgt, [])
        h.append((time.time(), best))
        if len(h) > HISTORY_MAX:
            self.history[tgt] = h[-HISTORY_MAX:]
        if tgt == self.chart_target:
            self.draw_chart()

    def _upsert_row(self, cat, tgt, best):
        note = {"基线": ("本地网络正常" if best is not None else "本机断网?")}.get(cat)
        if note is None:
            note = "超时" if best is None else "通"
        if best is None:
            tag, ms_s = "dead", "超时"
        elif best < 300:
            tag, ms_s = "fast", f"{best:.0f} ms"
        elif best < 1000:
            tag, ms_s = "mid", f"{best:.0f} ms"
        else:
            tag, ms_s = "slow", f"{best:.0f} ms"
        if tgt in self.iids and self.table.exists(self.iids[tgt]):
            self.table.item(self.iids[tgt], values=(cat, tgt, ms_s, note),
                            tags=(tag,))
        else:
            self.iids[tgt] = self.table.insert(
                "", "end", values=(cat, tgt, ms_s, note), tags=(tag,))

    def _finish(self, rows, stamp, fast):
        for tgt, iid in list(self.iids.items()):
            if self.table.exists(iid):
                self.table.move(iid, "", ROW_ORDER.get(tgt, 99))
        self.status.configure(text=f"更新于 {stamp}（{'快速' if fast else '全量'}）")
        self.btn.configure(state="normal")
        self.running = False
        self.draw_chart()

    def on_select(self, _event):
        sel = self.table.selection()
        if sel:
            tgt = self.table.item(sel[0], "values")[1]
            if tgt in self.history:
                self.chart_target = tgt
                self.draw_chart()

    def draw_chart(self):
        c = self.chart
        c.delete("all")
        w = max(c.winfo_width(), 600)
        h = max(c.winfo_height(), 120)
        ml, mr, mt, mb = 52, 14, 14, 22
        pw, ph = w - ml - mr, h - mt - mb
        series = self.history.get(self.chart_target, [])
        color = SERIES_COLOR.get(self.chart_target, BLUE)
        c.create_text(ml, 8, anchor="nw", fill=color,
                      font=("Microsoft YaHei UI", 10, "bold"),
                      text=f"{self.chart_target} 走势")
        if not series:
            c.create_text(ml + pw / 2, mt + ph / 2, fill=MUT,
                          font=("Microsoft YaHei UI", 10),
                          text="等待数据……")
            return
        vals = [ms for _, ms in series if ms is not None]
        ymax = max(vals + [300]) * 1.15
        c.create_line(ml, mt, ml, mt + ph, fill=BORDER)
        c.create_line(ml, mt + ph, ml + pw, mt + ph, fill=BORDER)
        for frac in (0.5, 1.0):
            y = mt + ph * frac
            c.create_line(ml, y, ml + pw, y, fill="#33343c")
            c.create_text(ml - 4, mt + ph - ph * frac, anchor="e", fill=MUT,
                          font=("Consolas", 8), text=f"{ymax*frac:.0f}")
        pts = series[-120:]
        n = len(pts)
        px = lambda i: ml + (pw * i / max(n - 1, 1))
        py = lambda ms: mt + ph - (min(ms, ymax) / ymax) * ph
        prev = None
        for i, (ts, ms) in enumerate(pts):
            if ms is None:
                c.create_rectangle(px(i) - 2, mt + ph - 4, px(i) + 2, mt + ph,
                                   fill=RED, outline="")
                prev = None
                continue
            if prev is not None:
                c.create_line(prev[0], prev[1], px(i), py(ms),
                              fill=color, width=2)
            prev = (px(i), py(ms))
        last_ms = pts[-1][1]
        lx, ly = px(n - 1), (py(last_ms) if last_ms is not None else mt + ph)
        c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill=color, outline="")
        label = f"{last_ms:.0f} ms" if last_ms is not None else "超时"
        c.create_text(min(lx + 6, w - mr - 60), ly, anchor="w", fill=FG,
                      font=("Consolas", 9, "bold"), text=label)


# ---------------------------------------------------------------- 单实例
_MUTEX = None

def single_instance():
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MUTEX = k32.CreateMutexW(None, False, "gh-latency-single")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, TITLE)
        if hwnd:
            u.ShowWindow(hwnd, 9)
            u.SetForegroundWindow(hwnd)
        return True
    return False


def main():
    try:
        if single_instance():
            return 0
        root = tk.Tk()
        App(root)
        root.mainloop()
        return 0
    except Exception:
        import traceback
        d = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
             else os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(d, "latency-crash.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
