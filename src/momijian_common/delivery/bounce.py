"""Gmail バウンスメール検出・送信ログ書き戻しモジュール。

手動起動専用（自動 cron なし）。5/23 一斉送信後に 24h/72h/翌月初の3回手動起動する想定。

使い方（CLI）:
    python -m momijian_common.delivery.bounce \\
        --since 2026-05-23T00:00:00Z \\
        --sheet-id 1qyhqvY9VCHFCqqCFrLztTLr2IDGzVOoVLq9X6e54ugE

依存:
    - momijian_common.delivery.mail.MailChannel  (Gmail service 取得に同じ OAuth 使用)
    - momijian_common.delivery.logger.DeliveryLogger  (送信ログ照合・書き戻し)

スコープ外（やらない）:
    - Cloud Run Job 化 (将来)
    - Cloud Scheduler 自動起動（6月以降）
    - メール本文の Gemini AI 解析（regex で十分）
"""

from __future__ import annotations

import argparse
import base64
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# 定数
# -------------------------------------------------------------------------

# バウンスメール送信者パターン（Gmail q= クエリ用）
_BOUNCE_SENDER_QUERY = (
    "from:(mailer-daemon OR postmaster OR "
    '"Mail Delivery Subsystem" OR '
    '"Mail Delivery System" OR '
    "noreply@google.com)"
)

# ラベル名（デフォルト）
_DEFAULT_BOUNCE_LABEL = "teikyohyou/bounce"

# 送信ログシート K列インデックス (0始まり)
_ERROR_DETAIL_COL_INDEX = 10  # K列

# 送信ログ全列取得範囲
_LOG_RANGE = "送信ログ!A:L"

# Message-ID 列 (I列, index=8)
_MESSAGE_ID_COL_INDEX = 8

# 宛先列 (H列, index=7)
_RECIPIENT_COL_INDEX = 7

# K列 BOUNCE プレフィックス
_BOUNCE_PREFIX = "BOUNCE:"

# raw_snippet 最大長
_SNIPPET_MAX_LEN = 200

# -------------------------------------------------------------------------
# BounceResult dataclass
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class BounceResult:
    """バウンス検出1件の結果。

    Attributes:
        bounce_message_id: バウンスメール自体の Message-ID（Gmail 内部 ID）。
        original_message_id: 元の送信 Message-ID。取得できなければ None。
        recipient: バウンス元の受信者アドレス。取得できなければ None。
        reason: バウンス理由（X-Failed-Recipients・エラーコード等から抽出）。
        sent_log_updated: 送信ログシートに書き戻せたか。
        raw_snippet: デバッグ用、バウンスメール snippet の最初の 200 文字。
    """

    bounce_message_id: str
    original_message_id: Optional[str]
    recipient: Optional[str]
    reason: str
    sent_log_updated: bool
    raw_snippet: str


# -------------------------------------------------------------------------
# ヘルパー関数（テスト可能な純粋関数として切り出し）
# -------------------------------------------------------------------------

def _extract_original_message_id(headers: list[dict]) -> Optional[str]:
    """メールヘッダから元の Message-ID を抽出する。

    フォールバック順:
        1. In-Reply-To ヘッダ
        2. References ヘッダ（最初のエントリ）

    Args:
        headers: Gmail API の payload.headers リスト。
                 各要素は {"name": str, "value": str} 形式。

    Returns:
        Message-ID 文字列（<...> 形式）、見つからなければ None。
    """
    header_map: dict[str, str] = {}
    for h in headers:
        name = h.get("name", "").lower()
        value = h.get("value", "")
        if value:
            header_map[name] = value

    # 1. In-Reply-To ヘッダ（最優先）
    in_reply_to = header_map.get("in-reply-to", "").strip()
    if in_reply_to:
        # <xxx@yyy> 形式を抽出
        match = re.search(r"<[^>]+>", in_reply_to)
        if match:
            return match.group(0)
        # angle bracket なしでもそのまま返す
        return in_reply_to

    # 2. References ヘッダ（最初の Message-ID を使う）
    references = header_map.get("references", "").strip()
    if references:
        match = re.search(r"<[^>]+>", references)
        if match:
            return match.group(0)

    return None


