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
        "2026-11-06T10:00:00 PushEvent = Event<X> sync_id: 1 path: 'C:\\Docs\\a.docx'\n"
        "2026-11-06T10:00:01 (1:2) [ERROR] proto-client.cpp(88): Failed to connect 'nas' code 110\n",
        encoding="utf-8",
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


def test_cli_fail_on(base, capsys):
    assert gui.main(["--json", "--base", str(base), "--fail-on", "errors,stalled"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["failed_conditions"] == ["errors"]
    # 用 warning 而不是 offline：Windows CI 上确实没有运行群晖客户端，offline 会真的命中
    assert gui.main(["--json", "--base", str(base), "--fail-on", "warning"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit):
        gui.main(["--json", "--base", str(base), "--fail-on", "nonsense"])


def test_color_helpers():
    assert gui.blend("#000000", "#FFFFFF", 0.0) == "#FFFFFF"
    assert gui.blend("#000000", "#FFFFFF", 1.0) == "#000000"
    assert gui.shorten_middle("a" * 100, 21) == "a" * 10 + "…" + "a" * 10
    assert set(gui.PALETTES["light"]) == set(gui.PALETTES["dark"])


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

        # 日志告警页
        assert len(app.alert_tree.get_children()) == 1
        assert "Failed to connect" in str(app.alert_tree.item(app.alert_tree.get_children()[0], "values")[-1])

        # 点击表头排序：升序 → 降序 → 恢复默认
        def paths():
            return [str(app.recent_tree.item(i, "values")[-1]) for i in app.recent_tree.get_children()]

        default = paths()
        app._sort_by("recent", "path")
        assert paths() == sorted(default, key=str.lower)
        app._sort_by("recent", "path")
        assert paths() == sorted(default, key=str.lower, reverse=True)
        app._sort_by("recent", "path")
        assert paths() == default

        # 筛选对三个页签同时生效，清空后恢复
        app.search_var.set("bad.xlsx")
        assert len(app.err_tree.get_children()) == 1 and len(app.recent_tree.get_children()) == 1
        assert len(app.alert_tree.get_children()) == 0
        app.search_var.set("")
        assert len(app.recent_tree.get_children()) == 3

        # 切换主题会重建界面，数据与筛选开关保留
        for theme in ("dark", "light"):
            app.theme_var.set(theme)
            app._apply_theme()
            assert gui.C.name == theme
            assert len(app.err_tree.get_children()) == 2  # show_temp 仍为开
            assert len(app.recent_tree.get_children()) == 3

        app.err_tree.selection_set(app.err_tree.get_children()[0])
        app.copy_selected(app.err_tree)
        app.copy_selected(app.err_tree, name_only=True)
        app.copy_summary()
        # 次要按钮与表格单元格提示在深浅两套色板下都有定义
        for theme in ("light", "dark"):
            assert gui.PALETTES[theme]["control_border"] != gui.PALETTES[theme]["surface"]
        app._reveal_recent_file(0)  # 没有记录时不应报错
        app._build_more_menu()  # 只构建不弹出：Windows 上弹出菜单会阻塞到用户关闭
        app.open_diagnostics()
        app.refresh_now()
        root.update()

        # 设置在关闭时落盘
        app._save_settings()
        saved = gui.core.load_settings()
        assert saved["theme"] == "light" and saved["show_temp"] is True
    finally:
        root.destroy()
