from expression import Expression, normalize_blend_shapes, normalize_name


def test_vrm0_and_vrm1_names_are_unified():
    assert normalize_name("happy") == "Joy"
    assert normalize_name("Joy") == "Joy"
    assert normalize_name("sad") == "Sorrow"
    assert normalize_name("aa") == "A"
    assert normalize_name("OH") == "O"
    assert normalize_name("blinkLeft") == "Blink_L"


def test_unknown_names_are_kept():
    assert normalize_name("TongueOut") == "TongueOut"


def test_values_are_clamped_and_duplicates_take_max():
    shapes = normalize_blend_shapes({"Joy": 1.7, "happy": 0.4, "Angry": -0.3})
    assert shapes == {"Joy": 1.0, "Angry": 0.0}


def test_blink_prefers_both_eyes_then_averages_sides():
    assert Expression.from_blend_shapes({"Blink": 0.8, "Blink_L": 0.1}).blink == 0.8
    assert Expression.from_blend_shapes({"Blink_L": 1.0, "Blink_R": 0.0}).blink == 0.5
    assert Expression.from_blend_shapes({}).blink == 0.0


def test_vowels_extracted_only_when_positive():
    expr = Expression.from_blend_shapes({"aa": 0.9, "ih": 0.0, "Fun": 0.5})
    assert expr.vowels == {"A": 0.9}
    assert expr.fun == 0.5
