"""drive_core 单元测试：全部使用合成的 daemon.log / history.sqlite，不需要真实群晖客户端。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import drive_core as core  # noqa: E402


class Clock:
    # 默认取真实当前时间：启动回读会用日志文件的真实 mtime 校正时间戳
    def __init__(self, t: float | None = None):
        self.t = time.time() if t is None else t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float):
        self.t += secs


def push(i: int, sync: int = 2) -> str:
    return f"2026-11-06T10:00:00 [INFO] PushEvent = Event<LocalModify> sync_id: {sync} path: 'C:\\Docs\\f{i}.docx'\n"


def done(i: int, sync: int = 2) -> str:
    return f"2026-11-06T10:00:01 [INFO] DoneEvent: Event<LocalModify> sync_id: {sync} path: 'C:\\Docs\\f{i}.docx'\n"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "SynologyDrive"
    (b / "log").mkdir(parents=True)
    (b / "data" / "db").mkdir(parents=True)
    for sid in ("3", "7", "12"):
        (b / "data" / "session" / sid / "db").mkdir(parents=True)
    # session 12 没有 event-db，应选中 7
    for sid in ("3", "7"):
        con = sqlite3.connect(b / "data" / "session" / sid / "db" / "event-db.sqlite")
        con.execute("CREATE TABLE events (id INTEGER)")
        con.executemany("INSERT INTO events VALUES (?)", [(i,) for i in range(5)])
        con.commit()
        con.close()
    (b / "log" / "daemon.log").write_text("", encoding="utf-8")
    return b


def make_history(base: Path, rows: list[tuple]) -> Path:
    p = base / "data" / "db" / "history.sqlite"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE history_table (path TEXT, update_time INTEGER, is_not_synced INTEGER, not_synced_reason INTEGER, action INTEGER)"
    )
    con.executemany("INSERT INTO history_table VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def append(base: Path, text: str):
    with open(base / "log" / "daemon.log", "a", encoding="utf-8", newline="") as f:
        f.write(text)


def reader_for(base: Path, tmp_path: Path, clock: Clock, **kw) -> core.DriveReader:
    return core.DriveReader(base, state_file=tmp_path / "state.json", clock=clock, **kw)


# ------------------------------------------------------------------ session
def test_active_session_is_highest_with_event_db(base, tmp_path):
    r = reader_for(base, tmp_path, Clock())
    assert r.session_id == "7"
    assert r.event_db.name == "event-db.sqlite"
    assert r.event_db_summary() == [("events", 5)]


# ------------------------------------------------------------------ 日志
def test_backlog_rebuilds_pool_but_not_rates(base, tmp_path):
    append(base, "".join(push(i) for i in range(100)) + "".join(done(i) for i in range(40)))
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    clock.advance(20)
    m = r.read_metrics()
    assert m["outstanding"] == 60
    # v1.0 会把回读的 140 条全部计为"刚刚发生"，1 分钟速率暴涨
    assert m["push_rate_1m"] == 0 and m["done_rate_1m"] == 0
    assert r.diag["backlog_events"] == 140
    assert m["idle_secs"] == 20


def test_live_events_pair_and_count(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    clock.advance(30)
    append(base, push(1) + push(2) + push(3) + done(1) + done(2))
    m = r.read_metrics()
    assert m["outstanding"] == 1
    assert m["idle_secs"] == 0
    assert len(r.push_history) == 3 and len(r.done_history) == 2


def test_partial_line_is_buffered_until_complete(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    line = push(9)
    append(base, line[:40])
    assert r.read_metrics()["outstanding"] == 0
    append(base, line[40:])
    assert r.read_metrics()["outstanding"] == 1
    assert "C:\\Docs\\f9.docx" in next(iter(r.outstanding_events))


def test_multibyte_split_across_reads(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    line = "2026-11-06T10:00:00 PushEvent = Event<X> sync_id: 1 path: 'C:\\资料\\图纸.dwg'\n".encode()
    cut = line.index("图".encode()) + 1  # 切在多字节字符中间
    with open(base / "log" / "daemon.log", "ab") as f:
        f.write(line[:cut])
    r.read_metrics()
    with open(base / "log" / "daemon.log", "ab") as f:
        f.write(line[cut:])
    r.read_metrics()
    assert any("图纸.dwg" in v["path"] for v in r.outstanding_events.values())


def test_log_truncation_reads_new_file_from_start(base, tmp_path):
    append(base, "".join(push(i) for i in range(50)))
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    (base / "log" / "daemon.log").write_text(done(0) + push(999), encoding="utf-8")  # 轮转后的新文件更短
    m = r.read_metrics()
    assert m["outstanding"] == 50  # 去掉 f0，新增 f999
    assert any("f999" in k for k in r.outstanding_events)


def test_outstanding_expires_after_ttl(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    append(base, push(1))
    assert r.read_metrics()["outstanding"] == 1
    clock.advance(core.OUTSTANDING_TTL_SECS + 1)
    assert r.read_metrics()["outstanding"] == 0
    assert r.expired_events == 1


# ------------------------------------------------------------------ 状态与停滞
def test_idle_with_empty_pool_is_not_stalled(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    clock.advance(3600)
    m = r.read_metrics()
    assert m["convergence_state"] == core.ST_CONVERGED
    assert m["stall_status"] == "finished"  # v1.0 这里会报"长时间无进展"
    assert m["eta_str"] == "已同步完成"


def test_idle_with_pending_events_warns(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    append(base, push(1))
    r.read_metrics()
    clock.advance(core.STALL_ALERT_SECS + 5)
    m = r.read_metrics()
    assert m["convergence_state"] == core.ST_LOW_RATE
    assert m["stall_status"] == "stalled"


@pytest.mark.parametrize(
    "args,expected",
    [
        ((5, 0, 0, 0), core.ST_SAMPLING),
        ((60, 1, 1, 0), core.ST_CONVERGED),
        ((60, 1, 1, 3), core.ST_LOW_RATE),
        ((60, 1, 50, 3), core.ST_TAIL),
        ((60, 20, 50, 3), core.ST_CONVERGING),
        ((60, 50, 20, 3), core.ST_GROWING),
        ((60, 50, 48, 3), core.ST_BALANCED),
    ],
)
def test_classify_convergence(args, expected):
    assert core.classify_convergence(*args) == expected


def test_tail_eta_is_computed(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    append(base, "".join(push(i) for i in range(60)))
    clock.advance(1)
    r.read_metrics()
    clock.advance(310)  # 让 Push 移出 5 分钟窗口
    for step in range(30):  # 之后只有 Done，没有新 Push
        clock.advance(2)
        append(base, done(step))
        m = r.read_metrics()
    assert m["outstanding"] == 30
    assert m["convergence_state"] == core.ST_TAIL
    assert "分钟" in m["eta_str"] or "小时" in m["eta_str"]


# ------------------------------------------------------------------ history.sqlite
def test_history_error_classification(base, tmp_path):
    t = 1_700_000_000
    hist = make_history(
        base,
        [
            ("C:\\Docs\\filtered.iso", t + 1, 1, -8192, 34),  # 选择性同步过滤 → 排除
            ("C:\\Docs\\locked.xlsx", t + 2, 1, -2048, 34),  # 真实异常
            ("c:\\docs\\LOCKED.xlsx", t + 1, 1, -2048, 34),  # 大小写不同的重复 → 去重
            ("C:\\Docs\\~$report.docx", t + 3, 1, -2048, 34),  # Office 锁文件 → 临时
            ("C:\\Docs\\null_reason.pdf", t + 4, 1, None, 34),  # NULL 原因 v1.0 会漏掉
            ("C:\\Docs\\fixed.dwg", t + 5, 1, -2048, 34),  # 之后成功同步 → 已恢复
            ("C:\\Docs\\fixed.dwg", t + 9, 0, 0, 2),
            ("C:\\Docs\\ok.txt", t + 6, 0, 0, 1),
        ],
    )
    digest = hashlib.sha256(hist.read_bytes()).hexdigest()

    r = reader_for(base, tmp_path, Clock())
    m = r.read_metrics(force_history=True)

    actionable = {e["path"] for e in m["actionable_errors"]}
    assert actionable == {"C:\\Docs\\locked.xlsx", "C:\\Docs\\null_reason.pdf"}
    assert [e["path"] for e in m["temp_errors"]] == ["C:\\Docs\\~$report.docx"]
    assert [e["path"] for e in m["recovered_errors"]] == ["C:\\Docs\\fixed.dwg"]
    assert m["recent_rows"][0]["action"] == "修改"
    # 只读：数据库内容未被改动
    assert hashlib.sha256(hist.read_bytes()).hexdigest() == digest


def test_history_is_cached_until_file_changes(base, tmp_path):
    make_history(base, [("C:\\a.txt", 1, 1, -2048, 34)])
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics(force_history=True)
    calls = []
    orig = r.query_history
    r.query_history = lambda: calls.append(1) or orig()
    clock.advance(core.HISTORY_REFRESH_SECS + 1)
    r.read_metrics()
    assert calls == []  # 文件未变化，不重复查询


def test_history_missing_columns_is_graceful(base, tmp_path):
    p = base / "data" / "db" / "history.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE history_table (foo TEXT)")
    con.commit()
    con.close()
    m = reader_for(base, tmp_path, Clock()).read_metrics(force_history=True)
    assert "缺少" in m["history_error"]
    assert m["actionable_errors_count"] == 0


def test_sqlite_ro_rejects_writes(base):
    p = make_history(base, [])
    with core.sqlite_ro(p) as con, pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO history_table (path) VALUES ('x')")


def test_sqlite_ro_handles_special_chars_in_path(tmp_path):
    d = tmp_path / "a#b ?c %20"
    d.mkdir()
    p = d / "h.sqlite"
    sqlite3.connect(p).close()
    with core.sqlite_ro(p) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


# ------------------------------------------------------------------ 持久化
def test_state_persists_within_same_session(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock)
    r.read_metrics()
    append(base, push(1) + push(2))
    r.read_metrics()
    r.save_persistent_state()
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["session_id"] == "7"

    r2 = reader_for(base, tmp_path, clock)
    assert r2.diag["persisted_loaded"] == 2

    clock.advance(core.STATE_MAX_AGE_SECS + 1)
    assert reader_for(base, tmp_path, clock).diag["persisted_loaded"] == 0


def test_state_from_other_session_is_ignored(base, tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"session_id": "3", "saved_time": Clock()(), "outstanding_events": {"k": {"timestamp": 1}}}),
        encoding="utf-8",
    )
    assert reader_for(base, tmp_path, Clock()).diag["persisted_loaded"] == 0


def test_persist_false_never_writes(base, tmp_path):
    clock = Clock()
    r = reader_for(base, tmp_path, clock, persist=False)
    clock.advance(60)
    r.read_metrics()
    assert not (tmp_path / "state.json").exists()


# ------------------------------------------------------------------ 输出
def test_snapshot_and_report(base, tmp_path):
    make_history(base, [("C:\\a.txt", 1, 1, -2048, 34)])
    r = reader_for(base, tmp_path, Clock())
    m = r.read_metrics(force_history=True)
    snap = core.snapshot(r, m)
    json.dumps(snap, ensure_ascii=False)
    assert snap["session_id"] == "7" and snap["actionable_errors_count"] == 1
    report = core.build_diagnostics_report(r, m)
    assert "Active Session        : 7" in report
    assert "166,924" not in report  # 不再出现写死的数字


@pytest.mark.parametrize(
    "name,temp",
    [("~$a.docx", True), ("._x.jpg", True), (".~lock.a.odt#", True), ("a.tmp", True), ("plan.dwg", False)],
)
def test_is_temp_file(name, temp):
    assert core.is_temp_file("C:\\x\\" + name) is temp


def test_normalize_path_dedupes_variants():
    assert core.normalize_path("C:\\\\Docs\\\\A.txt") == core.normalize_path("c:/docs/a.TXT")
    assert core.normalize_path("\\\\nas\\share\\x").startswith("\\\\nas")
