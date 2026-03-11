# momijian-common

## 概要
もみじあん介護サービス自動化システム群の共通ユーティリティパッケージ。
4アプリ（ShienKeika, renrakucho, pdf-automation, manual-generator）で共有。

## パッケージ構成
```
src/momijian_common/
  __init__.py         # バージョン情報
  logger.py           # Cloud Logging 構造化ログ
  text_utils.py       # 日本語テキスト正規化（旧字体変換）
tests/
  __init__.py
```

## 使い方

### インストール（各アプリの requirements.txt に追記）
```
momijian-common @ git+https://github.com/nagamoto-ryota/momijian-common.git@master
```

### インポート
```python
from momijian_common.logger import setup_logger
from momijian_common.text_utils import normalize_japanese
```

## モジュール仕様

### logger.py
- `setup_logger(name, level=None)` — Cloud Run 環境では JSON 構造化ログ、ローカルでは人間可読フォーマット
- `K_SERVICE` or `CLOUD_RUN_JOB` 環境変数で自動判定
- level 省略時は `LOG_LEVEL` 環境変数 → デフォルト `"INFO"`

### text_utils.py
- `normalize_japanese(text)` — NFKC正規化 + 旧字体→新字体変換（21文字対応）
- 介護業界で頻出する旧字体（髙、濵、﨑、齋、etc.）を網羅
