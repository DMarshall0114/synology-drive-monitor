# Synology Drive Monitor 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6.svg)](https://microsoft.com)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![Style: Fluent](https://img.shields.io/badge/Design-Windows%2011%20Fluent-0067C0.svg)]()

A lightweight, non-intrusive, read-only desktop monitoring utility for **Synology Drive Client** on Windows. Built with a modern **Windows 11 Fluent** UI (light & dark).

专为 Windows 群晖客户端（Synology Drive Client）量身打造的高性能、低侵入、**严格只读**同步状态监控工具。采用现代 **Windows 11 Fluent 风格（浅色 / 深色）**，提供精准的流式事件吞吐率、在途工作池追踪与同步异常去重分类。

---

## ✨ Features (核心特性)

- 🔒 **100% Strict Read-Only Safety (绝对只读安全)**
  - Accesses Synology SQLite databases with `mode=ro` and `PRAGMA query_only=ON`.
  - Zero risk of table locking, record modification, or synchronization interference.
- ⚡ **Dual-Track Stream Throughput (双轨流式吞吐监控)**
  - Incrementally parses `daemon.log` to track task generation (`PushEvent`) and task completion (`DoneEvent`).
  - Computes real-time 1-minute and 5-minute rolling completion rates and net drain rates (`Done - Push`).
- 🎯 **Outstanding Event Tracker (在途事件工作池)**
  - Pairs incoming Push events with completed Done events in memory.
  - Transparently indicates in-flight items without fabricating non-existent total countdowns.
- 🚦 **Convergence State Machine (数据驱动收敛状态)**
  - Automatically identifies synchronization lifecycle stages: `流式处理接近平衡` (Balancing), `正在收敛` (Converging), `正在处理尾部任务` (Finishing tail), and `同步已收敛` (Converged).
- 🛡️ **Smart Error Deduplication & Triage (同步异常去重与分级)**
  - Parses `history.sqlite`, excludes normal selective sync rule codes (`-8192`), and automatically separates harmless Office/WPS/LibreOffice lock files (`~$*`, `.~lock.*#`) from true actionable errors.
  - Files that failed once but **synced successfully later** are classified as *recovered* instead of being counted forever. (之后已成功同步的文件归为"已恢复"，不再长期计入异常)
  - Supports one-click CSV export and double-click to locate files in Windows Explorer.
- 🩺 **Likely-Cause Hints (异常"可能原因"提示)**
  - For every actionable error the tool inspects the path itself and flags common culprits: path ≥ 260 chars, file name over 143 / 255 bytes, characters Windows forbids, trailing space or dot, reserved names (`CON`, `NUL`…), conflict copies, or a local file that no longer exists.
  - 这是本工具按路径做的推断，不是群晖给出的结论；界面与 CSV 中都单独成列并标明。
- 📜 **daemon.log Alerts (日志告警)**
  - Extracts `[ERROR]` / `[WARNING]` lines from `daemon.log`, merges lines that differ only in file names or numbers, and shows count + last seen time. 连接 / 认证类问题不会进 history.sqlite，在这里能看到。
- 🗣️ **Plain-Language Summary (一句话结论)**
  - One sentence under the title says what is going on and whether anything needs attention; the same text is in the JSON snapshot.
- 🔔 **State-Change Alerts (状态变化提醒)**
  - Flashes the taskbar button when sync stalls, new errors appear, or sync completes (can be switched off).
- 🤖 **Headless JSON Mode (无界面 JSON 快照)**
  - `--json [--sample N]` prints a machine-readable status snapshot, handy for scripts, scheduled tasks or agents.
  - `--fail-on stalled,errors` makes the exit code `1` when a condition is hit.
- 🎨 **Windows 11 Fluent Design, Light & Dark (浅色 / 深色主题)**
  - Follows the system app mode or can be forced; High-DPI aware, sortable tables, per-minute throughput chart, keyboard shortcuts, remembered window size and options.

---

## 📸 Screenshots (界面预览)

```text
+-------------------------------------------------------------------------------------------+
| Synology Drive Monitor  [正在收敛 · 队列递减]   最后活动: 2 秒前 [诊断报告][导出][刷新][更多▾] |
+-------------------------------------------------------------------------------------------+
| ▌队列正在变短（在途 95），预计约 12 分钟后完成。 另有 3 个文件未同步，需要关注。              |
+----------------------+----------------------+----------------------+----------------------+
|      当前在途        |      净消化速度      |    预计完成 (ETA)    |      同步异常        |
|         95           |      8.0 / min       |     约 12 分钟       |         3            |
|  Push/Done 实时配对  |  完成 20 · 新增 12   |  基于净消化速度      | 需关注3·临时13·恢复4 |
+----------------------+----------------------+----------------------+----------------------+
| 在途事件池趋势            正在收敛             | 正在处理 · 下载中                          |
| [=============== 面积折线 ================]    | 当前文件.docx                              |
| 每分钟吞吐   完成 ↑ ▂▃▅▆▇▆▅  新增 ↓ ▂▂▃▂▁    | C:\...\当前文件.docx                       |
+------------------------------------------------+------------------------------------------+
| 筛选 [__________________ ✕]  共 200 条   [临时文件] [已恢复]                               |
| 最近活动 | 同步异常 (3) | 日志告警 (2)                                                    |
+-------------------------------------------------------------------------------------------+
```

