"""momijian_common.delivery の pytest テスト群。

MailChannel、DeliveryLogger、BounceMonitor を mock で検証する。
外部 API（Gmail / Sheets）は一切呼ばない。

カバレッジ対象:
  MailChannel: 送信成功 / 401 即時失敗 / 5xx リトライ成功 / 5xx リトライ枯渇
               / 429 リトライ / Message-ID 取得 / archive_message 成功&不在
  DeliveryLogger: append 成功 / Message-ID 重複 skip / Message-ID 空の場合
                  / from_address 記録 / error_message 記録
"""

from __future__ import annotations

import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from momijian_common.delivery.bounce import BounceMonitor
from momijian_common.delivery.channel import ChannelType, DeliveryResult
from momijian_common.delivery.mail import MailChannel
from momijian_common.delivery.logger import DeliveryLogger


# ---------------------------------------------------------------------------
# ヘルパー: HttpError モック生成
# ---------------------------------------------------------------------------

def _make_http_error(status: int, content: bytes = b"error") -> Exception:
    """googleapiclient.errors.HttpError 互換のエラーオブジェクトを返す。

    HttpError は resp.status で HTTP ステータスを保持する。
    mail.py では getattr(exc, "resp", None), .status で読む。
    """
    try:
        from googleapiclient.errors import HttpError

        resp = Mock()
        resp.status = status
        resp.reason = f"HTTP {status}"
        err = HttpError(resp=resp, content=content)
        return err
    except ImportError:
        # googleapiclient が入っていない環境向けのフォールバック
        err = Exception(f"HttpError {status}")
        err.resp = Mock()  # type: ignore[attr-defined]
        err.resp.status = status
        return err


# ---------------------------------------------------------------------------
# ヘルパー: Gmail service モック生成
# ---------------------------------------------------------------------------

def _make_service(
    send_side_effect: Any = None,
    send_return: dict | None = None,
    get_return: dict | None = None,
    list_return: dict | None = None,
    modify_return: dict | None = None,
) -> MagicMock:
    """Gmail service オブジェクトの Mock を組み立てる。

    service.users().messages().send().execute()  -> send_side_effect or send_return
    service.users().messages().get().execute()   -> get_return
    service.users().messages().list().execute()  -> list_return
    service.users().messages().modify().execute()-> modify_return
    """
    service = MagicMock()

    messages = service.users.return_value.messages.return_value

    # send
    send_execute = messages.send.return_value.execute
    if send_side_effect is not None:
        send_execute.side_effect = send_side_effect
    elif send_return is not None:
        send_execute.return_value = send_return

    # get (Message-ID 逆引き)
    get_execute = messages.get.return_value.execute
    if get_return is not None:
        get_execute.return_value = get_return

    # list (archive 用 rfc822msgid 検索)
    list_execute = messages.list.return_value.execute
    if list_return is not None:
        list_execute.return_value = list_return

    # modify (INBOX ラベル除去)
    modify_execute = messages.modify.return_value.execute
    if modify_return is not None:
        modify_execute.return_value = modify_return

    return service


# ---------------------------------------------------------------------------
# ヘルパー: Sheets service モック生成
# ---------------------------------------------------------------------------

def _make_sheets_service(
    get_return: dict | None = None,
    append_return: dict | None = None,
) -> MagicMock:
    """Sheets service オブジェクトの Mock を組み立てる。

    service.spreadsheets().values().get().execute()    -> get_return
    service.spreadsheets().values().append().execute() -> append_return
    """
    service = MagicMock()

    values = service.spreadsheets.return_value.values.return_value

    get_execute = values.get.return_value.execute
    get_execute.return_value = get_return or {"values": []}

    append_execute = values.append.return_value.execute
    append_execute.return_value = append_return or {}

    return service


# ===========================================================================
# MailChannel テスト
# ===========================================================================

