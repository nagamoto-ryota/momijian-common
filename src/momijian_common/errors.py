"""エラー分類ユーティリティ (Phase 0)

例外を「人間向けメッセージ + 通知ターゲット + リカバリ可否」に分類する。
Cloud Run アプリ全体で一貫したエラー体験を提供する基盤。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional
from googleapiclient.errors import HttpError

Target = Literal["owner_dm", "day_group", "both"]


@dataclass(frozen=True)
class ErrorReport:
    category: str
    user_message: str
    recovery_hint: str
    recoverable: bool           # True なら自動リトライで復旧可能(=通知不要)
    target: Target
    sentry_fingerprint: tuple[str, ...]


def classify_error(exc: BaseException, context: Optional[dict] = None) -> ErrorReport:
    """例外を人間向けメッセージに分類する。

    context は {"pdf_file_id": str, "template_doc_id": str, ...} 等の
    追加情報を渡す。HttpError 404 が template 関連かフォルダ関連かの判別等に使う。
    """
    ctx = context or {}

    # HttpError 系
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        try:
            status_int = int(status) if status is not None else None
        except Exception:
            status_int = None

        if status_int == 404:
            # template 関連かどうかを context から推定
            template_id = ctx.get("template_doc_id")
            msg = str(exc)
            if template_id and template_id in msg:
                return ErrorReport(
                    category="TEMPLATE_BROKEN",
                    user_message="テンプレートが見つかりません。長本さん対処必要",
                    recovery_hint="TEMPLATE_DOC_ID の Google Docs が削除された可能性",
                    recoverable=False,
                    target="owner_dm",
                    sentry_fingerprint=("TEMPLATE_BROKEN", str(template_id)),
                )
            return ErrorReport(
                category="CONFIG_MISSING",
                user_message="リソースが見つかりません。長本さん対処必要",
                recovery_hint=f"HttpError 404: {exc}",
                recoverable=False,
                target="owner_dm",
                sentry_fingerprint=("HTTP_404", msg[:200]),
            )

        if status_int == 403:
            return ErrorReport(
                category="AUTH_FAIL",
                user_message="認証/権限エラーです。長本さん対処必要",
                recovery_hint=f"HttpError 403: {exc}",
                recoverable=False,
                target="owner_dm",
                sentry_fingerprint=("HTTP_403", str(status_int)),
            )

        if status_int == 429:
            return ErrorReport(
                category="QUOTA_EXCEEDED",
                user_message="一時的な混雑です。時間をおいてファイル入れ直してください",
                recovery_hint=f"Rate limit: {exc}",
                recoverable=True,  # quota回復後は自動復旧可能
                target="day_group",
                sentry_fingerprint=("HTTP_429",),
            )

        if status_int in {500, 502, 503, 504}:
            return ErrorReport(
                category="TIMEOUT",
                user_message="タイムアウトでした。ファイルを入れ直してください",
                recovery_hint=f"HttpError {status_int}: {exc}",
                recoverable=True,
                target="day_group",
                sentry_fingerprint=("HTTP_5XX", str(status_int)),
            )

        return ErrorReport(
            category="UNKNOWN",
            user_message="予期せぬエラー。長本さんに連絡してください",
            recovery_hint=f"HttpError {status_int}: {exc}",
            recoverable=False,
            target="owner_dm",
            sentry_fingerprint=("HTTP_OTHER", str(status_int)),
        )

    # ネットワーク系
    import socket
    import ssl
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ErrorReport(
            category="TIMEOUT",
            user_message="タイムアウトでした。ファイルを入れ直してください",
            recovery_hint="ネットワークタイムアウト",
            recoverable=True,
            target="day_group",
            sentry_fingerprint=("TIMEOUT",),
        )

    if isinstance(exc, (ConnectionError, ConnectionResetError, BrokenPipeError, ssl.SSLError)):
        return ErrorReport(
            category="TIMEOUT",
            user_message="タイムアウトでした。ファイルを入れ直してください",
            recovery_hint=f"接続エラー: {type(exc).__name__}",
            recoverable=True,
            target="day_group",
            sentry_fingerprint=("CONNECTION", type(exc).__name__),
        )

    # エラーメッセージベースの分類
    msg = str(exc).lower()
    if "template" in msg or "placeholder" in msg:
        return ErrorReport(
            category="PLACEHOLDER_CORRUPT",
            user_message="テンプレート破損。長本さん対処必要",
            recovery_hint=str(exc),
            recoverable=False,
            target="owner_dm",
            sentry_fingerprint=("PLACEHOLDER_CORRUPT",),
        )
    if "gemini" in msg or "empty response" in msg or "json" in msg:
        return ErrorReport(
            category="GEMINI_PARSE_FAIL",
            user_message="AI解析失敗。ファイル入れ直してください",
            recovery_hint=str(exc),
            recoverable=False,  # Gemini応答破損は再試行でも直らないことが多い
            target="day_group",
            sentry_fingerprint=("GEMINI_PARSE_FAIL",),
        )
    if "utilizador" in msg or "利用者" in msg or "master" in msg or "sheet" in msg:
        return ErrorReport(
            category="MASTER_DB_INVALID",
            user_message="マスタDBに異常があります。長本さん対処必要",
            recovery_hint=str(exc),
            recoverable=False,
            target="owner_dm",
            sentry_fingerprint=("MASTER_DB_INVALID",),
        )

    # デフォルト
    return ErrorReport(
        category="UNKNOWN",
        user_message="予期せぬエラー。長本さんに連絡してください",
        recovery_hint=f"{type(exc).__name__}: {exc}",
        recoverable=False,
        target="owner_dm",
        sentry_fingerprint=("UNKNOWN", type(exc).__name__),
    )
