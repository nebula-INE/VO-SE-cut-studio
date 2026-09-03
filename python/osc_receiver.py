"""aural Studio - モーションキャプチャ受信(VMCプロトコル/OSC)

plan.mdのPhase 2「リアルタイム連携: スマホ等で取得したモーションデータを
OSC/WebSocket等でPCへ低遅延ストリーミングし、画面上のキャラクターと
同期させる」を実現する受信部。

独自プロトコルを新設せず、VTuber界隈で広く使われている VMC Protocol
(Virtual Motion Capture Protocol、OSCベース)のサブセットに対応させている。
これにより、iPhoneのARKitトラッキングアプリ(waidayo等)やVSeeFace等、
既存の実在アプリから直接モーションデータを受け取れる可能性がある。

対応しているメッセージ(VMC Protocolのうち、頭部の位置/回転のみ):
    /VMC/Ext/Bone/Pos (string boneName, f32 x, f32 y, f32 z,
                        f32 qx, f32 qy, f32 qz, f32 qw)
        boneName == "Head" のメッセージのみを処理する(基礎実装のため)。
        回転はクォータニオンで送られてくるが、2Dキャラクターの表現には
        過剰なため、Z軸(ロール、首をかしげる動き)のみをオイラー角に変換
        して使う。

現時点では他のボーン(腕や体全体等)やBlendShape(/VMC/Ext/Blend/Val)は
未対応。2D/3D共通モーションキャプチャ基盤の最初の一歩として、まず
「頭の位置・傾き」だけを通す。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

from pythonosc import dispatcher as osc_dispatcher
from pythonosc import osc_server

DEFAULT_OSC_PORT = 39539  # VMC Protocolの標準的な受信ポート(慣習上のデフォルト)


@dataclass
class HeadTransform:
    offset_x: float = 0.0   # -1.0〜1.0程度を想定(正規化はしていない生の値)
    offset_y: float = 0.0
    tilt_deg: float = 0.0   # 首をかしげる角度(ロール)


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
    """バックグラウンドスレッドでOSCサーバーを立て、VMC形式のHeadボーン
    メッセージを受信するたびにコールバックを呼ぶ。

    使い方:
        receiver = OscMocapReceiver(on_head_transform=callback)
        receiver.start()
        ...
        receiver.stop()
    """

    def __init__(self, on_head_transform, port: int = DEFAULT_OSC_PORT, ip: str = "0.0.0.0") -> None:
        """on_head_transform: HeadTransformを1つ受け取るコールバック関数。
        別スレッドから呼ばれるため、GUI側で直接ウィジェットを触らず、
        Qtのシグナル発行などスレッドセーフな手段で中継すること。
        """
        self._on_head_transform = on_head_transform
        self._port = port
        self._ip = ip
        self._server: osc_server.ThreadingOSCUDPServer | None = None
        self._thread: threading.Thread | None = None

    def _handle_bone_pos(self, address: str, *args) -> None:
        if len(args) < 8:
            return
        bone_name = args[0]
        if bone_name != "Head":
            return  # 基礎実装のため、Headボーン以外は無視する

        _x, _y, _z, qx, qy, qz, qw = args[1:8]
        offset_x, offset_y = float(args[1]), float(args[2])
        tilt_deg = _quaternion_to_roll_deg(float(qx), float(qy), float(qz), float(qw))

        self._on_head_transform(HeadTransform(offset_x=offset_x, offset_y=offset_y, tilt_deg=tilt_deg))

    def start(self) -> None:
        disp = osc_dispatcher.Dispatcher()
        disp.map("/VMC/Ext/Bone/Pos", self._handle_bone_pos)

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

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
