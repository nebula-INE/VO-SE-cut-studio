from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import vose_worker
from vose_worker import SynthesisLine, VoseSynthesisThread


def test_cancel_during_synthesis_does_not_emit_stale_line_ready(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    ready: list[tuple[str, str]] = []

    def slow_narration(_text: str, output_path: str) -> None:
        started.set()
        assert release.wait(timeout=3)
        Path(output_path).write_bytes(b"cancelled-output")

    monkeypatch.setattr(vose_worker, "synthesize_narration", slow_narration)
    worker = VoseSynthesisThread(
        lib_path="unused-for-openjtalk",
        lines=[SynthesisLine("old-line", "old project narration")],
    )
    worker.line_ready.connect(lambda line_id, path: ready.append((line_id, path)))

    worker.start()
    assert started.wait(timeout=2), "worker did not enter the synthesis function"
    worker.cancel()
    release.set()

    assert worker.wait(3000), "cancelled worker did not finish"
    assert ready == []