---

## 🚀 Quick Start (快速开始)

### Option 1: Run from Source (源码运行)

Ensure you have Python 3.10+ installed on Windows:

```bash
# Clone the repository
git clone https://github.com/DMarshall0114/synology-drive-monitor.git
cd synology-drive-monitor

# No third-party runtime dependencies (标准库即可运行)
# Launch monitor (GUI windowed mode)
python synology_drive_monitor.py

# Custom Synology Drive data directory (自定义群晖数据目录)
python synology_drive_monitor.py --base "D:\Portable\SynologyDrive"
```

### Option 1b: Headless JSON snapshot (无界面模式)

```bash
# 立即输出一次快照（异常统计、在途事件、进程状态）
python synology_drive_monitor.py --json

# 先采样 60 秒再输出（Push/Done 速率与收敛状态需要采样才有意义）
python synology_drive_monitor.py --json --sample 60
```

```bash
# 给计划任务 / agent 用：停滞或有需关注的异常时退出码为 1
python synology_drive_monitor.py --json --sample 60 --fail-on stalled,errors
```

Exit code `0` = OK, `1` = a `--fail-on` condition was hit (`stalled` / `warning` / `offline` / `errors`, listed in `failed_conditions`), `2` = Synology Drive data directory not found. 该模式只读、不写在途事件池状态文件；打包出的 `--noconsole` exe 没有控制台，JSON 模式请用源码运行。

### Option 2: Build Standalone EXE (打包为单文件绿色版)

You can build a self-contained `.exe` with zero external dependencies (no Python installation required on target PCs):

```cmd
build.bat
```

Or manually:
```bash
pyinstaller --noconsole --onefile --name SynologyDriveMonitor synology_drive_monitor.py
```
The output executable will be generated at `dist/SynologyDriveMonitor.exe`.

---

## 🛠️ Diagnostics & Report (诊断面板)

Click **`诊断报告`** on the top right (or press `Ctrl+D`) to inspect:
- Process status, monitor uptime, active session, event-db and daemon.log paths
- Row counts of every table in the active session's `event-db.sqlite` (read-only)
- In-flight outstanding event pool depth, breakdown by `sync_id`, oldest pending age and expired count
- PushEvent / DoneEvent stream rates (1m & 5m)
- Convergence state & sanity check verdicts
- Error breakdown by reason and by folder, plus the most recent `daemon.log` alert groups
- **`复制报告`** / **`另存为…`** / **`重新生成`** buttons.

### Keyboard shortcuts (快捷键)

| Key | Action |
| --- | --- |
| `F5` | 立即刷新 |
| `Ctrl+F` / `Esc` | 聚焦筛选框 / 清空筛选 |
| `Ctrl+E` | 导出当前异常列表为 CSV |
| `Ctrl+D` | 打开诊断报告 |
| `Enter` / 双击 | 在资源管理器中定位选中文件 |
| `Ctrl+C` | 复制选中行的路径（日志告警页为日志内容） |

Click a column header to sort (again to reverse, a third time to restore the default order). Theme, always-on-top and taskbar alerts live under **`更多 ▾`**.

---

## 📖 Data Source Notice (数据源说明)

- This tool operates strictly on local disk data (`%LOCALAPPDATA%\SynologyDrive`). Its own debug log and state file live in `%LOCALAPPDATA%\SynologyDriveMonitor`.
- Synology Drive does not expose a static "remaining tasks" counter; all stream rates and convergence indicators are derived from local log observation and event pairing.
- On startup the tail of `daemon.log` (400 KB) is replayed to rebuild the outstanding pool, but those historical events are **not** counted in the Push/Done rates. 启动回读的历史事件只用于重建在途池，不计入速率。
- Outstanding events not paired with a `DoneEvent` within 2 hours expire automatically, so a missed log line cannot pin the pool forever.
- The "可能原因" column and the `[ERROR]` / `[WARNING]` extraction are heuristics based on paths and observed log format; if Synology changes the log format the alerts tab may simply stay empty. "可能原因"与日志告警均为启发式，仅供排查参考。
- UI preferences are stored in `%LOCALAPPDATA%\SynologyDriveMonitor\settings.json`; nothing is ever written to the Synology Drive directory.
- Action codes and `not_synced_reason` codes other than `-8192` are empirical mappings, not official Synology documentation. 除 -8192 外的错误 / 动作代码含义为经验映射。

---

## 🧪 Development (开发)

```bash
pip install -r requirements-dev.txt
python -m pytest          # 核心逻辑测试使用合成的 daemon.log / history.sqlite，不需要真实客户端
ruff check . && ruff format --check .
```

- `drive_core.py` — read-only data collection and metrics (no tkinter, fully unit-tested)
- `synology_drive_monitor.py` — Fluent UI and CLI entry point
- CI runs lint + tests on Ubuntu and Windows (the GUI smoke test creates a real window on Windows).

---

## 📄 License (开源协议)

Distributed under the [MIT License](LICENSE).
