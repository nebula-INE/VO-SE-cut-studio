import math

from osc_receiver import OscMocapReceiver


def _make():
    heads, blends, bones = [], [], []
    rx = OscMocapReceiver(heads.append, blends.append, bones.append)
    return rx, heads, blends, bones


def test_head_message_carries_position_roll_and_raw_quaternion():
    rx, heads, _, _ = _make()
    half = math.radians(30) / 2  # Z軸に30度
    rx._handle_bone_pos("/VMC/Ext/Bone/Pos", "Head", 0.05, 0.1, 0.2,
                        0.0, 0.0, math.sin(half), math.cos(half))
    h = heads[0]
    assert (h.offset_x, h.offset_y, h.offset_z) == (0.05, 0.1, 0.2)
    assert abs(h.tilt_deg - 30.0) < 1e-6
    assert abs(h.qz - math.sin(half)) < 1e-9 and abs(h.qw - math.cos(half)) < 1e-9


def test_other_bones_go_to_bone_callback():
    rx, heads, _, bones = _make()
    rx._handle_bone_pos("/VMC/Ext/Bone/Pos", "LeftUpperArm", 0, 0, 0, 0.0, 0.0, 0.5, 0.8660254)
    assert not heads
    assert bones[0].name == "LeftUpperArm" and abs(bones[0].qz - 0.5) < 1e-9


def test_short_messages_are_ignored():
    rx, heads, _, bones = _make()
    rx._handle_bone_pos("/VMC/Ext/Bone/Pos", "Head", 0.0)
    assert not heads and not bones


def test_blend_values_are_buffered_until_apply_and_kept_between_applies():
    rx, _, blends, _ = _make()
    rx._handle_blend_val("/VMC/Ext/Blend/Val", "Joy", 1.0)
    assert blends == []
    rx._handle_blend_apply("/VMC/Ext/Blend/Apply")
    rx._handle_blend_val("/VMC/Ext/Blend/Val", "Angry", 0.5)
    rx._handle_blend_apply("/VMC/Ext/Blend/Apply")
    assert blends[0] == {"Joy": 1.0}
    assert blends[1] == {"Joy": 1.0, "Angry": 0.5}
    blends[1]["Joy"] = 0.0  # 受け取った側が書き換えても内部バッファは壊れない
    rx._handle_blend_apply("/VMC/Ext/Blend/Apply")
    assert blends[2]["Joy"] == 1.0
