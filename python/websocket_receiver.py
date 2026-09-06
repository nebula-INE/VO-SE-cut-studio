"""aural Studio - モーションキャプチャ受信(WebSocket/JSON)

OSC(VMCプロトコル)はUDPベースのバイナリプロトコルのため、ブラウザ上で
動くトラッキングツール(例: MediaPipe FaceMeshをJavaScriptで動かすWebアプリ)
からは直接送信しづらい。plan.mdの「OSC/WebSocket等でPCへ低遅延
ストリーミング」という要求に応えるため、WebSocket経由でも同じ種類の
モーションデータ(頭の位置・傾き、ブレンドシェイプ、その他ボーン)を
受け取れるようにする。

メッセージ形式は独自のシンプルなJSONで、OscMocapReceiverと同じ3種類の
コールバック(on_head_transform, on_blend_shapes, on_bone_transform)に
対応させている。両者はトランスポート層が違うだけで、GUI側(character_
renderer.py)から見れば同じ形のデータを渡すだけなので、受信経路を
意識せず同じ描画コードを使い回せる。

対応しているメッセージ形式(1メッセージ1JSON):
    {"type": "head_transform", "offset_x": 0.05, "offset_y": 0.02, "tilt_deg": 15.0}
    {"type": "blend_shapes", "values": {"Blink_L": 0.9, "Blink_R": 0.9, "Joy": 0.5}}
    {"type": "bone_transform", "name": "Neck",
     "x": 0.0, "y": 0.0, "z": 0.0, "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0}

VMCプロトコルの/VMC/Ext/Blend/Val + /VMC/Ext/Blend/Applyのような分割は
JSON版では行っていない("blend_shapes"メッセージ1件が、既にまとめられた
1フレーム分の値の辞書を運ぶ想定)。
"""

from __future__ import annotations

import asyncio
import json
import threading

import websockets

from osc_receiver import BoneTransform, HeadTransform

DEFAULT_WEBSOCKET_PORT = 39540  # OSC(39539)の隣のポート番号を割り当てている


class WebSocketMocapReceiver:
    """バックグラウンドスレッドでWebSocketサーバーを立て、JSON形式の
    モーションキャプチャメッセージを受信するたびに対応するコールバックを呼ぶ。

    OscMocapReceiverと同じ形のコールバックインターフェースを持つ
    (on_head_transform, on_blend_shapes, on_bone_transform)ため、GUI側は
    どちらの受信経路からのデータかを意識する必要が無い。

    使い方:
        receiver = WebSocketMocapReceiver(
            on_head_transform=on_head,
            on_blend_shapes=on_blend,     # 省略可
            on_bone_transform=on_bone,    # 省略可
        )
        receiver.start()
        ...
        receiver.stop()
    """

    def __init__(
        self,
        on_head_transform,
        on_blend_shapes=None,
        on_bone_transform=None,
        port: int = DEFAULT_WEBSOCKET_PORT,
        host: str = "0.0.0.0",
    ) -> None:
        self._on_head_transform = on_head_transform
        self._on_blend_shapes = on_blend_shapes
        self._on_bone_transform = on_bone_transform
        self._port = port
        self._host = host

        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None

    async def _handle_connection(self, websocket) -> None:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                continue
            self._dispatch(data)

    def _dispatch(self, data: dict) -> None:
        msg_type = data.get("type")

        if msg_type == "head_transform":
            self._on_head_transform(HeadTransform(
                offset_x=float(data.get("offset_x", 0.0)),
                offset_y=float(data.get("offset_y", 0.0)),
                offset_z=float(data.get("offset_z", 0.0)),
                tilt_deg=float(data.get("tilt_deg", 0.0)),
                qx=float(data.get("qx", 0.0)),
                qy=float(data.get("qy", 0.0)),
                qz=float(data.get("qz", 0.0)),
                qw=float(data.get("qw", 1.0)),
            ))
        elif msg_type == "blend_shapes" and self._on_blend_shapes is not None:
            values = data.get("values", {})
            if isinstance(values, dict):
                self._on_blend_shapes({k: float(v) for k, v in values.items()})
        elif msg_type == "bone_transform" and self._on_bone_transform is not None:
            self._on_bone_transform(BoneTransform(
                name=str(data.get("name", "")),
                x=float(data.get("x", 0.0)), y=float(data.get("y", 0.0)), z=float(data.get("z", 0.0)),
                qx=float(data.get("qx", 0.0)), qy=float(data.get("qy", 0.0)),
                qz=float(data.get("qz", 0.0)), qw=float(data.get("qw", 1.0)),
            ))
        # 未知のtypeは無視する(将来のメッセージ種別追加に対して、
        # 古いクライアント/サーバーが互いにクラッシュしないようにするため)。

    def _run_event_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()

        async def _serve() -> None:
            async with websockets.serve(self._handle_connection, self._host, self._port):
                await self._stop_event.wait()

        self._loop.run_until_complete(_serve())
        self._loop.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._loop = None
        self._stop_event = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
