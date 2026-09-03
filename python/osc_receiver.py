"""aural Studio - モーションキャプチャ受信(VMCプロトコル/OSC)

plan.mdのPhase 2「リアルタイム連携: スマホ等で取得したモーションデータを
OSC/WebSocket等でPCへ低遅延ストリーミングし、画面上のキャラクターと
同期させる」を実現する受信部。

独自プロトコルを新設せず、VTuber界隈で広く使われている VMC Protocol
(Virtual Motion Capture Protocol、OSCベース)のサブセットに対応させている。
これにより、iPhoneのARKitトラッキングアプリ(waidayo等)やVSeeFace等、
既存の実在アプリから直接モーションデータを受け取れる可能性がある。
対応アプリの具体的な設定手順は docs/motion_capture_setup.md を参照。

対応しているメッセージ:
    /VMC/Ext/Bone/Pos (string boneName, f32 x, f32 y, f32 z,
                        f32 qx, f32 qy, f32 qz, f32 qw)
        boneName == "Head" のものは特別扱いし、位置(x,y)+ロール角(Z軸回転
        をオイラー角に変換したもの)としてon_head_transformへ渡す。
        それ以外のボーン名は、生の(x,y,z,qx,qy,qz,qw)のままon_bone_transform
        へ渡す(2D側はまだ使っていないが、3D/将来の全身トラッキング拡張に
        備えて素通しだけしておく)。

    /VMC/Ext/Blend/Val (string blendShapeName, f32 value)
        表情のブレンドシェイプ値(0.0〜1.0が一般的)。VMCプロトコルの仕様上、
        1フレーム分の複数のBlend/Valメッセージは即座に反映せず、内部で
        バッファしておく。

    /VMC/Ext/Blend/Apply (引数無し)
        バッファしていたBlend/Valの内容を、この時点でまとめて確定させて
        on_blend_shapesへ渡す(VMCプロトコルが「複数の値をまとめて矛盾なく
        適用する」ために用意している仕組みをそのまま踏襲している)。
        送られてこなかったブレンドシェイプ名は直前の値を保持する
        (差分更新のみを送ってくるアプリにも対応するため、Apply時に
        バッファをクリアはしない)。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from pythonosc import dispatcher as osc_dispatcher
from pythonosc import osc_server

DEFAULT_OSC_PORT = 39539  # VMC Protocolの標準的な受信ポート(慣習上のデフォルト)


@dataclass
class HeadTransform:
    offset_x: float = 0.0   # -1.0〜1.0程度を想定(正規化はしていない生の値)
    offset_y: float = 0.0
    tilt_deg: float = 0.0   # 首をかしげる角度(ロール)


@dataclass
class BoneTransform:
    """Head以外の任意のボーンの生データ(2D側は現状未使用、将来の拡張用)。"""
    name: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0


def _quaternion_to_roll_deg(qx: float, qy: float, qz: float, qw: float) -> float:
    """クォータニオンからロール角(Z軸まわり、度)だけを取り出す。

    2Dキャラクターの表現では「首をかしげる」動き(ロール)だけあれば
    十分表現力があるため、ヨー/ピッチは今回は捨てている。
    """
    # 標準的なクォータニオン→オイラー角変換のロール成分
    sinr_cosp = 2.0 * (qw * qz + qx * qy)
    cosr_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    roll_rad = math.atan2(sinr_cosp, cosr_cosp)
    return math.degrees(roll_rad)


class OscMocapReceiver:
    """バックグラウンドスレッドでOSCサーバーを立て、VMC形式のメッセージを
    受信するたびに対応するコールバックを呼ぶ。

    使い方:
        receiver = OscMocapReceiver(
            on_head_transform=on_head,
            on_blend_shapes=on_blend,     # 省略可
            on_bone_transform=on_bone,    # 省略可(Head以外のボーン用)
        )
        receiver.start()
        ...
        receiver.stop()

    いずれのコールバックも別スレッドから呼ばれるため、GUI側で直接
    ウィジェットを触らず、Qtのシグナル発行などスレッドセーフな手段で
    中継すること。
    """

    def __init__(
        self,
        on_head_transform,
        on_blend_shapes=None,
        on_bone_transform=None,
        port: int = DEFAULT_OSC_PORT,
        ip: str = "0.0.0.0",
    ) -> None:
        self._on_head_transform = on_head_transform
        self._on_blend_shapes = on_blend_shapes
        self._on_bone_transform = on_bone_transform
        self._port = port
        self._ip = ip
        self._server: osc_server.ThreadingOSCUDPServer | None = None
        self._thread: threading.Thread | None = None

        # /VMC/Ext/Blend/Apply が来るまでの間、値をためておくバッファ。
        # Apply抜きで直接反映すると、1フレーム内で複数の表情が来た際に
        # 中途半端な組み合わせの一瞬(例: 笑顔の口だけ先に反映され、目は
        # まだ反映されていない状態)が見えてしまうため、VMCプロトコルの
        # 仕様通りApplyまでは確定させない。
        self._pending_blend_shapes: dict[str, float] = {}

    def _handle_bone_pos(self, address: str, *args) -> None:
        if len(args) < 8:
            return
        bone_name = args[0]
        x, y, z, qx, qy, qz, qw = (float(v) for v in args[1:8])

        if bone_name == "Head":
            tilt_deg = _quaternion_to_roll_deg(qx, qy, qz, qw)
            self._on_head_transform(HeadTransform(offset_x=x, offset_y=y, tilt_deg=tilt_deg))
        elif self._on_bone_transform is not None:
            self._on_bone_transform(BoneTransform(name=bone_name, x=x, y=y, z=z, qx=qx, qy=qy, qz=qz, qw=qw))

    def _handle_blend_val(self, address: str, *args) -> None:
        if len(args) < 2:
            return
        name, value = args[0], float(args[1])
        self._pending_blend_shapes[name] = value

    def _handle_blend_apply(self, address: str, *args) -> None:
        if self._on_blend_shapes is not None:
            # 呼び出し先が内部バッファを書き換えられないよう、コピーを渡す。
            self._on_blend_shapes(dict(self._pending_blend_shapes))

    def start(self) -> None:
        disp = osc_dispatcher.Dispatcher()
        disp.map("/VMC/Ext/Bone/Pos", self._handle_bone_pos)
        disp.map("/VMC/Ext/Blend/Val", self._handle_blend_val)
        disp.map("/VMC/Ext/Blend/Apply", self._handle_blend_apply)

        self._server = osc_server.ThreadingOSCUDPServer((self._ip, self._port), disp)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._pending_blend_shapes.clear()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
