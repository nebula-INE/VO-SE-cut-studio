"""Helpers for choosing a compatible destination track for imported assets."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtWidgets import QInputDialog


_DIALOG_KIND_LABELS = {
    "video": "動画",
    "audio": "音声",
    "image": "画像",
    "character": "キャラクター",
    "motion": "モーション",
    "other": "その他",
}


def select_timeline_asset_track(
    parent: Any,
    model: Any,
    tracks: Sequence[Any],
    clip_kind: str,
    *,
    dialog_get_item: Callable[..., tuple[str, bool]] | None = None,
) -> Any | None:
    """Return the selected track; return None on an empty candidate list or cancel.

    A single compatible track is selected without a dialog. For multiple tracks,
    the dialog shows each track name and the number of clips already assigned.
    """
    candidates = list(tracks)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    labels = [
        f"{index + 1}. {track.name} ({len(model.clips_for_track(track.id))} 件)"
        for index, track in enumerate(candidates)
    ]
    get_item = dialog_get_item or QInputDialog.getItem
    selected, accepted = get_item(
        parent,
        "配置先トラック",
        f"{_DIALOG_KIND_LABELS.get(clip_kind, clip_kind)}クリップの配置先:",
        labels,
        0,
        False,
    )
    if not accepted:
        return None
    try:
        return candidates[labels.index(selected)]
    except ValueError:
        return None
