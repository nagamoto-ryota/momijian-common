"""retry_api_v2 の単体テスト"""
import pytest
import socket
from unittest.mock import Mock
from momijian_common.retry import retry_api_v2
from googleapiclient.errors import HttpError


def _make_http_error(status: int):
    resp = Mock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


def test_retry_timeout_then_success():
    """TimeoutError が 2 回発生した後に成功するケース"""
    call_count = [0]

    @retry_api_v2(max_retries=3, initial_delay=0.01, max_total_seconds=10)
    def flaky():
        call_count[0] += 1
        if call_count[0] < 3:
            raise TimeoutError("timeout")
        return "ok"

    assert flaky() == "ok"
    assert call_count[0] == 3


def test_retry_exhaust_raises():
    """リトライ上限超過で最終例外を raise する"""
    @retry_api_v2(max_retries=2, initial_delay=0.01)
    def always_fail():
        raise TimeoutError("timeout")

    with pytest.raises(TimeoutError):
        always_fail()


def test_retry_http_404_no_retry():
    """HttpError 404 はリトライ対象外、即 raise"""
    call_count = [0]

    @retry_api_v2(max_retries=5, initial_delay=0.01)
    def not_found():
        call_count[0] += 1
        raise _make_http_error(404)

    with pytest.raises(HttpError):
        not_found()
    assert call_count[0] == 1


def test_retry_http_503_retries():
    """HttpError 503 はリトライ対象"""
    call_count = [0]

    @retry_api_v2(max_retries=3, initial_delay=0.01)
    def flaky():
        call_count[0] += 1
        if call_count[0] < 2:
            raise _make_http_error(503)
        return "ok"

    assert flaky() == "ok"
    assert call_count[0] == 2


def test_retry_compat_delay_kwarg():
    """旧 retry_api(delay=X) との互換性"""
    call_count = [0]

    @retry_api_v2(max_retries=2, delay=0.01)  # delay= キーワードを受け付ける
    def flaky():
        call_count[0] += 1
        if call_count[0] < 2:
            raise socket.timeout("timeout")
        return "ok"

    assert flaky() == "ok"


def test_retry_max_total_seconds():
    """max_total_seconds で累積時間を制御"""
    import time
    start = time.time()

    @retry_api_v2(max_retries=100, initial_delay=0.5, max_total_seconds=1.0)
    def always_fail():
        raise TimeoutError("t")

    with pytest.raises(TimeoutError):
        always_fail()
    elapsed = time.time() - start
    assert elapsed < 2.5  # max_total_seconds + jitter 余裕


def test_retry_invalidate_callback_called():
    """invalidate_callback がリトライ前に呼ばれる"""
    calls = []

    def invalidate():
        calls.append(1)

    call_count = [0]

    @retry_api_v2(max_retries=3, initial_delay=0.01, invalidate_callback=invalidate)
    def flaky():
        call_count[0] += 1
        if call_count[0] < 3:
            raise TimeoutError("t")
        return "ok"

    flaky()
    assert len(calls) >= 1  # 少なくとも1回は呼ばれる
