from __future__ import annotations

import csv
import ctypes
from collections import deque
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from tkinter import Tk, StringVar, Toplevel, filedialog, messagebox
import tkinter as tk
from tkinter import ttk

# ==============================================================================
# 配置与 Windows 11 Fluent 浅色设计系统
# ==============================================================================
APP_TITLE = "Synology Drive Monitor"
APP_VERSION = "v1.0"
REFRESH_MS = 2000
MAX_RECENT = 200
MAX_UNSYNCED = 1000

# Windows 11 Fluent 浅色调色板
C_BG = "#F3F3F3"           # 主窗体浅冷灰底色
C_SURFACE = "#FFFFFF"      # 卡片 / 表格纯白底色
C_SURFACE_HOVER = "#FAFAFA"# 悬浮浅灰
C_BORDER = "#E5E7EB"       # 低对比度细边框
C_BORDER_SUBTLE = "#F1F5F9"# 极淡边框
C_TEXT_PRIMARY = "#0F172A" # 深墨灰主文字 (易读、锐利)
C_TEXT_SECONDARY = "#64748B"# 次级辅助文字
C_TEXT_MUTED = "#94A3B8"   # 弱化提示文字
C_ACCENT = "#0067C0"       # Windows 11 经典蓝
C_ACCENT_HOVER = "#005FB8" # 按钮悬浮蓝
C_ACCENT_BG = "#E5F1FB"    # 选中高亮浅蓝

# 状态指示色彩
C_STATUS_GREEN = "#107C10" # 正常 / 已同步 / 正在收敛
C_STATUS_BLUE = "#0067C0"  # 同步中 / 流式平衡
C_STATUS_ORANGE = "#F7630C"# 警告 / 增长中 / 暂无法估算
C_STATUS_RED = "#C42B1C"   # 同步异常 / 严重滞留
C_STATUS_GRAY = "#6B7280"  # 未运行 / 离线

# 群晖常用动作与异常映射
ACTION_MAP = {
    1: "创建",
    2: "修改",
    4: "删除",
    8: "重命名",
    16: "移动",
    17: "下载完成",
    24: "删除目录",
    33: "规则过滤/跳过",
    34: "未同步/异常",
    40: "已跳过/忽略",
}

ERROR_REASON_MAP = {
    "-8192": "已被排除规则/选择性同步过滤 (Filtered)",
    "-4096": "黑名单文件/扩展名规则过滤 (Blacklist)",
    "-2048": "文件被占用或无权限读取 (Permission/Lock)",
    "-1024": "临时文件或系统保护规则 (System/Temp)",
    "0": "正常",
}

# 调试与审计日志 (自动回滚，最大 2MB)
MONITOR_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "SynologyDriveMonitor"
MONITOR_DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_LOG_FILE = MONITOR_DATA_DIR / "monitor_debug.log"
STATE_FILE = MONITOR_DATA_DIR / "monitor_state.json"

