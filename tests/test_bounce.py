"""BounceMonitor の pytest テスト群。

外部 API（Gmail / Sheets）は一切呼ばない。すべて mock で検証する。

テスト一覧:
  test_extract_original_message_id_from_in_reply_to    — In-Reply-To ヘッダから抽出
  test_extract_original_message_id_from_x_failed_recipients — X-Failed-Recipients から宛先抽出
  test_scan_finds_bounce                                 — mock Gmail でバウンス1件検出
  test_scan_no_bounce                                    — バウンス無しで空リスト
  test_scan_skip_already_recorded                        — 既に K列に "BOUNCE:" あればスキップ
  test_scan_label_added                                  — バウンスメールにラベル付与される
  test_scan_unknown_original_id                          — original_message_id 取れない場合
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from momijian_common.delivery.bounce import (
    BounceMonitor,
    BounceResult,
    _decode_body,
    _extract_bounce_reason,
    _extract_original_message_id,
    _extract_original_message_id_from_body,
    _extract_recipient_from_headers,
)


# ---------------------------------------------------------------------------
# ヘルパー: ヘッダリスト生成
# ---------------------------------------------------------------------------

def _headers(*pairs: tuple[str, str]) -> list[dict]:
    """ヘッダ名・値ペアのタプルから Gmail API 形式のヘッダリストを生成する。"""
    return [{"name": name, "value": value} for name, value in pairs]


# ---------------------------------------------------------------------------
# ヘルパー: Gmail service モック
# ---------------------------------------------------------------------------

def _make_gmail_service(
    list_return: dict | None = None,
    get_return: dict | None = None,
    labels_list_return: dict | None = None,
    labels_create_return: dict | None = None,
    modify_return: dict | None = None,
) -> MagicMock:
    """Gmail API service モックを生成する。

    以下のパスを mock する:
      service.users().messages().list().execute()   -> list_return
      service.users().messages().get().execute()    -> get_return
      service.users().labels().list().execute()     -> labels_list_return
      service.users().labels().create().execute()   -> labels_create_return
      service.users().messages().modify().execute() -> modify_return
    """
    service = MagicMock()
    users = service.users.return_value

    # messages
    messages = users.messages.return_value
    messages.list.return_value.execute.return_value = list_return or {"messages": []}
    messages.get.return_value.execute.return_value = get_return or {}
    messages.modify.return_value.execute.return_value = modify_return or {}

    # labels
    labels = users.labels.return_value
    labels.list.return_value.execute.return_value = labels_list_return or {"labels": []}
    labels.create.return_value.execute.return_value = labels_create_return or {
        "id": "Label_bounce_001",
        "name": "teikyohyou/bounce",
    }

    return service


def _make_sheets_service(
    get_return: dict | None = None,
    update_return: dict | None = None,
) -> MagicMock:
    """Sheets API service モックを生成する。"""
    service = MagicMock()
    values = service.spreadsheets.return_value.values.return_value
    values.get.return_value.execute.return_value = get_return or {"values": []}
    values.update.return_value.execute.return_value = update_return or {}
    return service


# ---------------------------------------------------------------------------
# _extract_original_message_id のテスト
# ---------------------------------------------------------------------------

class TestExtractOriginalMessageIdFromInReplyTo:
    """In-Reply-To ヘッダから元 Message-ID を抽出できること。"""

    def test_in_reply_to_angle_bracket(self):
        """<xxx@yyy> 形式の In-Reply-To から正確に抽出する。"""
        headers = _headers(
            ("In-Reply-To", "<original-mail-123@mail.gmail.com>"),
            ("Subject", "Delivery Status Notification (Failure)"),
        )
        result = _extract_original_message_id(headers)
        assert result == "<original-mail-123@mail.gmail.com>"

    def test_in_reply_to_no_angle_bracket(self):
        """angle bracket なしの In-Reply-To もそのまま返す。"""
        headers = _headers(
            ("In-Reply-To", "plain-id@example.com"),
        )
        result = _extract_original_message_id(headers)
        assert result == "plain-id@example.com"

    def test_references_fallback(self):
        """In-Reply-To がない場合 References の最初のエントリを返す。"""
        headers = _headers(
            ("References", "<ref-001@example.com> <ref-002@example.com>"),
        )
        result = _extract_original_message_id(headers)
        assert result == "<ref-001@example.com>"

    def test_no_headers_returns_none(self):
        """In-Reply-To も References もない場合 None を返す。"""
        headers = _headers(
            ("Subject", "Undeliverable: test"),
            ("From", "mailer-daemon@example.com"),
        )
        result = _extract_original_message_id(headers)
        assert result is None

    def test_case_insensitive_header_name(self):
        """ヘッダ名の大文字小文字を区別しないこと。"""
        headers = _headers(
            ("in-reply-to", "<lower-case@example.com>"),
        )
        result = _extract_original_message_id(headers)
        assert result == "<lower-case@example.com>"


# ---------------------------------------------------------------------------
# _extract_recipient_from_headers のテスト (X-Failed-Recipients)
# ---------------------------------------------------------------------------

class TestExtractOriginalMessageIdFromXFailedRecipients:
    """X-Failed-Recipients ヘッダからバウンス宛先メアドを抽出できること。"""

    def test_x_failed_recipients_single(self):
        """単一の X-Failed-Recipients を正確に抽出する。"""
        headers = _headers(
            ("X-Failed-Recipients", "failed@example.com"),
        )
        result = _extract_recipient_from_headers(headers)
        assert result == "failed@example.com"

    def test_x_failed_recipients_multiple(self):
        """カンマ区切りの複数宛先は最初の1件を返す。"""
        headers = _headers(
            ("X-Failed-Recipients", "first@example.com, second@example.com"),
        )
        result = _extract_recipient_from_headers(headers)
        assert result == "first@example.com"

    def test_to_fallback_angle_bracket(self):
        """X-Failed-Recipients がなければ To ヘッダにフォールバック（<email> 形式）。"""
        headers = _headers(
            ("To", "Some Name <fallback@example.com>"),
        )
        result = _extract_recipient_from_headers(headers)
        assert result == "fallback@example.com"

    def test_no_recipient_headers_returns_none(self):
        """宛先ヘッダが一切なければ None を返す。"""
        headers = _headers(
            ("Subject", "Delivery failure"),
        )
        result = _extract_recipient_from_headers(headers)
        assert result is None


# ---------------------------------------------------------------------------
# _extract_original_message_id_from_body のテスト
# ---------------------------------------------------------------------------

class TestExtractOriginalMessageIdFromBody:
    """本文テキストから Message-ID を抽出できること。"""

    def test_extracts_from_body_text(self):
        body = (
            "This message was created automatically by mail delivery software.\n"
            "Message-ID: <body-extracted@mail.gmail.com>\n"
            "From: office@momijian.co\n"
        )
        result = _extract_original_message_id_from_body(body)
        assert result == "<body-extracted@mail.gmail.com>"

    def test_case_insensitive(self):
        body = "message-id: <lower@example.com>\n"
        result = _extract_original_message_id_from_body(body)
        assert result == "<lower@example.com>"

    def test_no_message_id_in_body(self):
        body = "Delivery failed. Please check the address."
        result = _extract_original_message_id_from_body(body)
        assert result is None


# ---------------------------------------------------------------------------
# BounceMonitor.scan のテスト
# ---------------------------------------------------------------------------

def _make_bounce_message(
    gmail_id: str = "bounce_gmail_id_001",
    in_reply_to: str = "<original-001@mail.gmail.com>",
    subject: str = "Mail Delivery Subsystem: Delivery Status Notification (Failure)",
    x_failed_recipients: str = "target@example.com",
    snippet: str = "Delivery to the following recipient failed permanently",
    body_text: str = "",
) -> dict:
    """テスト用バウンスメール詳細オブジェクトを生成する。"""
    hdrs = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": "mailer-daemon@googlemail.com"},
        {"name": "In-Reply-To", "value": in_reply_to},
        {"name": "X-Failed-Recipients", "value": x_failed_recipients},
    ]

    # body_text がある場合は base64 エンコードして payload に埋める
    if body_text:
        encoded = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii")
        payload: dict = {
            "mimeType": "text/plain",
            "headers": hdrs,
            "body": {"data": encoded},
        }
    else:
        payload = {
            "mimeType": "text/plain",
            "headers": hdrs,
            "body": {},
        }

    return {
        "id": gmail_id,
        "snippet": snippet,
        "payload": payload,
    }


def _make_log_rows(
    message_ids: list[str],
    recipients: list[str] | None = None,
    k_col_values: list[str] | None = None,
) -> list[list[str]]:
    """送信ログシートの行データを生成する（ヘッダ行 + データ行）。

    列順は DeliveryLogger の _COL に従う (A〜L = index 0〜11):
        index 7  = H: recipient
        index 8  = I: message_id
        index 10 = K: error_detail
    """
    recipients = recipients or [""] * len(message_ids)
    k_col_values = k_col_values or [""] * len(message_ids)

    header_row = [
        "timestamp", "target_month", "office_id", "office_name",
        "channel", "filename", "sha256", "recipient",
        "message_id", "result", "error_detail", "from_address",
    ]
    data_rows = []
    for mid, rcpt, k_val in zip(message_ids, recipients, k_col_values):
        row = [""] * 12
        row[7] = rcpt
        row[8] = mid
        row[9] = "success"
        row[10] = k_val
        row[11] = "office@momijian.co"
        data_rows.append(row)

    return [header_row] + data_rows


class TestScanFindsBounce:
    """mock Gmail でバウンスメールを1件検出し BounceResult を返すこと。"""

    def test_scan_finds_bounce(self):
        original_mid = "<original-001@mail.gmail.com>"
        bounce_gmail_id = "bounce_gmail_id_001"
        recipient = "target@example.com"
        label_id = "Label_bounce_001"

        gmail = _make_gmail_service(
            list_return={"messages": [{"id": bounce_gmail_id}]},
            get_return=_make_bounce_message(
                gmail_id=bounce_gmail_id,
                in_reply_to=original_mid,
                x_failed_recipients=recipient,
            ),
            labels_list_return={"labels": [{"id": label_id, "name": "teikyohyou/bounce"}]},
        )
        sheets = _make_sheets_service(
            get_return={"values": _make_log_rows([original_mid], [recipient])},
        )

        monitor = BounceMonitor(sheet_id="dummy_sheet_id")
        with (
            patch.object(monitor, "_get_gmail_service", return_value=gmail),
            patch.object(monitor, "_get_sheets_service", return_value=sheets),
        ):
            results = monitor.scan(
                since=datetime(2026, 5, 23, tzinfo=timezone.utc),
            )

        assert len(results) == 1
        r = results[0]
        assert r.bounce_message_id == bounce_gmail_id
        assert r.original_message_id == original_mid
        assert r.recipient == recipient
        assert r.sent_log_updated is True
        assert len(r.raw_snippet) <= 200


class TestScanNoBounce:
    """バウンスメールが0件のとき空リストを返すこと。"""

    def test_scan_no_bounce(self):
        gmail = _make_gmail_service(
            list_return={"messages": []},
        )
        sheets = _make_sheets_service(
            get_return={"values": _make_log_rows(["<some-id@example.com>"])},
        )

        monitor = BounceMonitor(sheet_id="dummy_sheet_id")
        with (
            patch.object(monitor, "_get_gmail_service", return_value=gmail),
            patch.object(monitor, "_get_sheets_service", return_value=sheets),
        ):
            results = monitor.scan(
                since=datetime(2026, 5, 23, tzinfo=timezone.utc),
            )

        assert results == []


class TestScanSkipAlreadyRecorded:
    """K列に既に "BOUNCE:" で始まる値があれば K列更新しないこと。"""

    def test_scan_skip_already_recorded(self):
        original_mid = "<already-bounced@mail.gmail.com>"
        bounce_gmail_id = "bounce_skip_001"
        label_id = "Label_bounce_001"

        gmail = _make_gmail_service(
            list_return={"messages": [{"id": bounce_gmail_id}]},
            get_return=_make_bounce_message(
                gmail_id=bounce_gmail_id,
                in_reply_to=original_mid,
            ),
            labels_list_return={"labels": [{"id": label_id, "name": "teikyohyou/bounce"}]},
        )
        # K列 (index 10) に既に "BOUNCE:" が入っている
        log_rows = _make_log_rows(
            [original_mid],
            k_col_values=["BOUNCE: Mail delivery failed (previous run)"],
        )
        sheets = _make_sheets_service(get_return={"values": log_rows})

        monitor = BounceMonitor(sheet_id="dummy_sheet_id")
        with (
            patch.object(monitor, "_get_gmail_service", return_value=gmail),
            patch.object(monitor, "_get_sheets_service", return_value=sheets),
        ):
            results = monitor.scan(
                since=datetime(2026, 5, 23, tzinfo=timezone.utc),
            )

        # Sheets の update は呼ばれないこと
        sheets.spreadsheets().values().update.assert_not_called()

        # BounceResult は返る (sent_log_updated=True で既記録扱い)
        assert len(results) == 1
        assert results[0].sent_log_updated is True


class TestScanLabelAdded:
    """バウンスメールに teikyohyou/bounce ラベルが付与されること。"""

    def test_scan_label_added(self):
        original_mid = "<label-test@mail.gmail.com>"
        bounce_gmail_id = "bounce_label_001"
        label_id = "Label_bounce_test"

        gmail = _make_gmail_service(
            list_return={"messages": [{"id": bounce_gmail_id}]},
            get_return=_make_bounce_message(
                gmail_id=bounce_gmail_id,
                in_reply_to=original_mid,
            ),
            labels_list_return={"labels": [{"id": label_id, "name": "teikyohyou/bounce"}]},
        )
        sheets = _make_sheets_service(
            get_return={"values": _make_log_rows([original_mid])},
        )

        monitor = BounceMonitor(sheet_id="dummy_sheet_id")
        with (
            patch.object(monitor, "_get_gmail_service", return_value=gmail),
            patch.object(monitor, "_get_sheets_service", return_value=sheets),
        ):
            results = monitor.scan(
                since=datetime(2026, 5, 23, tzinfo=timezone.utc),
            )

        # messages.modify が addLabelIds=[label_id] で呼ばれること
        modify_call = gmail.users().messages().modify.call_args
        assert modify_call is not None
        body = modify_call[1]["body"]
        assert label_id in body["addLabelIds"]

        assert len(results) == 1


class TestScanUnknownOriginalId:
    """original_message_id が取れない場合、sent_log_updated=False になること。"""

    def test_scan_unknown_original_id(self):
        bounce_gmail_id = "bounce_no_mid_001"
        label_id = "Label_bounce_001"

        # In-Reply-To も References も空 + 本文も空
        msg_detail = _make_bounce_message(
            gmail_id=bounce_gmail_id,
            in_reply_to="",  # 空にする
            x_failed_recipients="nobody@example.com",
        )
        # In-Reply-To ヘッダを除去
        msg_detail["payload"]["headers"] = [
            h for h in msg_detail["payload"]["headers"]
            if h["name"] not in ("In-Reply-To",)
        ]

        gmail = _make_gmail_service(
            list_return={"messages": [{"id": bounce_gmail_id}]},
            get_return=msg_detail,
            labels_list_return={"labels": [{"id": label_id, "name": "teikyohyou/bounce"}]},
        )
        sheets = _make_sheets_service(
            get_return={"values": _make_log_rows(["<some-other-id@example.com>"])},
        )

        monitor = BounceMonitor(sheet_id="dummy_sheet_id")
        with (
            patch.object(monitor, "_get_gmail_service", return_value=gmail),
            patch.object(monitor, "_get_sheets_service", return_value=sheets),
        ):
            results = monitor.scan(
                since=datetime(2026, 5, 23, tzinfo=timezone.utc),
            )

        assert len(results) == 1
        r = results[0]
        assert r.original_message_id is None
        assert r.sent_log_updated is False
