"""momijian_common.delivery — 配信チャネル共通インターフェース。

公開 API:
    DeliveryChannel  — 抽象基底クラス
    ChannelType      — 配信チャネル種別 enum
    DeliveryResult   — 送信結果 dataclass（frozen）
    MailChannel      — Gmail OAuth2 メール配信チャネル
    DeliveryLogger   — 送信ログを Sheets「送信ログ」に追記
    BounceMonitor    — Gmail バウンスメール検出・送信ログ書き戻し（手動起動専用）
    BounceResult     — バウンス検出1件の結果 dataclass（frozen）
"""

from __future__ import annotations

from momijian_common.delivery.channel import (
    ChannelType,
    DeliveryChannel,
    DeliveryResult,
)
from momijian_common.delivery.mail import MailChannel
from momijian_common.delivery.logger import DeliveryLogger
from momijian_common.delivery.bounce import BounceMonitor, BounceResult

__all__ = [
    "ChannelType",
    "DeliveryChannel",
    "DeliveryResult",
    "MailChannel",
    "DeliveryLogger",
    "BounceMonitor",
    "BounceResult",
]
