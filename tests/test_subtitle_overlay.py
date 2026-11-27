from __future__ import annotations

from PIL import ImageFont

import pytest
import subtitle_overlay


def test_render_subtitle_png_writes_transparent_image_and_wraps_text(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        subtitle_overlay,
        "_load_font",
        lambda _size: ImageFont.load_default(),
    )
    output = tmp_path / "subtitle.png"

    result = subtitle_overlay.render_subtitle_png(
        "A subtitle that should wrap into a readable card",
        640,
        360,
        output,
    )

    assert result == output
    with subtitle_overlay.Image.open(result) as image:
        assert image.size == (640, 360)
        assert image.getchannel("A").getbbox() is not None
        assert image.getpixel((0, 0))[3] == 0


def test_render_subtitle_png_rejects_empty_text_and_invalid_size(tmp_path):
    with pytest.raises(ValueError, match="空"):
        subtitle_overlay.render_subtitle_png("", 640, 360, tmp_path / "empty.png")
    with pytest.raises(ValueError, match="サイズ"):
        subtitle_overlay.render_subtitle_png("text", 0, 360, tmp_path / "small.png")


def test_load_font_reports_missing_japanese_font(monkeypatch):
    monkeypatch.setattr(subtitle_overlay, "_font_candidates", lambda: [])
    with pytest.raises(RuntimeError, match="日本語字幕"):
        subtitle_overlay._load_font(36)



def test_render_subtitle_png_uses_speaker_color_accent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitle_overlay,
        "_load_font",
        lambda _size: ImageFont.load_default(),
    )
    output = tmp_path / "speaker-subtitle.png"
    subtitle_overlay.render_subtitle_png(
        "Speaker dialogue",
        640,
        360,
        output,
        speaker_color="#D94A4A",
    )

    with subtitle_overlay.Image.open(output) as image:
        pixels = set(image.getdata())
        assert (217, 74, 74, 255) in pixels
        assert image.getpixel((0, 0))[3] == 0


def test_render_subtitle_png_ignores_invalid_speaker_color(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subtitle_overlay,
        "_load_font",
        lambda _size: ImageFont.load_default(),
    )
    output = tmp_path / "invalid-color.png"
    subtitle_overlay.render_subtitle_png(
        "Speaker dialogue",
        640,
        360,
        output,
        speaker_color="not-a-color",
    )
    assert output.is_file()