def _extract_original_message_id_from_body(body_text: str) -> Optional[str]:
    """メール本文から Message-ID を抽出する（ヘッダ抽出失敗時のフォールバック）。

    バウンスメール本文には "Message-ID: <xxx@yyy>" の形式で
    元のヘッダが引用されることがある。

    Args:
        body_text: デコード済みのメール本文テキスト。

    Returns:
        Message-ID 文字列、見つからなければ None。
    """
    # "Message-ID: <xxx@yyy>" パターン
    match = re.search(r"[Mm]essage-[Ii][Dd]:\s*(<[^>]+>)", body_text)
    if match:
        return match.group(1)
    return None


def _extract_recipient_from_headers(headers: list[dict]) -> Optional[str]:
    """X-Failed-Recipients ヘッダからバウンス宛先メアドを抽出する。

    フォールバック順:
        1. X-Failed-Recipients ヘッダ
        2. To ヘッダ（最終手段）

    Args:
        headers: Gmail API の payload.headers リスト。

    Returns:
        メールアドレス文字列、見つからなければ None。
    """
    header_map: dict[str, str] = {}
    for h in headers:
        name = h.get("name", "").lower()
        value = h.get("value", "")
        if value:
            header_map[name] = value

    # 1. X-Failed-Recipients（mailer-daemon が付ける）
    failed = header_map.get("x-failed-recipients", "").strip()
    if failed:
        # カンマ区切りの場合は最初の1件
        first = failed.split(",")[0].strip()
        return first if first else None

    # 2. To ヘッダ（フォールバック）
    to_val = header_map.get("to", "").strip()
    if to_val:
        # <email> 形式ならアングルブラケット内を返す
        match = re.search(r"<([^>]+)>", to_val)
        if match:
            return match.group(1)
        return to_val

    return None


def _extract_bounce_reason(headers: list[dict], snippet: str) -> str:
    """バウンス理由を抽出する。

    抽出順:
        1. Subject ヘッダ
        2. snippet の最初の 100 文字

    Args:
        headers: Gmail API の payload.headers リスト。
        snippet: Gmail API の message.snippet。

    Returns:
        バウンス理由文字列（空にはならない）。
    """
    header_map: dict[str, str] = {}
    for h in headers:
        name = h.get("name", "").lower()
        value = h.get("value", "")
        if value:
            header_map[name] = value

    subject = header_map.get("subject", "").strip()
    if subject:
        return subject[:200]

    if snippet:
        return snippet[:100]

    return "不明（詳細なし）"


def _decode_body(payload: dict) -> str:
    """Gmail API の payload から本文テキストをデコードする。

    シングルパートおよびマルチパートに対応。
    text/plain パートを優先する。

    Args:
        payload: Gmail API の message.payload。

    Returns:
        デコードされた本文テキスト。取得できなければ空文字列。
    """
    # シングルパート
    if "body" in payload and payload["body"].get("data"):
        try:
            return base64.urlsafe_b64decode(
                payload["body"]["data"] + "=="
            ).decode("utf-8", errors="replace")
        except Exception:
            return ""

    # マルチパート: text/plain を探す
    for part in payload.get("parts", []):
        if part.get("mimeType", "").startswith("text/plain"):
            data = part.get("body", {}).get("data", "")
            if data:
                try:
                    return base64.urlsafe_b64decode(data + "==").decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    return ""
        # ネストしたマルチパート
        if part.get("mimeType", "").startswith("multipart/"):
            result = _decode_body(part)
            if result:
                return result

    return ""


# -------------------------------------------------------------------------
# BounceMonitor クラス
# -------------------------------------------------------------------------

