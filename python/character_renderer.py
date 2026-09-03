"""aural Studio - プレースホルダー2Dキャラクター描画

実際のキャラクターイラスト素材がまだ無いため、リップシンクアルゴリズム
(lipsync.py)・モーションキャプチャ受信(osc_receiver.py)の動作検証用に、
簡易的な円ベースのプレースホルダーキャラクターを描画する。

実装しているのは:
    - mouth_openness(0〜1)に応じた口の開閉(楕円の縦幅を変化させる)
    - 「微動」: 常時ゆっくり上下に揺れる呼吸のような動き(sin波、低振幅)
    - head_offset_x/y・head_tilt_deg: OSC(VMCプロトコル)経由で受信した
      実際の頭の位置・傾きをそのまま反映する(Phase 2: モーションキャプチャ)

本番のキャラクター素材(イラスト/画像レイヤー)が用意でき次第、
render_character_frame()の中身を「口パーツ/首から上の画像レイヤーの
差し替え・変形」に置き換えれば、呼び出し側のインターフェース
(mouth_openness, head_offset_x/y, head_tilt_deg)はそのまま使い回せる
設計にしている。
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


def _idle_offset_y(time_sec: float) -> float:
    """常時のゆっくりした上下の微動オフセット(px)。"""
    phase = (time_sec / IDLE_MOTION_PERIOD_SEC) * 2.0 * math.pi
    return math.sin(phase) * IDLE_MOTION_AMPLITUDE_PX


def render_character_frame(
    mouth_openness: float,
    time_sec: float,
    head_offset_x: float = 0.0,
    head_offset_y: float = 0.0,
    head_tilt_deg: float = 0.0,
) -> Image.Image:
    """1フレーム分のキャラクター画像(RGBA)を描画する。

    Args:
        mouth_openness: 0.0(閉じる)〜1.0(全開)。
        time_sec: 現在時刻(秒)。微動(呼吸)のアニメーションに使う。
        head_offset_x: 頭の左右位置オフセット(メートル、VMCプロトコル座標系)。
        head_offset_y: 頭の上下位置オフセット(メートル、VMCプロトコル座標系。
            上が正)。
        head_tilt_deg: 首をかしげる角度(度)。OSC受信データが無い間は0.0。
    """
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

    # 目(2つの小さい円、常に一定)
    eye_offset_x = 35
    eye_offset_y = -20
    eye_radius = 10
    for sign in (-1, 1):
        ex = cx + sign * eye_offset_x
        ey = cy + eye_offset_y
        draw.ellipse([ex - eye_radius, ey - eye_radius, ex + eye_radius, ey + eye_radius], fill=EYE_COLOR)

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

    if head_tilt_deg != 0.0:
        # 首をかしげる動きは、キャンバス全体を頭の中心を軸に回転させる
        # ことで簡易的に表現する(実素材では首から上のレイヤーだけを
        # 回転させる形になる想定)。
        img = img.rotate(
            head_tilt_deg,  # PILは反時計回りが正、VMCのロール符号と合わせて
            resample=Image.BICUBIC,
            center=(cx, cy),
        )

    return img
