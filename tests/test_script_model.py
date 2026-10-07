"""script_editor.pyのデータモデル部分のテスト。

PySide6(GUI)が無い環境でも動くよう、PySide6をダミーに差し替えてimportする。
"""

import importlib
import sys
import types

import pytest


class _Dummy:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return _Dummy()

    def __call__(self, *a, **k):
        return _Dummy()


def _stub_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: type(attr, (_Dummy,), {})
    return mod


@pytest.fixture
def script_editor(monkeypatch):
    for name in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtMultimedia", "PySide6.QtWidgets"):
        monkeypatch.setitem(sys.modules, name, _stub_module(name))
    sys.modules.pop("script_editor", None)
    mod = importlib.import_module("script_editor")
    yield mod
    sys.modules.pop("script_editor", None)


def test_unsynthesized_line_uses_estimate(script_editor):
    line = script_editor.ScriptLine("A", "こんにちは")
    assert line.is_estimated
    assert line.duration_sec == line.estimated_duration_sec


def test_synthesized_line_uses_actual_duration(script_editor):
    line = script_editor.ScriptLine("A", "こんにちは")
    line.actual_duration_sec = 2.5
    assert not line.is_estimated
    assert line.duration_sec == 2.5


def test_total_and_line_at_time_follow_actual_durations(script_editor):
    model = script_editor.ScriptModel()
    a = model.add_line("ナレーター", "あ")
    b = model.add_line("キャラA", "い")
    a.actual_duration_sec = 3.0
    b.actual_duration_sec = 1.0
    assert model.total_duration_sec() == 4.0
    assert model.line_at_time(2.9) is a
    assert model.line_at_time(3.1) is b
    assert model.line_at_time(4.1) is None


def test_actual_duration_is_not_saved(script_editor):
    model = script_editor.ScriptModel()
    line = model.add_line("ナレーター", "あ")
    line.actual_duration_sec = 3.0
    restored = script_editor.ScriptModel.from_dict(model.to_dict())
    assert restored.lines[0].is_estimated
