"""Synology Drive Monitor —— Windows 群晖 Drive 客户端只读同步状态监控（GUI 与命令行入口）。

用法：
    python synology_drive_monitor.py                 # 图形界面
    python synology_drive_monitor.py --json          # 不开窗口，输出一次 JSON 快照
    python synology_drive_monitor.py --json --sample 60   # 采样 60 秒后输出（速率才有意义）
    python synology_drive_monitor.py --base D:\\SynologyDrive   # 指定群晖本地数据目录
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, Tk, Toplevel, filedialog, messagebox, ttk

import drive_core as core
from drive_core import APP_TITLE, APP_VERSION, logger

REFRESH_MS = 2000
POLL_MS = 100

# ==============================================================================
# Windows 11 Fluent 浅色设计系统
# ==============================================================================
C_BG = "#F3F3F3"  # 主窗体浅冷灰底色
C_SURFACE = "#FFFFFF"  # 卡片 / 表格纯白底色
C_SURFACE_HOVER = "#FAFAFA"  # 悬浮浅灰
C_BORDER = "#E5E7EB"  # 低对比度细边框
C_TEXT_PRIMARY = "#0F172A"  # 深墨灰主文字
C_TEXT_SECONDARY = "#64748B"  # 次级辅助文字
C_TEXT_MUTED = "#94A3B8"  # 弱化提示文字
C_ACCENT = "#0067C0"  # Windows 11 经典蓝
C_ACCENT_HOVER = "#005FB8"
C_ACCENT_BG = "#E5F1FB"
C_TOOLTIP_BG = "#FFFFFF"

C_STATUS_GREEN = "#107C10"
C_STATUS_BLUE = "#0067C0"
C_STATUS_ORANGE = "#F7630C"
C_STATUS_RED = "#C42B1C"
C_STATUS_GRAY = "#6B7280"

FONT_TEXT = "Segoe UI Variable Text"
FONT_DISPLAY = "Segoe UI Variable Display"


def enable_dpi_awareness():
    """让 Windows 按真实 DPI 渲染，避免高分屏下界面发虚。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
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
            wraplength=320,
            font=(FONT_TEXT, 9),
            bg=C_TOOLTIP_BG,
            fg=C_TEXT_PRIMARY,
            relief="solid",
            bd=1,
            padx=8,
            pady=5,
        ).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class FluentCard(tk.Frame):
    """Windows 11 风格指标卡片。"""

    def __init__(self, parent, title: str, subtitle: str = "", tooltip: str = "", **kwargs):
        super().__init__(parent, bg=C_SURFACE, highlightbackground=C_BORDER, highlightthickness=1, padx=16, pady=12, **kwargs)
        self.lbl_title = tk.Label(self, text=title, font=(FONT_TEXT, 9, "bold"), fg=C_TEXT_SECONDARY, bg=C_SURFACE)
        self.lbl_title.pack(anchor="w")
        self.val_var = StringVar(value="--")
        self.lbl_val = tk.Label(self, textvariable=self.val_var, font=(FONT_DISPLAY, 22, "bold"), fg=C_TEXT_PRIMARY, bg=C_SURFACE)
        self.lbl_val.pack(anchor="w", pady=(2, 2))
        self.sub_var = StringVar(value=subtitle)
        self.lbl_sub = tk.Label(self, textvariable=self.sub_var, font=(FONT_TEXT, 9), fg=C_TEXT_MUTED, bg=C_SURFACE)
        self.lbl_sub.pack(anchor="w")

        parts = [self, self.lbl_title, self.lbl_val, self.lbl_sub]
        for w in parts:
            w.bind("<Enter>", lambda e: self._set_bg(C_SURFACE_HOVER), add="+")
            w.bind("<Leave>", lambda e: self._set_bg(C_SURFACE), add="+")
        if tooltip:
            ToolTip(parts, tooltip)

    def _set_bg(self, color: str):
        for w in (self, self.lbl_title, self.lbl_val, self.lbl_sub):
            w.configure(bg=color)

    def set_value(self, val: str, sub: str | None = None, color: str | None = None):
        self.val_var.set(val)
        if sub is not None:
            self.sub_var.set(sub)
        self.lbl_val.configure(fg=color or C_TEXT_PRIMARY)


