"""Gmail OAuth2 経由のメール配信チャネル実装。

DeliveryChannel の具象実装。送信 + INBOX アーカイブを提供する。
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path
from typing import Optional

from momijian_common.delivery.channel import (
    ChannelType,
    DeliveryChannel,
    DeliveryResult,
)


# 5xx / 429 でリトライする HTTP ステータスコード
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# リトライ間隔（秒）。 1s → 2s → 4s の3回
_RETRY_DELAYS = (1, 2, 4)


def _encode_subject(subject: str) -> str:
    """件名を RFC 2047 UTF-8 base64 エンコードする。"""
    return str(Header(subject, "utf-8"))


def _build_message(
    from_address: str,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[Path] | None,
) -> bytes:
    """MIME メッセージを組み立て raw bytes を返す。添付ありは multipart/mixed。"""
    if attachments:
        msg: MIMEMultipart | MIMEText = MIMEMultipart("mixed")
        assert isinstance(msg, MIMEMultipart)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for path in attachments:
            # バイナリ添付（PDF 想定）。octet-stream で safe 側
            data = path.read_bytes()
            part = MIMEApplication(data, _subtype="octet-stream")
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", path.name),
            )
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["From"] = from_address
    msg["To"] = recipient
    msg["Subject"] = _encode_subject(subject)

    return msg.as_bytes()


def _raw_encode(raw_bytes: bytes) -> str:
    """Gmail API が要求する URL-safe base64url エンコード。"""
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")


def _extract_message_id_header(service: object, gmail_internal_id: str) -> Optional[str]:
    """Gmail 内部 ID から RFC 822 Message-ID ヘッダ値を逆引きする。

    DeliveryLogger が冪等キーとして使う <xxx@mail.gmail.com> 形式の値。
    """
    try:
        result = (
            service.users()  # type: ignore[attr-defined]
            .messages()
            .get(
                userId="me",
                id=gmail_internal_id,
                format="metadata",
                metadataHeaders=["Message-ID"],
            )
            .execute()
        )
        headers = result.get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == "message-id":
                return h["value"]
        return None
    except Exception:
        return None


class MailChannel(DeliveryChannel):
    """Gmail OAuth2 を使ったメール配信チャネル。

    send() でメール送信、archive_message() で INBOX ラベルを除去する。
    """

    def __init__(
        self,
        from_address: str,
        scopes: list[str] | None = None,
    ) -> None:
        """初期化。

        Args:
            from_address: 送信元メールアドレス（例: office@momijian.co）。
            scopes: Gmail OAuth スコープ。省略時は gmail.modify（送信+アーカイブ両対応）。
        """
        self._from_address = from_address
        self._scopes = tuple(
            scopes or ["https://www.googleapis.com/auth/gmail.modify"]
        )
        # _service は遅延初期化（初回 send/archive 時に取得）
        self._service: object | None = None

    def _get_service(self) -> object:
        """Gmail service を返す。lru_cache により同スコープは1回だけ取得。"""
        if self._service is None:
            from momijian_common.auth.gmail_oauth import get_gmail_service

            # get_gmail_service は tuple[str, ...] をキャッシュキーに使うので tuple で渡す
            self._service = get_gmail_service(scopes=self._scopes)
        return self._service

    @property
    def channel_type(self) -> ChannelType:
        """MAIL チャネル種別を返す。"""
        return ChannelType.MAIL

    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
    ) -> DeliveryResult:
        """メールを送信して結果を返す。例外は再 raise しない。

        5xx / 429 は exponential backoff で最大3回リトライする（1s, 2s, 4s）。

        Args:
            recipient: 宛先メールアドレス。
            subject: 件名。UTF-8 で自動エンコードされる。
            body: 本文テキスト。
            attachments: 添付ファイルの Path リスト。PDF 想定。

        Returns:
            DeliveryResult: 成功時は success=True, message_id=<Message-ID ヘッダ値>。
                失敗時は success=False, error=エラーメッセージ, message_id=None。
        """
        sent_at = datetime.now(timezone.utc)

        try:
            raw_bytes = _build_message(
                self._from_address, recipient, subject, body, attachments
            )
        except Exception as e:
            return DeliveryResult(
                success=False,
                message_id=None,
                channel=ChannelType.MAIL,
                recipient=recipient,
                error=f"MIME 構築エラー: {e}",
                sent_at=sent_at,
            )

        raw_encoded = _raw_encode(raw_bytes)
        service = self._get_service()

        last_error: str = ""
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=0):  # type: ignore[misc]
            try:
                response = (
                    service.users()  # type: ignore[attr-defined]
                    .messages()
                    .send(userId="me", body={"raw": raw_encoded})
                    .execute()
                )
                gmail_internal_id: str = response["id"]
                message_id_header = _extract_message_id_header(
                    service, gmail_internal_id
                )
                # Message-ID ヘッダが取得できなかった場合は内部 ID にフォールバック
                effective_id = message_id_header or gmail_internal_id

                return DeliveryResult(
                    success=True,
                    message_id=effective_id,
                    channel=ChannelType.MAIL,
                    recipient=recipient,
                    error=None,
                    sent_at=sent_at,
                )

            except Exception as exc:
                # google.api_core.exceptions.GoogleAPICallError / HttpError を汎用 except で捕捉
                # HttpError は status_code 属性を持つ
                status_code: int | None = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "resp", None), "status", None
                )

                # 4xx（429 除く）は即時失敗
                if (
                    status_code is not None
                    and status_code not in _RETRYABLE_STATUS
                ):
                    last_error = str(exc)
                    break

                last_error = str(exc)

                # まだリトライ余地がある場合は sleep
                if delay is not None:
                    time.sleep(delay)  # type: ignore[arg-type]
                # delay が None = 最後のリトライ後 → ループ終了

        return DeliveryResult(
            success=False,
            message_id=None,
            channel=ChannelType.MAIL,
            recipient=recipient,
            error=last_error,
            sent_at=sent_at,
        )

    def archive_message(self, message_id: str) -> bool:
        """INBOX ラベルを除去してメッセージをアーカイブする。

        Args:
            message_id: DeliveryResult.message_id から得た RFC 822 形式の Message-ID
                ヘッダ値（例: <xxx@mail.gmail.com>）。Gmail 内部 ID ではない。

        Returns:
            True: 成功。False: メッセージ不在またはエラー（例外は再 raise しない）。
        """
        try:
            service = self._get_service()

            # Message-ID ヘッダ値 → Gmail 内部 ID を逆引き
            list_response = (
                service.users()  # type: ignore[attr-defined]
                .messages()
                .list(userId="me", q=f"rfc822msgid:{message_id}")
                .execute()
            )
            messages = list_response.get("messages", [])
            if not messages:
                return False

            gmail_internal_id: str = messages[0]["id"]

            # INBOX ラベルを除去
            service.users().messages().modify(  # type: ignore[attr-defined]
                userId="me",
                id=gmail_internal_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()

            return True

        except Exception:
            return False
