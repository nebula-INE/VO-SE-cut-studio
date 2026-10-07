import sys
from pathlib import Path

# python/ 配下のモジュールを、パッケージ化せずそのままimportできるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
