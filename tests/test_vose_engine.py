import pytest

import vose_engine


class _FakeFunction:
    def __init__(self) -> None:
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return 1.0


class _FakeLibrary:
    def __init__(self, *, supports_sample_rate: bool) -> None:
        self.set_oto_data = _FakeFunction()
        self.load_embedded_resource = _FakeFunction()
        if supports_sample_rate:
            self.load_embedded_resource_ex = _FakeFunction()
        self.execute_render = _FakeFunction()
        self.set_vocal_timeline = _FakeFunction()
        self.get_engine_version = _FakeFunction()
        self.clear_engine_cache = _FakeFunction()
        self.set_bigvgan_model = _FakeFunction()


def _make_engine(monkeypatch, *, supports_sample_rate: bool):
    library = _FakeLibrary(supports_sample_rate=supports_sample_rate)
    monkeypatch.setattr(vose_engine.ctypes, "CDLL", lambda _path: library)
    return vose_engine.VoseEngine("fake-vose-core"), library


@pytest.mark.parametrize("sample_rate", [22050, 44100, 48000, 96000])
def test_load_embedded_resource_passes_source_sample_rate(monkeypatch, sample_rate: int) -> None:
    engine, library = _make_engine(monkeypatch, supports_sample_rate=True)

    engine.load_embedded_resource("あ", [0, 100, -100], sample_rate)

    call = library.load_embedded_resource_ex.calls[0]
    assert call[0] == "あ".encode("utf-8")
    assert list(call[1]) == [0, 100, -100]
    assert call[2:] == (3, sample_rate)


def test_legacy_library_remains_supported_at_44100hz(monkeypatch) -> None:
    engine, library = _make_engine(monkeypatch, supports_sample_rate=False)

    engine.load_embedded_resource("a", [0, 100], 44100)

    assert len(library.load_embedded_resource.calls) == 1
    assert list(library.load_embedded_resource.calls[0][1]) == [0, 100]


def test_legacy_library_does_not_silently_discard_nonstandard_sample_rate(monkeypatch) -> None:
    engine, library = _make_engine(monkeypatch, supports_sample_rate=False)

    with pytest.raises(RuntimeError, match="does not support source sample rates"):
        engine.load_embedded_resource("a", [0, 100], 22050)

    assert library.load_embedded_resource.calls == []


@pytest.mark.parametrize("sample_rate", [0, 7999, 384001])
def test_load_embedded_resource_rejects_invalid_sample_rate(monkeypatch, sample_rate: int) -> None:
    engine, _library = _make_engine(monkeypatch, supports_sample_rate=True)

    with pytest.raises(ValueError, match="unsupported sample rate"):
        engine.load_embedded_resource("a", [0, 100], sample_rate)


def test_load_embedded_resource_rejects_empty_or_out_of_range_pcm(monkeypatch) -> None:
    engine, _library = _make_engine(monkeypatch, supports_sample_rate=True)

    with pytest.raises(ValueError, match="must not be empty"):
        engine.load_embedded_resource("a", [], 44100)
    with pytest.raises(ValueError, match="signed 16-bit"):
        engine.load_embedded_resource("a", [40000], 44100)
