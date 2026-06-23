"""実機テスト用データ状態の安全装置（全 Flask Web アプリ共通）。

各アプリの services/local_stub.py がこれを import して二重ガードを判定する。
stub データ本体（fake_tasks 等）はアプリ固有なので各アプリに置く。

二重ガード:
  LOCAL_DEV=true かつ FAKE_DATA in ("empty","sample") が両方揃った時だけ
  stub に分岐する。LOCAL_DEV 単独では入らない（本番事故防止）。

  - "empty":  データ0件（C 空状態バグ＝0件で真っ白を踏む）
  - "sample": 数件入り（A 動作バグ＝追加・編集・削除・並べ替えを踏む）
"""
from __future__ import annotations

_VALID_MODES = ("empty", "sample")


def fake_data_enabled(local_dev: bool, fake_data: str) -> bool:
    """LOCAL_DEV=true かつ FAKE_DATA が有効値なら True（二重ガード）。"""
    return bool(local_dev) and fake_data.lower() in _VALID_MODES


def fake_data_mode(local_dev: bool, fake_data: str) -> str:
    """有効なら正規化したモード（'empty'/'sample'）、無効なら ''。"""
    return fake_data.lower() if fake_data_enabled(local_dev, fake_data) else ""


def assert_not_production(local_dev: bool, *, is_cloud_run: bool) -> None:
    """本番 Cloud Run で LOCAL_DEV=true 起動なら例外で落とす。

    実機テスト用のダミーデータが本番画面に出る/本番 Sheets を触る事故を
    起動時点で止める。各アプリの main.py の起動直後に呼ぶ。
    """
    if is_cloud_run and local_dev:
        raise RuntimeError(
            "LOCAL_DEV=true は本番 Cloud Run で禁止"
            "（実機テスト用データが本番に出る事故防止）"
        )
