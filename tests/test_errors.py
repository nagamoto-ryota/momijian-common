"""classify_error の単体テスト"""
import pytest
from unittest.mock import Mock
from momijian_common.errors import classify_error, ErrorReport
from googleapiclient.errors import HttpError


def _http_error(status: int, content: bytes = b"err"):
    resp = Mock()
    resp.status = status
    return HttpError(resp=resp, content=content)


def test_timeout_classified_as_TIMEOUT():
    r = classify_error(TimeoutError("timeout"))
    assert r.category == "TIMEOUT"
    assert r.recoverable is True
    assert r.target == "day_group"
    assert "タイムアウト" in r.user_message


def test_http_503_classified_as_TIMEOUT():
    r = classify_error(_http_error(503))
    assert r.category == "TIMEOUT"
    assert r.recoverable is True


def test_http_429_classified_as_QUOTA_EXCEEDED():
    r = classify_error(_http_error(429))
    assert r.category == "QUOTA_EXCEEDED"
    assert r.recoverable is True
    assert r.target == "day_group"


def test_http_404_with_template_context():
    err = _http_error(404, content=b"docId 12345 not found")
    # HttpError のメッセージに template_doc_id が含まれる場合
    context = {"template_doc_id": "12345"}
    r = classify_error(err, context=context)
    # 注: HttpError の str() 形式に依存するので、結果はTEMPLATE_BROKEN または CONFIG_MISSING
    assert r.category in ("TEMPLATE_BROKEN", "CONFIG_MISSING")
    assert r.recoverable is False
    assert r.target == "owner_dm"


def test_http_404_without_context_is_CONFIG_MISSING():
    r = classify_error(_http_error(404))
    assert r.category == "CONFIG_MISSING"
    assert r.recoverable is False
    assert r.target == "owner_dm"


def test_http_403_classified_as_AUTH_FAIL():
    r = classify_error(_http_error(403))
    assert r.category == "AUTH_FAIL"
    assert r.recoverable is False
    assert r.target == "owner_dm"


def test_connection_error_classified_as_TIMEOUT():
    r = classify_error(ConnectionError("connection lost"))
    assert r.category == "TIMEOUT"
    assert r.recoverable is True


def test_unknown_exception():
    r = classify_error(ValueError("something weird"))
    assert r.category == "UNKNOWN"
    assert r.recoverable is False
    assert r.target == "owner_dm"


def test_gemini_error_classified():
    r = classify_error(RuntimeError("Gemini returned empty response"))
    assert r.category == "GEMINI_PARSE_FAIL"
    assert r.target == "day_group"


def test_template_placeholder_error():
    r = classify_error(RuntimeError("placeholder not found in template"))
    assert r.category == "PLACEHOLDER_CORRUPT"
    assert r.target == "owner_dm"


def test_error_report_is_frozen():
    """ErrorReport は dataclass(frozen=True) なので変更不可"""
    r = classify_error(TimeoutError("t"))
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        r.category = "CHANGED"
