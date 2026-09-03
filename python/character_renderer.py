"""aural Studio - プレースホルダー2Dキャラクター描画

実際のキャラクターイラスト素材がまだ無いため、リップシンクアルゴリズム
(lipsync.py)・モーションキャプチャ受信(osc_receiver.py)の動作検証用に、
簡易的な円ベースのプレースホルダーキャラクターを描画する。

実装しているのは:
    - mouth_openness(0〜1)に応じた口の開閉(楕円の縦幅を変化させる)
    - 「微動」: 常時ゆっくり上下に揺れる呼吸のような動き(sin波、低振幅)
    - head_offset_x/y・head_tilt_deg: OSC(VMCプロトコル)経由で受信した
      実際の頭の位置・傾きをそのまま反映する(Phase 2: モーションキャプチャ)
    - blend_shapes: OSC経由のブレンドシェイプ値の辞書。対応しているキー:
        "Blink"または"Blink_L"/"Blink_R"の平均 → まばたき(目の縦幅を潰す)
        "Joy" → 口角を上げる笑顔(PILのMESH変換による2D変形メッシュ)

「2D変形メッシュ」については、口周辺の矩形領域を2x1セルのメッシュに
分割し、口角(左右セルの外側下端)のサンプリング元を持ち上げることで、
単純な図形の描き直しではなく実際のピクセル領域ワープとして口角上げを
表現している(plan.mdの「2D(変形メッシュ)」に対応する最初の一歩)。

本番のキャラクター素材(イラスト/画像レイヤー)が用意でき次第、
render_character_frame()の中身を「口パーツ/首から上の画像レイヤーの
差し替え・変形」に置き換えれば、呼び出し側のインターフェース
(mouth_openness, head_offset_x/y, head_tilt_deg, blend_shapes)は
そのまま使い回せる設計にしている。
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

CANVAS_SIZE = (400, 400)
FACE_COLOR = (255, 224, 189)
OUTLINE_COLOR = (60, 40, 30)
EYE_COLOR = (40, 30, 25)
MOUTH_COLOR = (120, 40, 40)

FACE_RADIUS = 100
FACE_CENTER = (200, 200)

# 微動(呼吸のような揺れ)のパラメータ
IDLE_MOTION_AMPLITUDE_PX = 4.0
IDLE_MOTION_PERIOD_SEC = 3.0

# VMCプロトコルの位置は実世界のメートル単位で送られてくる(トラッキング
# デバイスの座標系)ため、キャラクターのピクセル座標へスケール変換する。
# この値は「頭を10cm動かしたら画面上で何px動くか」の目安。
HEAD_POSITION_SCALE_PX_PER_METER = 300.0

# 口角上げ(Joyブレンドシェイプ)の変形メッシュの最大変位量(px)
SMILE_MESH_MAX_LIFT_PX = 14.0


def _idle_offset_y(time_sec: float) -> float:
    """常時のゆっくりした上下の微動オフセット(px)。"""
    phase = (time_sec / IDLE_MOTION_PERIOD_SEC) * 2.0 * math.pi
    return math.sin(phase) * IDLE_MOTION_AMPLITUDE_PX


def _blink_amount(blend_shapes: dict[str, float]) -> float:
    """blend_shapesからまばたきの度合い(0〜1)を取り出す。

    "Blink"(両目まとめて)を優先し、無ければ"Blink_L"/"Blink_R"の平均を使う。
    どちらも無ければ0(まばたきしていない)。
    """
    if "Blink" in blend_shapes:
        return max(0.0, min(1.0, blend_shapes["Blink"]))
    left = blend_shapes.get("Blink_L")
    right = blend_shapes.get("Blink_R")
    if left is not None or right is not None:
        values = [v for v in (left, right) if v is not None]
        return max(0.0, min(1.0, sum(values) / len(values)))
    return 0.0


def _apply_smile_mesh(img: Image.Image, cx: float, cy: float, mouth_center_y: float, joy: float) -> Image.Image:
    """口周辺を変形メッシュでワープし、口角が上がった(笑顔の)見た目にする。
    joy=0なら無変形(元画像をそのまま返す)。

    [実装メモ]
    - Image.transform(..., MESH, ...)を画像全体に対して直接呼ぶと、
      メッシュで指定した矩形の外側が透明で塗りつぶされてしまう(顔全体が
      消える不具合を実際に踏んだ)。そのため、口周辺の矩形領域だけを
      先にcrop()で切り出し、その小さな画像に対してのみMESH変換を適用
      してから、paste()で元の画像へ貼り戻す方式にしている。
    - 当初、口の中心で左右2セルに分割していたが、セルの境界(中央)で
      不連続な段差ができ、口が2つに割れたような見た目になる不具合が
      あった。両端(口角)だけを持ち上げた1セルの変形に単純化し、
      双線形補間で中央がなだらかにつながるようにしている。
    - 矩形が顔の輪郭円からはみ出すと、変形時に円の境界(肌色→透明の
      切り替わり)まで歪んでしまい不自然な欠けができる不具合があった。
      そのため矩形は顔の円の内側に収まる保守的なサイズにしている。
    """
    if joy <= 0.0:
        return img

    lift = SMILE_MESH_MAX_LIFT_PX * min(1.0, joy)

    # 顔は半径FACE_RADIUSの円なので、矩形の下端でも円からはみ出さない
    # ように、控えめなサイズに抑える。
    box_half_width = 55
    box_top = int(mouth_center_y - 20)
    box_bottom = int(mouth_center_y + 20)
    box_left = int(cx - box_half_width)
    box_right = int(cx + box_half_width)
    box_width = box_right - box_left
    box_height = box_bottom - box_top

    crop = img.crop((box_left, box_top, box_right, box_bottom))

    # 1セルの変形メッシュ。上端2点はそのまま、下端2点(口角)だけを
    # lift分だけサンプリング元を上に引き上げる。中央は双線形補間で
    # なだらかにつながる。
    mesh = [
        (
            (0, 0, box_width, box_height),
            (
                0, 0,                             # 左上
                box_width, 0,                      # 右上
                box_width, box_height - lift,       # 右下(口角) ← 持ち上げ
                0, box_height - lift,               # 左下(口角) ← 持ち上げ
            ),
        ),
    ]

    warped_crop = crop.transform(crop.size, Image.MESH, mesh, resample=Image.BILINEAR)

    result = img.copy()
    # warped_crop自身のアルファをマスクにして貼り戻すことで、透過部分の
    # 扱いを自然にする(単純paste()だとアルファ値が正しく合成されない)。
    result.paste(warped_crop, (box_left, box_top), warped_crop)
    return result


def render_character_frame(
    mouth_openness: float,
    time_sec: float,
    head_offset_x: float = 0.0,
    head_offset_y: float = 0.0,
    head_tilt_deg: float = 0.0,
    blend_shapes: dict[str, float] | None = None,
) -> Image.Image:
    """1フレーム分のキャラクター画像(RGBA)を描画する。

    Args:
        mouth_openness: 0.0(閉じる)〜1.0(全開)。
        time_sec: 現在時刻(秒)。微動(呼吸)のアニメーションに使う。
        head_offset_x: 頭の左右位置オフセット(メートル、VMCプロトコル座標系)。
        head_offset_y: 頭の上下位置オフセット(メートル、VMCプロトコル座標系。
            上が正)。
        head_tilt_deg: 首をかしげる角度(度)。OSC受信データが無い間は0.0。
        blend_shapes: OSC経由のブレンドシェイプ値の辞書({名前: 0〜1})。
            Noneの場合は何も反映しない(全て未指定として扱う)。
    """
    blend_shapes = blend_shapes or {}
    blink = _blink_amount(blend_shapes)
    joy = max(0.0, min(1.0, blend_shapes.get("Joy", 0.0)))

    img = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    idle_y = _idle_offset_y(time_sec)
    # VMC座標系はY上が正、画像座標系はY下が正なので符号を反転させる。
    tracked_offset_x = head_offset_x * HEAD_POSITION_SCALE_PX_PER_METER
    tracked_offset_y = -head_offset_y * HEAD_POSITION_SCALE_PX_PER_METER

    cx = FACE_CENTER[0] + tracked_offset_x
    cy = FACE_CENTER[1] + idle_y + tracked_offset_y

    # 顔(円)
    draw.ellipse(
        [cx - FACE_RADIUS, cy - FACE_RADIUS, cx + FACE_RADIUS, cy + FACE_RADIUS],
        fill=FACE_COLOR, outline=OUTLINE_COLOR, width=3,
    )

    # 目(2つの小さい円)。blinkが大きいほど縦幅を潰して閉じ目にする。
    eye_offset_x = 35
    eye_offset_y = -20
    eye_radius = 10
    eye_height_scale = max(0.08, 1.0 - blink)  # 完全に0にすると見えなくなるので下限を設ける
    for sign in (-1, 1):
        ex = cx + sign * eye_offset_x
        ey = cy + eye_offset_y
        draw.ellipse(
            [
                ex - eye_radius, ey - eye_radius * eye_height_scale,
                ex + eye_radius, ey + eye_radius * eye_height_scale,
            ],
            fill=EYE_COLOR,
        )

    # 口(mouth_opennessに応じて縦幅が変化する楕円)
    mouth_width = 50
    mouth_min_height = 4    # 閉じた状態でも薄い線として見える最小の高さ
    mouth_max_height = 55
    mouth_height = mouth_min_height + (mouth_max_height - mouth_min_height) * mouth_openness
    mouth_center_y = cy + 45

    draw.ellipse(
        [
            cx - mouth_width / 2, mouth_center_y - mouth_height / 2,
            cx + mouth_width / 2, mouth_center_y + mouth_height / 2,
        ],
        fill=MOUTH_COLOR,
    )

    # 口角上げ(2D変形メッシュ)。回転より先に、無回転の状態で適用する。
    img = _apply_smile_mesh(img, cx, cy, mouth_center_y, joy)

    if head_tilt_deg != 0.0:
        # 首をかしげる動きは、キャンバス全体を頭の中心を軸に回転させる
        # ことで簡易的に表現する(実素材では首から上のレイヤーだけを
        # 回転させる想定)。
        img = img.rotate(
            head_tilt_deg,  # PILは反時計回りが正、VMCのロール符号と合わせて
            resample=Image.BICUBIC,
            center=(cx, cy),
        )

    return img
