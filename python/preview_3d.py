"""aural Studio - 3Dヘッドプロキシプレビュー(Phase 2: モーションキャプチャ)

「共通モーションキャプチャエンジンを、2D(変形メッシュ)と3D(ボーン)の
両方に流し込める」というplan.mdの設計目標を実証するため、2Dキャラクター
(character_renderer.py)と全く同じモーションキャプチャデータ(OSC/VMC
プロトコル、WebSocket)を、簡易的な3Dオブジェクト(球+目)に適用して
回転させる最小限のプレビューを実装している。

2D側はロール角(tilt_deg)だけを使っていたのに対し、こちらはクォータニオン
全体(qx,qy,qz,qw)をそのまま3D回転行列に変換して使う。3Dモデルの読み込み・
ボーンスキニング等、本格的な3Dキャラクター描画は未実装で、あくまで
「同じデータソースを3D側でも消費できる」ことの証明が目的の最小実装。
"""

from __future__ import annotations

import math

from OpenGL import GL
from PySide6.QtOpenGLWidgets import QOpenGLWidget


def _quaternion_to_matrix(qx: float, qy: float, qz: float, qw: float) -> list[float]:
    """クォータニオンを、OpenGLのglMultMatrixfにそのまま渡せる列優先(column-major)
    の4x4回転行列(16要素の配列)に変換する。
    """
    # 正規化(送信元によっては厳密に単位クォータニオンでない場合があるため)
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm < 1e-8:
        qx, qy, qz, qw = 0.0, 0.0, 0.0, 1.0
    else:
        qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm

    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz

    # 列優先(OpenGL形式)
    return [
        1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy), 0.0,
        2 * (xy - wz), 1 - 2 * (xx + zz), 2 * (yz + wx), 0.0,
        2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (xx + yy), 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _draw_sphere(radius: float, slices: int = 16, stacks: int = 16) -> None:
    """PyOpenGLにはgluSphere相当の簡易ヘルパーが無いため、緯度経度分割の
    球を素朴にGL_QUADSで描画する(固定機能パイプライン、GL1.1相当)。
    """
    for i in range(stacks):
        lat0 = math.pi * (-0.5 + i / stacks)
        lat1 = math.pi * (-0.5 + (i + 1) / stacks)
        y0, y1 = math.sin(lat0), math.sin(lat1)
        r0, r1 = math.cos(lat0), math.cos(lat1)

        GL.glBegin(GL.GL_QUAD_STRIP)
        for j in range(slices + 1):
            lng = 2 * math.pi * j / slices
            x, z = math.cos(lng), math.sin(lng)
            GL.glNormal3f(x * r0, y0, z * r0)
            GL.glVertex3f(radius * x * r0, radius * y0, radius * z * r0)
            GL.glNormal3f(x * r1, y1, z * r1)
            GL.glVertex3f(radius * x * r1, radius * y1, radius * z * r1)
        GL.glEnd()


class Head3DPreviewWidget(QOpenGLWidget):
    """モーションキャプチャで受信した頭のクォータニオン回転を、
    簡易的な3Dオブジェクト(球+目)にそのまま適用して表示するウィジェット。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 240)
        self._qx, self._qy, self._qz, self._qw = 0.0, 0.0, 0.0, 1.0
        self._offset_x, self._offset_y, self._offset_z = 0.0, 0.0, 0.0

    def set_head_quaternion(
        self,
        offset_x: float, offset_y: float, offset_z: float,
        qx: float, qy: float, qz: float, qw: float,
    ) -> None:
        """OSC/WebSocket経由で受信した頭の位置・クォータニオン回転をそのまま
        受け取り、次の再描画で反映する。
        """
        self._offset_x, self._offset_y, self._offset_z = offset_x, offset_y, offset_z
        self._qx, self._qy, self._qz, self._qw = qx, qy, qz, qw
        self.update()  # QOpenGLWidgetの再描画をスケジュールする

    def initializeGL(self) -> None:
        GL.glClearColor(0.12, 0.12, 0.12, 1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_LIGHTING)
        GL.glEnable(GL.GL_LIGHT0)
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, [1.0, 1.0, 2.0, 0.0])
        # GL_LIGHTING有効時、glColor3fだけでは表面色に反映されない
        # (デフォルトのマテリアル色のまま灰色に見えてしまう)。
        # GL_COLOR_MATERIALを有効にし、glColor3fの値をそのまま
        # ambient+diffuseマテリアル色として扱うようにする。
        GL.glEnable(GL.GL_COLOR_MATERIAL)
        GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)

    def resizeGL(self, w: int, h: int) -> None:
        GL.glViewport(0, 0, max(w, 1), max(h, 1))
        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glLoadIdentity()
        aspect = w / h if h > 0 else 1.0
        # gluPerspective相当を手計算(PyOpenGLのGLUに依存しないため)
        fov_y_deg, near, far = 45.0, 0.1, 10.0
        f = 1.0 / math.tan(math.radians(fov_y_deg) / 2.0)
        GL.glLoadMatrixf([
            f / aspect, 0, 0, 0,
            0, f, 0, 0,
            0, 0, (far + near) / (near - far), -1,
            0, 0, (2 * far * near) / (near - far), 0,
        ])
        GL.glMatrixMode(GL.GL_MODELVIEW)

    def paintGL(self) -> None:
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        GL.glLoadIdentity()
        GL.glTranslatef(self._offset_x, self._offset_y, -3.0 + self._offset_z)

        rotation_matrix = _quaternion_to_matrix(self._qx, self._qy, self._qz, self._qw)
        GL.glMultMatrixf(rotation_matrix)

        # 頭(球)
        GL.glColor3f(1.0, 0.878, 0.741)  # FACE_COLORと近い色調
        _draw_sphere(radius=1.0)

        # 目(2つの小さい球。頭のローカル座標系に乗るため、回転済みの
        # モデルビュー行列の上でそのまま描画すれば頭と一緒に回る)
        GL.glColor3f(0.16, 0.12, 0.10)
        for sign in (-1, 1):
            GL.glPushMatrix()
            GL.glTranslatef(sign * 0.35, 0.15, 0.85)
            _draw_sphere(radius=0.12, slices=8, stacks=8)
            GL.glPopMatrix()
