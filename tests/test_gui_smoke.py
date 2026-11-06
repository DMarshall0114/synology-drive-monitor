"""GUI 与命令行冒烟测试。需要 tkinter；没有图形环境时自动跳过（Windows CI 上会真实运行）。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

tkinter = pytest.importorskip("tkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import synology_drive_monitor as gui  # noqa: E402


@pytest.fixture
def base(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # 本程序的状态文件写进临时目录
    b = tmp_path / "SynologyDrive"
    (b / "log").mkdir(parents=True)
    (b / "data" / "db").mkdir(parents=True)
    (b / "log" / "daemon.log").write_text(
        "2026-11-06T10:00:00 PushEvent = Event<X> sync_id: 1 path: 'C:\\Docs\\a.docx'\n", encoding="utf-8"
    )
    con = sqlite3.connect(b / "data" / "db" / "history.sqlite")
    con.execute(
        "CREATE TABLE history_table (path TEXT, update_time INTEGER, is_not_synced INTEGER, not_synced_reason INTEGER, action INTEGER)"
    )
    con.executemany(
        "INSERT INTO history_table VALUES (?,?,?,?,?)",
        [("C:\\Docs\\bad.xlsx", 10, 1, -2048, 34), ("C:\\Docs\\~$tmp.docx", 11, 1, -2048, 34), ("C:\\Docs\\ok.txt", 12, 0, 0, 1)],
    )
    con.commit()
    con.close()
    return b


def test_cli_json(base, capsys):
    assert gui.main(["--json", "--base", str(base)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["actionable_errors_count"] == 1
    assert out["outstanding"] == 1


def test_cli_missing_base(tmp_path, capsys):
    assert gui.main(["--json", "--base", str(tmp_path / "nope")]) == 2


def test_gui_smoke(base):
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("没有图形显示环境")
    try:
        app = gui.App(root, base)
        m = app.reader.read_metrics(force_history=True)
        app._apply_metrics(m)
        assert len(app.err_tree.get_children()) == 1
        assert len(app.recent_tree.get_children()) == 3

        # 数据未变化时不重建表格，选中行保留
        first = app.err_tree.get_children()[0]
        app.err_tree.selection_set(first)
        app._apply_metrics(m)
        assert app.err_tree.selection() == (first,)

        app.show_temp_var.set(True)
        app._apply_filter()
        assert len(app.err_tree.get_children()) == 2

        app.open_diagnostics()
        app.refresh_now()
        root.update()
    finally:
        root.destroy()
