"""MailChannel DWD impersonation のユニットテスト。

設計書 §5.1 の 3 ケースを検証する:
- test_send_with_office_address_no_impersonation
- test_send_with_impersonation_calls_dwd_service
- test_send_dwd_failure_propagates_to_delivery_result

外部 API（Gmail / IAM）は一切呼ばない。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from momijian_common.delivery.mail import MailChannel
from momijian_common.delivery.channel import ChannelType


# ---------------------------------------------------------------------------
# ヘルパー: Gmail service モック生成
# ---------------------------------------------------------------------------

def _make_service(
    send_return: dict | None = None,
    get_return: dict | None = None,
) -> MagicMock:
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    if send_return is not None:
        messages.send.return_value.execute.return_value = send_return
    if get_return is not None:
        messages.get.return_value.execute.return_value = get_return
    return service


# ---------------------------------------------------------------------------
# テスト 1: impersonate_user=None → 既存 OAuth 経路
# ---------------------------------------------------------------------------

class TestSendWithOfficeAddressNoImpersonation:
    """impersonate_user=None (既定) では get_gmail_service が呼ばれ、
    get_dwd_gmail_service は呼ばれないこと。"""

    def test_send_with_office_address_no_impersonation(self):
        mock_service = _make_service(
            send_return={"id": "internal001"},
            get_return={
                "payload": {
                    "headers": [{"name": "Message-ID", "value": "<office@mail.gmail.com>"}]
                }
            },
        )

        ch = MailChannel(from_address="office@momijian.co")
        # impersonate_user は None（既定）

        with patch("momijian_common.auth.gmail_oauth.get_gmail_service", return_value=mock_service) as mock_oauth, \
             patch("momijian_common.auth.gmail_oauth.get_dwd_gmail_service") as mock_dwd:
            result = ch.send(
                recipient="dest@example.com",
                subject="テスト",
                body="本文",
            )

        assert result.success is True
        assert result.channel == ChannelType.MAIL
        # get_dwd_gmail_service は呼ばれていない
        mock_dwd.assert_not_called()


# ---------------------------------------------------------------------------
# テスト 2: impersonate_user 指定 → get_dwd_gmail_service が呼ばれる
# ---------------------------------------------------------------------------

class TestSendWithImpersonationCallsDwdService:
    """impersonate_user='takada@momijian.co' を指定すると
    get_dwd_gmail_service(impersonate_email='takada@momijian.co', ...) が呼ばれること。"""

    def test_send_with_impersonation_calls_dwd_service(self):
        mock_service = _make_service(
            send_return={"id": "dwd_internal002"},
            get_return={
                "payload": {
                    "headers": [
                        {"name": "Message-ID", "value": "<dwd@mail.gmail.com>"}
                    ]
                }
            },
        )

        impersonate_email = "takada@momijian.co"
        ch = MailChannel(
            from_address=impersonate_email,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            impersonate_user=impersonate_email,
        )

        with patch(
            "momijian_common.auth.gmail_oauth.get_dwd_gmail_service",
            return_value=mock_service,
        ) as mock_dwd, \
             patch("momijian_common.auth.gmail_oauth.get_gmail_service") as mock_oauth:
            result = ch.send(
                recipient="cm@example.com",
                subject="ケアプラン送付",
                body="よろしくお願いします",
            )

        assert result.success is True
        assert result.message_id == "<dwd@mail.gmail.com>"
        # get_dwd_gmail_service が impersonate_email を引数に呼ばれたこと
        mock_dwd.assert_called_once_with(
            impersonate_email=impersonate_email,
            scopes=("https://www.googleapis.com/auth/gmail.send",),
        )
        # 通常の OAuth 経路は呼ばれていない
        mock_oauth.assert_not_called()


# ---------------------------------------------------------------------------
# テスト 3: DWD 失敗 → DeliveryResult.success=False に伝播する
# ---------------------------------------------------------------------------

class TestSendDwdFailurePropagatesToDeliveryResult:
    """get_dwd_gmail_service が例外（RefreshError 等）を raise した場合、
    MailChannel.send() は例外を再 raise せず DeliveryResult.success=False を返すこと。"""

    def test_send_dwd_failure_propagates_to_delivery_result(self):
        impersonate_email = "invalid@momijian.co"
        ch = MailChannel(
            from_address=impersonate_email,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            impersonate_user=impersonate_email,
        )

        # _get_service が DWD 失敗で例外を投げるようにする
        # （実際には get_dwd_gmail_service 内の IAM/OAuth で発生する RefreshError 等）
        dwd_error = Exception("RefreshError: DWD scope not configured")

        with patch.object(ch, "_get_service", side_effect=dwd_error):
            result = ch.send(
                recipient="dest@example.com",
                subject="テスト",
                body="本文",
            )

        # 例外が外に出ず DeliveryResult として返ること（mail.py が _get_service 例外を捕捉）
        assert result.success is False
        assert result.message_id is None
        assert result.channel == ChannelType.MAIL
        # エラーメッセージに元の例外情報が含まれること
        assert result.error is not None
        assert "DWD scope not configured" in result.error or "サービス取得" in result.error