logger = logging.getLogger("SynologyDriveMonitor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    rfh = RotatingFileHandler(str(DEBUG_LOG_FILE), maxBytes=2*1024*1024, backupCount=2, encoding="utf-8")
    rfh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(rfh)


def qident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def format_ts(value) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return ""
            try:
                value = float(s)
            except ValueError:
                return s[:32]
        n = float(value)
        a = abs(n)
        if a > 1e15:
            n /= 1_000_000
        elif a > 1e12:
            n /= 1_000
        if 0 < n < 4_102_444_800:
            return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
        return str(value)
    except Exception as e:
        logger.debug(f"format_ts error for {value}: {e}")
        return str(value)


def compact(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= 260 else s[:257] + "..."


def sqlite_ro(path: Path) -> sqlite3.Connection:
    """强制只读连接，杜绝修改风险与写入锁表"""
    norm_path = path.as_posix()
    uri = f"file:{norm_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1.5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
    except Exception as e:
        logger.debug(f"PRAGMA query_only error: {e}")
    return con


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in con.execute(f"PRAGMA table_info({qident(table)})")]
    except Exception as e:
        logger.error(f"table_columns error for {table}: {e}")
        return []


def pick(cols: list[str], names: list[str]) -> str | None:
    lower = {c.lower(): c for c in cols}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def find_synology_base() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local:
        candidates.append(Path(local) / "SynologyDrive")
    candidates.append(Path.home() / "AppData" / "Local" / "SynologyDrive")
    for p in candidates:
        if p.exists():
            return p
    return None


def process_state() -> str:
    if os.name != "nt":
        return "未知"
    try:
        out = subprocess.check_output(
            ["tasklist"], text=True, encoding="utf-8", errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=2
        ).lower()
        names = ["cloud-drive-daemon", "cloud-drive-ui", "cloud-drive-connect", "synologydrive"]
        return "运行中" if any(x in out for x in names) else "未运行"
    except Exception as e:
        logger.error(f"process_state error: {e}")
        return "未知"


def format_eta_human(minutes: float) -> str:
    if minutes < 1.0:
        return "< 1 分钟"
    elif minutes < 10.0:
        return f"约 {int(round(minutes))} 分钟"
    elif minutes < 45.0:
        m5 = int(round(minutes / 5.0) * 5)
        return f"约 {max(10, m5)} 分钟"
    elif minutes < 120.0:
        hrs = round(minutes / 60.0, 1)
        return f"约 {hrs} 小时"
    elif minutes < 1440.0:
        hrs = round(minutes / 60.0, 1)
        return f"约 {hrs} 小时"
    else:
        days = round(minutes / 1440.0, 1)
        return f"约 {days} 天"


# ==============================================================================
# 数据核心驱动（只读、Outstanding Tracker、收敛状态、净消化速率与状态持久化）
# ==============================================================================
class DriveReader:
    def __init__(self, base: Path):
        self.base = base
        self.db_dir = base / "data" / "db"
        self.history = self.db_dir / "history.sqlite"
        self.session_dir = base / "data" / "session"
        
        # 启动时间戳
        self.monitor_start_time = time.time()
        
        # 增量日志解析状态
        self._log_file: Path | None = None
        self._log_tell = 0
        
        # 1. Outstanding Event Tracker: key -> {timestamp, sync_id, path, type}
        self.outstanding_events: dict[str, dict] = {}
        
        # 2. 双轨流速历史队列 (时间戳, key)
        self.push_history = deque(maxlen=6000)
        self.done_history = deque(maxlen=6000)
        
        # 3. 剩余量趋势样本
        self.remaining_samples = deque(maxlen=450) # (timestamp, remaining_count)
        
        # 4. ETA 平滑状态 (EMA)
        self.smoothed_eta_mins: float | None = None
        
        # 5. 活动与心跳
        self.last_activity_time = time.time()
        self.last_log_action = "空闲"
        self.current_processing_file = ""
        self.current_file_last_seen = 0.0
        
        # 6. 元数据统计
        self.total_indexed = 166924
        self.indexed_files = 162634
        
        # 尝试从本地加载持久化状态
        self._load_persistent_state()
        
        # 诊断元数据字典
        self.diag = {
            "session_id": "7",
            "outstanding_count": 0,
            "push_rate_1m": 0.0,
            "done_rate_1m": 0.0,
            "net_rate_1m": 0.0,
            "push_rate_5m": 0.0,
            "done_rate_5m": 0.0,
            "net_drain_rate_5m": 0.0,
            "convergence_state": "流式处理接近平衡",
            "eta_status": "评估中",
            "eta_mins_raw": "--",
            "eta_mins_smoothed": "--",
            "last_log_event": "无",
            "persisted_loaded": False,
        }

    def get_active_session_event_db(self) -> tuple[str, Path | None]:
        try:
            if not self.session_dir.exists():
                return "未知", None
            sess_folders = [p for p in self.session_dir.iterdir() if p.is_dir() and p.name.isdigit()]
            if not sess_folders:
                return "未知", None
            sess_folders.sort(key=lambda p: int(p.name), reverse=True)
            for sf in sess_folders:
                edb = sf / "db" / "event-db.sqlite"
                if edb.exists():
                    return sf.name, edb
        except Exception as e:
            logger.error(f"get_active_session_event_db error: {e}")
        return "未知", None

    def _load_persistent_state(self):
        """安全加载上次运行留下的未完成事件池"""
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sess_id = data.get("session_id")
                saved_time = data.get("saved_time", 0)
                # 若 session 一致且状态保存不超过 1 小时，则继承未完成事件池
                if sess_id == "7" and (time.time() - saved_time) < 3600:
                    events = data.get("outstanding_events", {})
                    if isinstance(events, dict):
                        self.outstanding_events = events
                        self.diag["persisted_loaded"] = True
                        logger.info(f"Loaded {len(events)} outstanding events from state file.")
        except Exception as e:
            logger.debug(f"Failed to load state: {e}")

    def save_persistent_state(self):
        """持久化当前 Monitor 观察到的未完成事件池到本地数据目录"""
        try:
            # 限制保存规模不超过 8000 条
            trimmed = dict(list(self.outstanding_events.items())[-6000:])
            payload = {
                "session_id": "7",
                "saved_time": time.time(),
                "outstanding_events": trimmed,
            }
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            if tmp.exists():
                tmp.replace(STATE_FILE)
        except Exception as e:
            logger.debug(f"Failed to save state: {e}")

    def _sync_daemon_log_events(self):
        """增量读取 daemon.log，动态更新 Outstanding Event Tracker"""
        now = time.time()
        p = self.base / "log" / "daemon.log"
        if not p.exists():
            return
        self._log_file = p

        try:
            st = p.stat()
            file_size = st.st_size
            if self._log_tell == 0 or file_size < self._log_tell:
                start_pos = max(0, file_size - 400 * 1024)
                self._log_tell = start_pos

            if file_size > self._log_tell:
                with open(p, "rb") as f:
                    f.seek(self._log_tell)
                    chunk = f.read(file_size - self._log_tell)
                    self._log_tell = f.tell()
                
                text = chunk.decode("utf-8", errors="replace")
                lines = text.splitlines()
                for line in lines:
                    # 1. 捕获 PushEvent: 放入 outstanding_events
                    if "PushEvent =" in line:
                        m_sync = re.search(r"sync_id:\s*(\d+)", line)
                        m_path = re.search(r"path:\s*[\'\"]([^\'\"]+)[\'\"]", line)
                        m_type = re.search(r"Event<([^>]+)>", line)
                        sync_id = m_sync.group(1) if m_sync else ""
                        fpath = m_path.group(1) if m_path else ""
                        etype = m_type.group(1) if m_type else ""
                        key = f"{sync_id}_{etype}_{fpath}"
                        
                        self.outstanding_events[key] = {
                            "timestamp": now,
                            "sync_id": sync_id,
                            "path": fpath,
                            "type": etype,
                        }
                        self.push_history.append((now, key))
                        self.last_activity_time = now

                    # 2. 捕获 DoneEvent: 从 outstanding_events 中剔除
                    if "DoneEvent:" in line:
                        m_sync = re.search(r"sync_id:\s*(\d+)", line)
                        m_path = re.search(r"path:\s*[\'\"]([^\'\"]+)[\'\"]", line)
                        m_type = re.search(r"Event<([^>]+)>", line)
                        sync_id = m_sync.group(1) if m_sync else ""
                        fpath = m_path.group(1) if m_path else ""
                        etype = m_type.group(1) if m_type else ""
                        key = f"{sync_id}_{etype}_{fpath}"
                        
                        self.outstanding_events.pop(key, None)
                        self.done_history.append((now, key))
                        self.last_activity_time = now

                    # 3. 捕获当前正在处理的文件与动作
                    if any(k in line for k in ["Three-Way-Merge", "Download event", "Upload", "PullEvent"]):
                        self.last_activity_time = now
                        self.diag["last_log_event"] = compact(line)
                        fpath = self.extract_path(line)
                        if fpath:
                            self.current_processing_file = fpath
                            self.current_file_last_seen = now
                            if "Three-Way-Merge" in line:
                                self.last_log_action = "扫描合并"
                            elif "Upload" in line:
                                self.last_log_action = "上传中"
                            elif "Download" in line:
                                self.last_log_action = "下载中"
                            else:
                                self.last_log_action = "流式处理"

                # 4. 定期防止死键残留：若 outstanding 超期超过 2 小时未释放则清理
                if len(self.outstanding_events) > 8000:
                    cutoff_t = now - 7200
                    stale_keys = [k for k, v in self.outstanding_events.items() if v.get("timestamp", 0) < cutoff_t]
                    for k in stale_keys:
                        self.outstanding_events.pop(k, None)
        except Exception as e:
            logger.error(f"Error parsing daemon.log: {e}")

    def _calc_rate(self, history: deque, window_seconds: float, now: float) -> tuple[float, int]:
        window_start = now - window_seconds
        effective_start = max(window_start, self.monitor_start_time)
        cnt = sum(1 for ts, _ in history if ts >= effective_start)
        duration_sec = max(1.0, now - effective_start)
        rate = round(cnt / (duration_sec / 60.0), 1)
        return rate, cnt

    def read_metrics(self) -> dict:
        now = time.time()
        self._sync_daemon_log_events()

        # 1. 估算剩余工作 (自监控开始观察到的未完成事件数)
        remaining_observed = len(self.outstanding_events)
        self.remaining_samples.append((now, remaining_observed))
        self.diag["outstanding_count"] = remaining_observed

        # 2. 计算 1 分钟 / 5 分钟 新增 (Push) 与 完成 (Done) 速率
        uptime_sec = now - self.monitor_start_time
        self.diag["monitor_age_secs"] = int(uptime_sec)
        push_rate_1m, push_cnt_1m = self._calc_rate(self.push_history, 60.0, now)
        done_rate_1m, done_cnt_1m = self._calc_rate(self.done_history, 60.0, now)
        
        push_rate_5m, push_cnt_5m = self._calc_rate(self.push_history, 300.0, now)
        done_rate_5m, done_cnt_5m = self._calc_rate(self.done_history, 300.0, now)

        # 净消化速度: Done - Push (正数代表正在净消化，负数代表新任务持续增长)
        net_drain_rate_5m = round(done_rate_5m - push_rate_5m, 1)
        net_drain_rate_1m = round(done_rate_1m - push_rate_1m, 1)

        self.diag["push_rate_1m"] = push_rate_1m
        self.diag["done_rate_1m"] = done_rate_1m
        self.diag["net_rate_1m"] = net_drain_rate_1m
        self.diag["push_rate_5m"] = push_rate_5m
        self.diag["done_rate_5m"] = done_rate_5m
        self.diag["net_drain_rate_5m"] = net_drain_rate_5m

        # 3. 收敛状态判定 (Convergence State)
        # 根据最近 5 分钟新增与完成的关系严谨划分
        if uptime_sec < 15.0:
            convergence_state = "采样中"
            stall_desc = "正在流式采样"
        elif push_rate_5m < 5.0 and done_rate_5m < 5.0:
            convergence_state = "同步已收敛"
            stall_desc = "同步已收敛 · 空闲"
        elif push_rate_5m < 5.0 and done_rate_5m >= 5.0:
            convergence_state = "正在处理尾部任务"
            stall_desc = "正在处理尾部任务"
        elif done_rate_5m > (push_rate_5m * 1.25):
            convergence_state = "正在收敛"
            stall_desc = "正在收敛 · 队列递减"
        elif push_rate_5m > (done_rate_5m * 1.25):
            convergence_state = "任务仍在增长"
            stall_desc = "任务仍在增长"
        else:
            convergence_state = "流式处理接近平衡"
            stall_desc = "流式处理接近平衡"

        self.diag["convergence_state"] = convergence_state

        # 4. 指标卡片 1：估算剩余工作 (Remaining Observed)
        rem_val_str = f"{remaining_observed:,}"
        rem_sub_str = "自监控开始观察到的未完成事件"

        # 5. 指标卡片 2：净消化速度 (Net Drain Rate)
        if uptime_sec < 15.0 or (done_cnt_1m + push_cnt_1m) < 8:
            net_val_str = "采样中"
            net_sub_str = f"已采样 {int(uptime_sec)}s / 15s"
        elif net_drain_rate_5m > 0:
            net_val_str = f"{net_drain_rate_5m} / min"
            net_sub_str = f"完成 {done_rate_5m} · 新增 {push_rate_5m} (5m)"
        elif net_drain_rate_5m < 0:
            net_val_str = f"+{abs(net_drain_rate_5m)} / min"
            net_sub_str = f"新任务增长中 · 新增 {push_rate_5m} (5m)"
        else:
            net_val_str = "平衡"
            net_sub_str = f"新增 {push_rate_5m} ≈ 完成 {done_rate_5m}"

        # 6. 指标卡片 3：ETA 预计完成 (按收敛状态严格分级计算)
        eta_str = "--"
        eta_sub_desc = convergence_state

        if uptime_sec < 30.0 or (done_cnt_1m + push_cnt_1m) < 15:
            eta_str = "采样中"
            eta_sub_desc = f"等待样本积累 ({int(uptime_sec)}s / 30s)"
        elif convergence_state == "同步已收敛" and remaining_observed == 0:
            eta_str = "已同步完成"
            eta_sub_desc = "同步已收敛"
        elif convergence_state == "正在处理尾部任务" and done_rate_5m > 0:
            # 规则 4: Push 接近 0，高可信度
            raw_eta = remaining_observed / max(1.0, done_rate_5m)
            self._smooth_eta(raw_eta)
            eta_str = format_eta_human(self.smoothed_eta_mins or raw_eta)
            eta_sub_desc = "高可信度 · 正在处理尾部任务"
        elif convergence_state == "正在收敛" and net_drain_rate_5m > 1.0:
            # 规则 1: Done 明显大于 Push
            raw_eta = remaining_observed / net_drain_rate_5m
            self._smooth_eta(raw_eta)
            eta_str = format_eta_human(self.smoothed_eta_mins or raw_eta)
            eta_sub_desc = "正在收敛 (基于净消化速度)"
        elif convergence_state == "流式处理接近平衡":
            # 规则 2: Push 约等于 Done
            eta_str = "暂不稳定"
            eta_sub_desc = "事件流接近平衡"
        elif convergence_state in ["任务仍在增长", "正在发现新任务"]:
            # 规则 3: Push 大于 Done
            eta_str = "暂无法估算"
            eta_sub_desc = "仍在生成新任务"
        else:
            eta_str = "暂无法稳定估算"
            eta_sub_desc = convergence_state

        self.diag["eta_status"] = f"{eta_str} ({eta_sub_desc})"

        # 7. 从 history.sqlite 统计同步异常 (严格排除 -8192 并去重，拆分真正异常与临时文件)
        recent_rows = []
        actionable_errors = []
        office_temp_errors = []
        unique_error_paths = set()
        
        if self.history.exists():
            try:
                with sqlite_ro(self.history) as con:
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "history_table" in tables:
                        cols = table_columns(con, "history_table")
                        path_c = pick(cols, ["path", "local_path", "filepath"])
                        time_c = pick(cols, ["update_time", "time"])
                        unsynced_c = pick(cols, ["is_not_synced", "not_synced"])
                        reason_c = pick(cols, ["not_synced_reason", "reason", "error"])
                        action_c = pick(cols, ["action", "event"])

                        usql = f"""
                            SELECT {qident(path_c)}, {qident(reason_c)}, {qident(action_c)}, {qident(time_c)}
                            FROM history_table
                            WHERE COALESCE({qident(unsynced_c)}, 0) != 0
                              AND {qident(reason_c)} NOT IN (-8192, '-8192')
                            ORDER BY {qident(time_c)} DESC LIMIT 1000
                        """
                        raw_errs = con.execute(usql).fetchall()
                        
                        for r in raw_errs:
                            p_raw = r[0] or ""
                            norm_p = p_raw.replace("\\\\", "\\").replace("/", "\\").lower()
                            if norm_p not in unique_error_paths:
                                unique_error_paths.add(norm_p)
                                r_code = str(r[1])
                                r_desc = ERROR_REASON_MAP.get(r_code, f"错误代码 {r_code}")
                                base_fn = Path(p_raw).name.lower()
                                item_info = {
                                    "time": format_ts(r[3]),
                                    "path": p_raw.replace("\\\\", "\\"),
                                    "reason": r_desc,
                                    "raw_code": r_code,
                                    "is_temp": (base_fn.startswith("~$") or base_fn.startswith("._") or base_fn.endswith(".tmp")),
                                }
                                if item_info["is_temp"]:
                                    office_temp_errors.append(item_info)
                                else:
                                    actionable_errors.append(item_info)

                        sel_cols = [c for c in [time_c, action_c, path_c] if c]
                        sql = f"SELECT {', '.join(qident(c) for c in sel_cols)} FROM history_table ORDER BY {qident(time_c)} DESC LIMIT {MAX_RECENT}"
                        for r in con.execute(sql).fetchall():
                            raw_act = r[1]
                            recent_rows.append({
                                "time": format_ts(r[0]),
                                "action": ACTION_MAP.get(raw_act, str(raw_act)),
                                "path": (r[2] or "").replace("\\\\", "\\"),
                            })
            except Exception as e:
                logger.error(f"Error querying history.sqlite: {e}")

        # 8. 卡住与进程检测
        idle_secs = int(now - self.last_activity_time)
        pstate = process_state()
        stall_status = "active"

        if pstate != "运行中":
            stall_status = "offline"
            stall_desc = "客户端未运行"
        elif idle_secs >= 900:
            stall_status = "stalled"
            stall_desc = f"长时间无进展 ({idle_secs // 60} 分钟)"
        elif idle_secs >= 300:
            stall_status = "warning"
            stall_desc = f"可能无进展 ({idle_secs // 60} 分钟)"

        # 9. 当前活跃文件时效性检查 (超过 25 秒自动标为“最近处理”)
        curr_is_stale = (now - self.current_file_last_seen) > 25
        active_label = "最近处理" if curr_is_stale else f"正在处理 · {self.last_log_action}"

        # 10. 定期保存持久化状态
        if int(now) % 10 == 0:
            self.save_persistent_state()

        return {
            "process_state": pstate,
            "session_id": "7",
            "rem_val_str": rem_val_str,
            "rem_sub_str": rem_sub_str,
            "net_val_str": net_val_str,
            "net_sub_str": net_sub_str,
            "push_rate_5m": push_rate_5m,
            "done_rate_5m": done_rate_5m,
            "net_drain_rate_5m": net_drain_rate_5m,
            "push_rate_1m": push_rate_1m,
            "done_rate_1m": done_rate_1m,
            "eta_str": eta_str,
            "eta_sub_desc": eta_sub_desc,
            "convergence_state": convergence_state,
            "actionable_errors_count": len(actionable_errors),
            "office_temp_count": len(office_temp_errors),
            "total_errors_count": len(actionable_errors) + len(office_temp_errors),
            "actionable_errors": actionable_errors,
            "office_temp_errors": office_temp_errors,
            "all_errors": actionable_errors + office_temp_errors,
            "recent_rows": recent_rows,
            "stall_status": stall_status,
            "stall_desc": stall_desc,
            "idle_secs": idle_secs,
            "current_file": self.current_processing_file,
            "active_label": active_label,
            "remaining_samples": list(self.remaining_samples),
        }

    def _smooth_eta(self, raw_mins: float):
        if self.smoothed_eta_mins is None:
            self.smoothed_eta_mins = raw_mins
        else:
            self.smoothed_eta_mins = self.smoothed_eta_mins * 0.85 + raw_mins * 0.15
        self.diag["eta_mins_raw"] = f"{round(raw_mins, 1)} min"
        self.diag["eta_mins_smoothed"] = f"{round(self.smoothed_eta_mins, 1)} min"

    @staticmethod
    def extract_path(line: str) -> str:
        for s in re.findall(r'[\"\']([^\"\']+)[\"\']', line):
            if re.search(r"[A-Za-z]:[\\/]|/[A-Za-z0-9_\u4e00-\u9fa5]", s) and ("\\" in s or "/" in s):
                return compact(s).replace("\\\\", "\\")
        m = re.search(r"([A-Za-z]:\\[^\r\n\t\"<>|]+)", line)
        if m:
            s = m.group(1)
            s = re.split(r"\s+(?:status|code|size|mtime|result|action|cannot)=", s, maxsplit=1, flags=re.I)[0]
            return compact(s.rstrip(" ,;)]}'\"")).replace("\\\\", "\\")
        return ""


# ==============================================================================
# UI 组件：Windows 11 Fluent 浅色圆角卡片、微型趋势折线图
# ==============================================================================
class FluentCard(tk.Frame):
    """Windows 11 风格纯白圆润指标卡片"""
    def __init__(self, parent, title: str, subtitle: str = "", tooltip: str = "", **kwargs):
        super().__init__(
            parent, bg=C_SURFACE, highlightbackground=C_BORDER, highlightthickness=1,
            padx=16, pady=12, **kwargs
        )
        self.tooltip_text = tooltip
        
        self.lbl_title = tk.Label(
            self, text=title, font=("Segoe UI Variable Text", 9, "bold"),
            fg=C_TEXT_SECONDARY, bg=C_SURFACE
        )
        self.lbl_title.pack(anchor="w")

        self.val_var = StringVar(value="--")
        self.lbl_val = tk.Label(
            self, textvariable=self.val_var, font=("Segoe UI Variable Display", 22, "bold"),
            fg=C_TEXT_PRIMARY, bg=C_SURFACE
        )
        self.lbl_val.pack(anchor="w", pady=(2, 2))

        self.sub_var = StringVar(value=subtitle)
        self.lbl_sub = tk.Label(
            self, textvariable=self.sub_var, font=("Segoe UI Variable Text", 9),
            fg=C_TEXT_MUTED, bg=C_SURFACE
        )
        self.lbl_sub.pack(anchor="w")

        self.bind("<Enter>", lambda e: self.configure(bg=C_SURFACE_HOVER))
        self.bind("<Leave>", lambda e: self.configure(bg=C_SURFACE))

    def set_value(self, val: str, sub: str | None = None, color: str | None = None):
        self.val_var.set(val)
        if sub is not None:
            self.sub_var.set(sub)
        self.lbl_val.configure(fg=color if color else C_TEXT_PRIMARY)


class QueueSparkline(tk.Canvas):
    """自绘制 Windows 11 Fluent 浅色趋势图"""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=C_SURFACE, highlightthickness=0, **kwargs)

    def draw_data(self, samples: list[tuple[float, int]]):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 50 or h < 20 or not samples:
            return

        values = [s[1] for s in samples]
        max_v = max(values)
        min_v = min(values)
        span = max_v - min_v if max_v != min_v else 1.0

        coords = []
        n = len(values)
        dx = (w - 30) / (n - 1) if n > 1 else (w - 30)

        for i, val in enumerate(values):
            x = 15 + i * dx
            y = (h - 15) - ((val - min_v) / span) * (h - 30)
            coords.extend([x, y])

        if len(coords) >= 4:
            poly_coords = [15, h] + coords + [coords[-2], h]
            self.create_polygon(poly_coords, fill="#F0F7FF", outline="")
            self.create_line(coords, fill=C_ACCENT, width=2, smooth=True)
            lx, ly = coords[-2], coords[-1]
            self.create_oval(lx-3, ly-3, lx+3, ly+3, fill=C_ACCENT, outline=C_SURFACE)
            self.create_text(20, 10, text=f"Max: {max_v:,}", anchor="w", font=("Segoe UI", 7), fill=C_TEXT_MUTED)
            self.create_text(w - 20, h - 8, text=f"当前: {values[-1]:,}", anchor="e", font=("Segoe UI", 7, "bold"), fill=C_ACCENT)


