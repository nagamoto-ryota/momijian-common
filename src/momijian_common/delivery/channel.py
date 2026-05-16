"""配信チャネル抽象基底クラスと関連型定義。

DeliveryChannel: メール / FAX / ケアプランCSV / LINEWORKS 等の送信チャネル共通インターフェース。
具体実装は各チャネル担当者が別ファイルに作成する:
  - mail.py    — MailChannel (Gmail OAuth2) ← T4 担当
  - bounce.py  — BounceMonitor              ← T20 担当
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class ChannelType(Enum):
    """配信チャネルの種別。"""

    MAIL = "mail"
    FAX = "fax"
    CARE_PLAN_CSV = "care_plan_csv"
    LINEWORKS = "lineworks"


@dataclass(frozen=True)
class DeliveryResult:
    """送信1件の結果を表す不変データクラス。

    Attributes:
        success: 送信成功なら True。
        message_id: 成功時の外部 ID（Gmail なら Message-ID ヘッダ値）。失敗時は None。
        channel: 使用した配信チャネル種別。
        recipient: 送信先（メールアドレス / FAX番号 等）。
        error: 失敗時のエラーメッセージ。成功時は None。
        sent_at: 送信試行日時（UTC）。
    """

    success: bool
    message_id: Optional[str]
    channel: ChannelType
    recipient: str
    error: Optional[str]
    sent_at: datetime


class DeliveryChannel(ABC):
    """配信チャネルの抽象基底クラス。

    サブクラスは channel_type プロパティと send メソッドを実装する。
    """

    @property
    @abstractmethod
    def channel_type(self) -> ChannelType:
        """このチャネルの種別を返す。"""

    @abstractmethod
    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
    ) -> DeliveryResult:
        """メッセージを送信して結果を返す。

        Args:
            recipient: 送信先（メールアドレス / FAX番号 等）。
            subject: 件名（FAX等では空文字列可）。
            body: 本文テキスト。
            attachments: 添付ファイルのパスリスト。省略時は添付なし。

        Returns:
            DeliveryResult: 送信結果。成功・失敗いずれの場合も返す（例外は投げない）。
        """
