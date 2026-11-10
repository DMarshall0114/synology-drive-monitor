"""Synology Drive Monitor 核心：只读数据采集与指标计算。

本模块不依赖 tkinter，GUI（synology_drive_monitor.py）、命令行 JSON 模式和单元测试共用它。

安全约束（见 CONTRIBUTING.md）：
- 所有 SQLite 访问必须 mode=ro + PRAGMA query_only=ON，不得出现任何写语句。
- 不编造进度：无法从本地日志 / 数据库稳定推算的指标一律降级显示。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_TITLE = "Synology Drive Monitor"
APP_VERSION = "v1.2.0"

# ------------------------------------------------------------------------------
# 可调参数
# ------------------------------------------------------------------------------
MAX_RECENT = 200  # 最近活动表行数
MAX_UNSYNCED = 1000  # 最多读取的未同步记录数
LOG_BACKLOG_BYTES = 400 * 1024  # 首次启动回读 daemon.log 尾部的字节数
LOG_MAX_READ_BYTES = 8 * 1024 * 1024  # 单次刷新最多读取的新日志字节数
OUTSTANDING_TTL_SECS = 7200  # 在途事件超过 2 小时未配对即视为过期
OUTSTANDING_MAX = 8000  # 在途事件池上限
HISTORY_REFRESH_SECS = 10  # history.sqlite 最短查询间隔（文件未变化时直接复用缓存）
PROCESS_CHECK_SECS = 10  # tasklist 进程检测缓存时间
STATE_SAVE_SECS = 10  # 持久化在途事件池的间隔
STATE_MAX_AGE_SECS = 3600  # 超过该时间的持久化状态不再继承
STATE_MAX_EVENTS = 6000  # 持久化的在途事件上限
SAMPLING_SECS = 15  # 启动后多久开始给出收敛判断
ETA_SAMPLING_SECS = 30  # 启动后多久开始给出 ETA
LOW_RATE_PER_MIN = 5.0  # 低于该速率视为"几乎无事件流"
STALL_WARN_SECS = 300
STALL_ALERT_SECS = 900
CURRENT_FILE_STALE_SECS = 25
RATE_SERIES_MINUTES = 15  # 每分钟吞吐图覆盖的分钟数
LOG_ALERT_MAX_GROUPS = 200  # daemon.log 告警最多保留的分组数
LOCAL_CHECK_MAX = 300  # 每次最多核对多少个异常文件在本地是否仍存在
WIN_MAX_PATH = 260

# 群晖动作代码（经验映射，非官方文档）
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

# 未同步原因代码（经验映射，非官方文档；未知代码原样显示）
ERROR_REASON_MAP = {
    "-8192": "已被排除规则/选择性同步过滤 (Filtered)",
    "-4096": "黑名单文件/扩展名规则过滤 (Blacklist)",
    "-2048": "文件被占用或无权限读取 (Permission/Lock)",
    "-1024": "临时文件或系统保护规则 (System/Temp)",
    "0": "正常",
}
FILTERED_REASON = "-8192"  # 选择性同步过滤，属于正常行为，不计入异常

# Office / WPS / LibreOffice / macOS 产生的临时与锁文件
TEMP_PREFIXES = ("~$", "._", ".~lock.", "~wrl")
TEMP_SUFFIXES = (".tmp", ".temp", "#")

DRIVE_PROCESS_NAMES = ("cloud-drive-daemon", "cloud-drive-ui", "cloud-drive-connect", "synologydrive")
SELF_PROCESS_PREFIX = "synologydrivemonitor"  # 本程序打包后的 exe 名，不能算作群晖客户端

# Windows 文件名规则（用于给同步异常附加"可能原因"提示）
WIN_ILLEGAL_CHARS = '<>:"|?*'
WIN_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

# 收敛状态
ST_SAMPLING = "采样中"
ST_CONVERGED = "同步已收敛"
ST_LOW_RATE = "低速运行"
ST_TAIL = "正在处理尾部任务"
ST_CONVERGING = "正在收敛"
ST_GROWING = "任务仍在增长"
ST_BALANCED = "流式处理接近平衡"

logger = logging.getLogger("SynologyDriveMonitor")


# ==============================================================================
# 通用工具
# ==============================================================================
def data_dir() -> Path:
    """本程序自己的数据目录（调试日志与在途事件池），与群晖目录分离。"""
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / "SynologyDriveMonitor"


def setup_logging(directory: Path | None = None) -> Path:
    """配置回滚调试日志（最大 2MB × 3）。返回日志文件路径。"""
    directory = directory or data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "monitor_debug.log"
    logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        rfh = RotatingFileHandler(str(log_file), maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        rfh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(rfh)
    return log_file


def qident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def format_ts(value) -> str:
    """把秒 / 毫秒 / 微秒时间戳或字符串统一格式化为本地时间。"""
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
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def compact(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= 260 else s[:257] + "..."


def normalize_path(p: str) -> str:
    """用于去重的路径键：统一分隔符、合并重复反斜杠、忽略大小写（Windows 语义）。"""
    s = (p or "").replace("/", "\\")
    prefix = "\\\\" if s.startswith("\\\\") else ""  # 保留 UNC 前缀
    return (prefix + re.sub(r"\\{2,}", r"\\", s[len(prefix) :])).lower()


def display_path(p: str) -> str:
    return (p or "").replace("\\\\", "\\")


def is_temp_file(path: str) -> bool:
    name = re.split(r"[\\/]", path or "")[-1].lower()
    return name.startswith(TEMP_PREFIXES) or name.endswith(TEMP_SUFFIXES)


def sqlite_ro(path: Path) -> sqlite3.Connection:
    """强制只读连接，杜绝修改风险与写入锁表。"""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1.5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in con.execute(f"PRAGMA table_info({qident(table)})")]
    except sqlite3.Error as e:
        logger.error(f"table_columns error for {table}: {e}")
        return []


def pick(cols: list[str], names: list[str]) -> str | None:
    lower = {c.lower(): c for c in cols}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def find_synology_base(override: str | Path | None = None) -> Path | None:
    if override:
        p = Path(override).expanduser()
        return p if p.exists() else None
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "SynologyDrive")
    candidates.append(Path.home() / "AppData" / "Local" / "SynologyDrive")
    for p in candidates:
        if p.exists():
            return p
    return None


def drive_process_running(tasklist_csv: str) -> bool:
    """从 `tasklist /FO CSV /NH` 的输出判断群晖客户端是否在运行。

    只比较映像名，并排除本程序自己：打包后的 SynologyDriveMonitor.exe 名字里也含
    "synologydrive"，整段文本做子串匹配会让"客户端未运行"永远检测不出来。
    """
    for line in tasklist_csv.splitlines():
        name = line.split(",", 1)[0].strip().strip('"').lower()
        if not name or name.startswith(SELF_PROCESS_PREFIX):
            continue
        if any(x in name for x in DRIVE_PROCESS_NAMES):
            return True
    return False


_process_cache: tuple[float, str] = (0.0, "未知")


def process_state(force: bool = False) -> str:
    """检测群晖客户端进程（结果缓存 PROCESS_CHECK_SECS 秒，避免每次刷新都拉起 tasklist）。"""
    global _process_cache
    if os.name != "nt":
        return "未知"
    now = time.time()
    if not force and now - _process_cache[0] < PROCESS_CHECK_SECS:
        return _process_cache[1]
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=3,
        )
        state = "运行中" if drive_process_running(out) else "未运行"
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"process_state error: {e}")
        state = "未知"
    _process_cache = (now, state)
    return state


def format_eta_human(minutes: float) -> str:
    if minutes < 1.0:
        return "< 1 分钟"
    if minutes < 10.0:
        return f"约 {int(round(minutes))} 分钟"
    if minutes < 45.0:
        return f"约 {max(10, int(round(minutes / 5.0) * 5))} 分钟"
    if minutes < 1440.0:
        return f"约 {round(minutes / 60.0, 1)} 小时"
    return f"约 {round(minutes / 1440.0, 1)} 天"


def format_age(secs: float) -> str:
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs} 秒"
    if secs < 3600:
        return f"{secs // 60} 分钟"
    return f"{round(secs / 3600, 1)} 小时"


# ==============================================================================
# daemon.log 解析
# ==============================================================================
_TS_RE = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})[T ](\d{2}):(\d{2}):(\d{2})")
_SYNC_RE = re.compile(r"sync_id:\s*(\d+)")
_PATH_RE = re.compile(r"path:\s*['\"]([^'\"]+)['\"]")
_TYPE_RE = re.compile(r"Event<([^>]+)>")
_ACTIVITY_KEYWORDS = ("Three-Way-Merge", "Download event", "Upload", "PullEvent")


def parse_line_ts(line: str) -> float | None:
    """解析行首时间戳（形如 2026-11-06 10:00:00 / 2026-11-06T10:00:00），失败返回 None。"""
    m = _TS_RE.search(line[:64])
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def parse_event(line: str) -> tuple[str, dict] | None:
    """解析 PushEvent / DoneEvent 行，返回 (kind, info)。kind 为 'push' 或 'done'。"""
    if "PushEvent =" in line:
        kind = "push"
    elif "DoneEvent:" in line:
        kind = "done"
    else:
        return None
    m_sync = _SYNC_RE.search(line)
    m_path = _PATH_RE.search(line)
    m_type = _TYPE_RE.search(line)
    info = {
        "sync_id": m_sync.group(1) if m_sync else "",
        "path": m_path.group(1) if m_path else "",
        "type": m_type.group(1) if m_type else "",
    }
    info["key"] = f"{info['sync_id']}_{info['type']}_{info['path']}"
    return kind, info


def extract_path(line: str) -> str:
    for s in re.findall(r"[\"']([^\"']+)[\"']", line):
        if re.search(r"[A-Za-z]:[\\/]|/[A-Za-z0-9_一-龥]", s) and ("\\" in s or "/" in s):
            return compact(s).replace("\\\\", "\\")
    m = re.search(r"([A-Za-z]:\\[^\r\n\t\"<>|]+)", line)
    if m:
        s = m.group(1)
        s = re.split(r"\s+(?:status|code|size|mtime|result|action|cannot)=", s, maxsplit=1, flags=re.I)[0]
        return compact(s.rstrip(" ,;)]}'\"")).replace("\\\\", "\\")
    return ""


_LEVEL_RE = re.compile(r"\[([A-Z]{3,9})\]")
_ALERT_LEVELS = {"ERROR", "ERR", "CRIT", "CRITICAL", "FATAL", "ALERT", "EMERG", "WARNING", "WARN"}
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def parse_log_alert(line: str) -> tuple[str, str, str] | None:
    """识别 daemon.log 里的 [ERROR] / [WARNING] 行，返回 (级别, 消息, 归并键)。

    归并键把引号内容和数字抹掉，同一类报错（只是文件名 / 编号不同）会合并成一组。
    日志格式属于经验观察，识别不到级别标记时返回 None。
    """
    m = _LEVEL_RE.search(line[:120])  # 只认行首附近的第一个级别标记，避免误认路径里的文字
    if not m or m.group(1) not in _ALERT_LEVELS:
        return None
    level = "警告" if m.group(1).startswith("WARN") else "错误"
    msg = line[m.end() :].strip(" :-\t")
    if not msg:
        return None
    sig = re.sub(r"\d+", "#", _QUOTED_RE.sub("'…'", msg))[:160]
    return level, compact(msg), f"{level}|{sig}"


def path_hints(path: str, exists: bool | None = None) -> list[str]:
    """只根据路径本身给出"可能的未同步原因"提示（启发式，不是群晖给出的结论）。

    exists 为 False 表示已核对过本地文件不存在；None 表示没有核对。
    """
    p = display_path(path)
    parts = [x for x in re.split(r"[\\/]", p) if x]
    if not parts:
        return []
    hints: list[str] = []
    if len(p) >= WIN_MAX_PATH:
        hints.append(f"路径过长（{len(p)} 字符，Windows 默认上限 {WIN_MAX_PATH}）")
    name_bytes = len(parts[-1].encode("utf-8", errors="replace"))
    if name_bytes > 255:
        hints.append(f"文件名过长（{name_bytes} 字节，上限 255）")
    elif name_bytes > 143:
        hints.append(f"文件名 {name_bytes} 字节，NAS 加密共享文件夹上限为 143 字节")

    bad_chars: set[str] = set()
    trailing = reserved = False
    for i, part in enumerate(parts):
        if i == 0 and re.fullmatch(r"[A-Za-z]:", part):
            continue  # 盘符
        bad_chars.update(c for c in part if c in WIN_ILLEGAL_CHARS or ord(c) < 32)
        trailing = trailing or part != part.rstrip(" .")
        reserved = reserved or part.split(".")[0].strip().lower() in WIN_RESERVED_NAMES
    if bad_chars:
        shown = " ".join(c if ord(c) >= 32 else "(控制字符)" for c in sorted(bad_chars))
        hints.append(f"名称含 Windows 不允许的字符: {shown}")
    if trailing:
        hints.append("名称以空格或句点结尾，Windows 无法创建")
    if reserved:
        hints.append("使用了 Windows 保留名称（CON / NUL / COM1 等）")
    if "_conflict" in parts[-1].lower():
        hints.append("疑似冲突副本，需人工比对后保留一份")
    if exists is False:
        hints.append("本地已不存在该文件（可能已删除或移走，此条多半可以忽略）")
    return hints


def error_breakdown(errors: list[dict], top: int = 5) -> dict:
    """把异常按原因和所在文件夹汇总，方便看出问题是不是集中在某一处。"""
    by_reason = Counter(e.get("reason") or "未知原因" for e in errors)
    by_folder: Counter = Counter()
    for e in errors:
        parts = [x for x in re.split(r"[\\/]", e.get("path") or "") if x]
        by_folder["\\".join(parts[:-1][:3]) or "(根目录)"] += 1
    return {"by_reason": by_reason.most_common(), "by_folder": by_folder.most_common(top)}


def classify_convergence(uptime: float, push5: float, done5: float, outstanding: int) -> str:
    """根据最近 5 分钟新增 (Push) 与完成 (Done) 速率划分收敛阶段。"""
    if uptime < SAMPLING_SECS:
        return ST_SAMPLING
    if push5 < LOW_RATE_PER_MIN and done5 < LOW_RATE_PER_MIN:
        # 几乎没有事件流：只有在途池清空才算真正收敛，否则如实标为低速
        return ST_CONVERGED if outstanding == 0 else ST_LOW_RATE
    if push5 < LOW_RATE_PER_MIN:
        return ST_TAIL
    if done5 > push5 * 1.25:
        return ST_CONVERGING
    if push5 > done5 * 1.25:
        return ST_GROWING
    return ST_BALANCED


# ==============================================================================
# 数据核心驱动
# ==============================================================================
class DriveReader:
    """只读采集器。read_metrics() 由单个工作线程调用；save_persistent_state() 可在任意线程调用。"""

    def __init__(self, base: Path, state_file: Path | None = None, clock=time.time, persist: bool = True):
        self.base = base
        self.persist = persist
        self.db_dir = base / "data" / "db"
        self.history = self.db_dir / "history.sqlite"
        self.session_dir = base / "data" / "session"
        self.state_file = state_file if state_file is not None else data_dir() / "monitor_state.json"
        self._clock = clock
        self._lock = threading.RLock()

        self.monitor_start_time = clock()

        # 增量日志解析状态
        self.log_file: Path | None = None
        self._log_tell: int | None = None
        self._log_partial = b""

        # 在途事件池：key -> {timestamp, sync_id, path, type}
        self.outstanding_events: dict[str, dict] = {}
        self.expired_events = 0

        # 双轨流速历史（仅记录监控启动后到达的事件时间）
        self.push_history: deque[float] = deque(maxlen=20000)
        self.done_history: deque[float] = deque(maxlen=20000)

        # 在途池趋势样本 (timestamp, count)，2 秒一次约 15 分钟
        self.remaining_samples: deque[tuple[float, int]] = deque(maxlen=450)

        self.smoothed_eta_mins: float | None = None

        self.last_activity_time = self.monitor_start_time
        self.last_log_action = "空闲"
        self.current_processing_file = ""
        self.current_file_last_seen = 0.0

        # history.sqlite 查询缓存
        self._history_sig: tuple | None = None
        self._history_checked = 0.0
        self._history_cache: dict = self._empty_history()

        self._last_state_save = 0.0

        # daemon.log 里的 [ERROR] / [WARNING]：归并键 -> {level, message, count, first_ts, last_ts}
        self.log_alerts: dict[str, dict] = {}

        # 是否核对异常文件在本地是否仍存在（只做 os.path.exists，仅 Windows 本地盘符路径）
        self.check_local_files = os.name == "nt"

        self.session_id, self.event_db = self.get_active_session_event_db()

        self.diag: dict = {
            "session_id": self.session_id,
            "outstanding_count": 0,
            "push_rate_1m": 0.0,
            "done_rate_1m": 0.0,
            "net_rate_1m": 0.0,
            "push_rate_5m": 0.0,
            "done_rate_5m": 0.0,
            "net_drain_rate_5m": 0.0,
            "convergence_state": ST_SAMPLING,
            "eta_status": "评估中",
            "eta_mins_raw": "--",
            "eta_mins_smoothed": "--",
            "last_log_event": "无",
            "persisted_loaded": 0,
            "backlog_events": 0,
            "history_error": "",
        }
        self._load_persistent_state()

    # ------------------------------------------------------------------ session
    def get_active_session_event_db(self) -> tuple[str, Path | None]:
        """编号最大且含 event-db.sqlite 的 session 视为当前活动 session。"""
        try:
            if not self.session_dir.exists():
                return "未知", None
            folders = [p for p in self.session_dir.iterdir() if p.is_dir() and p.name.isdigit()]
            for sf in sorted(folders, key=lambda p: int(p.name), reverse=True):
                edb = sf / "db" / "event-db.sqlite"
                if edb.exists():
                    return sf.name, edb
        except OSError as e:
            logger.error(f"get_active_session_event_db error: {e}")
        return "未知", None

    def event_db_summary(self) -> list[tuple[str, int | str]]:
        """诊断用：列出当前 session event-db 各表行数（只读、按需调用）。"""
        if not self.event_db or not self.event_db.exists():
            return []
        out: list[tuple[str, int | str]] = []
        try:
            with sqlite_ro(self.event_db) as con:
                names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                for n in names:
                    try:
                        out.append((n, con.execute(f"SELECT COUNT(*) FROM {qident(n)}").fetchone()[0]))
                    except sqlite3.Error as e:
                        out.append((n, f"读取失败: {e}"))
        except sqlite3.Error as e:
            out.append(("(event-db)", f"打开失败: {e}"))
        return out

    # ------------------------------------------------------------------ 持久化
    def _load_persistent_state(self):
        """继承上次运行留下的在途事件池（同一 session 且 1 小时内）。"""
        try:
            if not self.state_file.exists():
                return
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if data.get("session_id") != self.session_id:
                return
            if self._clock() - float(data.get("saved_time", 0)) >= STATE_MAX_AGE_SECS:
                return
            events = data.get("outstanding_events", {})
            if isinstance(events, dict):
                self.outstanding_events = {k: v for k, v in events.items() if isinstance(v, dict)}
                self.diag["persisted_loaded"] = len(self.outstanding_events)
                logger.info(f"Loaded {len(self.outstanding_events)} outstanding events from state file.")
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"Failed to load state: {e}")

    def save_persistent_state(self):
        try:
            with self._lock:
                items = list(self.outstanding_events.items())[-STATE_MAX_EVENTS:]
                payload = {"session_id": self.session_id, "saved_time": self._clock(), "outstanding_events": dict(items)}
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
            self._last_state_save = self._clock()
        except OSError as e:
            logger.warning(f"Failed to save state: {e}")

    # ------------------------------------------------------------------ 日志
    def _resolve_log_file(self) -> Path | None:
        log_dir = self.base / "log"
        p = log_dir / "daemon.log"
        if p.exists():
            return p
        try:
            cands = [c for c in log_dir.glob("*daemon*.log") if c.is_file()]
        except OSError:
            return None
        return max(cands, key=lambda c: c.stat().st_mtime) if cands else None

    def _read_new_log_lines(self) -> tuple[list[str], bool, float]:
        """返回 (完整行列表, 是否为启动回读, 文件 mtime)。未写完的末行留到下次。"""
        p = self._resolve_log_file()
        if p is None:
            return [], False, 0.0
        try:
            st = p.stat()
        except OSError:
            return [], False, 0.0

        backlog = False
        size = st.st_size
        if self._log_tell is None:
            # 首次启动：只回读尾部一段，用来重建在途池，不计入速率
            start = max(0, size - LOG_BACKLOG_BYTES)
            backlog = True
            self._log_partial = b""
        elif p != self.log_file or size < self._log_tell:
            # 换了文件或被截断 / 轮转：新文件从头读，都是新内容
            start = 0
            self._log_partial = b""
        else:
            start = self._log_tell
        self.log_file = p

        if size <= start:
            self._log_tell = start
            return [], backlog, st.st_mtime

        try:
            with open(p, "rb") as f:
                f.seek(start)
                data = f.read(min(size - start, LOG_MAX_READ_BYTES))
                self._log_tell = f.tell()
        except OSError as e:
            logger.error(f"Error reading daemon.log: {e}")
            return [], backlog, st.st_mtime

        if backlog and start > 0:
            nl = data.find(b"\n")  # 从文件中间开始读，丢掉第一行残片
            data = data[nl + 1 :] if nl >= 0 else b""

        buf = self._log_partial + data
        parts = buf.split(b"\n")
        self._log_partial = parts.pop()
        if len(self._log_partial) > 1024 * 1024:  # 防御：异常超长行
            self._log_partial = b""
        lines = [x.decode("utf-8", errors="replace").rstrip("\r") for x in parts]
        return lines, backlog, st.st_mtime

    def _sync_daemon_log_events(self, now: float):
        lines, backlog, mtime = self._read_new_log_lines()
        if not lines:
            self._expire_outstanding(now)
            return

        # 回读行使用日志自带时间戳，并以文件 mtime 校正时区偏差；
        # 统一压到监控启动之前，保证不会被计入"启动后"的速率。
        offset = 0.0
        if backlog:
            last_ts = next((t for t in map(parse_line_ts, reversed(lines)) if t is not None), None)
            if last_ts is not None and mtime:
                offset = mtime - last_ts
        floor_ts = self.monitor_start_time - 1.0

        backlog_events = 0
        for line in lines:
            if backlog:
                t = parse_line_ts(line)
                ts = min(floor_ts, (t + offset) if t is not None else floor_ts)
            else:
                ts = now

            alert = parse_log_alert(line)
            if alert:
                self._record_log_alert(alert, ts)

            ev = parse_event(line)
            if ev:
                kind, info = ev
                key = info.pop("key")
                if kind == "push":
                    self.outstanding_events[key] = {"timestamp": ts, **info}
                    if not backlog:
                        self.push_history.append(ts)
                else:
                    self.outstanding_events.pop(key, None)
                    if not backlog:
                        self.done_history.append(ts)
                if backlog:
                    backlog_events += 1
                else:
                    self.last_activity_time = now

            if any(k in line for k in _ACTIVITY_KEYWORDS):
                if not backlog:
                    self.last_activity_time = now
                self.diag["last_log_event"] = compact(line)
                fpath = extract_path(line)
                if fpath:
                    self.current_processing_file = fpath
                    self.current_file_last_seen = ts
                    if "Three-Way-Merge" in line:
                        self.last_log_action = "扫描合并"
                    elif "Upload" in line:
                        self.last_log_action = "上传中"
                    elif "Download" in line:
                        self.last_log_action = "下载中"
                    else:
                        self.last_log_action = "流式处理"

        if backlog:
            self.diag["backlog_events"] = backlog_events
        self._expire_outstanding(now)

    def _record_log_alert(self, alert: tuple[str, str, str], ts: float):
        level, msg, sig = alert
        g = self.log_alerts.get(sig)
        if g is None:
            if len(self.log_alerts) >= LOG_ALERT_MAX_GROUPS:
                del self.log_alerts[min(self.log_alerts, key=lambda k: self.log_alerts[k]["last_ts"])]
            self.log_alerts[sig] = {"level": level, "message": msg, "count": 1, "first_ts": ts, "last_ts": ts}
        else:
            g["count"] += 1
            g["last_ts"] = max(g["last_ts"], ts)
            g["message"] = msg

    def _log_alert_rows(self) -> list[dict]:
        rows = sorted(self.log_alerts.values(), key=lambda g: g["last_ts"], reverse=True)
        return [
            {"time": format_ts(g["last_ts"]), "level": g["level"], "count": g["count"], "message": g["message"]} for g in rows
        ]

    def _expire_outstanding(self, now: float):
        cutoff = now - OUTSTANDING_TTL_SECS
        stale = [k for k, v in self.outstanding_events.items() if float(v.get("timestamp", 0) or 0) < cutoff]
        overflow = len(self.outstanding_events) - len(stale) - OUTSTANDING_MAX
        if overflow > 0:
            stale_set = set(stale)
            fresh = sorted(
                (k for k in self.outstanding_events if k not in stale_set),
                key=lambda k: float(self.outstanding_events[k].get("timestamp", 0) or 0),
            )
            stale.extend(fresh[:overflow])
        for k in stale:
            del self.outstanding_events[k]
        self.expired_events += len(stale)

    def _calc_rate(self, history: deque, window_seconds: float, now: float) -> tuple[float, int]:
        effective_start = max(now - window_seconds, self.monitor_start_time)
        cnt = 0
        for ts in reversed(history):  # 时间递增，遇到窗口外的即可停止
            if ts < effective_start:
                break
            cnt += 1
        duration_sec = max(1.0, now - effective_start)
        return round(cnt / (duration_sec / 60.0), 1), cnt

    def _rate_series(self, now: float, minutes: int = RATE_SERIES_MINUTES) -> list[tuple[int, int]]:
        """最近 N 分钟每分钟的 (新增 Push, 完成 Done) 条数，最后一项是最近一分钟。"""
        start = now - minutes * 60.0
        buckets = [[0, 0] for _ in range(minutes)]
        for idx, history in enumerate((self.push_history, self.done_history)):
            for ts in reversed(history):
                if ts < start:
                    break
                buckets[min(minutes - 1, int((ts - start) // 60))][idx] += 1
        return [(b[0], b[1]) for b in buckets]

    def _local_exists(self, path: str) -> bool | None:
        """核对本地文件是否还在；不是本机绝对路径或未启用核对时返回 None（不猜）。"""
        if not self.check_local_files:
            return None
        if os.name == "nt":
            if not re.match(r"^[A-Za-z]:[\\/]", path):  # 只查本地盘符路径，UNC 可能卡住
                return None
        elif not path.startswith("/") or path.startswith("//"):
            return None
        try:
            return os.path.exists(path)
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------------ history.sqlite
    @staticmethod
    def _empty_history() -> dict:
        return {"recent_rows": [], "actionable": [], "temp": [], "recovered": [], "error": ""}

    def _history_signature(self) -> tuple:
        sig = []
        for suffix in ("", "-wal"):
            p = Path(str(self.history) + suffix)
            try:
                st = p.stat()
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        return tuple(sig)

    def _maybe_query_history(self, now: float, force: bool):
        if not self.history.exists():
            self._history_cache = self._empty_history()
            self._history_cache["error"] = "未找到 history.sqlite"
            return
        if not force and now - self._history_checked < HISTORY_REFRESH_SECS:
            return
        self._history_checked = now
        sig = self._history_signature()
        if not force and sig == self._history_sig:
            return
        try:
            self._history_cache = self.query_history()
            self._history_sig = sig
        except sqlite3.Error as e:
            logger.error(f"Error querying history.sqlite: {e}")
            self._history_cache["error"] = str(e)
        # session 可能因客户端重建而变化
        self.session_id, self.event_db = self.get_active_session_event_db()

    def query_history(self) -> dict:
        """读取最近活动与同步异常。

        异常规则：is_not_synced != 0 且原因不是 -8192（选择性同步过滤），按路径去重保留最新一条；
        若同一路径之后又有成功同步记录，则归入"已恢复"，不再计为需关注异常；
        临时 / 锁文件单独归类。
        """
        res = self._empty_history()
        with sqlite_ro(self.history) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "history_table" not in tables:
                res["error"] = "history.sqlite 中没有 history_table 表"
                return res
            cols = table_columns(con, "history_table")
            path_c = pick(cols, ["path", "local_path", "filepath"])
            time_c = pick(cols, ["update_time", "time"])
            unsynced_c = pick(cols, ["is_not_synced", "not_synced"])
            reason_c = pick(cols, ["not_synced_reason", "reason", "error"])
            action_c = pick(cols, ["action", "event"])
            if not path_c or not time_c:
                res["error"] = f"history_table 缺少路径或时间列（现有列: {', '.join(cols)}）"
                return res

            P, T = qident(path_c), qident(time_c)
            A = qident(action_c) if action_c else "NULL"

            for r in con.execute(f"SELECT {T}, {A}, {P} FROM history_table ORDER BY {T} DESC LIMIT {MAX_RECENT}"):
                raw_act = r[1]
                try:
                    act = ACTION_MAP.get(int(raw_act), str(raw_act))
                except (TypeError, ValueError):
                    act = "" if raw_act is None else str(raw_act)
                res["recent_rows"].append({"time": format_ts(r[0]), "action": act, "path": display_path(r[2] or "")})

            if not unsynced_c:
                return res
            U = qident(unsynced_c)
            R = f"COALESCE(CAST({qident(reason_c)} AS TEXT), '')" if reason_c else "''"
            rows = con.execute(
                f"SELECT {P}, {R}, {T} FROM history_table "
                f"WHERE COALESCE({U}, 0) != 0 AND {R} != ? ORDER BY {T} DESC LIMIT {MAX_UNSYNCED}",
                (FILTERED_REASON,),
            ).fetchall()

            seen: dict[str, dict] = {}
            for r in rows:
                raw_path = r[0] or ""
                key = normalize_path(raw_path)
                if not key or key in seen:
                    continue
                code = str(r[1])
                seen[key] = {
                    "time": format_ts(r[2]),
                    "_raw_time": r[2],
                    "_raw_path": raw_path,
                    "path": display_path(raw_path),
                    "raw_code": code,
                    "reason": ERROR_REASON_MAP.get(code, f"错误代码 {code}" if code else "未知原因"),
                    "is_temp": is_temp_file(raw_path),
                }

            # 之后是否又成功同步过（分批 IN 查询，避免 SQLite 变量数上限）
            synced_after: dict[str, object] = {}
            raw_paths = [v["_raw_path"] for v in seen.values()]
            for i in range(0, len(raw_paths), 400):
                chunk = raw_paths[i : i + 400]
                q = (
                    f"SELECT {P}, MAX({T}) FROM history_table "
                    f"WHERE COALESCE({U}, 0) = 0 AND {P} IN ({','.join('?' * len(chunk))}) GROUP BY {P}"
                )
                for r in con.execute(q, chunk):
                    synced_after[normalize_path(r[0] or "")] = r[1]

        for key, item in seen.items():
            ok_t = synced_after.get(key)
            raw_t = item.pop("_raw_time")
            item.pop("_raw_path")
            try:
                recovered = ok_t is not None and raw_t is not None and float(ok_t) > float(raw_t)
            except (TypeError, ValueError):
                recovered = ok_t is not None and str(ok_t) > str(raw_t)
            item["hint"] = ""
            if recovered:
                item["status"] = "已恢复"
                res["recovered"].append(item)
            elif item["is_temp"]:
                item["status"] = "临时文件"
                res["temp"].append(item)
            else:
                item["status"] = "需关注"
                exists = self._local_exists(item["path"]) if len(res["actionable"]) < LOCAL_CHECK_MAX else None
                item["hint"] = "；".join(path_hints(item["path"], exists))
                res["actionable"].append(item)
        return res

    # ------------------------------------------------------------------ 主入口
    def read_metrics(self, force_history: bool = False) -> dict:
        with self._lock:
            return self._read_metrics(force_history)

    def _read_metrics(self, force_history: bool) -> dict:
        now = self._clock()
        self._sync_daemon_log_events(now)

        outstanding = len(self.outstanding_events)
        self.remaining_samples.append((now, outstanding))
        self.diag["outstanding_count"] = outstanding

        uptime = now - self.monitor_start_time
        self.diag["monitor_age_secs"] = int(uptime)
        push_1m, push_cnt_1m = self._calc_rate(self.push_history, 60.0, now)
        done_1m, done_cnt_1m = self._calc_rate(self.done_history, 60.0, now)
        push_5m, _ = self._calc_rate(self.push_history, 300.0, now)
        done_5m, _ = self._calc_rate(self.done_history, 300.0, now)
        net_5m = round(done_5m - push_5m, 1)
        net_1m = round(done_1m - push_1m, 1)
        self.diag.update(
            push_rate_1m=push_1m,
            done_rate_1m=done_1m,
            net_rate_1m=net_1m,
            push_rate_5m=push_5m,
            done_rate_5m=done_5m,
            net_drain_rate_5m=net_5m,
        )

        state = classify_convergence(uptime, push_5m, done_5m, outstanding)
        self.diag["convergence_state"] = state
        stall_desc = {
            ST_SAMPLING: "正在流式采样",
            ST_CONVERGED: "同步已收敛 · 空闲",
            ST_LOW_RATE: f"低速运行 · 在途 {outstanding:,}",
            ST_CONVERGING: "正在收敛 · 队列递减",
        }.get(state, state)

        # 卡片：净消化速度
        if uptime < SAMPLING_SECS or (done_cnt_1m + push_cnt_1m) < 8:
            net_val, net_sub = "采样中", f"已采样 {int(min(uptime, 999))}s"
        elif net_5m > 0:
            net_val, net_sub = f"{net_5m} / min", f"完成 {done_5m} · 新增 {push_5m} (5m)"
        elif net_5m < 0:
            # 负数 = 队列在变长（v1.1 这里显示成 "+N"，容易被读成"消化得更快"）
            net_val, net_sub = f"-{abs(net_5m)} / min", f"队列增长中 · 新增 {push_5m} · 完成 {done_5m} (5m)"
        else:
            net_val, net_sub = "平衡", f"新增 {push_5m} ≈ 完成 {done_5m}"
        if state in (ST_CONVERGED, ST_LOW_RATE) and uptime >= SAMPLING_SECS:
            net_val, net_sub = "空闲", f"完成 {done_5m} · 新增 {push_5m} (5m)"

        # 卡片：ETA（只在收敛 / 尾部阶段计算，其余如实降级）
        eta_val, eta_sub = "--", state
        eta_calc = False
        if state == ST_CONVERGED:
            eta_val, eta_sub = "已同步完成", ST_CONVERGED
        elif uptime < ETA_SAMPLING_SECS or (done_cnt_1m + push_cnt_1m) < 15:
            if state == ST_LOW_RATE:
                eta_val, eta_sub = "暂无法稳定估算", "事件流很少，在途事件可能未配对"
            else:
                eta_val, eta_sub = "采样中", f"等待样本积累 ({int(min(uptime, 999))}s / {ETA_SAMPLING_SECS}s)"
        elif state == ST_TAIL and done_5m > 0:
            self._smooth_eta(outstanding / max(1.0, done_5m))
            eta_val, eta_sub, eta_calc = format_eta_human(self.smoothed_eta_mins), "高可信度 · 正在处理尾部任务", True
        elif state == ST_CONVERGING and net_5m > 1.0:
            self._smooth_eta(outstanding / net_5m)
            eta_val, eta_sub, eta_calc = format_eta_human(self.smoothed_eta_mins), "正在收敛 (基于净消化速度)", True
        elif state == ST_BALANCED:
            eta_val, eta_sub = "暂不稳定", "事件流接近平衡"
        elif state == ST_GROWING:
            eta_val, eta_sub = "暂无法估算", "仍在生成新任务"
        else:
            eta_val, eta_sub = "暂无法稳定估算", state
        if not eta_calc:
            self.smoothed_eta_mins = None  # 离开可估算阶段后重置平滑，避免旧值污染
        self.diag["eta_status"] = f"{eta_val} ({eta_sub})"

        self._maybe_query_history(now, force_history)
        hist = self._history_cache
        self.diag["history_error"] = hist.get("error", "")
        self.diag["session_id"] = self.session_id

        # 停滞检测：只有"还有在途事件却长时间没有进展"才告警，空闲不算停滞
        idle = int(now - self.last_activity_time)
        pstate = process_state()
        stall_status = "active"
        if pstate == "未运行":
            stall_status, stall_desc = "offline", "客户端未运行"
        elif outstanding > 0 and idle >= STALL_ALERT_SECS:
            stall_status, stall_desc = "stalled", f"长时间无进展 ({idle // 60} 分钟)"
        elif outstanding > 0 and idle >= STALL_WARN_SECS:
            stall_status, stall_desc = "warning", f"可能无进展 ({idle // 60} 分钟)"
        elif state == ST_CONVERGED:
            stall_status = "finished"

        curr_stale = (now - self.current_file_last_seen) > CURRENT_FILE_STALE_SECS
        active_label = "最近处理" if curr_stale else f"正在处理 · {self.last_log_action}"

        if self.persist and now - self._last_state_save >= STATE_SAVE_SECS:
            self.save_persistent_state()

        alerts = self._log_alert_rows()
        m = {
            "time": now,
            "process_state": pstate,
            "session_id": self.session_id,
            "outstanding": outstanding,
            "rem_val_str": f"{outstanding:,}",
            "net_val_str": net_val,
            "net_sub_str": net_sub,
            "push_rate_5m": push_5m,
            "done_rate_5m": done_5m,
            "net_drain_rate_5m": net_5m,
            "push_rate_1m": push_1m,
            "done_rate_1m": done_1m,
            "eta_str": eta_val,
            "eta_sub_desc": eta_sub,
            "convergence_state": state,
            "actionable_errors": hist["actionable"],
            "temp_errors": hist["temp"],
            "recovered_errors": hist["recovered"],
            "actionable_errors_count": len(hist["actionable"]),
            "office_temp_count": len(hist["temp"]),
            "recovered_count": len(hist["recovered"]),
            "history_error": hist.get("error", ""),
            "recent_rows": hist["recent_rows"],
            "stall_status": stall_status,
            "stall_desc": stall_desc,
            "idle_secs": idle,
            "current_file": self.current_processing_file,
            "active_label": active_label,
            "remaining_samples": list(self.remaining_samples),
            "rate_series": self._rate_series(now),
            "log_alerts": alerts,
            "log_alert_errors": sum(1 for a in alerts if a["level"] == "错误"),
            "error_breakdown": error_breakdown(hist["actionable"]),
        }
        m["summary_level"], m["summary"] = summarize(m)
        return m

    def _smooth_eta(self, raw_mins: float):
        if self.smoothed_eta_mins is None:
            self.smoothed_eta_mins = raw_mins
        else:
            self.smoothed_eta_mins = self.smoothed_eta_mins * 0.85 + raw_mins * 0.15
        self.diag["eta_mins_raw"] = f"{round(raw_mins, 1)} min"
        self.diag["eta_mins_smoothed"] = f"{round(self.smoothed_eta_mins, 1)} min"

    # ------------------------------------------------------------------ 诊断
    def outstanding_breakdown(self, now: float | None = None) -> tuple[Counter, float | None]:
        """按 sync_id 统计在途事件，并返回最老一条的等待秒数。"""
        now = self._clock() if now is None else now
        with self._lock:
            events = list(self.outstanding_events.values())
        by_sync = Counter(str(e.get("sync_id") or "?") for e in events)
        oldest = min((float(e.get("timestamp", now) or now) for e in events), default=None)
        return by_sync, (now - oldest) if oldest is not None else None


def summarize(m: dict) -> tuple[str, str]:
    """把各项指标翻译成一句人话结论。返回 (级别, 文本)，级别为 ok / info / warn / bad / off。"""
    stall, state = m.get("stall_status"), m.get("convergence_state")
    n = m.get("outstanding", 0) or 0
    idle_min = int((m.get("idle_secs", 0) or 0) // 60)
    net = m.get("net_drain_rate_5m", 0) or 0
    if stall == "offline":
        level, text = "off", "群晖 Drive 客户端没有在运行，下面的数据不会再更新。"
    elif stall == "stalled":
        level, text = "bad", f"还有 {n:,} 个在途事件，但已经 {idle_min} 分钟没有任何进展，建议检查网络或客户端状态。"
    elif stall == "warning":
        level, text = "warn", f"还有 {n:,} 个在途事件，最近 {idle_min} 分钟没有进展，继续观察。"
    elif state == ST_CONVERGED:
        level, text = "ok", "同步已完成，当前空闲。"
    elif state == ST_GROWING:
        level, text = "warn", f"新任务产生得比处理快（每分钟净增约 {abs(net):g} 个），队列还在变长。"
    elif state == ST_CONVERGING:
        eta = m.get("eta_str", "")
        tail = f"预计{eta}后完成。" if eta.startswith("约") or eta.startswith("<") else "完成时间还在估算。"
        level, text = "ok", f"队列正在变短（在途 {n:,}），{tail}"
    elif state == ST_TAIL:
        eta = m.get("eta_str", "")
        tail = f"预计{eta}后完成。" if eta.startswith("约") or eta.startswith("<") else ""
        level, text = "ok", f"没有新任务了，正在处理剩下的 {n:,} 个。{tail}"
    elif state == ST_BALANCED:
        level, text = "info", f"正在同步：新增和完成的速度差不多，在途 {n:,} 个。"
    elif state == ST_LOW_RATE:
        level, text = "info", f"几乎没有事件流，但还有 {n:,} 个在途事件未配对完成。"
    else:
        level, text = "info", "刚启动，正在采样事件流……"

    errs = m.get("actionable_errors_count", 0) or 0
    if errs:
        text += f" 另有 {errs:,} 个文件未同步，需要关注。"
        if level in ("ok", "info"):
            level = "warn"
    return level, text


FAIL_CONDITIONS = ("stalled", "warning", "offline", "errors")


def check_failures(m: dict, conditions: list[str] | tuple[str, ...]) -> list[str]:
    """--fail-on 用：返回当前命中的条件（stalled / warning / offline / errors）。"""
    hit = []
    for c in conditions:
        if c == "errors":
            if (m.get("actionable_errors_count") or 0) > 0:
                hit.append(c)
        elif c in ("stalled", "warning", "offline") and m.get("stall_status") == c:
            hit.append(c)
    return hit


# ==============================================================================
# 界面设置（只存本程序自己的偏好，放在本程序的数据目录）
# ==============================================================================
DEFAULT_SETTINGS: dict = {
    "theme": "system",  # system / light / dark
    "topmost": False,
    "flash_alerts": True,
    "show_temp": False,
    "show_recovered": False,
    "geometry": "",
}


def settings_file() -> Path:
    return data_dir() / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    """读取设置；文件缺失、损坏或类型不对的项一律回落到默认值。"""
    out = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads((path or settings_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for k, default in DEFAULT_SETTINGS.items():
            if k in data and type(data[k]) is type(default):
                out[k] = data[k]
    if out["theme"] not in ("system", "light", "dark"):
        out["theme"] = "system"
    return out


def save_settings(settings: dict, path: Path | None = None) -> bool:
    path = path or settings_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}, ensure_ascii=False), "utf-8")
        tmp.replace(path)
        return True
    except OSError as e:
        logger.warning(f"Failed to save settings: {e}")
        return False


def build_diagnostics_report(reader: DriveReader, metrics: dict | None) -> str:
    d = dict(reader.diag)
    m = metrics or {}
    by_sync, oldest_age = reader.outstanding_breakdown()
    age = d.get("monitor_age_secs", 0) or 0
    sync_desc = ", ".join(f"sync_id {k}: {v:,}" for k, v in by_sync.most_common(6)) or "无"
    lines = [
        "===========================================================",
        f"         SYNOLOGY DRIVE MONITOR {APP_VERSION} DIAGNOSTICS",
        "===========================================================",
        f"Generated At          : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Process Status        : {process_state(force=True)}",
        f"Summary               : {m.get('summary', '')}",
        f"Monitor Uptime        : {age}s ({round(age / 60.0, 1)} min)",
        f"Drive Data Dir        : {reader.base}",
        f"Active Session        : {reader.session_id}",
        f"Event DB              : {reader.event_db or '未找到'}",
        f"Daemon Log            : {reader.log_file or '未找到'}",
        "-----------------------------------------------------------",
        " [当前在途事件 (Outstanding Pool)]",
        f"Outstanding Events    : {d.get('outstanding_count', 0):,} (Push 未配对 Done)",
        f"By sync_id            : {sync_desc}",
        f"Oldest Outstanding    : {format_age(oldest_age) if oldest_age is not None else '无'}",
        f"Expired (>2h, 累计)   : {reader.expired_events:,}",
        f"Rebuilt From Backlog  : {d.get('backlog_events', 0):,} 条事件 (启动时回读，不计入速率)",
        f"Loaded From State File: {d.get('persisted_loaded', 0):,} 条",
        f"State File            : {reader.state_file}",
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
        "Scan Coverage Model   : UNSUPPORTED (本地 Three-Way-Merge 非全量遍历，降级为流式收敛模式)",
        "Cursor Gap Model      : UNSUPPORTED (max_id 非全局高水位，不可计算游标差)",
        "-----------------------------------------------------------",
        " [同步异常 (分级核验)]",
        f"History SQLite Path   : {reader.history}",
        f"需关注 / 临时 / 已恢复 : {m.get('actionable_errors_count', 0)} / "
        f"{m.get('office_temp_count', 0)} / {m.get('recovered_count', 0)}",
    ]
    if d.get("history_error"):
        lines.append(f"History Error         : {d['history_error']}")
    bd = m.get("error_breakdown") or {}
    for title, key in (("按原因", "by_reason"), ("按文件夹 (前 5)", "by_folder")):
        if bd.get(key):
            lines.append(f"  {title}:")
            lines.extend(f"    {cnt:>5,}  {name}" for name, cnt in bd[key])
    lines.append("-----------------------------------------------------------")
    lines.append(" [daemon.log 告警 (同类合并，最近 8 组)]")
    alerts = m.get("log_alerts") or []
    lines.extend(f"  {a['time']} [{a['level']}] x{a['count']:<4} {a['message'][:150]}" for a in alerts[:8])
    if not alerts:
        lines.append("  (未发现 [ERROR] / [WARNING] 行)")
    lines.append("-----------------------------------------------------------")
    lines.append(" [Event DB 表行数 (只读统计)]")
    summary = reader.event_db_summary()
    lines.extend(f"  {name:<20}: {cnt:,}" if isinstance(cnt, int) else f"  {name:<20}: {cnt}" for name, cnt in summary)
    if not summary:
        lines.append("  (无)")
    lines += [
        "-----------------------------------------------------------",
        f"Last Log Activity     : {d.get('last_log_event')}",
        "===========================================================",
        " 说明：本工具通过读取本地数据库与日志进行严格只读监控。",
        " 流速与收敛状态属于本地观测推算，非群晖官方公开数据；",
        " 错误 / 动作代码含义为经验映射，未经群晖官方确认。",
        "===========================================================",
    ]
    return "\n".join(lines)


def snapshot(reader: DriveReader, metrics: dict) -> dict:
    """命令行 --json 输出：去掉 UI 专用字段，便于脚本 / 自动化读取。"""
    keep = (
        "process_state",
        "session_id",
        "outstanding",
        "push_rate_1m",
        "done_rate_1m",
        "push_rate_5m",
        "done_rate_5m",
        "net_drain_rate_5m",
        "convergence_state",
        "eta_str",
        "eta_sub_desc",
        "stall_status",
        "stall_desc",
        "idle_secs",
        "current_file",
        "actionable_errors_count",
        "office_temp_count",
        "recovered_count",
        "history_error",
        "summary",
        "summary_level",
        "log_alert_errors",
    )
    out = {k: metrics.get(k) for k in keep}
    out["version"] = APP_VERSION
    out["generated_at"] = datetime.now().isoformat(timespec="seconds")
    out["base"] = str(reader.base)
    out["actionable_errors"] = [
        {k: e.get(k, "") for k in ("time", "raw_code", "reason", "hint", "path")} for e in metrics.get("actionable_errors", [])
    ]
    out["error_breakdown"] = metrics.get("error_breakdown", {})
    out["log_alerts"] = (metrics.get("log_alerts") or [])[:20]
    return out