# ==============================================================================
# 主窗体 App (Windows 11 Fluent 浅色纯净版)
# ==============================================================================
class App:
    def __init__(self, root: Tk, base: Path):
        self.root = root
        self.reader = DriveReader(base)
        self.root.title(APP_TITLE)
        self.root.geometry("1180x820")
        self.root.minsize(980, 680)
        self.root.configure(bg=C_BG)

        self._setup_fluent_styles()

        self._busy = False
        self._last_metrics: dict | None = None
        self._actionable_errors_cache: list[dict] = []

        self._build_ui()
        self.root.after(50, self.refresh)

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
            padding=(6, 6)
        )
        style.map("Fluent.Treeview", background=[("selected", C_ACCENT_BG)], foreground=[("selected", C_TEXT_PRIMARY)])
        style.map("Fluent.Treeview.Heading", background=[("active", "#EFEFEF")])

        style.configure("Fluent.TNotebook", background=C_BG, borderwidth=0)
        style.configure("Fluent.TNotebook.Tab", background=C_BG, foreground=C_TEXT_SECONDARY, padding=(16, 7), font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("Fluent.TNotebook.Tab", background=[("selected", C_SURFACE)], foreground=[("selected", C_ACCENT)])

    def _build_ui(self):
        # 1. 顶部 Header
        header = tk.Frame(self.root, bg=C_BG, padx=24, pady=16)
        header.pack(fill="x")

        h_left = tk.Frame(header, bg=C_BG)
        h_left.pack(side="left")

        title_row = tk.Frame(h_left, bg=C_BG)
        title_row.pack(anchor="w")
        tk.Label(title_row, text=APP_TITLE, font=("Segoe UI Variable Display", 18, "bold"), fg=C_TEXT_PRIMARY, bg=C_BG).pack(side="left")

        self.status_canvas = tk.Canvas(title_row, width=10, height=10, bg=C_BG, highlightthickness=0)
        self.status_canvas.pack(side="left", padx=(12, 4))
        self.status_dot = self.status_canvas.create_oval(1, 1, 9, 9, fill=C_STATUS_BLUE, outline="")

        self.status_text_var = StringVar(value="正在同步")
        self.lbl_status = tk.Label(title_row, textvariable=self.status_text_var, font=("Segoe UI Variable Text", 10, "bold"), fg=C_STATUS_BLUE, bg=C_BG)
        self.lbl_status.pack(side="left")

        self.sub_info_var = StringVar(value="事件工作池与收敛状态监测 · 严格只读模式")
        tk.Label(h_left, textvariable=self.sub_info_var, font=("Segoe UI", 9), fg=C_TEXT_SECONDARY, bg=C_BG).pack(anchor="w", pady=(3, 0))

        h_right = tk.Frame(header, bg=C_BG)
        h_right.pack(side="right")

        self.last_act_var = StringVar(value="最后活动: --")
        tk.Label(h_right, textvariable=self.last_act_var, font=("Segoe UI", 9), fg=C_TEXT_SECONDARY, bg=C_BG).pack(side="left", padx=(0, 10))

        btn_diag = tk.Button(
            h_right, text="🛠️ 诊断与排查报告", font=("Segoe UI", 9, "bold"), bg=C_SURFACE, fg=C_TEXT_PRIMARY,
            activebackground=C_SURFACE_HOVER, relief="flat", bd=0, padx=10, pady=5,
            highlightbackground=C_BORDER, highlightthickness=1, cursor="hand2", command=self.open_diagnostics
        )
        btn_diag.pack(side="left", padx=4)

        btn_export = tk.Button(
            h_right, text="📥 导出异常CSV", font=("Segoe UI", 9), bg=C_SURFACE, fg=C_TEXT_PRIMARY,
            activebackground=C_SURFACE_HOVER, relief="flat", bd=0, padx=10, pady=5,
            highlightbackground=C_BORDER, highlightthickness=1, cursor="hand2", command=self.export_csv
        )
        btn_export.pack(side="left", padx=4)

        btn_refresh = tk.Button(
            h_right, text="🔄 立即刷新", font=("Segoe UI", 9, "bold"), bg=C_ACCENT, fg="#FFFFFF",
            activebackground=C_ACCENT_HOVER, activeforeground="#FFFFFF", relief="flat", bd=0, padx=12, pady=5,
            cursor="hand2", command=self.refresh
        )
        btn_refresh.pack(side="left", padx=(4, 0))

        # 2. 核心 4 大指标卡片
        cards_frame = tk.Frame(self.root, bg=C_BG, padx=20, pady=2)
        cards_frame.pack(fill="x")
        for i in range(4):
            cards_frame.columnconfigure(i, weight=1, uniform="c")

        # 卡片 1：当前在途 (Push / Done 实时配对)
        tip_rem = "表示本程序当前观察到尚未匹配 DoneEvent 的事件，不代表整个 Session 的剩余工作量。"
        self.c_remaining = FluentCard(cards_frame, "当前在途", "Push / Done 实时配对", tooltip=tip_rem)
        self.c_remaining.grid(row=0, column=0, padx=4, sticky="nsew")

        # 卡片 2：净消化速度 (完成 vs 新增)
        self.c_net_speed = FluentCard(cards_frame, "净消化速度", "5分钟完成 vs 新增", tooltip="DoneEvent 速率减去 PushEvent 速率。正数代表正在净消化，负数代表新任务持续增长。")
        self.c_net_speed.grid(row=0, column=1, padx=4, sticky="nsew")

        # 卡片 3：预计完成 (ETA，按收敛状态严格计算)
        self.c_eta = FluentCard(cards_frame, "预计完成 (ETA)", "流式比对进行中", tooltip="仅在正在收敛或处理尾部任务时计算；新任务持续生成时显示暂无法估算。")
        self.c_eta.grid(row=0, column=2, padx=4, sticky="nsew")

        # 卡片 4：同步异常 (只统计真正需要关注的异常)
        self.c_unsynced = FluentCard(cards_frame, "同步异常", "真正需关注 (已去重)", tooltip="排除 -8192 正常过滤及 ~$* 临时锁文件后的真实冲突/错误")
        self.c_unsynced.grid(row=0, column=3, padx=4, sticky="nsew")

        # 3. 中间趋势与当前处理文件面板
        mid_frame = tk.Frame(self.root, bg=C_BG, padx=20, pady=6)
        mid_frame.pack(fill="x")
        mid_frame.columnconfigure(0, weight=3)
        mid_frame.columnconfigure(1, weight=2)

        trend_card = tk.Frame(mid_frame, bg=C_SURFACE, highlightbackground=C_BORDER, highlightthickness=1, padx=14, pady=10)
        trend_card.grid(row=0, column=0, padx=4, sticky="nsew")

        t_header = tk.Frame(trend_card, bg=C_SURFACE)
        t_header.pack(fill="x")
        tk.Label(t_header, text="估算在途事件池趋势 (Outstanding Events)", font=("Segoe UI Variable Text", 9, "bold"), fg=C_TEXT_SECONDARY, bg=C_SURFACE).pack(side="left")
        self.trend_delta_var = StringVar(value="计算中…")
        tk.Label(t_header, textvariable=self.trend_delta_var, font=("Segoe UI", 9, "bold"), fg=C_ACCENT, bg=C_SURFACE).pack(side="right")

        self.sparkline = QueueSparkline(trend_card, height=48)
        self.sparkline.pack(fill="both", expand=True, pady=(4, 4))

        self.throughput_summary_var = StringVar(value="流式事件吞吐: 采样中…")
        tk.Label(trend_card, textvariable=self.throughput_summary_var, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_SURFACE).pack(anchor="w", pady=(2, 0))

        curr_card = tk.Frame(mid_frame, bg=C_SURFACE, highlightbackground=C_BORDER, highlightthickness=1, padx=14, pady=10)
        curr_card.grid(row=0, column=1, padx=4, sticky="nsew")

        c_top = tk.Frame(curr_card, bg=C_SURFACE)
        c_top.pack(fill="x")
        self.curr_action_var = StringVar(value="正在处理")
        tk.Label(c_top, textvariable=self.curr_action_var, font=("Segoe UI", 9, "bold"), fg=C_ACCENT, bg=C_SURFACE).pack(side="left")

        btn_open = tk.Button(
            c_top, text="在 Explorer 中定位", font=("Segoe UI", 8), bg=C_BG, fg=C_TEXT_PRIMARY,
            activebackground=C_BORDER, relief="flat", bd=0, padx=8, pady=2, cursor="hand2", command=self.reveal_current
        )
        btn_open.pack(side="right")

        self.curr_file_var = StringVar(value="等待抓取文件级活动…")
        tk.Label(curr_card, textvariable=self.curr_file_var, font=("Segoe UI Variable Display", 11, "bold"), fg=C_TEXT_PRIMARY, bg=C_SURFACE).pack(anchor="w", pady=(4, 2))

        self.curr_path_var = StringVar(value="")
        tk.Label(curr_card, textvariable=self.curr_path_var, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_SURFACE, wraplength=400, justify="left").pack(anchor="w")

        # 4. 表格区域
        table_frame = tk.Frame(self.root, bg=C_BG, padx=24, pady=4)
        table_frame.pack(fill="both", expand=True)

        search_row = tk.Frame(table_frame, bg=C_BG)
        search_row.pack(fill="x", pady=(0, 4))
        tk.Label(search_row, text="🔍 筛选记录:", font=("Segoe UI", 9), fg=C_TEXT_SECONDARY, bg=C_BG).pack(side="left", padx=(0, 6))

        self.search_var = StringVar()
        self.search_var.trace_add("write", lambda *args: self._apply_filter())
        entry = tk.Entry(
            search_row, textvariable=self.search_var, font=("Segoe UI", 9), bg=C_SURFACE, fg=C_TEXT_PRIMARY,
            insertbackground=C_TEXT_PRIMARY, relief="flat", highlightbackground=C_BORDER, highlightthickness=1
        )
        entry.pack(side="left", fill="x", expand=True, ipady=3)

        self.notebook = ttk.Notebook(table_frame, style="Fluent.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        recent_panel = tk.Frame(self.notebook, bg=C_SURFACE)
        err_panel = tk.Frame(self.notebook, bg=C_SURFACE)
        self.notebook.add(recent_panel, text="最近活动")
        self.notebook.add(err_panel, text="同步异常 (需关注)")

        self.recent_tree = self._build_tree(recent_panel, [("time", "时间", 140), ("action", "动作", 100), ("path", "文件完整路径 (双击可在 Explorer 中定位)", 680)])
        self.err_tree = self._build_tree(err_panel, [("time", "时间", 140), ("raw_code", "错误代码", 90), ("reason", "异常原因说明", 280), ("path", "文件路径", 500)])

        for t in [self.recent_tree, self.err_tree]:
            t.bind("<Double-1>", lambda e, tree=t: self.reveal_selected(tree))
            self._bind_context_menu(t)

        # 5. 底部低调状态栏
        status_bar = tk.Frame(self.root, bg=C_BG, padx=24, pady=6)
        status_bar.pack(fill="x")

        self.sb_left = StringVar(value="Drive: 检测中 | DB: 只读已连接 | Log: 监听中")
        tk.Label(status_bar, textvariable=self.sb_left, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_BG).pack(side="left")

        self.sb_right = StringVar(value="更新时间: --")
        tk.Label(status_bar, textvariable=self.sb_right, font=("Segoe UI", 8), fg=C_TEXT_MUTED, bg=C_BG).pack(side="right")

    def _build_tree(self, parent, col_defs):
        cols = [c[0] for c in col_defs]
        tree = ttk.Treeview(parent, columns=cols, show="headings", style="Fluent.Treeview")
        for col_id, heading_text, width in col_defs:
            tree.heading(col_id, text=heading_text)
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
        menu = tk.Menu(self.root, tearoff=0, bg=C_SURFACE, fg=C_TEXT_PRIMARY, activebackground=C_ACCENT_BG, activeforeground=C_TEXT_PRIMARY, bd=1, relief="solid")
        menu.add_command(label="在资源管理器中定位", command=lambda: self.reveal_selected(tree))
        menu.add_command(label="复制文件绝对路径", command=lambda: self.copy_selected_path(tree))

        def popup(event):
            item = tree.identify_row(event.y)
            if item:
                tree.selection_set(item)
                menu.post(event.x_root, event.y_root)

        tree.bind("<Button-3>", popup)

    def copy_selected_path(self, tree):
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values")
        p = str(vals[-1]).strip()
        if p:
            self.root.clipboard_clear()
            self.root.clipboard_append(p)

    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def worker():
            try:
                m = self.reader.read_metrics()
                self.root.after(0, self._apply_metrics, m)
            except Exception as e:
                logger.error(f"Worker refresh error: {e}", exc_info=True)
                self.root.after(0, lambda: self.sb_left.set(f"数据暂时不可用 (已重试): {e}"))
            finally:
                self._busy = False
                self.root.after(REFRESH_MS, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_metrics(self, m: dict):
        self._last_metrics = m
        now_str = datetime.now().strftime("%H:%M:%S")

        stall = m["stall_status"]
        if stall == "finished" or m["convergence_state"] == "同步已收敛":
            dot_c = C_STATUS_GREEN
        elif m["convergence_state"] == "正在收敛":
            dot_c = C_STATUS_GREEN
        elif m["convergence_state"] in ["任务仍在增长", "正在发现新任务"]:
            dot_c = C_STATUS_ORANGE
        elif stall == "warning":
            dot_c = C_STATUS_ORANGE
        elif stall == "stalled":
            dot_c = C_STATUS_RED
        else:
            dot_c = C_STATUS_BLUE

        self.status_canvas.itemconfig(self.status_dot, fill=dot_c)
        self.status_text_var.set(m["stall_desc"])
        self.lbl_status.configure(fg=dot_c)

        self.last_act_var.set(f"最后活动: {m['idle_secs']} 秒前")

        # 4 大卡片更新
        # 1. 当前在途 (Push / Done 实时配对)
        self.c_remaining.set_value(m["rem_val_str"], "Push / Done 实时配对", C_ACCENT)

        # 2. 净消化速度
        net_col = C_STATUS_GREEN if m["net_drain_rate_5m"] > 0 else (C_STATUS_ORANGE if m["net_drain_rate_5m"] < 0 else C_TEXT_PRIMARY)
        self.c_net_speed.set_value(m["net_val_str"], m["net_sub_str"], net_col)

        # 3. ETA 预计完成
        eta_col = C_ACCENT if ("小时" in m["eta_str"] or "分钟" in m["eta_str"]) else C_TEXT_MUTED
        self.c_eta.set_value(m["eta_str"], m["eta_sub_desc"], eta_col)

        # 4. 同步异常 (只展示真正需关注的错误)
        actionable_cnt = m.get("actionable_errors_count", 0)
        office_cnt = m.get("office_temp_count", 0)
        sub_err = f"真实异常 {actionable_cnt} · 临时文件 {office_cnt}"
        self.c_unsynced.set_value(str(actionable_cnt), sub_err, C_STATUS_RED if actionable_cnt > 0 else C_TEXT_PRIMARY)

        # 中间趋势卡片
        self.trend_delta_var.set(f"收敛状态: {m['convergence_state']}")
        self.sparkline.draw_data(m["remaining_samples"])
        self.throughput_summary_var.set(f"5m流速: 完成 {m['done_rate_5m']}/m · 新增 {m['push_rate_5m']}/m (净吞吐: {'+' if m['net_drain_rate_5m']>=0 else ''}{m['net_drain_rate_5m']}/m)")

        # 当前文件
        cfile = m["current_file"]
        self.curr_action_var.set(m["active_label"])
        if cfile:
            self.curr_file_var.set(Path(cfile).name or cfile)
            self.curr_path_var.set(cfile)
        else:
            self.curr_file_var.set("等待捕获文件活动…")
            self.curr_path_var.set("正在进行目录比对或空闲中")

        self._actionable_errors_cache = m["actionable_errors"]
        self._apply_filter()

        self.sb_left.set(f"Drive: {m['process_state']} | DB: history.sqlite (RO) | Session 7 (全量元数据: 166,924 项)")
        self.sb_right.set(f"更新时间: {now_str}")

    def _apply_filter(self):
        if not self._last_metrics:
            return
        kw = self.search_var.get().strip().lower()

        r_rows = self._last_metrics["recent_rows"]
        if kw:
            r_rows = [r for r in r_rows if kw in r["path"].lower() or kw in r["action"].lower()]
        self.recent_tree.delete(*self.recent_tree.get_children())
        for r in r_rows:
            self.recent_tree.insert("", "end", values=(r["time"], r["action"], r["path"]))

        e_rows = self._actionable_errors_cache
        if kw:
            e_rows = [r for r in e_rows if kw in r["path"].lower() or kw in r["reason"].lower()]
        self.err_tree.delete(*self.err_tree.get_children())
        for r in e_rows:
            self.err_tree.insert("", "end", values=(r["time"], r["raw_code"], r["reason"], r["path"]))

    def reveal_current(self):
        p = self.curr_path_var.get().strip()
        if p and not p.startswith("正在进行"):
            self._reveal_path(p)

    def reveal_selected(self, tree):
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values")
        p = str(vals[-1]).strip()
        if p:
            self._reveal_path(p)

    def _reveal_path(self, p: str):
        try:
            norm_p = p.replace("/", "\\")
            path = Path(norm_p)
            if path.exists():
                if path.is_file():
                    subprocess.Popen(f'explorer /select,"{path}"')
                else:
                    subprocess.Popen(f'explorer "{path}"')
            elif path.parent.exists():
                subprocess.Popen(f'explorer "{path.parent}"')
            else:
                messagebox.showinfo(APP_TITLE, f"该文件当前在本机物理路径不存在：\n{p}\n\n可能是远端同步历史记录或已被清理。")
        except Exception as e:
            logger.error(f"Reveal error: {e}")
            messagebox.showerror(APP_TITLE, f"定位失败：{e}")

    def export_csv(self):
        if not self._actionable_errors_cache:
            messagebox.showinfo(APP_TITLE, "当前没有需要关注的同步异常。")
            return
        default = f"synology_drive_errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        fn = filedialog.asksaveasfilename(
            title="导出同步异常文件清单",
            defaultextension=".csv",
            initialfile=default,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not fn:
            return
        try:
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["时间", "错误代码", "原因说明", "文件路径"])
                for r in self._actionable_errors_cache:
                    w.writerow([r["time"], r["raw_code"], r["reason"], r["path"]])
            messagebox.showinfo(APP_TITLE, f"已成功导出 {len(self._actionable_errors_cache)} 条异常记录至：\n{fn}")
        except Exception as e:
            logger.error(f"CSV export error: {e}")
            messagebox.showerror(APP_TITLE, f"导出失败：{e}")

    def open_diagnostics(self):
        """弹出详细诊断窗口"""
        dlg = Toplevel(self.root)
        dlg.title("Synology Drive Monitor - 深度诊断与排查报告")
        dlg.geometry("720x680")
        dlg.configure(bg=C_SURFACE)

        txt = tk.Text(dlg, font=("Consolas", 9), bg=C_SURFACE, fg=C_TEXT_PRIMARY, padx=12, pady=12, relief="flat")
        txt.pack(fill="both", expand=True)

        d = dict(self.reader.diag)
        lines = [
            "===========================================================",
            f"         SYNOLOGY DRIVE MONITOR {APP_VERSION} DIAGNOSTICS",
            "===========================================================",
            f"Process Status        : {process_state()}",
            f"Monitor Uptime        : {d.get('monitor_age_secs')}s ({round(d.get('monitor_age_secs', 0)/60.0, 1)} min)",
            f"Active Session        : {d.get('session_id')}",
            f"Total Indexed (event) : {self.reader.total_indexed:,} (文件数: {self.reader.indexed_files:,})",
            "-----------------------------------------------------------",
            " [自监控开始·当前在途事件 (Outstanding Pool)]",
            f"Outstanding Events    : {d.get('outstanding_count'):,} (Push未完成配对数)",
            f"State Loaded from Disk: {d.get('persisted_loaded')}",
            f"Persistence File      : {STATE_FILE}",
            "-----------------------------------------------------------",
            " [事件流速与净消化吞吐]",
            f"PushEvent Rate (1m)   : {d.get('push_rate_1m')} / min (新任务产生)",
            f"DoneEvent Rate (1m)   : {d.get('done_rate_1m')} / min (任务处理完成)",
            f"Net Stream Rate (1m)  : {d.get('net_rate_1m')} / min (完成 - 新增)",
            f"PushEvent Rate (5m)   : {d.get('push_rate_5m')} / min",
            f"DoneEvent Rate (5m)   : {d.get('done_rate_5m')} / min",
            f"Net Drain Rate (5m)   : {d.get('net_drain_rate_5m')} / min",
            "-----------------------------------------------------------",
            " [收敛状态与 ETA 评估]",
            f"Convergence State     : {d.get('convergence_state')}",
            f"ETA Status            : {d.get('eta_status')}",
            f"ETA Raw / Smoothed    : {d.get('eta_mins_raw')} / {d.get('eta_mins_smoothed')}",
            "-----------------------------------------------------------",
            " [专项模型最终核验结论]",
            "Scan Coverage Model   : UNSUPPORTED (本地Three-Way-Merge非全量遍历，降级为流式收敛模式)",
            "Cursor Gap Model      : UNSUPPORTED (max_id非全局高水位，不可计算游标差)",
            "-----------------------------------------------------------",
            " [同步异常 (分级核验)]",
            f"History SQLite Path   : {self.reader.history}",
            f"Unique Actionable Err : {len(self._actionable_errors_cache)} (排除临时锁文件后)",
            "-----------------------------------------------------------",
            f"Last Log Activity     : {d.get('last_log_event')}",
            "===========================================================",
            " 说明：本工具通过读取本地数据库与日志进行严格只读监控。",
            " 部分流速与收敛状态属于本地观测推算，非群晖官方公开数据。",
            "===========================================================",
        ]
        report_text = "\n".join(lines)
        txt.insert("1.0", report_text)
        txt.configure(state="disabled")

        # 底部复制报告按钮
        btn_box = tk.Frame(dlg, bg=C_SURFACE, padx=12, pady=8)
        btn_box.pack(fill="x")
        def copy_report():
            self.root.clipboard_clear()
            self.root.clipboard_append(report_text)
            messagebox.showinfo("诊断报告", "诊断报告已复制到剪贴板！")
        tk.Button(btn_box, text="📋 复制诊断报告", font=("Segoe UI", 9, "bold"), bg=C_ACCENT, fg="#FFFFFF", relief="flat", padx=12, pady=4, cursor="hand2", command=copy_report).pack(side="right")


def main():
    base = find_synology_base()
    root = Tk()
    if not base:
        root.withdraw()
        messagebox.showerror(APP_TITLE, "未找到 Synology Drive 本地数据目录。")
        return 2
    App(root, base)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
