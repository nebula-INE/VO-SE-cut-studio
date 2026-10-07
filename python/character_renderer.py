"""aural Studio - プレースホルダー2Dキャラクター描画

実際のキャラクターイラスト素材がまだ無いため、リップシンク(lipsync.py)・
モーションキャプチャ受信(osc_receiver.py)の動作検証用に、簡易的な円ベースの
プレースホルダーキャラクターを描画する。

実装しているもの:
    - mouth_openness(0〜1)に応じた口の開閉
    - 母音(A/I/U/E/O)の口形: 音声解析(lipsync.py)またはVMCのブレンドシェイプから
    - 微動: 常時ゆっくり上下に揺れる呼吸のような動き
    - head_offset_x/y・head_tilt_deg: VMC由来の頭の位置・傾き(頭だけが傾く)
    - 表情: Blink / Joy(笑顔) / Angry / Sorrow / Fun(眉・目・口角の変化)
    - 腕: bones引数(LeftUpperArm等のクォータニオン)で上腕・前腕の角度を反映
      (bonesが空なら従来どおり体なしの400x400キャンバス)

「2D変形メッシュ」: 口周辺をメッシュでワープし、口角を上げる/下げる。
本番のキャラクター素材が用意でき次第、render_character_frame()の中身を
レイヤー差し替え・変形に置き換えれば、呼び出し側のインターフェースは
そのまま使い回せる。
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from expression import VOWELS, Expression

CANVAS_SIZE = (400, 400)
CANVAS_SIZE_WITH_BODY = (400, 560)
FACE_COLOR = (255, 224, 189)
OUTLINE_COLOR = (60, 40, 30)
EYE_COLOR = (40, 30, 25)
MOUTH_COLOR = (120, 40, 40)
BODY_COLOR = (88, 120, 170)
ARM_COLOR = (110, 142, 190)

FACE_RADIUS = 100
FACE_CENTER = (200, 200)

# 微動(呼吸のような揺れ)のパラメータ
IDLE_MOTION_AMPLITUDE_PX = 4.0
IDLE_MOTION_PERIOD_SEC = 3.0

# VMCの位置はメートル単位。頭を10cm動かしたら画面上で何px動くかの目安。
HEAD_POSITION_SCALE_PX_PER_METER = 300.0

# 口角メッシュの最大変位量(px)
SMILE_MESH_MAX_LIFT_PX = 14.0

# 母音ごとの口形(横幅の倍率, 縦開きの倍率)。値は見た目の目安。
VOWEL_MOUTH_SHAPES: dict[str, tuple[float, float]] = {
    "A": (1.0, 1.0),
    "I": (1.25, 0.35),
    "U": (0.55, 0.55),
    "E": (1.15, 0.6),
    "O": (0.7, 0.9),
}

# 腕の長さ(px)・太さ
UPPER_ARM_LENGTH = 85
LOWER_ARM_LENGTH = 75
ARM_WIDTH = 20
# ボーンが来ていない腕は、体の脇に垂らした姿勢(Z軸ロール、度)で描く。
DEFAULT_ARM_ROLL_DEG = {"Left": -70.0, "Right": 70.0}

ARM_BONE_NAMES = ("LeftUpperArm", "LeftLowerArm", "RightUpperArm", "RightLowerArm")


def _idle_offset_y(time_sec: float) -> float:
    """常時のゆっくりした上下の微動オフセット(px)。"""
    phase = (time_sec / IDLE_MOTION_PERIOD_SEC) * 2.0 * math.pi
    return math.sin(phase) * IDLE_MOTION_AMPLITUDE_PX


def _blink_amount(blend_shapes: dict[str, float]) -> float:
    """blend_shapesからまばたきの度合い(0〜1)を取り出す(表記揺れも吸収)。"""
    return Expression.from_blend_shapes(blend_shapes).blink


def quaternion_roll_deg(qx: float, qy: float, qz: float, qw: float) -> float:
    """クォータニオンのZ軸まわり回転(ロール)を度で返す。"""
    sinr_cosp = 2.0 * (qw * qz + qx * qy)
    cosr_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    return math.degrees(math.atan2(sinr_cosp, cosr_cosp))


def _apply_mouth_curve_mesh(
    img: Image.Image, cx: float, mouth_center_y: float, curve: float
) -> Image.Image:
    """口周辺を変形メッシュでワープし、口角を上げる(curve>0)/下げる(curve<0)。

    curve=0なら無変形。mouth_center_y付近の横方向の両端ほど大きく動かし、
    矩形の上下の縁・中央は動かさない(縁が動くと元画像の外を参照して穴が
    開くため)。メッシュは縦6x横2ではなく、縦2行x横6列のセルに分け、
    中段の行だけ両端に向かって変位を増やす区分的な双線形ワープにしている。

    PILのMESHは、各セルのquadを「左上・左下・右下・右上」の順(4点x(x,y))
    で指定する。
    """
    if abs(curve) < 1e-3:
        return img

    lift = SMILE_MESH_MAX_LIFT_PX * max(-1.0, min(1.0, curve))
    margin = int(SMILE_MESH_MAX_LIFT_PX)
    half_h = 20 + margin
    half_w = 55
    left = int(cx - half_w)
    right = int(cx + half_w)
    top = int(mouth_center_y - half_h)
    bottom = int(mouth_center_y + half_h)
    w, h = right - left, bottom - top

    crop = img.crop((left, top, right, bottom))

    cols = 6
    xs = [round(w * i / cols) for i in range(cols + 1)]
    ys = [0, h // 2, h]

    def shift(ix: int, iy: int) -> float:
        # 中段の行だけ、中央(0)から両端(lift)へ向けて変位を増やす
        if iy != 1:
            return 0.0
        t = abs(xs[ix] - w / 2) / (w / 2)
        return lift * (t ** 0.7)

    mesh = []
    for ix in range(cols):
        for iy in range(2):
            x0, x1 = xs[ix], xs[ix + 1]
            y0, y1 = ys[iy], ys[iy + 1]
            # 変位の符号: 出力(dst)の口角位置には、元画像のより下(+)の内容を
            # 表示する → 口角が上がって見える。
            ul = (x0, y0 + shift(ix, iy))
            ll = (x0, y1 + shift(ix, iy + 1))
            lr = (x1, y1 + shift(ix + 1, iy + 1))
            ur = (x1, y0 + shift(ix + 1, iy))
            mesh.append(((x0, y0, x1, y1), (*ul, *ll, *lr, *ur)))

    warped = crop.transform(crop.size, Image.MESH, mesh, resample=Image.BILINEAR)
    result = img.copy()
    result.paste(warped, (left, top), warped)
    return result


def _apply_smile_mesh(img: Image.Image, cx: float, cy: float, mouth_center_y: float, joy: float) -> Image.Image:
    """後方互換用ラッパー(旧API)。joy(0〜1)を口角上げとして適用する。"""
    return _apply_mouth_curve_mesh(img, cx, mouth_center_y, joy)


def _mouth_shape(
    mouth_openness: float, vowels: dict[str, float]
) -> tuple[float, float]:
    """母音の重みから、(横幅の倍率, 縦開きの倍率)を求める。

    母音の重みの合計が小さい(=母音が分からない)ときは、中立の形(1.0,1.0)に
    寄せる。
    """
    total = sum(vowels.get(v, 0.0) for v in VOWELS)
    if total < 0.05:
        return 1.0, 1.0
    width = sum(vowels.get(v, 0.0) * VOWEL_MOUTH_SHAPES[v][0] for v in VOWELS) / total
    height = sum(vowels.get(v, 0.0) * VOWEL_MOUTH_SHAPES[v][1] for v in VOWELS) / total
    strength = min(1.0, total)  # 重みが弱いほど中立に寄せる
    return 1.0 + (width - 1.0) * strength, 1.0 + (height - 1.0) * strength


def _arm_segment_dir(side: str, roll_deg: float) -> tuple[float, float]:
    """腕の向き(画面座標、Y下向き)の単位ベクトル。

    T字ポーズでロール0=真横。ロールはZ軸まわりの反時計回りが正。
    Left側は画面の右、Right側は画面の左に伸びる前提(キャラが正面を向く想定)。
    実機のトラッキングデータでの符号確認はまだ(要検証)。
    """
    r = math.radians(roll_deg)
    if side == "Left":
        return math.cos(r), -math.sin(r)
    return -math.cos(r), math.sin(r)


def _draw_body_and_arms(
    draw: ImageDraw.ImageDraw,
    cx: float,
    neck_y: float,
    bones: dict[str, tuple[float, float, float, float]],
) -> None:
    shoulder_y = neck_y + 20
    half_shoulder = 70
    draw.rounded_rectangle(
        [cx - half_shoulder, neck_y, cx + half_shoulder, neck_y + 200],
        radius=30, fill=BODY_COLOR, outline=OUTLINE_COLOR, width=3,
    )

    for side, sign in (("Left", 1), ("Right", -1)):
        upper = bones.get(f"{side}UpperArm")
        lower = bones.get(f"{side}LowerArm")
        upper_roll = quaternion_roll_deg(*upper) if upper else DEFAULT_ARM_ROLL_DEG[side]
        # 前腕のロールは上腕からの相対(VMCのボーン回転は親に対するローカル回転)。
        # ボーンが無ければ上腕と同じ向き(まっすぐ)に伸ばす。
        lower_roll = upper_roll + (quaternion_roll_deg(*lower) if lower else 0.0)

        sx, sy = cx + sign * half_shoulder, shoulder_y
        ux, uy = _arm_segment_dir(side, upper_roll)
        ex, ey = sx + ux * UPPER_ARM_LENGTH, sy + uy * UPPER_ARM_LENGTH
        lx, ly = _arm_segment_dir(side, lower_roll)
        hx, hy = ex + lx * LOWER_ARM_LENGTH, ey + ly * LOWER_ARM_LENGTH

        draw.line([sx, sy, ex, ey], fill=ARM_COLOR, width=ARM_WIDTH)
        draw.line([ex, ey, hx, hy], fill=ARM_COLOR, width=ARM_WIDTH)
        r = ARM_WIDTH / 2
        for px, py in ((sx, sy), (ex, ey)):
            draw.ellipse([px - r, py - r, px + r, py + r], fill=ARM_COLOR)
        draw.ellipse([hx - r, hy - r, hx + r, hy + r], fill=FACE_COLOR, outline=OUTLINE_COLOR, width=2)


def render_character_frame(
    mouth_openness: float,
    time_sec: float,
    head_offset_x: float = 0.0,
    head_offset_y: float = 0.0,
    head_tilt_deg: float = 0.0,
    blend_shapes: dict[str, float] | None = None,
    vowels: dict[str, float] | None = None,
    bones: dict[str, tuple[float, float, float, float]] | None = None,
) -> Image.Image:
    """1フレーム分のキャラクター画像(RGBA)を描画する。

    Args:
        mouth_openness: 0.0(閉じる)〜1.0(全開)。
        time_sec: 現在時刻(秒)。微動(呼吸)のアニメーションに使う。
        head_offset_x / head_offset_y: 頭の位置オフセット(メートル、VMC座標系、
            Yは上が正)。
        head_tilt_deg: 首をかしげる角度(度)。
        blend_shapes: VMC由来のブレンドシェイプ値({名前: 0〜1})。表記揺れ
            (happy/Joy、aa/A等)はexpression.pyで吸収する。
        vowels: 音声解析由来の母音の重み({"A":..,"I":..})。VMCの母音
            ブレンドシェイプとは大きい方を採用する。
        bones: {ボーン名: (qx,qy,qz,qw)}。LeftUpperArm等があれば体と腕を描く。
    """
    expr = Expression.from_blend_shapes(blend_shapes)
    bones = {k: v for k, v in (bones or {}).items() if k in ARM_BONE_NAMES}
    draw_body = bool(bones)

    size = CANVAS_SIZE_WITH_BODY if draw_body else CANVAS_SIZE
    base = Image.new("RGBA", size, (0, 0, 0, 0))

    idle_y = _idle_offset_y(time_sec)
    tracked_offset_x = head_offset_x * HEAD_POSITION_SCALE_PX_PER_METER
    tracked_offset_y = -head_offset_y * HEAD_POSITION_SCALE_PX_PER_METER

    cx = FACE_CENTER[0] + tracked_offset_x
    cy = FACE_CENTER[1] + idle_y + tracked_offset_y

    if draw_body:
        # 体は頭の追従(トラッキング)では動かさず、基準位置に固定する。
        _draw_body_and_arms(
            ImageDraw.Draw(base), FACE_CENTER[0], FACE_CENTER[1] + FACE_RADIUS - 10, bones
        )

    # 頭は別レイヤーに描き、傾きを頭だけに適用してから体の上に重ねる。
    head = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(head)

    draw.ellipse(
        [cx - FACE_RADIUS, cy - FACE_RADIUS, cx + FACE_RADIUS, cy + FACE_RADIUS],
        fill=FACE_COLOR, outline=OUTLINE_COLOR, width=3,
    )

    # 目: まばたき、Funの細め目(両方の大きい方)で縦幅を潰す
    eye_close = max(expr.blink, 0.55 * expr.fun)
    eye_height_scale = max(0.08, 1.0 - eye_close)
    eye_offset_x, eye_offset_y, eye_radius = 35, -20, 10
    for sign in (-1, 1):
        ex = cx + sign * eye_offset_x
        ey = cy + eye_offset_y
        draw.ellipse(
            [ex - eye_radius, ey - eye_radius * eye_height_scale,
             ex + eye_radius, ey + eye_radius * eye_height_scale],
            fill=EYE_COLOR,
        )

        # 眉: Angry=内側が下がる(つり眉)、Sorrow=内側が上がる(困り眉)。
        # Joy/Funはやや持ち上げる。
        brow_y = ey - 24 - 6 * max(expr.joy, expr.fun)
        slant = 9 * expr.angry - 9 * expr.sorrow
        outer = (ex + sign * 15, brow_y - slant * 0.4)
        inner = (ex - sign * 15, brow_y + slant)
        draw.line([outer, inner], fill=OUTLINE_COLOR, width=4)

    # 口: 母音の口形(音声解析 + VMC母音)と開き具合
    merged_vowels = dict(vowels or {})
    for v, w in expr.vowels.items():
        merged_vowels[v] = max(merged_vowels.get(v, 0.0), w)
    vowel_sum = min(1.0, sum(merged_vowels.get(v, 0.0) for v in VOWELS))
    # VMCの母音だけが来ている(音声再生が無い)場合は、母音の強さを開き具合に使う
    openness = max(mouth_openness, vowel_sum if expr.vowels else 0.0)
    openness = max(0.0, min(1.0, openness))
    width_scale, height_scale = _mouth_shape(openness, merged_vowels)

    mouth_width = 50 * width_scale
    mouth_min_height = 4
    mouth_max_height = 55
    mouth_height = mouth_min_height + (mouth_max_height - mouth_min_height) * openness * height_scale
    mouth_center_y = cy + 45

    draw.ellipse(
        [cx - mouth_width / 2, mouth_center_y - mouth_height / 2,
         cx + mouth_width / 2, mouth_center_y + mouth_height / 2],
        fill=MOUTH_COLOR,
    )

    # 口角: Joy/Funで上げ、Sorrow/Angryで下げる(正負を合成)
    curve = max(expr.joy, 0.6 * expr.fun) - max(expr.sorrow, 0.5 * expr.angry)
    head = _apply_mouth_curve_mesh(head, cx, mouth_center_y, curve)

    if head_tilt_deg != 0.0:
        # 頭レイヤーだけを頭の中心まわりに回転(体は傾かない)
        head = head.rotate(head_tilt_deg, resample=Image.BICUBIC, center=(cx, cy))

    base.alpha_composite(head)
    return base
