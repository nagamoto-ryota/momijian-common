"""堅牢な Google API リトライデコレータ (Phase 0)

tenacity ベースで指数バックオフ + full jitter、
TimeoutError/socket.timeout/SSLError/HttpError(429/5xx) を包括的にリトライする。
"""
from __future__ import annotations
import logging
import random
import socket
import ssl
from typing import Callable, Optional
from functools import wraps

import tenacity
from googleapiclient.errors import HttpError
try:
    import httplib2  # type: ignore
    _HTTPLIB2_ERRORS = (httplib2.ServerNotFoundError,)
except Exception:
    _HTTPLIB2_ERRORS = ()

logger = logging.getLogger(__name__)

_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _is_retriable(exc: BaseException) -> bool:
    """リトライ対象例外かを判定"""
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        try:
            status_int = int(status) if status is not None else None
        except Exception:
            status_int = None
        return status_int in _TRANSIENT_HTTP_STATUSES
    if isinstance(exc, (TimeoutError, socket.timeout, BrokenPipeError,
                        ConnectionError, ConnectionResetError, ssl.SSLError)):
        return True
    if _HTTPLIB2_ERRORS and isinstance(exc, _HTTPLIB2_ERRORS):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {32, 104, 110}  # EPIPE, ECONNRESET, ETIMEDOUT
    return False


def retry_api_v2(
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_total_seconds: float = 90.0,
    invalidate_callback: Optional[Callable[[], None]] = None,
    *,
    delay: Optional[float] = None,  # 互換性: 旧 retry_api(delay=2.0) 対応
):
    """Google API 呼び出し用の堅牢なリトライデコレータ。

    - tenacity ベースで指数バックオフ + full jitter
    - max_total_seconds で総待機時間の上限を制御
    - invalidate_callback で Google API サービスキャッシュを無効化
    - delay は後方互換のため initial_delay のエイリアスとして受け付ける

    Args:
        max_retries: 最大リトライ回数 (初回+リトライの合計は max_retries+1 回)
        initial_delay: 初回バックオフ秒 (2^attempt でスケール、最大16秒)
        max_total_seconds: リトライ全体の累積時間上限
        invalidate_callback: リトライ前に呼ぶキャッシュ無効化関数 (省略可)
        delay: 旧 retry_api の互換キーワード引数 (initial_delay と同義)
    """
    if delay is not None:
        initial_delay = delay

    def _wait(retry_state: tenacity.RetryCallState) -> float:
        attempt = retry_state.attempt_number  # 1-indexed
        base = min(initial_delay * (2 ** (attempt - 1)), 16.0)
        sleep = base * random.uniform(0.5, 1.0)  # full jitter
        return sleep

    def _before_sleep(retry_state: tenacity.RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        fn_name = retry_state.fn.__name__ if retry_state.fn else "<unknown>"
        logger.warning(
            f"接続エラー({type(exc).__name__})リトライ "
            f"{retry_state.attempt_number}/{max_retries}: {fn_name}"
        )
        if invalidate_callback is not None:
            try:
                invalidate_callback()
            except Exception as e:
                logger.warning(f"invalidate_callback 失敗: {e}")

    def decorator(func: Callable):
        retry = tenacity.Retrying(
            stop=tenacity.stop_any(
                tenacity.stop_after_attempt(max_retries + 1),
                tenacity.stop_after_delay(max_total_seconds),
            ),
            wait=_wait,
            retry=tenacity.retry_if_exception(_is_retriable),
            before_sleep=_before_sleep,
            reraise=True,
        )

        @wraps(func)
        def wrapper(*args, **kwargs):
            return retry(func, *args, **kwargs)
        return wrapper
    return decorator
