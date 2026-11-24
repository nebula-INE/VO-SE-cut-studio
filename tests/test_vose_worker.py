from __future__ import annotations

import importlib.util
import shutil
import sys
import threading
import types
from pathlib import Path

import pytest


def _load_worker_with_stubbed_engine_dependencies(monkeypatch):
    synthesize = types.ModuleType("synthesize")
    synthesize.synthesize_narration = lambda _text, _path: None
    synthesize.synthesize_text = lambda *_args, **_kwargs: None
    synthesize.synthesize_with_voicebank = lambda *_args, **_kwargs: None

    voicebank_manager = types.ModuleType("voicebank_manager")

    class FakeVoicebankManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover_voicebanks(self, *_args, **_kwargs):
            return []

        def get(self, *_args, **_kwargs):
            return None

    voicebank_manager.VoicebankManager = FakeVoicebankManager

    vose_engine = types.ModuleType("vose_engine")

    class FakeVoseEngine:
        def __init__(self, *_args, **_kwargs):
            pass

    vose_engine.VoseEngine = FakeVoseEngine

    monkeypatch.setitem(sys.modules, "synthesize", synthesize)
    monkeypatch.setitem(sys.modules, "voicebank_manager", voicebank_manager)
    monkeypatch.setitem(sys.modules, "vose_engine", vose_engine)

    worker_path = Path(__file__).resolve().parents[1] / "python" / "vose_worker.py"
    spec = importlib.util.spec_from_file_location("isolated_vose_worker", worker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cancel_during_synthesis_does_not_emit_stale_line_ready(monkeypatch):
    pytest.importorskip("PySide6")
    worker_module = _load_worker_with_stubbed_engine_dependencies(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    ready: list[tuple[str, str]] = []

    def slow_narration(_text: str, output_path: str) -> None:
        started.set()
        assert release.wait(timeout=3)
        Path(output_path).write_bytes(b"cancelled-output")

    worker = worker_module.VoseSynthesisThread(
        lib_path="unused-for-openjtalk",
        lines=[worker_module.SynthesisLine("old-line", "old project narration")],
    )
    worker_module.synthesize_narration = slow_narration
    worker.line_ready.connect(lambda line_id, path: ready.append((line_id, path)))

    worker.start()
    try:
        assert started.wait(timeout=2), "worker did not enter the synthesis function"
        worker.cancel()
        release.set()

        assert worker.wait(3000), "cancelled worker did not finish"
        assert ready == []
    finally:
        release.set()
        if worker.isRunning():
            worker.wait(3000)
        shutil.rmtree(worker._tmp_dir, ignore_errors=True)