class BounceMonitor:
    """Gmail INBOX をスキャンしてバウンスメールを検出し送信ログに書き戻す。

    手動起動専用。Cloud Scheduler / 自動 cron は未対応（6月以降の予定）。

    Args:
        sheet_id: マスタDB の Google Sheets ID。
        from_address: 送信元メールアドレス（Gmail OAuth の認証アカウント）。
                      デフォルト: office@momijian.co
    """

    def __init__(
        self,
        sheet_id: str,
        from_address: str = "office@momijian.co",
    ) -> None:
        self._sheet_id = sheet_id
        self._from_address = from_address
        # Gmail / Sheets service は遅延初期化
        self._gmail_service: object | None = None
        self._sheets_service: object | None = None

    # ------------------------------------------------------------------
    # 内部: サービス取得
    # ------------------------------------------------------------------

    def _get_gmail_service(self) -> object:
        """Gmail API service を遅延初期化して返す（MailChannel と同じ OAuth）。"""
        if self._gmail_service is None:
            from momijian_common.auth.gmail_oauth import get_gmail_service

            self._gmail_service = get_gmail_service(
                scopes=("https://www.googleapis.com/auth/gmail.modify",)
            )
        return self._gmail_service

    def _get_sheets_service(self) -> object:
        """Sheets API service を遅延初期化して返す（DeliveryLogger と同じフロー）。"""
        if self._sheets_service is None:
            from momijian_common.delivery._sheets import get_sheets_service

            self._sheets_service = get_sheets_service()
        return self._sheets_service

    # ------------------------------------------------------------------
    # 内部: Gmail ラベル ID 取得 / 作成
    # ------------------------------------------------------------------

    def _get_or_create_label_id(self, service: object, label_name: str) -> Optional[str]:
        """ラベル名から Gmail ラベル ID を取得する。存在しなければ作成する。

        Args:
            service: Gmail API service。
            label_name: ラベル名（例: "teikyohyou/bounce"）。

        Returns:
            ラベル ID 文字列、エラー時は None。
        """
        try:
            resp = (
                service.users()  # type: ignore[attr-defined]
                .labels()
                .list(userId="me")
                .execute()
            )
            for lbl in resp.get("labels", []):
                if lbl.get("name") == label_name:
                    return lbl["id"]

            # ラベルが存在しない場合は作成
            created = (
                service.users()  # type: ignore[attr-defined]
                .labels()
                .create(userId="me", body={"name": label_name})
                .execute()
            )
            return created["id"]
        except Exception as exc:
            logger.warning("ラベル ID 取得/作成失敗: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 内部: 送信ログ全件取得
    # ------------------------------------------------------------------

    def _fetch_sent_log_rows(self, sheets_service: object) -> list[list[str]]:
        """送信ログシートの全行を取得する（ヘッダ行含む）。

        Returns:
            2次元リスト。各行は列値のリスト（文字列）。
        """
        try:
            resp = (
                sheets_service.spreadsheets()  # type: ignore[attr-defined]
                .values()
                .get(
                    spreadsheetId=self._sheet_id,
                    range=_LOG_RANGE,
                )
                .execute()
            )
            return resp.get("values", [])
        except Exception as exc:
            logger.warning("送信ログ取得失敗: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 内部: 送信ログ K列更新
    # ------------------------------------------------------------------

    def _update_error_detail(
        self,
        sheets_service: object,
        row_number: int,  # 1始まり（Sheets の行番号）
        error_text: str,
    ) -> bool:
        """送信ログシートの K列（error_detail）を更新する。

        Args:
            sheets_service: Sheets API service。
            row_number: Sheets の行番号（1始まり、ヘッダ行含む）。
            error_text: 書き込むテキスト（例: "BOUNCE: Mail delivery failed"）。

        Returns:
            True: 更新成功。False: エラー。
        """
        cell_range = f"送信ログ!K{row_number}"
        try:
            sheets_service.spreadsheets().values().update(  # type: ignore[attr-defined]
                spreadsheetId=self._sheet_id,
                range=cell_range,
                valueInputOption="RAW",
                body={"values": [[error_text]]},
            ).execute()
            return True
        except Exception as exc:
            logger.warning("送信ログ K列更新失敗 (row=%d): %s", row_number, exc)
            return False

    # ------------------------------------------------------------------
    # 公開 API: scan
    # ------------------------------------------------------------------

    def scan(
        self,
        since: datetime,
        *,
        label_bounce: str = _DEFAULT_BOUNCE_LABEL,
    ) -> list[BounceResult]:
        """Gmail INBOX をスキャンしてバウンスメールを検出・処理する。

        処理手順:
            1. Gmail messages.list で mailer-daemon 等からのメールを取得
            2. 各メッセージから In-Reply-To / References ヘッダで元 Message-ID を抽出
               フォールバック: 本文テキストから Message-ID: ヘッダ行を regex 検索
            3. 元 Message-ID を送信ログ I列に逆引き
            4. マッチした行の K列を "BOUNCE: <理由>" で更新（既記録ならスキップ）
            5. バウンスメールに label_bounce ラベルを付与
            6. BounceResult list を返す

        Args:
            since: この日時以降に受信したメールを対象とする（UTC）。
            label_bounce: バウンスメールに付与する Gmail ラベル名。

        Returns:
            検出された BounceResult のリスト。バウンスなしなら空リスト。
        """
        gmail = self._get_gmail_service()
        sheets = self._get_sheets_service()

        # since を Unix タイムスタンプ（秒）に変換
        since_ts = int(since.replace(tzinfo=timezone.utc).timestamp()) if since.tzinfo is None else int(since.timestamp())

        # Gmail 検索クエリ
        query = f"{_BOUNCE_SENDER_QUERY} after:{since_ts}"
        logger.info("BounceMonitor.scan: query=%s", query)

        # バウンスラベル ID を取得/作成
        label_id = self._get_or_create_label_id(gmail, label_bounce)

        # 送信ログを全件取得（逆引き用）
        log_rows = self._fetch_sent_log_rows(sheets)
        # ヘッダ行を除いた data rows（インデックス 0 = シート行 2）
        data_rows = log_rows[1:] if log_rows else []

        # Gmail メッセージ一覧を取得
        try:
            list_resp = (
                gmail.users()  # type: ignore[attr-defined]
                .messages()
                .list(userId="me", q=query, maxResults=100)
                .execute()
            )
        except Exception as exc:
            logger.error("Gmail messages.list 失敗: %s", exc)
            return []

        messages = list_resp.get("messages", [])
        logger.info("BounceMonitor.scan: %d 件のバウンス候補を取得", len(messages))

        results: list[BounceResult] = []

        for msg_stub in messages:
            gmail_id: str = msg_stub["id"]

            # メッセージ詳細を取得（ヘッダ + 本文）
            try:
                msg_detail = (
                    gmail.users()  # type: ignore[attr-defined]
                    .messages()
                    .get(
                        userId="me",
                        id=gmail_id,
                        format="full",
                    )
                    .execute()
                )
            except Exception as exc:
                logger.warning("メッセージ取得失敗 (id=%s): %s", gmail_id, exc)
                continue

            headers: list[dict] = msg_detail.get("payload", {}).get("headers", [])
            snippet: str = msg_detail.get("snippet", "")[:_SNIPPET_MAX_LEN]

            # 本文デコード（Message-ID フォールバック用）
            body_text = _decode_body(msg_detail.get("payload", {}))

            # 元 Message-ID 抽出（フォールバック順: In-Reply-To → References → 本文）
            original_mid = _extract_original_message_id(headers)
            if original_mid is None:
                original_mid = _extract_original_message_id_from_body(body_text)

            # バウンス宛先抽出
            recipient = _extract_recipient_from_headers(headers)

            # バウンス理由
            reason = _extract_bounce_reason(headers, snippet)

            # 送信ログ照合 + K列更新
            sent_log_updated = False
            if original_mid:
                for row_idx, row in enumerate(data_rows):
                    # I列 (index 8) の Message-ID と照合
                    if len(row) > _MESSAGE_ID_COL_INDEX:
                        row_mid = row[_MESSAGE_ID_COL_INDEX].strip()
                        if row_mid == original_mid:
                            # K列 (index 10) を確認
                            existing_k = ""
                            if len(row) > _ERROR_DETAIL_COL_INDEX:
                                existing_k = row[_ERROR_DETAIL_COL_INDEX].strip()

                            if existing_k.startswith(_BOUNCE_PREFIX):
                                # 既に記録済 → スキップ
                                logger.info(
                                    "bounce 既記録のためスキップ: message_id=%s", original_mid
                                )
                                sent_log_updated = True  # 既記録も「更新済」扱い
                            else:
                                # K列に書き戻し（シート行番号は +2: ヘッダ1行 + 0始まり→1始まり）
                                sheet_row_number = row_idx + 2
                                error_text = f"{_BOUNCE_PREFIX} {reason}"
                                ok = self._update_error_detail(sheets, sheet_row_number, error_text)
                                if ok:
                                    sent_log_updated = True
                                    logger.info(
                                        "送信ログ K列更新: row=%d message_id=%s reason=%s",
                                        sheet_row_number,
                                        original_mid,
                                        reason,
                                    )
                            break

            # バウンスメールにラベル付与
            if label_id:
                try:
                    gmail.users().messages().modify(  # type: ignore[attr-defined]
                        userId="me",
                        id=gmail_id,
                        body={"addLabelIds": [label_id]},
                    ).execute()
                except Exception as exc:
                    logger.warning("ラベル付与失敗 (id=%s): %s", gmail_id, exc)

            result = BounceResult(
                bounce_message_id=gmail_id,
                original_message_id=original_mid,
                recipient=recipient,
                reason=reason,
                sent_log_updated=sent_log_updated,
                raw_snippet=snippet,
            )
            results.append(result)

        logger.info("BounceMonitor.scan 完了: %d 件処理", len(results))
        return results


# -------------------------------------------------------------------------
# CLI エントリーポイント（手動起動用）
# -------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "BounceMonitor: Gmail INBOX からバウンスメールを検出し送信ログを更新する。\n"
            "手動起動専用（自動 cron なし）。\n\n"
            "例:\n"
            "  python -m momijian_common.delivery.bounce \\\n"
            "      --since 2026-05-23T00:00:00Z \\\n"
            "      --sheet-id 1qyhqvY9VCHFCqqCFrLztTLr2IDGzVOoVLq9X6e54ugE"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sheet-id",
        required=True,
        help="マスタDB の Google Sheets ID（送信ログシートがあるスプレッドシート）",
    )
    parser.add_argument(
        "--since",
        required=True,
        help="この日時以降に受信したバウンスを対象とする（ISO 8601 形式、例: 2026-05-23T00:00:00Z）",
    )
    parser.add_argument(
        "--from-address",
        default="office@momijian.co",
        help="送信元メールアドレス（デフォルト: office@momijian.co）",
    )
    parser.add_argument(
        "--label-bounce",
        default=_DEFAULT_BOUNCE_LABEL,
        help=f"バウンスメールに付与する Gmail ラベル名（デフォルト: {_DEFAULT_BOUNCE_LABEL}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI メインエントリーポイント。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = _parse_args(argv)

    # --since を datetime にパース
    try:
        since_str: str = args.since
        # "Z" を "+00:00" に変換して fromisoformat で処理
        since_str_normalized = since_str.replace("Z", "+00:00")
        since_dt = datetime.fromisoformat(since_str_normalized)
    except ValueError as e:
        print(f"ERROR: --since の日時フォーマットが不正です: {e}", file=sys.stderr)
        print("例: --since 2026-05-23T00:00:00Z", file=sys.stderr)
        sys.exit(1)

    monitor = BounceMonitor(
        sheet_id=args.sheet_id,
        from_address=args.from_address,
    )

    print(f"BounceMonitor 起動中... (since={since_dt.isoformat()})")
    print(f"  sheet_id: {args.sheet_id}")
    print(f"  label: {args.label_bounce}")
    print()

    results = monitor.scan(since=since_dt, label_bounce=args.label_bounce)

    if not results:
        print("バウンスメールは検出されませんでした。")
        return

    print(f"検出されたバウンス: {len(results)} 件\n")
    print("-" * 70)
    for i, r in enumerate(results, start=1):
        print(f"[{i}] bounce_message_id : {r.bounce_message_id}")
        print(f"     original_message_id: {r.original_message_id or '(不明)'}")
        print(f"     recipient          : {r.recipient or '(不明)'}")
        print(f"     reason             : {r.reason}")
        print(f"     sent_log_updated   : {r.sent_log_updated}")
        print(f"     raw_snippet        : {r.raw_snippet[:80]}...")
        print()


if __name__ == "__main__":
    main()
