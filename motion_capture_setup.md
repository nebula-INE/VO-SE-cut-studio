# モーションキャプチャ(OSC/VMCプロトコル)接続ガイド

aural Studioは、[VMC Protocol](https://protocol.vmc.info/)(Virtual Motion
Capture Protocol)のサブセットに対応しています。VMCプロトコルはOSCベースの
オープンな規格で、VTuber界隈の多くのアプリが対応しているため、**特別な
連携機能を持たないアプリでも、VMCプロトコルの送信設定さえあれば、そのまま
aural Studioでキャラクターを動かせる可能性があります。**

## 現在対応しているデータ

| 種類 | メッセージ | 内容 |
|---|---|---|
| 頭の位置・傾き | `/VMC/Ext/Bone/Pos`(boneName=`"Head"`) | 2Dは位置(x, y)とZ軸回転(首をかしげる)、3Dプレビューはクォータニオン全体を使用。傾くのは頭だけで、体は動かない |
| 腕のボーン | `/VMC/Ext/Bone/Pos`(`LeftUpperArm`/`LeftLowerArm`/`RightUpperArm`/`RightLowerArm`) | Z軸回転(ロール)で腕の角度を反映。1つでも受信すると体と腕を描画する。前腕は上腕からの相対回転として扱う |
| 表情 | `/VMC/Ext/Blend/Val` + `/VMC/Ext/Blend/Apply` | `Blink`/`Blink_L`/`Blink_R`(まばたき)、`Joy`(笑顔)、`Angry`(つり眉・口角下げ)、`Sorrow`(困り眉・口角下げ)、`Fun`(目を細めて笑う) |
| 母音の口形 | 同上 | `A`/`I`/`U`/`E`/`O`(VRM0.x系)および`aa`/`ih`/`ou`/`ee`/`oh`(VRM1.0系) |

VRM1.0系の表情名(`happy`/`sad`/`relaxed`)も、内部で`Joy`/`Sorrow`/`Fun`に
読み替えて受け付けます(`python/expression.py`)。

**未対応**: 脚・胴体(Spine等)のボーン、`Surprised`等その他のブレンドシェイプ
(受信はしますが描画には使いません)。腕の左右・回転の符号は実機の
トラッキングデータでは未確認です(下記「動作確認用の疑似送信スクリプト」で
確認できます)。

モーションキャプチャを繋いだだけ(動画を開いていない状態)でも、暗い背景の上に
キャラクターが表示されます。

`表示` → `モード` から、2Dモード(キャラのみ)・3Dモード(3Dプロキシのみ)・
2D+3D(両方)を切り替えられます。どのモードでも同じデータで駆動されます。

## 受信ポート

デフォルトで **UDPポート 39539** で待ち受けます(VMCプロトコルで慣習的に
使われるポート番号)。aural StudioのGUI(`main_window.py`)を起動すると、
自動的にこのポートでの受信を開始します。

## 対応が見込まれる送信側アプリの例

以下は、VMCプロトコルでOSC送信に対応していることが知られているアプリの例です
(実機での動作確認はまだ行っていません。設定手順はアプリのバージョンにより
変わる可能性があるため、各アプリの最新のドキュメントも参照してください)。

### iPhoneのARKit顔トラッキングアプリ(例: waidayo等)

1. iPhoneとPCを同じWi-Fiネットワークに接続する
2. アプリの送信先設定で、PCのIPアドレスとポート`39539`を指定する
3. アプリを起動し、顔を映すとトラッキングが始まる

### VSeeFace(Windows、Webカメラベースのトラッキング)

1. VSeeFaceの設定画面で「OSC/VMC」の送信を有効にする
2. 送信先IPアドレスにaural Studioを動かしているPCのIPアドレス(同一PC上で
   動かす場合は`127.0.0.1`)、ポートに`39539`を指定する

### PCのIPアドレスの確認方法

- **Windows**: コマンドプロンプトで `ipconfig` を実行し、「IPv4 アドレス」を確認
- **macOS**: 「システム設定」→「Wi-Fi」→ 接続中のネットワークの詳細
- **Linux**: ターミナルで `ip addr` を実行

同一PC上でトラッキングアプリとaural Studioを両方動かす場合は、
`127.0.0.1`(localhost)を指定してください。

## 動作確認用の疑似送信スクリプト

実機が無くても、以下のようなPythonスクリプトでOSCメッセージを送信すれば、
aural Studio側の受信・反映を確認できます(`python-osc`が必要:
`pip install python-osc`)。

```python
from pythonosc.udp_client import SimpleUDPClient
import math

client = SimpleUDPClient('127.0.0.1', 39539)

# 頭を軽く右に傾ける
client.send_message('/VMC/Ext/Bone/Pos',
    ['Head', 0.05, 0.0, 0.0, 0.0, 0.0, 0.1, 0.995])

# まばたき
client.send_message('/VMC/Ext/Blend/Val', ['Blink_L', 1.0])
client.send_message('/VMC/Ext/Blend/Val', ['Blink_R', 1.0])
client.send_message('/VMC/Ext/Blend/Apply', [])
```

## トラブルシューティング

- **キャラクターが全く反応しない**: ファイアウォールがUDPポート39539を
  ブロックしていないか確認してください。また、送信側アプリと同じネットワーク
  (同一Wi-Fi)に接続されているか確認してください。
- **動きが不自然/過剰に大きい・小さい**: 現在の位置スケール
  (`character_renderer.py`の`HEAD_POSITION_SCALE_PX_PER_METER`)はまだ
  プレースホルダー調整値です。実機での見え方に応じて調整してください。
- **ポート39539が使用中というエラーが出る**: 他のVMC対応アプリ(VSeeFace等)
  が同じポートで待ち受けていないか確認してください。1台のPCで同時に
  複数のVMC受信側を起動することはできません。

腕を動かす例(左上腕をZ軸まわりに60度回転):

```python
import math
half = math.radians(60) / 2
client.send_message('/VMC/Ext/Bone/Pos',
    ['LeftUpperArm', 0.0, 0.0, 0.0, 0.0, 0.0, math.sin(half), math.cos(half)])

# 母音と表情
client.send_message('/VMC/Ext/Blend/Val', ['aa', 1.0])
client.send_message('/VMC/Ext/Blend/Val', ['happy', 0.8])
client.send_message('/VMC/Ext/Blend/Apply', [])
```

## テストの実行

GUI・FFmpeg・OpenGLが無くても動く純Pythonのテストがあります。

```
pip install -r requirements-dev.txt
python -m pytest tests -q
```