class TestMailChannelSendSuccess:
    """添付なし送信成功 — success=True、message_id が RFC 822 形式。"""

    def test_send_success(self):
        gmail_internal_id = "abc123"
        rfc822_mid = "<unique-id@mail.gmail.com>"

        service = _make_service(
            send_return={"id": gmail_internal_id},
            get_return={
                "payload": {
                    "headers": [{"name": "Message-ID", "value": rfc822_mid}]
                }
            },
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            result = ch.send(
                recipient="test@example.com",
                subject="テスト件名",
                body="テスト本文",
            )

        assert result.success is True
        assert result.message_id == rfc822_mid
        # RFC 822 形式: <...@...>
        assert re.match(r"<[^@]+@[^>]+>", result.message_id)
        assert result.channel == ChannelType.MAIL
        assert result.error is None


class TestMailChannelSendWithCategory:
    """category 引数を渡すと X-Momijian-Category ヘッダーが MIME に含まれる。"""

    def test_send_with_category_adds_header(self):
        gmail_internal_id = "cat123"
        rfc822_mid = "<category-test@mail.gmail.com>"

        service = _make_service(
            send_return={"id": gmail_internal_id},
            get_return={
                "payload": {
                    "headers": [{"name": "Message-ID", "value": rfc822_mid}]
                }
            },
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            result = ch.send(
                recipient="dest@example.com",
                subject="提供票送付",
                body="本文",
                category="teikyohyou",
            )

        assert result.success is True

        call_args = service.users().messages().send.call_args
        raw_encoded = call_args[1]["body"]["raw"]
        import base64
        raw_bytes = base64.urlsafe_b64decode(raw_encoded)
        assert b"X-Momijian-Category: teikyohyou" in raw_bytes

    def test_send_without_category_no_header(self):
        """category 省略時はヘッダーが付かない（既存挙動の維持）。"""
        gmail_internal_id = "nocat123"
        rfc822_mid = "<no-category-test@mail.gmail.com>"

        service = _make_service(
            send_return={"id": gmail_internal_id},
            get_return={
                "payload": {
                    "headers": [{"name": "Message-ID", "value": rfc822_mid}]
                }
            },
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            result = ch.send(
                recipient="dest@example.com",
                subject="件名",
                body="本文",
            )

        assert result.success is True

        call_args = service.users().messages().send.call_args
        raw_encoded = call_args[1]["body"]["raw"]
        import base64
        raw_bytes = base64.urlsafe_b64decode(raw_encoded)
        assert b"X-Momijian-Category" not in raw_bytes


class TestMailChannelSendWithAttachment:
    """PDF 1個添付して送信成功 — multipart 構築確認。"""

    def test_send_with_attachment(self, tmp_path: Path):
        # 最小 PDF バイナリ（PyMuPDF 不要）
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")

        gmail_internal_id = "xyz789"
        rfc822_mid = "<attach-test@mail.gmail.com>"

        service = _make_service(
            send_return={"id": gmail_internal_id},
            get_return={
                "payload": {
                    "headers": [{"name": "Message-ID", "value": rfc822_mid}]
                }
            },
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            result = ch.send(
                recipient="dest@example.com",
                subject="提供票送付",
                body="本文",
                attachments=[pdf_path],
            )

        assert result.success is True
        assert result.message_id == rfc822_mid

        # send() に渡された raw には multipart の境界文字列が含まれるはず
        call_args = service.users().messages().send.call_args
        raw_encoded = call_args[1]["body"]["raw"] if call_args[1] else call_args[0][0]["raw"]
        import base64
        raw_bytes = base64.urlsafe_b64decode(raw_encoded)
        assert b"Content-Disposition" in raw_bytes
        assert b"test.pdf" in raw_bytes


class TestMailChannelAuthError:
    """401 は即時失敗（リトライなし）。"""

    def test_send_401_auth_error(self):
        err = _make_http_error(401)
        service = _make_service(send_side_effect=err)

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            with patch("momijian_common.delivery.mail.time.sleep") as mock_sleep:
                result = ch.send(
                    recipient="dest@example.com",
                    subject="件名",
                    body="本文",
                )

        assert result.success is False
        assert result.message_id is None
        assert result.error  # エラーメッセージが入っていること
        # リトライ不要: sleep を呼ばない
        mock_sleep.assert_not_called()


class TestMailChannelRetrySuccess:
    """503 → 503 → 200 のレスポンスでリトライ後成功。"""

    def test_send_5xx_retry_success(self):
        err_503 = _make_http_error(503)
        rfc822_mid = "<retry-ok@mail.gmail.com>"

        # side_effect リスト: [例外, 例外, 成功辞書]
        send_side_effects = [
            err_503,
            err_503,
            {"id": "retried_id"},
        ]

        service = _make_service(send_side_effect=send_side_effects)
        # get: Message-ID 逆引き
        service.users().messages().get.return_value.execute.return_value = {
            "payload": {
                "headers": [{"name": "Message-ID", "value": rfc822_mid}]
            }
        }

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            with patch("momijian_common.delivery.mail.time.sleep"):
                result = ch.send(
                    recipient="dest@example.com",
                    subject="件名",
                    body="本文",
                )

        assert result.success is True
        assert result.message_id == rfc822_mid
        # send が3回呼ばれた
        assert service.users().messages().send.call_count == 3


class TestMailChannelRetryExhausted:
    """503 × 4回で諦めて success=False。"""

    def test_send_5xx_retry_exhausted(self):
        # _RETRY_DELAYS = (1, 2, 4) → 最大4回試行（初回 + 3リトライ）
        err_503 = _make_http_error(503)
        # side_effect リスト: 4つの例外
        side_effects = [err_503, err_503, err_503, err_503]

        service = _make_service(send_side_effect=side_effects)

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            with patch("momijian_common.delivery.mail.time.sleep"):
                result = ch.send(
                    recipient="dest@example.com",
                    subject="件名",
                    body="本文",
                )

        assert result.success is False
        assert result.message_id is None


class TestMailChannelRetry429:
    """429 は retryable なので再試行する。"""

    def test_send_429_retry(self):
        err_429 = _make_http_error(429)
        rfc822_mid = "<rate-limited@mail.gmail.com>"

        # 429 → 成功
        side_effects = [err_429, {"id": "rate_limited_id"}]
        service = _make_service(send_side_effect=side_effects)
        service.users().messages().get.return_value.execute.return_value = {
            "payload": {
                "headers": [{"name": "Message-ID", "value": rfc822_mid}]
            }
        }

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            with patch("momijian_common.delivery.mail.time.sleep"):
                result = ch.send(
                    recipient="dest@example.com",
                    subject="件名",
                    body="本文",
                )

        assert result.success is True
        assert service.users().messages().send.call_count == 2


class TestMailChannelMessageIdExtraction:
    """messages.get(format='metadata') で Message-ID ヘッダを取得する。"""

    def test_message_id_extraction(self):
        rfc822_mid = "<extracted@mail.gmail.com>"

        service = _make_service(
            send_return={"id": "internal_id_001"},
            get_return={
                "payload": {
                    "headers": [
                        {"name": "From", "value": "office@momijian.co"},
                        {"name": "Message-ID", "value": rfc822_mid},
                    ]
                }
            },
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            result = ch.send(
                recipient="dest@example.com",
                subject="件名",
                body="本文",
            )

        assert result.success is True
        assert result.message_id == rfc822_mid

        # get が format="metadata" で呼ばれていること
        get_call = service.users().messages().get.call_args
        assert get_call[1]["format"] == "metadata"
        assert "Message-ID" in get_call[1]["metadataHeaders"]


class TestMailChannelArchiveSuccess:
    """rfc822msgid 検索 → 内部 ID → messages.modify(removeLabelIds=['INBOX'])。"""

    def test_archive_message_success(self):
        rfc822_mid = "<archive-me@mail.gmail.com>"
        internal_id = "internal_archive_001"

        service = _make_service(
            list_return={"messages": [{"id": internal_id}]},
            modify_return={"id": internal_id, "labelIds": []},
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            ok = ch.archive_message(rfc822_mid)

        assert ok is True

        # list で rfc822msgid:<message_id> クエリが渡されていること
        list_call = service.users().messages().list.call_args
        assert f"rfc822msgid:{rfc822_mid}" in list_call[1]["q"]

        # modify で INBOX が removeLabelIds に指定されていること
        modify_call = service.users().messages().modify.call_args
        assert modify_call[1]["id"] == internal_id
        assert "INBOX" in modify_call[1]["body"]["removeLabelIds"]


class TestMailChannelArchiveNotFound:
    """検索結果 0 件で False を返す。"""

    def test_archive_message_not_found(self):
        service = _make_service(
            list_return={"messages": []},
        )

        ch = MailChannel(from_address="office@momijian.co")
        with patch.object(ch, "_get_service", return_value=service):
            ok = ch.archive_message("<nonexistent@mail.gmail.com>")

        assert ok is False
        # modify は呼ばれない
        service.users().messages().modify.assert_not_called()


# ===========================================================================
# DeliveryLogger テスト
# ===========================================================================

class TestDeliveryLoggerService:
    """認証環境に応じた Sheets API サービスを構築する。"""

    def test_get_service_prefers_local_gcp_auth(self):
        service = MagicMock()
        gcp_auth = types.ModuleType("gcp_auth")
        gcp_auth.get_sheets_service = MagicMock(  # type: ignore[attr-defined]
            return_value=service
        )

        with (
            patch.dict(sys.modules, {"gcp_auth": gcp_auth}),
            patch("google.auth.default") as mock_default,
        ):
            actual = DeliveryLogger()._get_service()

        assert actual is service
        gcp_auth.get_sheets_service.assert_called_once_with()  # type: ignore[attr-defined]
        mock_default.assert_not_called()

    def test_get_service_falls_back_to_adc_when_gcp_auth_is_unavailable(self):
        credentials = MagicMock()
        service = MagicMock()
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]

        with (
            patch.dict(sys.modules, {"gcp_auth": None}),
            patch(
                "google.auth.default",
                return_value=(credentials, "test-project"),
            ) as mock_default,
            patch(
                "googleapiclient.discovery.build",
                return_value=service,
            ) as mock_build,
        ):
            actual = DeliveryLogger()._get_service()

        assert actual is service
        mock_default.assert_called_once_with(scopes=scopes)
        mock_build.assert_called_once_with(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )


class TestBounceMonitorService:
    """認証環境に応じた Sheets API サービスを構築する。"""

    def test_get_sheets_service_falls_back_to_adc_when_gcp_auth_is_unavailable(self):
        credentials = MagicMock()
        service = MagicMock()
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]

        with (
            patch.dict(sys.modules, {"gcp_auth": None}),
            patch(
                "google.auth.default",
                return_value=(credentials, "test-project"),
            ) as mock_default,
            patch(
                "googleapiclient.discovery.build",
                return_value=service,
            ) as mock_build,
        ):
            actual = BounceMonitor(sheet_id="test-sheet")._get_sheets_service()

        assert actual is service
        mock_default.assert_called_once_with(scopes=scopes)
        mock_build.assert_called_once_with(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )

    def test_get_sheets_service_falls_back_to_adc_when_sa_key_is_missing(self):
        credentials = MagicMock()
        service = MagicMock()
        gcp_auth = types.ModuleType("gcp_auth")
        gcp_auth.get_sheets_service = MagicMock(  # type: ignore[attr-defined]
            side_effect=FileNotFoundError("local-dev-sa.json")
        )

        with (
            patch.dict(sys.modules, {"gcp_auth": gcp_auth}),
            patch(
                "google.auth.default",
                return_value=(credentials, "test-project"),
            ) as mock_default,
            patch(
                "googleapiclient.discovery.build",
                return_value=service,
            ),
        ):
            actual = BounceMonitor(sheet_id="test-sheet")._get_sheets_service()

        assert actual is service
        mock_default.assert_called_once_with(
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )


def _make_result(
    success: bool = True,
    message_id: str | None = "<test@mail.gmail.com>",
    channel: ChannelType = ChannelType.MAIL,
    recipient: str = "dest@example.com",
    error: str | None = None,
    sent_at: datetime | None = None,
) -> DeliveryResult:
    """テスト用 DeliveryResult を生成するファクトリ。"""
    return DeliveryResult(
        success=success,
        message_id=message_id,
        channel=channel,
        recipient=recipient,
        error=error,
        sent_at=sent_at or datetime(2026, 5, 23, 10, 0, 0, tzinfo=timezone.utc),
    )


class TestDeliveryLoggerAppendSuccess:
    """送信ログシートに正しい 12列の行が append される。"""

    def test_append_success(self):
        service = _make_sheets_service(
            get_return={"values": []},  # 既存 Message-ID なし
            append_return={},
        )

        dl = DeliveryLogger(sheet_id="dummy_sheet_id", sheet_name="送信ログ")
        with patch.object(dl, "_get_service", return_value=service):
            ok = dl.append(
                result=_make_result(
                    success=True,
                    message_id="<append-test@mail.gmail.com>",
                ),
                office_id="1234567890",
                from_address="office@momijian.co",
                extra={
                    "target_month": "202606",
                    "office_name": "テスト事業所",
                    "subject_or_filename": "teikyohyou_202606.pdf",
                    "sha256": "a" * 64,
                },
            )

        assert ok is True

        # append が呼ばれたこと
        append_call = service.spreadsheets().values().append.call_args
        body = append_call[1]["body"]
        row = body["values"][0]

        # 12列あること
        assert len(row) == 12

        # 主要な列値の検証
        assert row[2] == "1234567890"           # C: 事業所番号
        assert row[3] == "テスト事業所"          # D: 事業所名
        assert row[4] == "mail"                  # E: チャネル
        assert row[5] == "teikyohyou_202606.pdf" # F: 添付ファイル名
        assert row[7] == "dest@example.com"      # H: 宛先
        assert row[8] == "<append-test@mail.gmail.com>"  # I: Message-ID
        assert row[9] == "success"               # J: 結果
        assert row[10] == ""                     # K: エラー詳細（成功時は空）
        assert row[11] == "office@momijian.co"   # L: from_address


class TestDeliveryLoggerDuplicateSkip:
    """同じ Message-ID が既に I 列にあれば append せず False。"""

    def test_append_duplicate_message_id_skip(self):
        existing_mid = "<duplicate@mail.gmail.com>"

        service = _make_sheets_service(
            get_return={"values": [["Message-ID"], [existing_mid]]},
        )

        dl = DeliveryLogger(sheet_id="dummy_sheet_id", sheet_name="送信ログ")
        with patch.object(dl, "_get_service", return_value=service):
            ok = dl.append(
                result=_make_result(message_id=existing_mid),
                office_id="9999999999",
                from_address="office@momijian.co",
            )

        assert ok is False
        # append API は呼ばれない
        service.spreadsheets().values().append.assert_not_called()


class TestDeliveryLoggerEmptyMessageId:
    """Message-ID が空文字 / None なら冪等チェックをスキップして append する。"""

    def test_append_empty_message_id_no_dedup(self):
        service = _make_sheets_service(
            get_return={"values": []},
            append_return={},
        )

        dl = DeliveryLogger(sheet_id="dummy_sheet_id", sheet_name="送信ログ")
        with patch.object(dl, "_get_service", return_value=service):
            ok = dl.append(
                result=_make_result(message_id=None),
                office_id="0000000001",
                from_address="office@momijian.co",
            )

        assert ok is True
        # get (重複チェック) は呼ばれるが append も呼ばれること
        service.spreadsheets().values().append.assert_called_once()

    def test_append_none_message_id_no_dedup(self):
        service = _make_sheets_service(
            get_return={"values": []},
            append_return={},
        )

        dl = DeliveryLogger(sheet_id="dummy_sheet_id", sheet_name="送信ログ")
        with patch.object(dl, "_get_service", return_value=service):
            ok = dl.append(
                result=_make_result(message_id=""),
                office_id="0000000002",
                from_address="office@momijian.co",
            )

        assert ok is True
        service.spreadsheets().values().append.assert_called_once()


class TestDeliveryLoggerFromAddress:
    """L 列 from_address に値が入ること。"""

    def test_append_from_address_recorded(self):
        service = _make_sheets_service(
            get_return={"values": []},
            append_return={},
        )

        dl = DeliveryLogger(sheet_id="dummy_sheet_id", sheet_name="送信ログ")
        with patch.object(dl, "_get_service", return_value=service):
            dl.append(
                result=_make_result(message_id="<from-addr@mail.gmail.com>"),
                office_id="1111111111",
                from_address="hello@momijian.co",
            )

        append_call = service.spreadsheets().values().append.call_args
        row = append_call[1]["body"]["values"][0]
        assert row[11] == "hello@momijian.co"  # L列


class TestDeliveryLoggerErrorMessage:
    """success=False の DeliveryResult で K 列 error_message に入ること。"""

    def test_append_error_message_recorded(self):
        service = _make_sheets_service(
            get_return={"values": []},
            append_return={},
        )

        error_text = "HttpError 503: server error"
        dl = DeliveryLogger(sheet_id="dummy_sheet_id", sheet_name="送信ログ")
        with patch.object(dl, "_get_service", return_value=service):
            ok = dl.append(
                result=_make_result(
                    success=False,
                    message_id=None,
                    error=error_text,
                ),
                office_id="2222222222",
                from_address="office@momijian.co",
            )

        assert ok is True

        append_call = service.spreadsheets().values().append.call_args
        row = append_call[1]["body"]["values"][0]
        assert row[9] == "failed"     # J: 結果
        assert row[10] == error_text  # K: エラー詳細