class QueueSparkline(tk.Canvas):
    """自绘在途事件池趋势图。窗口尺寸变化时自动重绘。"""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=C_SURFACE, highlightthickness=0, **kwargs)
        self._samples: list[tuple[float, int]] = []
        self.bind("<Configure>", lambda e: self.draw_data(self._samples))

    def draw_data(self, samples: list[tuple[float, int]]):
        self._samples = samples
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 20 or len(samples) < 2:
            return
        values = [s[1] for s in samples]
        max_v, min_v = max(values), min(values)
        span = (max_v - min_v) or 1.0
        n = len(values)
        dx = (w - 30) / (n - 1)
        coords: list[float] = []
        for i, val in enumerate(values):
            coords.extend([15 + i * dx, (h - 15) - ((val - min_v) / span) * (h - 30)])
        self.create_polygon([15, h, *coords, coords[-2], h], fill="#F0F7FF", outline="")
        self.create_line(coords, fill=C_ACCENT, width=2, smooth=True)
        lx, ly = coords[-2], coords[-1]
        self.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill=C_ACCENT, outline=C_SURFACE)
        minutes = max(1, round((samples[-1][0] - samples[0][0]) / 60))
        self.create_text(
            20, 10, text=f"Max: {max_v:,} · 最近 {minutes} 分钟", anchor="w", font=("Segoe UI", 7), fill=C_TEXT_MUTED
        )
        self.create_text(w - 20, h - 8, text=f"当前: {values[-1]:,}", anchor="e", font=("Segoe UI", 7, "bold"), fill=C_ACCENT)


def fluent_button(parent, text: str, command, primary: bool = False, small: bool = False) -> tk.Button:
    kw = {
        "text": text,
        "command": command,
        "relief": "flat",
        "bd": 0,
        "cursor": "hand2",
        "font": ("Segoe UI", 8 if small else 9, "bold" if primary or not small else "normal"),
        "padx": 8 if small else 10,
        "pady": 2 if small else 5,
    }
    if primary:
        kw.update(bg=C_ACCENT, fg="#FFFFFF", activebackground=C_ACCENT_HOVER, activeforeground="#FFFFFF")
    else:
        kw.update(
            bg=C_SURFACE,
            fg=C_TEXT_PRIMARY,
            activebackground=C_SURFACE_HOVER,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        )
    return tk.Button(parent, **kw)


