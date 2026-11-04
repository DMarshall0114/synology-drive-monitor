# Synology Drive Monitor 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6.svg)](https://microsoft.com)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![Style: Fluent](https://img.shields.io/badge/Design-Windows%2011%20Fluent-0067C0.svg)]()

A lightweight, non-intrusive, read-only desktop monitoring utility for **Synology Drive Client** on Windows. Built with a modern **Windows 11 Fluent Light** UI.

专为 Windows 群晖客户端（Synology Drive Client）量身打造的高性能、低侵入、**严格只读**同步状态监控工具。采用现代 **Windows 11 Fluent 浅色风格**，提供精准的流式事件吞吐率、在途工作池追踪与同步异常去重分类。

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
  - Parses `history.sqlite`, excludes normal selective sync rule codes (`-8192`), and automatically separates harmless Office lock files (`~$*`) from true actionable errors.
  - Supports one-click CSV export and double-click to locate files in Windows Explorer.
- 🎨 **Windows 11 Fluent Light Design (原生浅色沉浸体验)**
  - High-DPI support, clean typography (`Segoe UI Variable`), responsive cards, and dynamic mini sparklines.

---

## 📸 Screenshots (界面预览)

```text
+-----------------------------------------------------------------------------------------+
| Synology Drive Monitor v1.0   🟢 流式处理接近平衡             最后活动: 2秒前  [🛠️ 诊断报告] |
+-----------------------+-----------------------+-----------------------+-----------------+
|      当前在途         |       净消化速度      |     预计完成 (ETA)     |    同步异常     |
|         95            |          平衡         |      暂无法稳定估算    |       77        |
|  Push/Done 实时配对   |  完成 1380 ≈ 新增 1380 |    流式处理接近平衡    | 真实 77 · 临时 13|
+-----------------------+-----------------------+-----------------------+-----------------+
| 估算在途事件池趋势 (最近观测)                           | 正在处理 · 下载中               |
| [==================== Sparkline ====================] | path/to/current/file.docx       |
+-------------------------------------------------------+---------------------------------+
| 最近活动 | 同步异常 (需关注)                                                               |
+-----------------------------------------------------------------------------------------+
```

---

## 🚀 Quick Start (快速开始)

### Option 1: Run from Source (源码运行)

Ensure you have Python 3.10+ installed on Windows:

```bash
# Clone the repository
git clone https://github.com/DMarshall0114/synology-drive-monitor.git
cd synology-drive-monitor

# Install dependencies
pip install -r requirements.txt

# Launch monitor (GUI windowed mode)
python synology_drive_monitor.py
```

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

Click the **`🛠️ 诊断与排查报告`** button on the top right to inspect:
- Process status and monitor uptime
- Total indexed items in active session
- In-flight outstanding event pool depth
- PushEvent / DoneEvent stream rates (1m & 5m)
- Convergence state & sanity check verdicts
- One-click **`📋 复制诊断报告`** to copy formatted diagnostics for troubleshooting.

---

## 📖 Data Source Notice (数据源说明)

- This tool operates strictly on local disk data (`%LOCALAPPDATA%\SynologyDrive`).
- Synology Drive does not expose a static "remaining tasks" counter; all stream rates and convergence indicators are derived from local log observation and event pairing.

---

## 📄 License (开源协议)

Distributed under the [MIT License](LICENSE).
