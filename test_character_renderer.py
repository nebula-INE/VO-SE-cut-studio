import math

import numpy as np

import character_renderer as cr


def _render(**kw):
    kw.setdefault("mouth_openness", 0.0)
    kw.setdefault("time_sec", 0.0)
    return cr.render_character_frame(**kw)


def _differs(a, b) -> bool:
    # 注意: RGBA画像に対するImageChops.difference().getbbox()はアルファ値しか
    # 見ないため、色だけの違いを見逃す。全チャンネルをnumpyで比較する。
    return not np.array_equal(np.array(a), np.array(b))


def _quat_z(deg):
    r = math.radians(deg) / 2
    return (0.0, 0.0, math.sin(r), math.cos(r))


def test_canvas_size_without_and_with_body():
    assert _render().size == cr.CANVAS_SIZE
    assert _render(bones={"LeftUpperArm": _quat_z(0)}).size == cr.CANVAS_SIZE_WITH_BODY


def test_each_expression_changes_the_face():
    neutral = _render()
    for shape in ("Joy", "Angry", "Sorrow", "Fun", "Blink"):
        assert _differs(neutral, _render(blend_shapes={shape: 1.0})), shape


def test_alias_names_render_identically():
    assert not _differs(_render(blend_shapes={"Joy": 1.0}), _render(blend_shapes={"happy": 1.0}))


def test_smile_and_frown_are_different_and_neither_punches_holes():
    smile = _render(blend_shapes={"Joy": 1.0})
    frown = _render(blend_shapes={"Sorrow": 1.0})
    assert _differs(smile, frown)
    # 顔の中心付近(口の周辺含む)に透明な穴が空いていないこと
    for img in (smile, frown):
        alpha = np.array(img)[:, :, 3]
        cx, cy = cr.FACE_CENTER
        assert alpha[cy + 20:cy + 75, cx - 40:cx + 40].min() == 255


def test_vowel_shapes_differ_from_each_other():
    shapes = {v: _render(mouth_openness=1.0, vowels={v: 1.0}) for v in "AIUEO"}
    for a in "AIUEO":
        for b in "AIUEO":
            if a < b:
                assert _differs(shapes[a], shapes[b]), (a, b)


def test_vmc_vowel_blendshape_opens_mouth_without_audio():
    assert _differs(_render(), _render(blend_shapes={"aa": 1.0}))


def test_arm_roll_changes_the_arm_but_not_the_torso():
    down = _render(bones={"LeftUpperArm": _quat_z(-70)})
    up = _render(bones={"LeftUpperArm": _quat_z(80)})
    assert _differs(down, up)
    # 胴体の中央付近は同じ
    box = (150, 330, 250, 420)
    assert not _differs(down.crop(box), up.crop(box))


def test_head_tilt_does_not_rotate_the_body():
    bones = {"LeftUpperArm": _quat_z(-70)}
    straight = _render(bones=bones)
    tilted = _render(bones=bones, head_tilt_deg=25.0)
    body_box = (130, 330, 270, 540)
    assert not _differs(straight.crop(body_box), tilted.crop(body_box))
    assert _differs(straight, tilted)


def test_unknown_bones_are_ignored():
    assert _render(bones={"Spine": _quat_z(30)}).size == cr.CANVAS_SIZE
