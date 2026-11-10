"""Synology Drive Monitor —— Windows 群晖 Drive 客户端只读同步状态监控（GUI 与命令行入口）。

用法：
    python synology_drive_monitor.py                 # 图形界面
    python synology_drive_monitor.py --json          # 不开窗口，输出一次 JSON 快照
    python synology_drive_monitor.py --json --sample 60   # 采样 60 秒后输出（速率才有意义）
    python synology_drive_monitor.py --json --fail-on stalled,errors   # 命中条件时退出码为 1
    python synology_drive_monitor.py --base D:\\SynologyDrive   # 指定群晖本地数据目录
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, Tk, Toplevel, filedialog, messagebox, ttk

import drive_core as core
from drive_core import APP_TITLE, APP_VERSION, logger

REFRESH_MS = 2000
POLL_MS = 100
TOAST_MS = 3500

# ==============================================================================
# 设计系统：Windows 11 Fluent 浅色 / 深色两套色板
# ==============================================================================
PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#F3F3F3",  # 窗体底色
        "surface": "#FFFFFF",  # 卡片 / 表格
        "hover": "#F5F6F7",
        "border": "#E5E7EB",
        "heading": "#F7F7F8",  # 表头
        "zebra": "#FAFBFC",  # 表格隔行
        "text": "#0F172A",
        "text2": "#5B6676",
        "muted": "#8A94A3",
        "accent": "#0067C0",
        "accent_hover": "#005A9E",
        "accent_bg": "#E5F1FB",
        "on_accent": "#FFFFFF",
        "control": "#FBFBFB",  # 次要按钮底色
        "control_hover": "#EEF0F2",
        "control_border": "#C9CED6",
        "green": "#107C10",
        "blue": "#0067C0",
        "orange": "#B8500A",
        "red": "#C42B1C",
        "gray": "#6B7280",
    },
    "dark": {
        "bg": "#202020",
        "surface": "#2B2B2B",
        "hover": "#343434",
        "border": "#3F3F3F",
        "heading": "#323232",
        "zebra": "#303030",
        "text": "#F3F4F6",
        "text2": "#B6BBC4",
        "muted": "#8B919B",
        "accent": "#4CC2FF",
        "accent_hover": "#7AD3FF",
        "accent_bg": "#143A52",
        "on_accent": "#00243A",
        "control": "#3A3A3A",
        "control_hover": "#454545",
        "control_border": "#5C5C5C",
        "green": "#6CCB5F",
        "blue": "#4CC2FF",
        "orange": "#FCA65C",
        "red": "#FF99A4",
        "gray": "#9CA3AF",
    },
}


class Theme:
    """当前生效的色板。切换主题时整体替换，界面随后重建。"""

    name: str
    bg: str
    surface: str
    hover: str
    border: str
    heading: str
    zebra: str
    text: str
    text2: str
    muted: str
    accent: str
    accent_hover: str
    accent_bg: str
    on_accent: str
    control: str
    control_hover: str
    control_border: str
    green: str
    blue: str
    orange: str
    red: str
    gray: str

    def __init__(self):
        self.apply("light")

    def apply(self, name: str):
        self.name = name if name in PALETTES else "light"
        self.__dict__.update(PALETTES[self.name])


C = Theme()

# 字体在 resolve_fonts() 里按本机实际安装情况确定（Windows 10 没有 Segoe UI Variable）
FONT_TEXT = "Segoe UI"
FONT_DISPLAY = "Segoe UI"
FONT_MONO = "Consolas"


def resolve_fonts(root: tk.Misc):
    global FONT_TEXT, FONT_DISPLAY, FONT_MONO
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return

    def first(candidates: tuple[str, ...], default: str) -> str:
        return next((c for c in candidates if c in families), default)

    FONT_TEXT = first(("Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"), FONT_TEXT)
    FONT_DISPLAY = first(("Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI"), FONT_DISPLAY)
    FONT_MONO = first(("Cascadia Mono", "Consolas", "Courier New"), FONT_MONO)


def font(size: int, bold: bool = False, display: bool = False) -> tuple:
    family = FONT_DISPLAY if display else FONT_TEXT
    return (family, size, "bold") if bold else (family, size)


def blend(color: str, base: str, share: float) -> str:
    """把 color 按 share (0~1) 的比例混入 base，用来生成状态色的浅底。"""
    a = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i : i + 2], 16) for i in (1, 3, 5)]
    return "#{:02X}{:02X}{:02X}".format(*(round(x * share + y * (1 - share)) for x, y in zip(a, b, strict=True)))


def shorten_middle(s: str, limit: int = 64) -> str:
    if len(s) <= limit:
        return s
    keep = (limit - 1) // 2
    return s[:keep] + "…" + s[-keep:]


# ==============================================================================
# Windows 专属：DPI、标题栏深色、系统主题、任务栏闪烁
# ==============================================================================
def enable_dpi_awareness():
    """让 Windows 按真实 DPI 渲染，避免高分屏下界面发虚。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def system_prefers_dark() -> bool:
    """读取 Windows"应用模式"设置；读不到时按浅色处理。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _hwnd(window: tk.Misc) -> int:
    if sys.platform != "win32":
        return 0
    return ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()


def set_titlebar_dark(window: tk.Misc, dark: bool):
    """让系统标题栏跟随深色主题（Windows 10 20H1+ / 11）。失败时保持系统默认。"""
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新 / 旧编号）
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(_hwnd(window), attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass


def flash_taskbar(window: tk.Misc):
    """窗口不在前台时闪烁任务栏按钮，直到用户切回来。"""
    if sys.platform != "win32":
        return

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("hwnd", ctypes.c_void_p),
            ("dwFlags", ctypes.c_uint),
            ("uCount", ctypes.c_uint),
            ("dwTimeout", ctypes.c_uint),
        ]

    try:
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), _hwnd(window), 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except (AttributeError, OSError, tk.TclError):
        pass


# ==============================================================================
# UI 组件
# ==============================================================================
class ToolTip:
    """悬停提示。"""

    def __init__(self, widgets: list[tk.Widget], text: str):
        self.text = text
        self.tip: Toplevel | None = None
        for w in widgets:
            w.bind("<Enter>", self._show, add="+")
            w.bind("<Leave>", self._hide, add="+")
            w.bind("<Destroy>", self._hide, add="+")

    def _show(self, event):
        if self.tip or not self.text:
            return
        self.tip = tw = Toplevel(event.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{event.x_root + 14}+{event.y_root + 16}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            wraplength=340,
            font=font(9),
            bg=C.surface,
            fg=C.text,
            highlightbackground=C.border,
            highlightthickness=1,
            padx=10,
            pady=6,
        ).pack()

    def _hide(self, _event=None):
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class CellTip:
    """表格单元格内容被列宽截断时，悬停显示全文（多条提示按"；"分行）。"""

    def __init__(self, tree: ttk.Treeview):
        self.tree = tree
        self.tip: Toplevel | None = None
        self.key: tuple[str, str] | None = None
        self.measure = tkfont.Font(font=font(9))
        tree.bind("<Motion>", self._motion, add="+")
        tree.bind("<Leave>", self._hide, add="+")
        tree.bind("<Destroy>", self._hide, add="+")
        tree.bind("<MouseWheel>", self._hide, add="+")

    def _motion(self, event):
        row, col = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if (row, col) == self.key:
            return
        self._hide()
        self.key = (row, col)
        if not row or not col.startswith("#"):
            return
        try:
            idx = int(col[1:]) - 1
            text = str(self.tree.item(row, "values")[idx])
            width = int(self.tree.column(col, "width"))
        except (ValueError, IndexError, tk.TclError):
            return
        if not text or self.measure.measure(text) + 16 <= width:
            return  # 没被截断就不打扰
        self.tip = tw = Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{event.x_root + 14}+{event.y_root + 18}")
        tk.Label(
            tw,
            text=text.replace("；", "\n"),
            justify="left",
            wraplength=560,
            font=font(9),
            bg=C.surface,
            fg=C.text,
            highlightbackground=C.border,
            highlightthickness=1,
            padx=10,
            pady=6,
        ).pack()

    def _hide(self, _event=None):
        self.key = None
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


def card_frame(parent, **kwargs) -> tk.Frame:
    return tk.Frame(parent, bg=C.surface, highlightbackground=C.border, highlightthickness=1, **kwargs)


class FluentCard(tk.Frame):
    """指标卡片：标题 / 大数字 / 说明。可点击（on_click）时悬停高亮并显示手型。"""

    def __init__(self, parent, title: str, subtitle: str = "", tooltip: str = "", on_click=None, **kwargs):
        super().__init__(parent, bg=C.surface, highlightbackground=C.border, highlightthickness=1, padx=16, pady=12, **kwargs)
        self.lbl_title = tk.Label(self, text=title, font=font(9, bold=True), fg=C.text2, bg=C.surface)
        self.lbl_title.pack(anchor="w")
        self.val_var = StringVar(value="--")
        self.lbl_val = tk.Label(self, textvariable=self.val_var, font=font(22, bold=True, display=True), fg=C.text, bg=C.surface)
        self.lbl_val.pack(anchor="w", pady=(2, 2))
        self.sub_var = StringVar(value=subtitle)
        self.lbl_sub = tk.Label(self, textvariable=self.sub_var, font=font(9), fg=C.muted, bg=C.surface)
        self.lbl_sub.pack(anchor="w")

        self._parts = [self, self.lbl_title, self.lbl_val, self.lbl_sub]
        for w in self._parts:
            w.bind("<Enter>", lambda e: self._set_bg(C.hover), add="+")
            w.bind("<Leave>", lambda e: self._set_bg(C.surface), add="+")
            if on_click:
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e: on_click(), add="+")
        if tooltip:
            ToolTip(self._parts, tooltip)

    def _set_bg(self, color: str):
        for w in self._parts:
            w.configure(bg=color)

    def set_value(self, val: str, sub: str | None = None, color: str | None = None):
        self.val_var.set(val)
        if sub is not None:
            self.sub_var.set(sub)
        self.lbl_val.configure(fg=color or C.text)


class OutstandingChart(tk.Canvas):
    """在途事件池趋势（面积折线）。窗口尺寸变化时自动重绘。"""

    def __init__(self, parent, scale: float = 1.0, **kwargs):
        super().__init__(parent, bg=C.surface, highlightthickness=0, **kwargs)
        self._scale = scale
        self._samples: list[tuple[float, int]] = []
        self.bind("<Configure>", lambda e: self.draw_data(self._samples))

    def draw_data(self, samples: list[tuple[float, int]]):
        self._samples = samples
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 30:
            return
        if len(samples) < 2:
            self.create_text(w / 2, h / 2, text="正在积累样本…", font=font(8), fill=C.muted)
            return
        values = [s[1] for s in samples]
        max_v, min_v = max(values), min(values)
        span = (max_v - min_v) or 1.0
        left, right, top, bottom = 8, w - 8, 18 * self._scale, h - 6
        dx = (right - left) / (len(values) - 1)
        coords: list[float] = []
        for i, val in enumerate(values):
            coords.extend([left + i * dx, bottom - ((val - min_v) / span) * (bottom - top)])
        self.create_line(left, bottom, right, bottom, fill=C.border)
        self.create_polygon([left, bottom, *coords, coords[-2], bottom], fill=blend(C.accent, C.surface, 0.14), outline="")
        self.create_line(coords, fill=C.accent, width=2, smooth=True)
        lx, ly = coords[-2], coords[-1]
        self.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill=C.accent, outline=C.surface)
        minutes = max(1, round((samples[-1][0] - samples[0][0]) / 60))
        self.create_text(
            left, 2, anchor="nw", font=font(8), fill=C.muted, text=f"最近 {minutes} 分钟 · 最高 {max_v:,} · 最低 {min_v:,}"
        )
        self.create_text(right, 2, anchor="ne", font=font(8, bold=True), fill=C.accent, text=f"当前 {values[-1]:,}")


class ThroughputChart(tk.Canvas):
    """每分钟吞吐：完成 (Done) 的柱子朝上，新增 (Push) 的柱子朝下，一眼看出谁快。"""

    def __init__(self, parent, scale: float = 1.0, **kwargs):
        super().__init__(parent, bg=C.surface, highlightthickness=0, **kwargs)
        self._scale = scale
        self._series: list[tuple[int, int]] = []
        self.bind("<Configure>", lambda e: self.draw_data(self._series))

    def draw_data(self, series: list[tuple[int, int]]):
        self._series = series
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 30 or not series:
            return
        peak = max(max(p, d) for p, d in series)
        left, right, mid = 58 * self._scale, w - 8, h / 2
        self.create_text(4, mid - 3, anchor="sw", font=font(8), fill=C.green, text="完成 ↑")
        self.create_text(4, mid + 3, anchor="nw", font=font(8), fill=C.orange, text="新增 ↓")
        self.create_line(left, mid, right, mid, fill=C.border)
        if peak <= 0:
            self.create_text((left + right) / 2, mid - 9, font=font(8), fill=C.muted, text=f"最近 {len(series)} 分钟没有事件")
            return
        slot = (right - left) / len(series)
        bar = max(2.0, slot * 0.62)
        half = mid - 3
        for i, (push, done) in enumerate(series):
            x0 = left + i * slot + (slot - bar) / 2
            if done:
                self.create_rectangle(x0, mid - 1 - max(1.0, done / peak * half), x0 + bar, mid - 1, fill=C.green, outline="")
            if push:
                self.create_rectangle(x0, mid + 1, x0 + bar, mid + 1 + max(1.0, push / peak * half), fill=C.orange, outline="")
        self.create_text(right, 0, anchor="ne", font=font(7), fill=C.muted, text=f"峰值 {peak:,}/分 · 右侧为最近")


class Chip(tk.Label):
    """开关式筛选标签，绑定一个 BooleanVar。"""

    def __init__(self, parent, text: str, var: tk.BooleanVar, command):
        super().__init__(parent, font=font(9), padx=10, pady=3, cursor="hand2", highlightthickness=1)
        self._text, self._var, self._command = text, var, command
        self.bind("<Button-1>", self._toggle)
        self.render()

    def _toggle(self, _event=None):
        self._var.set(not self._var.get())
        self.render()
        self._command()

    def render(self):
        if self._var.get():
            self.configure(text=f"✓ {self._text}", bg=C.accent_bg, fg=C.accent, highlightbackground=C.accent)
        else:
            self.configure(text=self._text, bg=C.surface, fg=C.text2, highlightbackground=C.border)


def fluent_button(parent, text: str, command, primary: bool = False, small: bool = False, tooltip: str = "") -> tk.Button:
    normal, hover = (C.accent, C.accent_hover) if primary else (C.control, C.control_hover)
    btn = tk.Button(
        parent,
        text=text,
        command=command,
        relief="flat",
        bd=0,
        cursor="hand2",
        font=font(8 if small else 9, bold=primary),
        padx=8 if small else 12,
        pady=2 if small else 5,
        bg=normal,
        fg=C.on_accent if primary else C.text,
        activebackground=hover,
        activeforeground=C.on_accent if primary else C.text,
        highlightbackground=C.accent if primary else C.control_border,
        highlightthickness=1,
    )
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover), add="+")
    btn.bind("<Leave>", lambda e: btn.configure(bg=normal), add="+")
    if tooltip:
        ToolTip([btn], tooltip)
    return btn


# 表格定义：(列 id, 表头, 宽度, 是否随窗口拉伸)
TREE_COLUMNS: dict[str, list[tuple[str, str, int, bool]]] = {
    "recent": [("time", "时间", 150, False), ("action", "动作", 110, False), ("path", "文件路径", 700, True)],
    "err": [
        ("time", "时间", 150, False),
        ("status", "状态", 76, False),
        ("raw_code", "代码", 66, False),
        ("reason", "群晖记录的原因", 210, False),
        ("hint", "可能原因（本工具按路径推断）", 380, False),
        ("path", "文件路径", 420, True),
    ],
    "alerts": [
        ("time", "最近出现", 150, False),
        ("level", "级别", 60, False),
        ("count", "次数", 60, False),
        ("message", "日志内容", 760, True),
    ],
}
EMPTY_TEXT = {
    "recent": "还没有读到同步活动记录",
    "err": "没有需要关注的同步异常",
    "alerts": "daemon.log 中没有发现 [ERROR] / [WARNING]",
}
THEME_LABELS = {"system": "跟随系统", "light": "浅色", "dark": "深色"}


# ==============================================================================
# 主窗体
# ==============================================================================
class App:
    def __init__(self, root: Tk, base: Path):
        self.root = root
        self.reader = core.DriveReader(base)
        self.settings = core.load_settings()
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        resolve_fonts(root)

        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.geometry(self._initial_geometry())
        self.root.minsize(self.px(980), self.px(700))

        # 这些变量在切换主题重建界面时保留
        self.search_var = StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.show_temp_var = tk.BooleanVar(value=bool(self.settings["show_temp"]))
        self.show_recovered_var = tk.BooleanVar(value=bool(self.settings["show_recovered"]))
        self.theme_var = StringVar(value=str(self.settings["theme"]))
        self.topmost_var = tk.BooleanVar(value=bool(self.settings["topmost"]))
        self.flash_var = tk.BooleanVar(value=bool(self.settings["flash_alerts"]))

        self._results: queue.Queue = queue.Queue()
        self._busy = False
        self._force_pending = False
        self._last_metrics: dict | None = None
        self._tree_sigs: dict[str, int] = {}
        self._sort: dict[str, tuple[str, bool]] = {}
        self._visible_errors: list[dict] = []
        self._counts: dict[str, tuple[int, int]] = {}
        self._current_path = ""
        self._toast_job: str | None = None
        self._prev_stall: str | None = None
        self._prev_errors: int | None = None
        self._had_work = False

        self.main: tk.Frame | None = None
        self._more_menu: tk.Menu | None = None
        self._apply_theme(rebuild=False)
        self._build_ui()
        self.root.attributes("-topmost", self.topmost_var.get())

        self.root.bind("<F5>", lambda e: self.refresh_now())
        self.root.bind("<Control-f>", lambda e: self._focus_search())
        self.root.bind("<Control-e>", lambda e: self.export_csv())
        self.root.bind("<Control-d>", lambda e: self.open_diagnostics())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._tick)
        self.root.after(POLL_MS, self._poll_results)

    def px(self, n: float) -> int:
        """按屏幕缩放换算像素尺寸（字号由 Tk 自己缩放，像素尺寸需要手动乘）。"""
        return int(n * self.scale)

    def _initial_geometry(self) -> str:
        w, h = self.px(1200), self.px(860)
        m = re.fullmatch(r"(\d+)x(\d+)", str(self.settings.get("geometry") or ""))
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        w = max(self.px(980), min(w, self.root.winfo_screenwidth()))
        h = max(self.px(700), min(h, self.root.winfo_screenheight() - self.px(60)))
        return f"{w}x{h}"

    # ------------------------------------------------------------------ 主题与样式
    def _apply_theme(self, rebuild: bool = True):
        choice = self.theme_var.get()
        C.apply("dark" if (choice == "dark" or (choice == "system" and system_prefers_dark())) else "light")
        self.root.configure(bg=C.bg)
        self._setup_styles()
        set_titlebar_dark(self.root, C.name == "dark")
        if rebuild:
            self._build_ui()
            if self._last_metrics:
                self._apply_metrics(self._last_metrics, notify=False)

    def _setup_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Fluent.Treeview",
            background=C.surface,
            foreground=C.text,
            fieldbackground=C.surface,
            bordercolor=C.surface,
            lightcolor=C.surface,
            darkcolor=C.surface,
            borderwidth=0,
            font=font(9),
            rowheight=self.px(28),
        )
        style.configure(
            "Fluent.Treeview.Heading",
            background=C.heading,
            foreground=C.text2,
            borderwidth=0,
            font=font(9, bold=True),
            relief="flat",
            padding=(6, 6),
        )
        style.map("Fluent.Treeview", background=[("selected", C.accent_bg)], foreground=[("selected", C.text)])
        style.map("Fluent.Treeview.Heading", background=[("active", C.hover)])
        # clam 主题默认用浅灰描边，深色模式下必须一并改掉，否则表格四周会有一圈亮线
        style.configure(
            "Fluent.TNotebook",
            background=C.bg,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
            bordercolor=C.border,
            lightcolor=C.surface,
            darkcolor=C.surface,
        )
        style.configure(
            "Fluent.TNotebook.Tab",
            background=C.bg,
            foreground=C.text2,
            padding=(16, 7),
            font=font(9, bold=True),
            borderwidth=0,
            bordercolor=C.border,
            lightcolor=C.bg,
            darkcolor=C.bg,
        )
        style.map(
            "Fluent.TNotebook.Tab",
            background=[("selected", C.surface), ("active", C.hover)],
            foreground=[("selected", C.accent)],
            lightcolor=[("selected", C.surface)],
            darkcolor=[("selected", C.surface)],
        )
        for orient in ("Vertical", "Horizontal"):
            style.configure(
                f"Fluent.{orient}.TScrollbar",
                background=C.border,
                troughcolor=C.surface,
                bordercolor=C.surface,
                lightcolor=C.border,
                darkcolor=C.border,
                arrowcolor=C.text2,
                gripcount=0,
                relief="flat",
            )
            style.map(f"Fluent.{orient}.TScrollbar", background=[("active", C.muted)])

    # ------------------------------------------------------------------ 布局
    def _build_ui(self):
        selected_tab = 0
        if self.main is not None:
            try:
                selected_tab = self.notebook.index(self.notebook.select())
            except tk.TclError:
                pass
            self.main.destroy()
        self._tree_sigs.clear()
        self.main = main = tk.Frame(self.root, bg=C.bg)
        main.pack(fill="both", expand=True)

        self._build_header(main)
        self._build_summary(main)
        self._build_cards(main)
        self._build_charts(main)
        self._build_status_bar(main)  # 先占住底部，再让表格吃掉剩余空间
        self._build_tables(main)
        self.notebook.select(selected_tab)

    def _build_header(self, main):
        header = tk.Frame(main, bg=C.bg, padx=24)
        header.pack(fill="x", pady=(16, 8))
        left = tk.Frame(header, bg=C.bg)
        left.pack(side="left")
        tk.Label(left, text=APP_TITLE, font=font(18, bold=True, display=True), fg=C.text, bg=C.bg).pack(side="left")
        self.status_text_var = StringVar(value="正在启动")
        self.lbl_status = tk.Label(left, textvariable=self.status_text_var, font=font(9, bold=True), padx=10, pady=3)
        self.lbl_status.pack(side="left", padx=(14, 0))
        self._set_status_pill(C.gray)

        right = tk.Frame(header, bg=C.bg)
        right.pack(side="right")
        self.last_act_var = StringVar(value="最后活动: --")
        tk.Label(right, textvariable=self.last_act_var, font=font(9), fg=C.text2, bg=C.bg).pack(side="left", padx=(0, 12))
        fluent_button(right, "诊断报告", self.open_diagnostics, tooltip="查看并复制完整诊断信息 (Ctrl+D)").pack(
            side="left", padx=3
        )
        fluent_button(right, "导出异常 CSV", self.export_csv, tooltip="导出当前异常列表，与筛选一致 (Ctrl+E)").pack(
            side="left", padx=3
        )
        fluent_button(right, "立即刷新", self.refresh_now, primary=True, tooltip="重新读取日志和数据库 (F5)").pack(
            side="left", padx=3
        )
        self.btn_more = fluent_button(right, "更多 ▾", self._open_more_menu, tooltip="主题、置顶、提醒等更多选项")
        self.btn_more.pack(side="left", padx=(3, 0))

    def _set_status_pill(self, color: str):
        self.lbl_status.configure(fg=color, bg=blend(color, C.bg, 0.16))

    def _build_summary(self, main):
        """一句话结论：把指标翻译成人话，放在最显眼的位置。"""
        wrap = tk.Frame(main, bg=C.bg, padx=24)
        wrap.pack(fill="x", pady=(0, 8))
        box = card_frame(wrap)
        box.pack(fill="x")
        self.summary_stripe = tk.Frame(box, bg=C.gray, width=self.px(4))
        self.summary_stripe.pack(side="left", fill="y")
        self.summary_var = StringVar(value="正在读取群晖 Drive 的本地日志与数据库…")
        self.lbl_summary = tk.Label(
            box,
            textvariable=self.summary_var,
            font=font(10),
            fg=C.text,
            bg=C.surface,
            justify="left",
            anchor="w",
            padx=14,
            pady=9,
        )
        self.lbl_summary.pack(side="left", fill="x", expand=True)
        box.bind("<Configure>", lambda e: self.lbl_summary.configure(wraplength=max(200, e.width - self.px(48))))

    def _build_cards(self, main):
        cards = tk.Frame(main, bg=C.bg, padx=20)
        cards.pack(fill="x")
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="c")
        self.c_remaining = FluentCard(
            cards,
            "当前在途",
            "Push / Done 实时配对",
            tooltip="本程序观察到、尚未匹配 DoneEvent 的事件数（含启动时从日志尾部重建的部分）。"
            "不代表整个 Session 的剩余工作量；超过 2 小时未配对的事件会自动过期。",
        )
        self.c_net_speed = FluentCard(
            cards,
            "净消化速度",
            "5 分钟完成 vs 新增",
            tooltip="DoneEvent 速率减去 PushEvent 速率（仅统计监控启动后实时到达的事件）。"
            "正数代表正在净消化，负数代表新任务持续增长。",
        )
        self.c_eta = FluentCard(
            cards,
            "预计完成 (ETA)",
            "流式比对进行中",
            tooltip="仅在正在收敛或处理尾部任务时计算；新任务持续生成或事件流平衡时如实显示无法估算。",
        )
        self.c_unsynced = FluentCard(
            cards,
            "同步异常",
            "真正需关注 (已去重)",
            tooltip="排除 -8192 选择性同步过滤、临时 / 锁文件，以及之后已成功同步的记录后，仍未同步的文件数。点击查看列表。",
            on_click=lambda: self.notebook.select(self.panels["err"]),
        )
        for i, c in enumerate([self.c_remaining, self.c_net_speed, self.c_eta, self.c_unsynced]):
            c.grid(row=0, column=i, padx=4, sticky="nsew")

    def _build_charts(self, main):
        mid = tk.Frame(main, bg=C.bg, padx=20)
        mid.pack(fill="x", pady=8)
        mid.columnconfigure(0, weight=3, uniform="m")
        mid.columnconfigure(1, weight=2, uniform="m")

        trend = card_frame(mid, padx=14, pady=10)
        trend.grid(row=0, column=0, padx=4, sticky="nsew")
        t_header = tk.Frame(trend, bg=C.surface)
        t_header.pack(fill="x")
        tk.Label(t_header, text="在途事件池趋势", font=font(9, bold=True), fg=C.text2, bg=C.surface).pack(side="left")
        self.trend_delta_var = StringVar(value="计算中…")
        tk.Label(t_header, textvariable=self.trend_delta_var, font=font(9, bold=True), fg=C.accent, bg=C.surface).pack(
            side="right"
        )
        self.sparkline = OutstandingChart(trend, scale=self.scale, height=self.px(62))
        self.sparkline.pack(fill="x", pady=(4, 6))
        tk.Label(trend, text="每分钟吞吐（最近 15 分钟）", font=font(9, bold=True), fg=C.text2, bg=C.surface).pack(anchor="w")
        self.throughput_chart = ThroughputChart(trend, scale=self.scale, height=self.px(52))
        self.throughput_chart.pack(fill="x", pady=(2, 2))
        self.throughput_var = StringVar(value="流式事件吞吐: 采样中…")
        tk.Label(trend, textvariable=self.throughput_var, font=font(8), fg=C.muted, bg=C.surface).pack(anchor="w")

        curr = card_frame(mid, padx=14, pady=10)
        curr.grid(row=0, column=1, padx=4, sticky="nsew")
        c_top = tk.Frame(curr, bg=C.surface)
        c_top.pack(fill="x")
        self.curr_action_var = StringVar(value="正在处理")
        tk.Label(c_top, textvariable=self.curr_action_var, font=font(9, bold=True), fg=C.accent, bg=C.surface).pack(side="left")
        fluent_button(c_top, "在资源管理器中定位", self.reveal_current, small=True).pack(side="right")
        self.curr_file_var = StringVar(value="等待捕获文件活动…")
        lbl_file = tk.Label(
            curr,
            textvariable=self.curr_file_var,
            font=font(11, bold=True, display=True),
            fg=C.text,
            bg=C.surface,
            justify="left",
            anchor="w",
        )
        lbl_file.pack(fill="x", pady=(8, 2))
        self.curr_path_var = StringVar(value="")
        lbl_path = tk.Label(
            curr, textvariable=self.curr_path_var, font=font(8), fg=C.muted, bg=C.surface, justify="left", anchor="w"
        )
        lbl_path.pack(fill="x")

        def rewrap(event):
            width = max(160, event.width - self.px(34))
            lbl_file.configure(wraplength=width)
            lbl_path.configure(wraplength=width)

        curr.bind("<Configure>", rewrap)

        tk.Frame(curr, bg=C.border, height=1).pack(fill="x", pady=(10, 6))
        tk.Label(curr, text="此前处理", font=font(8, bold=True), fg=C.text2, bg=C.surface, anchor="w").pack(fill="x")
        self.recent_files_box = tk.Frame(curr, bg=C.surface)
        self.recent_files_box.pack(fill="both", expand=True, pady=(2, 0))
        self._recent_file_rows: list[tuple[tk.Label, tk.Label]] = []
        self._recent_file_paths: list[str] = ["", "", "", ""]
        for i in range(4):
            row = tk.Frame(self.recent_files_box, bg=C.surface)
            row.pack(fill="x")
            t = tk.Label(row, font=font(8), fg=C.muted, bg=C.surface, width=8, anchor="w")
            t.pack(side="left")
            n = tk.Label(row, font=font(8), fg=C.text2, bg=C.surface, anchor="w", cursor="hand2")
            n.pack(side="left", fill="x", expand=True)
            n.bind("<Double-1>", lambda e, i=i: self._reveal_recent_file(i))
            self._recent_file_rows.append((t, n))
        ToolTip([n for _, n in self._recent_file_rows], "双击在资源管理器中定位")

        self.proc_var = StringVar(value="")
        tk.Label(curr, textvariable=self.proc_var, font=font(8), fg=C.muted, bg=C.surface, justify="left", anchor="w").pack(
            side="bottom", fill="x"
        )

    def _build_tables(self, main):
        table_frame = tk.Frame(main, bg=C.bg, padx=24)
        table_frame.pack(fill="both", expand=True)

        bar = tk.Frame(table_frame, bg=C.bg)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="筛选", font=font(9), fg=C.text2, bg=C.bg).pack(side="left", padx=(0, 8))
        entry_box = tk.Frame(bar, bg=C.surface, highlightbackground=C.border, highlightcolor=C.accent, highlightthickness=1)
        entry_box.pack(side="left", fill="x", expand=True)
        self.search_entry = tk.Entry(
            entry_box,
            textvariable=self.search_var,
            font=font(9),
            bg=C.surface,
            fg=C.text,
            insertbackground=C.text,
            relief="flat",
            bd=0,
        )
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(8, 0))
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        clear = tk.Label(entry_box, text="✕", font=font(9), fg=C.muted, bg=C.surface, cursor="hand2", padx=8)
        clear.pack(side="right")
        clear.bind("<Button-1>", lambda e: self.search_var.set(""))
        ToolTip([self.search_entry], "输入路径、动作、原因或错误代码的一部分；Ctrl+F 聚焦，Esc 清空。三个页签同时生效。")

        self.count_var = StringVar(value="")
        tk.Label(bar, textvariable=self.count_var, font=font(9), fg=C.muted, bg=C.bg).pack(side="left", padx=(12, 4))
        self.chips = [
            Chip(bar, "临时文件", self.show_temp_var, self._apply_filter),
            Chip(bar, "已恢复", self.show_recovered_var, self._apply_filter),
        ]
        for chip in self.chips:
            chip.pack(side="left", padx=(6, 0))
        ToolTip(list(self.chips), "在「同步异常」列表里额外显示 Office / WPS 临时锁文件，或之后已成功同步的记录。")

        self.notebook = ttk.Notebook(table_frame, style="Fluent.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._update_count_label())
        self.panels: dict[str, tk.Frame] = {}
        self.trees: dict[str, ttk.Treeview] = {}
        self.empty_labels: dict[str, tk.Label] = {}
        for name, title in (("recent", "最近活动"), ("err", "同步异常"), ("alerts", "日志告警")):
            panel = tk.Frame(self.notebook, bg=C.surface)
            self.notebook.add(panel, text=title)
            self.panels[name] = panel
            self.trees[name] = self._build_tree(name, panel)
        self.recent_tree, self.err_tree, self.alert_tree = self.trees["recent"], self.trees["err"], self.trees["alerts"]
        self.err_panel = self.panels["err"]

        self.breakdown_var = StringVar(value="")
        tk.Label(
            self.err_panel, textvariable=self.breakdown_var, font=font(8), fg=C.text2, bg=C.surface, anchor="w", padx=10, pady=4
        ).grid(row=2, column=0, columnspan=2, sticky="ew")

    def _build_status_bar(self, main):
        status_bar = tk.Frame(main, bg=C.bg, padx=24, pady=6)
        status_bar.pack(side="bottom", fill="x")
        self.sb_left = StringVar(value="Drive: 检测中 | 严格只读")
        tk.Label(status_bar, textvariable=self.sb_left, font=font(8), fg=C.muted, bg=C.bg).pack(side="left")
        self.sb_right = StringVar(value="更新时间: --")
        tk.Label(status_bar, textvariable=self.sb_right, font=font(8), fg=C.muted, bg=C.bg).pack(side="right")
        self.toast_var = StringVar(value="")
        tk.Label(status_bar, textvariable=self.toast_var, font=font(8, bold=True), fg=C.accent, bg=C.bg).pack(
            side="right", padx=16
        )

    def _build_tree(self, name: str, parent: tk.Frame) -> ttk.Treeview:
        col_defs = TREE_COLUMNS[name]
        tree = ttk.Treeview(parent, columns=[c[0] for c in col_defs], show="headings", style="Fluent.Treeview")
        for col_id, heading, width, stretch in col_defs:
            tree.heading(col_id, text=heading, anchor="w", command=lambda c=col_id: self._sort_by(name, c))
            tree.column(col_id, width=self.px(width), minwidth=self.px(50), stretch=stretch, anchor="w")
        tree.tag_configure("odd", background=C.zebra)
        tree.tag_configure("bad", foreground=C.red)
        tree.tag_configure("warn", foreground=C.orange)
        tree.tag_configure("ok", foreground=C.green)
        tree.tag_configure("dim", foreground=C.muted)
        y = ttk.Scrollbar(parent, orient="vertical", command=tree.yview, style="Fluent.Vertical.TScrollbar")
        x = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview, style="Fluent.Horizontal.TScrollbar")
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self.empty_labels[name] = tk.Label(parent, text=EMPTY_TEXT[name], font=font(10), fg=C.muted, bg=C.surface)
        has_path = name != "alerts"
        if has_path:
            tree.bind("<Double-1>", lambda e: self.reveal_selected(tree))
            tree.bind("<Return>", lambda e: self.reveal_selected(tree))
        tree.bind("<Control-c>", lambda e: self.copy_selected(tree))
        CellTip(tree)
        self._bind_context_menu(tree, has_path)
        self._refresh_headings(name, tree)
        return tree

    def _bind_context_menu(self, tree: ttk.Treeview, has_path: bool):
        menu = self._new_menu(tree)
        if has_path:
            menu.add_command(label="在资源管理器中定位", command=lambda: self.reveal_selected(tree))
            menu.add_command(label="复制完整路径    Ctrl+C", command=lambda: self.copy_selected(tree))
            menu.add_command(label="复制文件名", command=lambda: self.copy_selected(tree, name_only=True))
        else:
            menu.add_command(label="复制日志内容    Ctrl+C", command=lambda: self.copy_selected(tree))
        menu.add_command(label="复制整行", command=lambda: self.copy_selected(tree, whole_row=True))

        def popup(event):
            item = tree.identify_row(event.y)
            if item:
                tree.selection_set(item)
                menu.tk_popup(event.x_root, event.y_root)

        tree.bind("<Button-3>", popup)

    def _new_menu(self, parent: tk.Misc) -> tk.Menu:
        return tk.Menu(
            parent,
            tearoff=0,
            bg=C.surface,
            fg=C.text,
            activebackground=C.accent_bg,
            activeforeground=C.text,
            selectcolor=C.accent,
            bd=1,
            relief="solid",
            font=font(9),
        )

    def _build_more_menu(self) -> tk.Menu:
        if self._more_menu is not None:
            try:
                self._more_menu.destroy()
            except tk.TclError:
                pass
        self._more_menu = menu = self._new_menu(self.root)
        theme_menu = self._new_menu(menu)
        for key, label in THEME_LABELS.items():
            theme_menu.add_radiobutton(label=label, value=key, variable=self.theme_var, command=self._on_theme_change)
        menu.add_cascade(label="主题", menu=theme_menu)
        menu.add_checkbutton(label="窗口置顶", variable=self.topmost_var, command=self._on_topmost_change)
        menu.add_checkbutton(label="出现异常 / 同步完成时闪烁任务栏", variable=self.flash_var, command=self._save_settings)
        menu.add_separator()
        menu.add_command(label="复制状态摘要", command=self.copy_summary)
        menu.add_command(label="打开群晖 Drive 日志目录", command=lambda: self._open_dir(self.reader.base / "log"))
        menu.add_command(label="打开本程序数据目录", command=lambda: self._open_dir(core.data_dir()))
        menu.add_separator()
        menu.add_command(label="关于", command=self._show_about)
        return menu

    def _open_more_menu(self):
        self._build_more_menu().tk_popup(self.btn_more.winfo_rootx(), self.btn_more.winfo_rooty() + self.btn_more.winfo_height())

    # ------------------------------------------------------------------ 设置
    def _on_theme_change(self):
        self._save_settings()
        # 这里是从菜单命令里调用的：等菜单收起后再重建界面，避免在回调中销毁正在使用的控件
        self.root.after_idle(self._apply_theme)

    def _on_topmost_change(self):
        self.root.attributes("-topmost", self.topmost_var.get())
        self._save_settings()

    def _save_settings(self):
        self.settings.update(
            theme=self.theme_var.get(),
            topmost=bool(self.topmost_var.get()),
            flash_alerts=bool(self.flash_var.get()),
            show_temp=bool(self.show_temp_var.get()),
            show_recovered=bool(self.show_recovered_var.get()),
        )
        try:
            if self.root.state() == "normal":
                self.settings["geometry"] = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
        except tk.TclError:
            pass
        core.save_settings(self.settings)

    # ------------------------------------------------------------------ 刷新调度
    # 只在主线程操作 Tk：工作线程只负责读数据，结果经队列交回主线程。
    def _tick(self):
        self._start_worker(force=False)
        self.root.after(REFRESH_MS, self._tick)

    def refresh_now(self):
        self._toast("正在刷新…")
        self._start_worker(force=True)

    def _start_worker(self, force: bool):
        if self._busy:
            self._force_pending = self._force_pending or force
            return
        self._busy = True
        threading.Thread(target=self._worker, args=(force,), daemon=True).start()

    def _worker(self, force: bool):
        try:
            self._results.put(("ok", self.reader.read_metrics(force_history=force)))
        except Exception as e:  # 工作线程兜底，避免静默退出
            logger.error(f"Worker refresh error: {e}", exc_info=True)
            self._results.put(("err", str(e)))

    def _poll_results(self):
        try:
            while True:
                kind, payload = self._results.get_nowait()
                self._busy = False
                if kind == "ok":
                    self._apply_metrics(payload)
                else:
                    self.sb_left.set(f"数据暂时不可用（将自动重试）: {payload}")
        except queue.Empty:
            pass
        if not self._busy and self._force_pending:
            self._force_pending = False
            self._start_worker(force=True)
        self.root.after(POLL_MS, self._poll_results)

    def _on_close(self):
        self.reader.save_persistent_state()
        self._save_settings()
        self.root.destroy()

    # ------------------------------------------------------------------ 渲染
    def _level_color(self, level: str) -> str:
        return {"ok": C.green, "info": C.blue, "warn": C.orange, "bad": C.red, "off": C.gray}.get(level, C.blue)

    def _apply_metrics(self, m: dict, notify: bool = True):
        self._last_metrics = m
        stall, state = m["stall_status"], m["convergence_state"]
        if stall == "offline":
            pill = C.gray
        elif stall == "stalled":
            pill = C.red
        elif stall == "warning" or state == core.ST_GROWING:
            pill = C.orange
        elif stall == "finished" or state == core.ST_CONVERGING:
            pill = C.green
        else:
            pill = C.blue
        self.status_text_var.set(m["stall_desc"])
        self._set_status_pill(pill)
        self.last_act_var.set(f"最后活动: {core.format_age(m['idle_secs'])}前")
        self.summary_var.set(m.get("summary", ""))
        self.summary_stripe.configure(bg=self._level_color(m.get("summary_level", "info")))
        # 最小化到任务栏时也能从标题看出状态
        self.root.title(f"{m['rem_val_str']} 在途 · {m['stall_desc']} — {APP_TITLE}")

        self.c_remaining.set_value(m["rem_val_str"], "Push / Done 实时配对", C.accent)
        net = m["net_drain_rate_5m"]
        net_col = C.green if net > 0 else (C.orange if net < 0 else C.text)
        if m["net_val_str"] in ("采样中", "空闲"):
            net_col = C.muted
        self.c_net_speed.set_value(m["net_val_str"], m["net_sub_str"], net_col)
        eta_col = C.accent if any(u in m["eta_str"] for u in ("小时", "分钟", "天")) else C.muted
        if m["eta_str"] == "已同步完成":
            eta_col = C.green
        self.c_eta.set_value(m["eta_str"], m["eta_sub_desc"], eta_col)
        a, t, r = m["actionable_errors_count"], m["office_temp_count"], m["recovered_count"]
        self.c_unsynced.set_value(f"{a:,}", f"需关注 {a} · 临时 {t} · 已恢复 {r}", C.red if a else C.green)

        self.trend_delta_var.set(m["convergence_state"])
        self.sparkline.draw_data(m["remaining_samples"])
        self.throughput_chart.draw_data(m.get("rate_series", []))
        sign = "+" if net >= 0 else ""
        self.throughput_var.set(
            f"5 分钟: 完成 {m['done_rate_5m']}/分 · 新增 {m['push_rate_5m']}/分 (净 {sign}{net}/分)"
            f"   |   1 分钟: 完成 {m['done_rate_1m']}/分 · 新增 {m['push_rate_1m']}/分"
        )

        cfile = m["current_file"]
        self.curr_action_var.set(m["active_label"])
        self._current_path = cfile
        if cfile:
            self.curr_file_var.set(shorten_middle(Path(cfile.replace("\\", "/")).name or cfile))
            self.curr_path_var.set(shorten_middle(cfile, 180))
        else:
            self.curr_file_var.set("等待捕获文件活动…")
            self.curr_path_var.set("正在进行目录比对或空闲中")
        self.proc_var.set(f"客户端进程: {m['process_state']}  ·  Session {m['session_id']}")
        recent_files = m.get("recent_files", [])
        for i, (t_lbl, n_lbl) in enumerate(self._recent_file_rows):
            if i < len(recent_files):
                rf = recent_files[i]
                t_lbl.configure(text=rf["time"])
                n_lbl.configure(text=f"{rf['action']} · {shorten_middle(Path(rf['path'].replace(chr(92), '/')).name, 48)}")
                self._recent_file_paths[i] = rf["path"]
            else:
                t_lbl.configure(text="")
                n_lbl.configure(text="—" if i == 0 and not recent_files else "")
                self._recent_file_paths[i] = ""

        self._apply_filter()
        sb = f"Drive: {m['process_state']} | Session {m['session_id']} | 严格只读"
        if m.get("history_error"):
            sb += f" | ⚠ {m['history_error']}"
        self.sb_left.set(sb)
        self.sb_right.set(f"更新时间: {datetime.now().strftime('%H:%M:%S')}")
        if notify:
            self._check_notifications(m)

    def _check_notifications(self, m: dict):
        """状态发生值得注意的变化时提醒一次（首次读取不提醒）。"""
        stall, errors = m["stall_status"], m["actionable_errors_count"]
        message = ""
        if self._prev_stall is not None and self._prev_errors is not None:
            if stall == "stalled" and self._prev_stall != "stalled":
                message = "同步长时间没有进展"
            elif errors > self._prev_errors:
                message = f"新增 {errors - self._prev_errors} 个同步异常"
            elif stall == "finished" and self._prev_stall != "finished" and self._had_work:
                message = "同步已完成"
        if m["outstanding"] > 0:
            self._had_work = True
        elif stall == "finished":
            self._had_work = False
        self._prev_stall, self._prev_errors = stall, errors
        if message:
            self._toast(message, long=True)
            if self.flash_var.get():
                flash_taskbar(self.root)

    def _toast(self, message: str, long: bool = False):
        self.toast_var.set(message)
        if self._toast_job:
            try:
                self.root.after_cancel(self._toast_job)
            except tk.TclError:
                pass
        self._toast_job = self.root.after(TOAST_MS * (3 if long else 1), lambda: self.toast_var.set(""))

    # ------------------------------------------------------------------ 表格
    def _sort_by(self, name: str, col: str):
        current = self._sort.get(name)
        if current is None or current[0] != col:
            self._sort[name] = (col, False)
        elif not current[1]:
            self._sort[name] = (col, True)
        else:
            del self._sort[name]  # 第三次点击恢复默认顺序
        self._refresh_headings(name, self.trees[name])
        self._apply_filter()

    def _refresh_headings(self, name: str, tree: ttk.Treeview):
        current = self._sort.get(name)
        for col_id, heading, _w, _s in TREE_COLUMNS[name]:
            arrow = ""
            if current and current[0] == col_id:
                arrow = "  ▼" if current[1] else "  ▲"
            tree.heading(col_id, text=heading + arrow)

    def _sorted(self, name: str, rows: list[tuple]) -> list[tuple]:
        current = self._sort.get(name)
        if not current:
            return rows
        idx = [c[0] for c in TREE_COLUMNS[name]].index(current[0])

        def key(row: tuple):
            v = str(row[0][idx])
            try:
                return (0, float(v.replace(",", "")), "")
            except ValueError:
                return (1, 0.0, v.lower())

        return sorted(rows, key=key, reverse=current[1])

    def _fill_tree(self, name: str, rows: list[tuple]):
        """rows 为 (各列值元组, 状态 tag) 列表。仅在内容变化时重建，并保留选中行与滚动位置。"""
        tree = self.trees[name]
        rows = self._sorted(name, rows)
        sig = hash(tuple(rows))
        if self._tree_sigs.get(name) == sig:
            return
        self._tree_sigs[name] = sig
        # Tk 可能把数字样式的值转回 int，统一按字符串比较
        selected = {tuple(str(v) for v in tree.item(i, "values")) for i in tree.selection()}
        top = tree.yview()[0]
        tree.delete(*tree.get_children())
        reselect = []
        for i, (values, tag) in enumerate(rows):
            tags = tuple(x for x in ("odd" if i % 2 else "", tag) if x)
            iid = tree.insert("", "end", values=values, tags=tags)
            if tuple(str(v) for v in values) in selected:
                reselect.append(iid)
        if reselect:
            tree.selection_set(reselect)
        tree.yview_moveto(top)
        if rows:
            self.empty_labels[name].place_forget()
        else:
            self.empty_labels[name].place(in_=tree, relx=0.5, rely=0.5, anchor="center")

    def _apply_filter(self):
        m = self._last_metrics
        if not m:
            return
        kw = self.search_var.get().strip().lower()

        def match(*fields) -> bool:
            return not kw or any(kw in str(f).lower() for f in fields)

        recent = [r for r in m["recent_rows"] if match(r["path"], r["action"])]
        self._fill_tree("recent", [((r["time"], r["action"], r["path"]), "") for r in recent])
        self._counts["recent"] = (len(recent), len(m["recent_rows"]))

        errs = list(m["actionable_errors"])
        if self.show_temp_var.get():
            errs += m["temp_errors"]
        if self.show_recovered_var.get():
            errs += m["recovered_errors"]
        total_errs = len(errs)
        errs = [e for e in errs if match(e["path"], e["reason"], e["raw_code"], e.get("hint", ""), e["status"])]
        self._visible_errors = errs
        status_tag = {"需关注": "bad", "临时文件": "dim", "已恢复": "ok"}
        self._fill_tree(
            "err",
            [
                (
                    (e["time"], e["status"], e["raw_code"], e["reason"], e.get("hint", ""), e["path"]),
                    status_tag.get(e["status"], ""),
                )
                for e in errs
            ],
        )
        self._counts["err"] = (len(errs), total_errs)

        all_alerts = m.get("log_alerts", [])
        alerts = [x for x in all_alerts if match(x["message"], x["level"])]
        self._fill_tree(
            "alerts",
            [
                ((x["time"], x["level"], f"{x['count']:,}", x["message"]), "bad" if x["level"] == "错误" else "warn")
                for x in alerts
            ],
        )
        self._counts["alerts"] = (len(alerts), len(all_alerts))

        self.notebook.tab(self.panels["err"], text=f"同步异常 ({m['actionable_errors_count']})")
        self.notebook.tab(self.panels["alerts"], text=f"日志告警 ({len(all_alerts)})")
        bd = m.get("error_breakdown") or {}
        parts = []
        if bd.get("by_reason"):
            parts.append("按原因: " + " · ".join(f"{name} {cnt}" for name, cnt in bd["by_reason"][:4]))
        if bd.get("by_folder"):
            parts.append("集中在: " + " · ".join(f"{name} {cnt}" for name, cnt in bd["by_folder"][:3]))
        self.breakdown_var.set("    |    ".join(parts) if parts else "「可能原因」一列是本工具根据路径做的推断，仅供排查参考。")
        self._update_count_label()

    def _current_tab(self) -> str:
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return "recent"
        return ("recent", "err", "alerts")[idx] if 0 <= idx < 3 else "recent"

    def _update_count_label(self):
        shown, total = self._counts.get(self._current_tab(), (0, 0))
        self.count_var.set(f"显示 {shown:,} / {total:,}" if shown != total else f"共 {total:,} 条")

    def _focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")

    # ------------------------------------------------------------------ 交互
    def _selected_values(self, tree: ttk.Treeview) -> tuple | None:
        sel = tree.selection()
        return tuple(str(v) for v in tree.item(sel[0], "values")) if sel else None

    def _copy(self, text: str, what: str):
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._toast(f"已复制{what}")

    def copy_selected(self, tree: ttk.Treeview, name_only: bool = False, whole_row: bool = False):
        values = self._selected_values(tree)
        if not values:
            return
        if whole_row:
            self._copy("\t".join(values), "整行")
        elif name_only:
            self._copy(re.split(r"[\\/]", values[-1].strip())[-1], "文件名")
        else:
            self._copy(values[-1].strip(), "日志内容" if tree is self.alert_tree else "路径")

    def copy_selected_path(self, tree: ttk.Treeview):  # 兼容旧名称
        self.copy_selected(tree)

    def copy_summary(self):
        m = self._last_metrics
        if not m:
            return
        lines = [
            f"[{APP_TITLE} {APP_VERSION}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            m.get("summary", ""),
            f"在途 {m['rem_val_str']} · {m['convergence_state']} · ETA {m['eta_str']}",
            f"5 分钟速率: 完成 {m['done_rate_5m']}/分, 新增 {m['push_rate_5m']}/分",
            f"同步异常: 需关注 {m['actionable_errors_count']} · 临时 {m['office_temp_count']} · 已恢复 {m['recovered_count']}",
        ]
        self._copy("\n".join(lines), "状态摘要")

    def _reveal_recent_file(self, i: int):
        if self._recent_file_paths[i]:
            self._reveal_path(self._recent_file_paths[i])

    def reveal_current(self):
        if self._current_path:
            self._reveal_path(self._current_path)

    def reveal_selected(self, tree: ttk.Treeview):
        values = self._selected_values(tree)
        if values and values[-1].strip():
            self._reveal_path(values[-1].strip())

    def _reveal_path(self, p: str):
        if sys.platform != "win32":
            messagebox.showinfo(APP_TITLE, f"仅 Windows 支持在资源管理器中定位：\n{p}", parent=self.root)
            return
        try:
            path = Path(p.replace("/", "\\"))
            if path.is_file():
                subprocess.Popen(f'explorer /select,"{path}"')
            elif path.is_dir():
                subprocess.Popen(f'explorer "{path}"')
            elif path.parent.exists() and str(path.parent) not in (".", "\\"):
                subprocess.Popen(f'explorer "{path.parent}"')
                self._toast("文件已不存在，已打开它所在的文件夹")
            else:
                messagebox.showinfo(
                    APP_TITLE,
                    f"该文件当前在本机物理路径不存在：\n{p}\n\n可能是 NAS 端路径、远端同步历史记录或已被清理。",
                    parent=self.root,
                )
        except OSError as e:
            logger.error(f"Reveal error: {e}")
            messagebox.showerror(APP_TITLE, f"定位失败：{e}", parent=self.root)

    def _open_dir(self, directory: Path):
        if sys.platform == "win32" and directory.exists():
            try:
                os.startfile(directory)
                return
            except OSError as e:
                logger.error(f"Open dir error: {e}")
        self._copy(str(directory), f"目录路径（无法直接打开）: {directory}")

    def _show_about(self):
        messagebox.showinfo(
            f"关于 {APP_TITLE}",
            f"{APP_TITLE} {APP_VERSION}\n\n"
            "只读监控 Windows 版 Synology Drive Client 的同步状态。\n"
            "所有数据库访问均为只读，不会修改群晖客户端的任何文件。\n\n"
            f"群晖数据目录: {self.reader.base}\n"
            f"本程序数据目录: {core.data_dir()}\n\n"
            "快捷键: F5 刷新 · Ctrl+F 筛选 · Ctrl+E 导出 · Ctrl+D 诊断报告",
            parent=self.root,
        )

    def export_csv(self):
        rows = self._visible_errors
        if not rows:
            messagebox.showinfo(APP_TITLE, "当前列表中没有同步异常记录。", parent=self.root)
            return
        fn = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出同步异常文件清单",
            defaultextension=".csv",
            initialfile=f"synology_drive_errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not fn:
            return
        try:
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["时间", "状态", "错误代码", "原因说明", "可能原因(推断)", "文件路径"])
                for r in rows:
                    w.writerow([r["time"], r["status"], r["raw_code"], r["reason"], r.get("hint", ""), r["path"]])
            self._toast(f"已导出 {len(rows)} 条记录到 {Path(fn).name}", long=True)
        except OSError as e:
            logger.error(f"CSV export error: {e}")
            messagebox.showerror(APP_TITLE, f"导出失败：{e}", parent=self.root)

    def open_diagnostics(self):
        dlg = Toplevel(self.root)
        dlg.title(f"{APP_TITLE} - 诊断报告")
        dlg.geometry(f"{self.px(820)}x{self.px(700)}")
        dlg.minsize(self.px(520), self.px(360))
        dlg.configure(bg=C.surface)
        dlg.transient(self.root)
        set_titlebar_dark(dlg, C.name == "dark")

        btn_box = tk.Frame(dlg, bg=C.surface, padx=12, pady=10)
        btn_box.pack(side="bottom", fill="x")
        body = tk.Frame(dlg, bg=C.surface)
        body.pack(fill="both", expand=True)
        txt = tk.Text(
            body,
            font=(FONT_MONO, 9),
            bg=C.surface,
            fg=C.text,
            insertbackground=C.text,
            padx=14,
            pady=12,
            relief="flat",
            wrap="none",
        )
        y = ttk.Scrollbar(body, orient="vertical", command=txt.yview, style="Fluent.Vertical.TScrollbar")
        x = ttk.Scrollbar(body, orient="horizontal", command=txt.xview, style="Fluent.Horizontal.TScrollbar")
        txt.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        txt.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        report = {"text": ""}

        def load():
            report["text"] = core.build_diagnostics_report(self.reader, self._last_metrics)
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("1.0", report["text"])
            txt.configure(state="disabled")

        def copy_report():
            self.root.clipboard_clear()
            self.root.clipboard_append(report["text"])
            btn_copy.configure(text="已复制 ✓")
            dlg.after(1500, lambda: btn_copy.winfo_exists() and btn_copy.configure(text="复制报告"))

        def save_report():
            fn = filedialog.asksaveasfilename(
                parent=dlg,
                title="保存诊断报告",
                defaultextension=".txt",
                initialfile=f"synology_drive_diagnostics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
            if not fn:
                return
            try:
                Path(fn).write_text(report["text"], encoding="utf-8-sig")
            except OSError as e:
                messagebox.showerror(APP_TITLE, f"保存失败：{e}", parent=dlg)

        btn_copy = fluent_button(btn_box, "复制报告", copy_report, primary=True)
        btn_copy.pack(side="right")
        fluent_button(btn_box, "另存为…", save_report).pack(side="right", padx=6)
        fluent_button(btn_box, "重新生成", load).pack(side="right")
        tk.Label(btn_box, text="报告只包含本机路径与统计数字，不含文件内容。", font=font(8), fg=C.muted, bg=C.surface).pack(
            side="left"
        )
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        load()


# ==============================================================================
# 入口
# ==============================================================================
def _fail_on(value: str) -> list[str]:
    items = [x.strip().lower() for x in value.split(",") if x.strip()]
    bad = [x for x in items if x not in core.FAIL_CONDITIONS]
    if bad:
        raise argparse.ArgumentTypeError(f"不支持的条件: {', '.join(bad)}（可选: {', '.join(core.FAIL_CONDITIONS)}）")
    return items


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=f"{APP_TITLE} {APP_VERSION}（严格只读）")
    ap.add_argument("--base", help="群晖 Drive 本地数据目录，默认 %%LOCALAPPDATA%%\\SynologyDrive")
    ap.add_argument("--json", action="store_true", help="不开窗口，输出一次 JSON 状态快照")
    ap.add_argument("--sample", type=float, default=0.0, help="--json 模式下先采样 N 秒再输出（速率需要采样才有意义）")
    ap.add_argument(
        "--fail-on",
        type=_fail_on,
        default=[],
        metavar="条件",
        help="--json 模式下，命中任一条件时退出码为 1，逗号分隔: " + ", ".join(core.FAIL_CONDITIONS),
    )
    ap.add_argument("--version", action="version", version=f"{APP_TITLE} {APP_VERSION}")
    return ap.parse_args(argv)


def run_cli(base: Path, sample: float, fail_on: list[str] | None = None) -> int:
    reader = core.DriveReader(base, persist=False)
    m = reader.read_metrics(force_history=True)
    if sample > 0:
        time.sleep(sample)
        m = reader.read_metrics(force_history=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    snap = core.snapshot(reader, m)
    failed = core.check_failures(m, fail_on or [])
    snap["failed_conditions"] = failed
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        core.setup_logging()
    except OSError:
        pass  # 日志目录不可写时不影响监控本身
    base = core.find_synology_base(args.base)

    if args.json:
        if not base:
            print(json.dumps({"error": "未找到 Synology Drive 本地数据目录", "base": args.base}, ensure_ascii=False))
            return 2
        return run_cli(base, args.sample, args.fail_on)

    enable_dpi_awareness()
    root = Tk()
    if not base:
        root.withdraw()
        hint = f"指定的目录不存在：{args.base}" if args.base else "默认位置：%LOCALAPPDATA%\\SynologyDrive"
        messagebox.showerror(APP_TITLE, f"未找到 Synology Drive 本地数据目录。\n{hint}\n\n可用 --base 参数指定。")
        return 2
    App(root, base)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