# ==============================================================================
# 主窗体
# ==============================================================================
class App:
    def __init__(self, root: Tk, base: Path):
        self.root = root
        self.reader = core.DriveReader(base)
        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.root.geometry(f"{int(1180 * scale)}x{int(820 * scale)}")
        self.root.minsize(int(980 * scale), int(680 * scale))
        self.root.configure(bg=C_BG)
        self._setup_fluent_styles()

        self._results: queue.Queue = queue.Queue()
        self._busy = False
        self._force_pending = False
        self._last_metrics: dict | None = None
        self._tree_sigs: dict[str, int] = {}
        self._visible_errors: list[dict] = []

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._tick)
        self.root.after(POLL_MS, self._poll_results)

    # ------------------------------------------------------------------ 样式
    def _setup_fluent_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Fluent.Treeview",
            background=C_SURFACE,
            foreground=C_TEXT_PRIMARY,
            fieldbackground=C_SURFACE,
            borderwidth=0,
            font=("Segoe UI", 9),
            rowheight=28,
        )
        style.configure(
            "Fluent.Treeview.Heading",
            background="#F7F7F7",
            foreground=C_TEXT_SECONDARY,
            borderwidth=0,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padding=(6, 6),
        )
        style.map("Fluent.Treeview", background=[("selected", C_ACCENT_BG)], foreground=[("selected", C_TEXT_PRIMARY)])
        style.map("Fluent.Treeview.Heading", background=[("active", "#EFEFEF")])
        style.configure("Fluent.TNotebook", background=C_BG, borderwidth=0)
        style.configure(
            "Fluent.TNotebook.Tab",
            background=C_BG,
            foreground=C_TEXT_SECONDARY,
            padding=(16, 7),
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
        )
        style.map("Fluent.TNotebook.Tab", background=[("selected", C_SURFACE)], foreground=[("selected", C_ACCENT)])
        style.configure("Fluent.TCheckbutton", background=C_BG, foreground=C_TEXT_SECONDARY, font=("Segoe UI", 9))

    # ------------------------------------------------------------------ 布局
    def _build_ui(self):
        header = tk.Frame(self.root, bg=C_BG, padx=24, pady=16)
        header.pack(fill="x")
        h_left = tk.Frame(header, bg=C_BG)
        h_left.pack(side="left")
        title_row = tk.Frame(h_left, bg=C_BG)
        title_row.pack(anchor="w")
        tk.Label(title_row, text=APP_TITLE, font=(FONT_DISPLAY, 18, "bold"), fg=C_TEXT_PRIMARY, bg=C_BG).pack(side="left")
        self.status_canvas = tk.Canvas(title_row, width=10, height=10, bg=C_BG, highlightthickness=0)
        self.status_canvas.pack(side="left", padx=(12, 4))
        self.status_dot = self.status_canvas.create_oval(1, 1, 9, 9, fill=C_STATUS_GRAY, outline="")
        self.status_text_var = StringVar(value="正在启动")
        self.lbl_status = tk.Label(
            title_row, textvariable=self.status_text_var, font=(FONT_TEXT, 10, "bold"), fg=C_STATUS_GRAY, bg=C_BG
        )
        self.lbl_status.pack(side="left")
        tk.Label(h_left, text="事件工作池与收敛状态监测 · 严格只读模式", font=("Segoe UI", 9), fg=C_TEXT_SECONDARY, bg=C_BG).pack(
            anchor="w", pady=(3, 0)
        )

        h_right = tk.Frame(header, bg=C_BG)
        h_right.pack(side="right")
        self.last_act_var = StringVar(value="最后活动: --")
        tk.Label(h_right, textvariable=self.last_act_var, font=("Segoe UI", 9), fg=C_TEXT_SECONDARY, bg=C_BG).pack(
            side="left", padx=(0, 10)
        )
        fluent_button(h_right, "🛠️ 诊断与排查报告", self.open_diagnostics).pack(side="left", padx=4)
        fluent_button(h_right, "📥 导出异常CSV", self.export_csv).pack(side="left", padx=4)
        fluent_button(h_right, "🔄 立即刷新", self.refresh_now, primary=True).pack(side="left", padx=(4, 0))

        # 4 大指标卡片
        cards = tk.Frame(self.root, bg=C_BG, padx=20, pady=2)
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
            "5分钟完成 vs 新增",
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
            tooltip="排除 -8192 选择性同步过滤、临时 / 锁文件，以及之后已成功同步的记录后，仍未同步的文件数。",
        )
        for i, c in enumerate([self.c_remaining, self.c_net_speed, self.c_eta, self.c_unsynced]):
            c.grid(row=0, column=i, padx=4, sticky="nsew")

        # 趋势 + 当前文件
        mid = tk.Frame(self.root, bg=C_BG, padx=20, pady=6)
        mid.pack(fill="x")
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)
        trend = tk.Frame(mid, bg=C_SURFACE, highlightbackground=C_BORDER, highlightthickness=1, padx=14, pady=10)
        trend.grid(row=0, column=0, padx=4, sticky="nsew")
        t_header = tk.Frame(trend, bg=C_SURFACE)
        t_header.pack(fill="x")
        tk.Label(
            t_header,
            text="在途事件池趋势 (Outstanding Events)",
            font=(FONT_TEXT, 9, "bold"),
            fg=C_TEXT_SECONDARY,
            bg=C_SURFACE,
        ).pack(side="left")
        self.trend_delta_var = StringVar(value="计算中…")
        tk.Label(t_header, textvariable=self.trend_delta_var, font=("Segoe UI", 9, "bold"), fg=C_ACCENT, bg=C_SURFACE).pack(
            side="right"
        )
        self.sparkline = QueueSparkline(trend, height=48)
        self.sparkline.pack(fill="both", expand=True, pady=(4, 4))
        self.throughput_var = StringVar(value="流式事件吞吐: 采样中…")
        tk.Label(trend, textvariable=self.throughput_var, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_SURFACE).pack(
            anchor="w", pady=(2, 0)
        )

        curr = tk.Frame(mid, bg=C_SURFACE, highlightbackground=C_BORDER, highlightthickness=1, padx=14, pady=10)
        curr.grid(row=0, column=1, padx=4, sticky="nsew")
        c_top = tk.Frame(curr, bg=C_SURFACE)
        c_top.pack(fill="x")
        self.curr_action_var = StringVar(value="正在处理")
        tk.Label(c_top, textvariable=self.curr_action_var, font=("Segoe UI", 9, "bold"), fg=C_ACCENT, bg=C_SURFACE).pack(
            side="left"
        )
        fluent_button(c_top, "在 Explorer 中定位", self.reveal_current, small=True).pack(side="right")
        self.curr_file_var = StringVar(value="等待抓取文件级活动…")
        tk.Label(curr, textvariable=self.curr_file_var, font=(FONT_DISPLAY, 11, "bold"), fg=C_TEXT_PRIMARY, bg=C_SURFACE).pack(
            anchor="w", pady=(4, 2)
        )
        self._current_path = ""
        self.curr_path_var = StringVar(value="")
        tk.Label(
            curr,
            textvariable=self.curr_path_var,
            font=("Segoe UI", 8),
            fg=C_TEXT_MUTED,
            bg=C_SURFACE,
            wraplength=400,
            justify="left",
        ).pack(anchor="w")

        # 表格
        table_frame = tk.Frame(self.root, bg=C_BG, padx=24, pady=4)
        table_frame.pack(fill="both", expand=True)
        search_row = tk.Frame(table_frame, bg=C_BG)
        search_row.pack(fill="x", pady=(0, 4))
        tk.Label(search_row, text="🔍 筛选记录:", font=("Segoe UI", 9), fg=C_TEXT_SECONDARY, bg=C_BG).pack(
            side="left", padx=(0, 6)
        )
        self.search_var = StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        tk.Entry(
            search_row,
            textvariable=self.search_var,
            font=("Segoe UI", 9),
            bg=C_SURFACE,
            fg=C_TEXT_PRIMARY,
            insertbackground=C_TEXT_PRIMARY,
            relief="flat",
            highlightbackground=C_BORDER,
            highlightthickness=1,
        ).pack(side="left", fill="x", expand=True, ipady=3)
        self.show_temp_var = tk.BooleanVar(value=False)
        self.show_recovered_var = tk.BooleanVar(value=False)
        for text, var in (("显示临时文件", self.show_temp_var), ("显示已恢复", self.show_recovered_var)):
            ttk.Checkbutton(search_row, text=text, variable=var, style="Fluent.TCheckbutton", command=self._apply_filter).pack(
                side="left", padx=(10, 0)
            )

        self.notebook = ttk.Notebook(table_frame, style="Fluent.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        recent_panel = tk.Frame(self.notebook, bg=C_SURFACE)
        self.err_panel = tk.Frame(self.notebook, bg=C_SURFACE)
        self.notebook.add(recent_panel, text="最近活动")
        self.notebook.add(self.err_panel, text="同步异常 (需关注)")
        self.recent_tree = self._build_tree(
            recent_panel,
            [("time", "时间", 140), ("action", "动作", 100), ("path", "文件完整路径 (双击可在 Explorer 中定位)", 680)],
        )
        self.err_tree = self._build_tree(
            self.err_panel,
            [
                ("time", "时间", 140),
                ("status", "状态", 70),
                ("raw_code", "错误代码", 80),
                ("reason", "异常原因说明", 260),
                ("path", "文件路径", 480),
            ],
        )
        for t in (self.recent_tree, self.err_tree):
            t.bind("<Double-1>", lambda e, tree=t: self.reveal_selected(tree))
            self._bind_context_menu(t)

        status_bar = tk.Frame(self.root, bg=C_BG, padx=24, pady=6)
        status_bar.pack(fill="x")
        self.sb_left = StringVar(value="Drive: 检测中 | DB: 只读 | Log: 监听中")
        tk.Label(status_bar, textvariable=self.sb_left, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_BG).pack(side="left")
        self.sb_right = StringVar(value="更新时间: --")
        tk.Label(status_bar, textvariable=self.sb_right, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_BG).pack(side="right")

    def _build_tree(self, parent, col_defs):
        cols = [c[0] for c in col_defs]
        tree = ttk.Treeview(parent, columns=cols, show="headings", style="Fluent.Treeview")
        for col_id, heading, width in col_defs:
            tree.heading(col_id, text=heading)
            tree.column(col_id, width=width, stretch=(col_id == "path"))
        y = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        x = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        return tree

    def _bind_context_menu(self, tree):
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=C_SURFACE,
            fg=C_TEXT_PRIMARY,
            activebackground=C_ACCENT_BG,
            activeforeground=C_TEXT_PRIMARY,
            bd=1,
            relief="solid",
        )
        menu.add_command(label="在资源管理器中定位", command=lambda: self.reveal_selected(tree))
        menu.add_command(label="复制文件绝对路径", command=lambda: self.copy_selected_path(tree))

        def popup(event):
            item = tree.identify_row(event.y)
            if item:
                tree.selection_set(item)
                menu.post(event.x_root, event.y_root)

        tree.bind("<Button-3>", popup)

    # ------------------------------------------------------------------ 刷新调度
    # 只在主线程操作 Tk：工作线程只负责读数据，结果经队列交回主线程。
    def _tick(self):
        self._start_worker(force=False)
        self.root.after(REFRESH_MS, self._tick)

    def refresh_now(self):
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
        self.root.destroy()

    # ------------------------------------------------------------------ 渲染
    def _apply_metrics(self, m: dict):
        self._last_metrics = m
        stall, state = m["stall_status"], m["convergence_state"]
        if stall == "offline":
            dot_c = C_STATUS_GRAY
        elif stall == "stalled":
            dot_c = C_STATUS_RED
        elif stall == "warning" or state == core.ST_GROWING:
            dot_c = C_STATUS_ORANGE
        elif stall == "finished" or state == core.ST_CONVERGING:
            dot_c = C_STATUS_GREEN
        else:
            dot_c = C_STATUS_BLUE
        self.status_canvas.itemconfig(self.status_dot, fill=dot_c)
        self.status_text_var.set(m["stall_desc"])
        self.lbl_status.configure(fg=dot_c)
        self.last_act_var.set(f"最后活动: {core.format_age(m['idle_secs'])}前")

        self.c_remaining.set_value(m["rem_val_str"], "Push / Done 实时配对", C_ACCENT)
        net = m["net_drain_rate_5m"]
        net_col = C_STATUS_GREEN if net > 0 else (C_STATUS_ORANGE if net < 0 else C_TEXT_PRIMARY)
        if m["net_val_str"] in ("采样中", "空闲"):
            net_col = C_TEXT_MUTED
        self.c_net_speed.set_value(m["net_val_str"], m["net_sub_str"], net_col)
        eta_col = C_ACCENT if ("小时" in m["eta_str"] or "分钟" in m["eta_str"] or "天" in m["eta_str"]) else C_TEXT_MUTED
        if m["eta_str"] == "已同步完成":
            eta_col = C_STATUS_GREEN
        self.c_eta.set_value(m["eta_str"], m["eta_sub_desc"], eta_col)
        a, t, r = m["actionable_errors_count"], m["office_temp_count"], m["recovered_count"]
        self.c_unsynced.set_value(str(a), f"真实 {a} · 临时 {t} · 已恢复 {r}", C_STATUS_RED if a else C_TEXT_PRIMARY)
        self.notebook.tab(self.err_panel, text=f"同步异常 ({a})")

        self.trend_delta_var.set(f"收敛状态: {m['convergence_state']}")
        self.sparkline.draw_data(m["remaining_samples"])
        sign = "+" if net >= 0 else ""
        self.throughput_var.set(
            f"5m流速: 完成 {m['done_rate_5m']}/m · 新增 {m['push_rate_5m']}/m (净吞吐: {sign}{net}/m)"
            f"  |  1m: 完成 {m['done_rate_1m']}/m · 新增 {m['push_rate_1m']}/m"
        )

        cfile = m["current_file"]
        self.curr_action_var.set(m["active_label"])
        self._current_path = cfile
        if cfile:
            self.curr_file_var.set(Path(cfile.replace("\\", "/")).name or cfile)
            self.curr_path_var.set(cfile)
        else:
            self.curr_file_var.set("等待捕获文件活动…")
            self.curr_path_var.set("正在进行目录比对或空闲中")

        self._apply_filter()
        sb = f"Drive: {m['process_state']} | Session {m['session_id']} | DB: history.sqlite (只读)"
        if m.get("history_error"):
            sb += f" | ⚠ {m['history_error']}"
        self.sb_left.set(sb)
        self.sb_right.set(f"更新时间: {datetime.now().strftime('%H:%M:%S')}")

    def _fill_tree(self, name: str, tree: ttk.Treeview, rows: list[tuple]):
        """仅在数据变化时重建表格，并保留选中行与滚动位置。"""
        sig = hash(tuple(rows))
        if self._tree_sigs.get(name) == sig:
            return
        self._tree_sigs[name] = sig
        # Tk 可能把数字样式的值转回 int，统一按字符串比较
        selected = {tuple(str(v) for v in tree.item(i, "values")) for i in tree.selection()}
        top = tree.yview()[0]
        tree.delete(*tree.get_children())
        reselect = []
        for row in rows:
            iid = tree.insert("", "end", values=row)
            if tuple(str(v) for v in row) in selected:
                reselect.append(iid)
        if reselect:
            tree.selection_set(reselect)
        tree.yview_moveto(top)

    def _apply_filter(self):
        m = self._last_metrics
        if not m:
            return
        kw = self.search_var.get().strip().lower()

        recent = [r for r in m["recent_rows"] if not kw or kw in r["path"].lower() or kw in r["action"].lower()]
        self._fill_tree("recent", self.recent_tree, [(r["time"], r["action"], r["path"]) for r in recent])

        errs = list(m["actionable_errors"])
        if self.show_temp_var.get():
            errs += m["temp_errors"]
        if self.show_recovered_var.get():
            errs += m["recovered_errors"]
        if kw:
            errs = [e for e in errs if kw in e["path"].lower() or kw in e["reason"].lower() or kw in e["raw_code"]]
        self._visible_errors = errs
        self._fill_tree("err", self.err_tree, [(e["time"], e["status"], e["raw_code"], e["reason"], e["path"]) for e in errs])

    # ------------------------------------------------------------------ 交互
    def copy_selected_path(self, tree):
        sel = tree.selection()
        if not sel:
            return
        p = str(tree.item(sel[0], "values")[-1]).strip()
        if p:
            self.root.clipboard_clear()
            self.root.clipboard_append(p)

    def reveal_current(self):
        if self._current_path:
            self._reveal_path(self._current_path)

    def reveal_selected(self, tree):
        sel = tree.selection()
        if not sel:
            return
        p = str(tree.item(sel[0], "values")[-1]).strip()
        if p:
            self._reveal_path(p)

    def _reveal_path(self, p: str):
        if os.name != "nt":
            messagebox.showinfo(APP_TITLE, f"仅 Windows 支持在资源管理器中定位：\n{p}")
            return
        try:
            path = Path(p.replace("/", "\\"))
            if path.is_file():
                subprocess.Popen(f'explorer /select,"{path}"')
            elif path.is_dir():
                subprocess.Popen(f'explorer "{path}"')
            elif path.parent.exists() and str(path.parent) not in (".", "\\"):
                subprocess.Popen(f'explorer "{path.parent}"')
            else:
                messagebox.showinfo(
                    APP_TITLE, f"该文件当前在本机物理路径不存在：\n{p}\n\n可能是 NAS 端路径、远端同步历史记录或已被清理。"
                )
        except OSError as e:
            logger.error(f"Reveal error: {e}")
            messagebox.showerror(APP_TITLE, f"定位失败：{e}")

    def export_csv(self):
        rows = self._visible_errors
        if not rows:
            messagebox.showinfo(APP_TITLE, "当前列表中没有同步异常记录。")
            return
        fn = filedialog.asksaveasfilename(
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
                w.writerow(["时间", "状态", "错误代码", "原因说明", "文件路径"])
                for r in rows:
                    w.writerow([r["time"], r["status"], r["raw_code"], r["reason"], r["path"]])
            messagebox.showinfo(APP_TITLE, f"已导出 {len(rows)} 条记录（与当前列表筛选一致）至：\n{fn}")
        except OSError as e:
            logger.error(f"CSV export error: {e}")
            messagebox.showerror(APP_TITLE, f"导出失败：{e}")

    def open_diagnostics(self):
        dlg = Toplevel(self.root)
        dlg.title(f"{APP_TITLE} - 深度诊断与排查报告")
        dlg.geometry("760x700")
        dlg.configure(bg=C_SURFACE)
        report = core.build_diagnostics_report(self.reader, self._last_metrics)

        btn_box = tk.Frame(dlg, bg=C_SURFACE, padx=12, pady=8)
        btn_box.pack(side="bottom", fill="x")
        txt = tk.Text(dlg, font=("Consolas", 9), bg=C_SURFACE, fg=C_TEXT_PRIMARY, padx=12, pady=12, relief="flat", wrap="none")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", report)
        txt.configure(state="disabled")

        def copy_report():
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
            messagebox.showinfo("诊断报告", "诊断报告已复制到剪贴板！", parent=dlg)

        fluent_button(btn_box, "📋 复制诊断报告", copy_report, primary=True).pack(side="right")


# ==============================================================================
# 入口
# ==============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=f"{APP_TITLE} {APP_VERSION}（严格只读）")
    ap.add_argument("--base", help="群晖 Drive 本地数据目录，默认 %%LOCALAPPDATA%%\\SynologyDrive")
    ap.add_argument("--json", action="store_true", help="不开窗口，输出一次 JSON 状态快照")
    ap.add_argument("--sample", type=float, default=0.0, help="--json 模式下先采样 N 秒再输出（速率需要采样才有意义）")
    ap.add_argument("--version", action="version", version=f"{APP_TITLE} {APP_VERSION}")
    return ap.parse_args(argv)


def run_cli(base: Path, sample: float) -> int:
    reader = core.DriveReader(base, persist=False)
    m = reader.read_metrics(force_history=True)
    if sample > 0:
        time.sleep(sample)
        m = reader.read_metrics(force_history=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(json.dumps(core.snapshot(reader, m), ensure_ascii=False, indent=2))
    return 0


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
        return run_cli(base, args.sample)

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
