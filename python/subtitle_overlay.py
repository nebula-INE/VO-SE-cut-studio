"""Portable Japanese subtitle rasterization for timeline MP4 export."""
from __future__ import annotations

import os
import platform
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("VOSE_SUBTITLE_FONT")
    if configured:
        candidates.append(Path(configured).expanduser())

    system = platform.system()
    if system == "Windows":
        root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend(
            root / name
            for name in ("YuGothR.ttc", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc")
        )
    elif system == "Darwin":
        candidates.extend(
            Path("/System/Library/Fonts") / name
            for name in (
                "ヒラギノ角ゴシック W3.ttc",
                "ヒラギノ角ゴ Pro W3.otf",
                "ヒラギノ丸ゴ ProN W4.ttc",
            )
        )
        candidates.extend(
            Path("/System/Library/Fonts/Supplemental") / name
            for name in ("Arial Unicode.ttf", "Arial.ttf")
        )
    else:
        candidates.extend(
            Path(path)
            for path in (
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
                "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
                "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf",
            )
        )
    return candidates


def _load_font(size: int):
    for candidate in _font_candidates():
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    raise RuntimeError(
        "日本語字幕に使用できるフォントが見つかりません。WindowsはYu Gothic/Meiryo、"
        "macOSはヒラギノ、Ubuntu/Debianはfonts-noto-cjkをインストールするか、"
        "VOSE_SUBTITLE_FONTに日本語対応フォントのパスを指定してください。"
    )


def _wrap_lines(text: str, draw: ImageDraw.ImageDraw, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def render_subtitle_png(
    text: str,
    width: int,
    height: int,
    output_path: str | Path,
) -> Path:
    """Create a transparent, bottom-centered subtitle card with wrapped text."""
    normalized = str(text).strip()
    if not normalized:
        raise ValueError("字幕テキストが空です")
    if width < 2 or height < 2:
        raise ValueError("字幕画像のサイズが不正です")

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font_size = max(20, min(84, int(round(height * 0.055))))
    max_text_width = int(width * 0.84)
    font = _load_font(font_size)
    lines = _wrap_lines(normalized, draw, font, max_text_width)
    while len(lines) > 3 and font_size > 18:
        font_size = max(18, int(font_size * 0.88))
        font = _load_font(font_size)
        lines = _wrap_lines(normalized, draw, font, max_text_width)
    if len(lines) > 3:
        lines = lines[:3]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_text_width:
            last = last[:-1]
        lines[-1] = last + "…"

    boxes = [draw.textbbox((0, 0), line or " ", font=font, stroke_width=2) for line in lines]
    line_heights = [max(1, box[3] - box[1]) for box in boxes]
    spacing = max(4, int(font_size * 0.22))
    text_height = sum(line_heights) + spacing * (len(lines) - 1)
    padding_x = max(18, int(width * 0.035))
    padding_y = max(10, int(font_size * 0.35))
    box_width = min(
        width - int(width * 0.06),
        max(int(draw.textlength(line, font=font)) for line in lines) + padding_x * 2,
    )
    box_height = text_height + padding_y * 2
    box_left = (width - box_width) // 2
    box_top = max(int(height * 0.72), height - box_height - int(height * 0.055))
    box_top = min(box_top, height - box_height - int(height * 0.025))
    box_bottom = box_top + box_height
    draw.rounded_rectangle(
        (box_left, box_top, box_left + box_width, box_bottom),
        radius=max(8, int(font_size * 0.28)),
        fill=(0, 0, 0, 178),
    )

    y = box_top + padding_y
    for line, box, line_height in zip(lines, boxes, line_heights):
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) / 2
        draw.text(
            (x - box[0], y - box[1]),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 220),
        )
        y += line_height + spacing

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path
