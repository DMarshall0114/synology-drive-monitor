"""用合成数据启动真实界面并截图（仅 Windows，需要 Pillow）。

    python tools/make_screenshots.py [输出目录]

数据全部是脚本现造的演示数据，不读取本机真实的群晖目录；用于 README 配图和界面回归检查。
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_ROOT = "D:\\SynologyDrive\\工程项目"
FOLDERS = ["01-施工图\\建筑", "01-施工图\\结构", "02-设计变更", "03-会议纪要", "04-往来函件"]
NAMES = [
    "A-101 总平面图.dwg",
    "S-203 基础平面布置图.dwg",
    "变更通知单-017.docx",
    "周例会纪要 11-06.docx",
    "工程量清单.xlsx",
    "复函.pdf",
]


def demo_path(i: int) -> str:
    return f"{DEMO_ROOT}\\{FOLDERS[i % len(FOLDERS)]}\\{i:03d}-{NAMES[i % len(NAMES)]}"


def build_demo_base(tmp: Path) -> Path:
    base = tmp / "SynologyDrive"
    (base / "log").mkdir(parents=True)
    (base / "data" / "db").mkdir(parents=True)
    (base / "data" / "session" / "3" / "db").mkdir(parents=True)
    (base / "log" / "daemon.log").write_text("", encoding="utf-8")
    con = sqlite3.connect(base / "data" / "session" / "3" / "db" / "event-db.sqlite")
    con.execute("CREATE TABLE event_table (id INTEGER)")
    con.executemany("INSERT INTO event_table VALUES (?)", [(i,) for i in range(1284)])
    con.commit()
    con.close()

    now = int(time.time())
    rows: list[tuple] = [(demo_path(i), now - i * 37, 0, 0, (1, 2, 2, 17, 8, 2)[i % 6]) for i in range(120)]
    long_name = "关于车辆段综合楼施工图第三次会审意见的回复及后续修改安排的说明（含附件一至附件九汇总版）-最终版-修订稿"
    problems = [
        (f"{DEMO_ROOT}\\02-设计变更\\变更通知单-021.docx", -2048),
        (f"{DEMO_ROOT}\\05-造价\\工程量清单 v3.xlsx", -2048),
        (f"{DEMO_ROOT}\\03-会议纪要\\{long_name}\\{long_name}\\{long_name}.docx", -2048),
        (f"{DEMO_ROOT}\\04-往来函件\\回复: 关于进度?.pdf", -1024),
        (f"{DEMO_ROOT}\\01-施工图\\建筑\\A-105 立面图_OFFICE-PC_Conflict.dwg", -2048),
        (f"{DEMO_ROOT}\\01-施工图\\结构\\S-210 梁配筋图.dwg", None),
        (f"{DEMO_ROOT}\\02-设计变更\\~$变更通知单-021.docx", -2048),
        (f"{DEMO_ROOT}\\05-造价\\.~lock.工程量清单 v3.xlsx#", -2048),
        (f"{DEMO_ROOT}\\01-施工图\\建筑\\A-102 一层平面图.dwg", -2048),
        (f"{DEMO_ROOT}\\07-归档\\2025 竣工资料.iso", -8192),
    ]
    rows += [(p, now - 400 - i * 211, 1, code, 34) for i, (p, code) in enumerate(problems)]
    rows.append((f"{DEMO_ROOT}\\01-施工图\\建筑\\A-102 一层平面图.dwg", now - 60, 0, 0, 2))  # 之后同步成功 → 已恢复
    con = sqlite3.connect(base / "data" / "db" / "history.sqlite")
    con.execute(
        "CREATE TABLE history_table (path TEXT, update_time INTEGER, is_not_synced INTEGER, not_synced_reason INTEGER, action INTEGER)"
    )
    con.executemany("INSERT INTO history_table VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()

    # 让大部分"异常文件"在本地真实存在，这样只有确实缺失的那条会得到"本地已不存在"提示
    for p, _code in problems[:2] + problems[4:6]:
        try:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_text("demo", encoding="utf-8")
        except OSError:
            pass
    return base


def simulate(app, base: Path):
    """用假时钟回放 15 分钟的同步过程：先积压、后收敛。"""
    log = base / "log" / "daemon.log"
    reader = app.reader
    t = [time.time() - 900]
    reader._clock = lambda: t[0]
    reader.monitor_start_time = reader.last_activity_time = t[0]
    reader.read_metrics()

    def line(kind: str, i: int) -> str:
        head = "PushEvent = Event<LocalModify>" if kind == "push" else "DoneEvent: Event<LocalModify>"
        return f"2026-11-10T10:00:00 (4312:9120) [INFO] {head} sync_id: 2 path: '{demo_path(i)}'\n"

    pushed = finished = 0
    for step in range(450):
        t[0] += 2
        n_push, n_done = (6, 2) if step < 150 else (1, 3 if step % 2 else 2)
        out = []
        for _ in range(n_push):
            out.append(line("push", pushed))
            pushed += 1
        for _ in range(n_done):
            if finished < pushed:
                out.append(line("done", finished))
                finished += 1
        if step % 9 == 0:
            out.append(f"2026-11-10T10:00:00 (4312:9120) [INFO] Upload file '{demo_path(finished)}' size=1048576\n")
        if step in (40, 41, 42, 200, 330):
            out.append(
                f"2026-11-10T10:00:00 (4312:9120) [ERROR] proto-client.cpp(412): Failed to connect to server 'nas.example.com' port 6690, code {step}\n"
            )
        if step % 60 == 5:
            out.append(
                f"2026-11-10T10:00:00 (4312:9120) [WARNING] worker.cpp(88): Slow response from server, took {900 + step} ms\n"
            )
        if step == 300:
            out.append(
                "2026-11-10T10:00:00 (4312:9120) [ERROR] file-op.cpp(77): Failed to open 'D:\\SynologyDrive\\工程项目\\05-造价\\工程量清单 v3.xlsx': sharing violation (32)\n"
            )
        with open(log, "a", encoding="utf-8", newline="") as f:
            f.write("".join(out))
        reader.read_metrics()
    reader._clock = time.time


def window_bounds(win) -> tuple[int, int, int, int]:
    """窗口的可见外框（含标题栏，不含透明的缩放边）。"""
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def main() -> int:
    if sys.platform != "win32":
        print("截图脚本只能在 Windows 上运行。")
        return 1
    from PIL import ImageGrab

    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sdm-demo-"))
    os.environ["LOCALAPPDATA"] = str(tmp)  # 本程序的设置 / 状态文件也写进临时目录

    import tkinter as tk

    import synology_drive_monitor as gui

    gui.core.process_state = lambda force=False: "运行中"
    gui.enable_dpi_awareness()
    base = build_demo_base(tmp)
    root = tk.Tk()
    app = gui.App(root, base)
    root.geometry("1280x900+80+40")
    root.attributes("-topmost", True)
    simulate(app, base)

    def refresh():
        app._apply_metrics(app.reader.read_metrics(force_history=True), notify=False)

    def shot(name: str, win=root):
        for _ in range(6):
            root.update()
            time.sleep(0.15)
        ImageGrab.grab(bbox=window_bounds(win), all_screens=True).save(out_dir / name)
        print("saved", name)

    def tab(name: str):
        app.notebook.select(app.panels[name])

    refresh()
    shot("01-overview-light.png")

    tab("err")
    app.err_tree.selection_set(app.err_tree.get_children()[0])
    shot("02-errors-light.png")

    for chip in app.chips:
        chip._toggle()
    app._sort_by("err", "status")
    shot("03-errors-with-temp-and-recovered-sorted.png")

    app.search_var.set("dwg")
    shot("04-errors-filtered.png")
    app.search_var.set("")
    app._sort_by("err", "status")
    app._sort_by("err", "status")
    for chip in app.chips:
        chip._toggle()

    tab("alerts")
    shot("05-log-alerts-light.png")

    try:
        card = app.c_unsynced
        card.event_generate("<Enter>", rootx=card.winfo_rootx() + 40, rooty=card.winfo_rooty() + 40)
        shot("06-card-tooltip.png")
        card.event_generate("<Leave>")
    except tk.TclError as e:
        print("tooltip shot skipped:", e)

    app.copy_summary()
    tab("recent")
    shot("07-toast-after-copy.png")

    app.theme_var.set("dark")
    app._apply_theme()
    refresh()
    shot("08-overview-dark.png")
    tab("err")
    shot("09-errors-dark.png")
    tab("alerts")
    shot("10-log-alerts-dark.png")

    app.open_diagnostics()
    dlg = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    dlg.geometry("+160+90")
    dlg.attributes("-topmost", True)
    shot("11-diagnostics-dark.png", dlg)

    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
