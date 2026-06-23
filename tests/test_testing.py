"""momijian_common.testing（実機テスト用データ状態の安全装置）の単体テスト。"""
import pytest
from momijian_common.testing import (
    fake_data_enabled,
    fake_data_mode,
    assert_not_production,
)


@pytest.mark.parametrize(
    "local_dev,fake_data,expected",
    [
        (True, "empty", True),    # 二重ガード両方揃う → 有効
        (True, "sample", True),
        (True, "EMPTY", True),    # 大文字小文字は無視
        (True, "", False),        # FAKE_DATA 未設定 → 無効
        (True, "full", False),    # full は廃止語、無効
        (True, "enpty", False),   # typo は無視（実シートを読む安全側）
        (False, "empty", False),  # LOCAL_DEV=false → 無効（本番事故防止の核）
        (False, "sample", False),
    ],
)
def test_fake_data_enabled(local_dev, fake_data, expected):
    assert fake_data_enabled(local_dev, fake_data) is expected


def test_fake_data_mode_returns_normalized_mode():
    assert fake_data_mode(True, "EMPTY") == "empty"
    assert fake_data_mode(True, "sample") == "sample"


def test_fake_data_mode_empty_string_when_disabled():
    assert fake_data_mode(False, "empty") == ""   # LOCAL_DEV=false
    assert fake_data_mode(True, "") == ""          # FAKE_DATA 未設定
    assert fake_data_mode(True, "full") == ""      # 廃止語


def test_assert_not_production_raises_on_cloud_run_with_local_dev():
    with pytest.raises(RuntimeError, match="LOCAL_DEV"):
        assert_not_production(True, is_cloud_run=True)


def test_assert_not_production_allows_local_dev_off_cloud_run():
    # 本番でも LOCAL_DEV=false なら通る（None を返す＝例外を投げない）
    assert assert_not_production(False, is_cloud_run=True) is None


def test_assert_not_production_allows_local_machine():
    # ローカル（Cloud Run でない）なら LOCAL_DEV=true でも通る
    assert assert_not_production(True, is_cloud_run=False) is None
