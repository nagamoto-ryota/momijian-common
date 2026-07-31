"""送信ログを Google Sheets「送信ログ」シートに記録する DeliveryLogger。

スキーマ定義: C:/Users/Owner/projects/active/teikyohyou-mail/01_send_log_sheets_schema.md

12 列（A〜L）:
    A  timestamp      送信日時 (ISO 8601, Asia/Tokyo)
    B  対象月         YYYYMM 形式
    C  事業所番号     マスタDB 主キー（10桁）
    D  事業所名       マスタDB 原本値（正規化禁止）
    E  チャネル       ChannelType.value
    F  添付ファイル名 送信時の PDF ファイル名
    G  添付SHA256     SHA-256 フルハッシュ（hex64）
    H  宛先           メールアドレス / FAX番号
    I  Message-ID     RFC 822 形式（<uuid@...>）— 冪等キー
    J  結果           success / failed / bounced / skipped
    K  エラー詳細     失敗時のみ
    L  from_address   送信元アドレス

冪等性:
    append() は Message-ID が既に I 列に存在する場合 skip して False を返す。
    Message-ID 列のみを取得してメモリ内照合する（全件 get でも 60社程度なら問題なし）。

認証:
    credentials_path が None の場合は ~/ .claude/scripts/gcp_auth.py の
    local-dev-sa SA 鍵を優先し、利用できなければ ADC を使う。
    credentials_path が指定された場合は google-auth の
    service_account.Credentials として扱う。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from momijian_common.delivery.channel import DeliveryResult

logger = logging.getLogger(__name__)

# 送信ログシートのデフォルト設定
_MASTER_DB_ID = "1qyhqvY9VCHFCqqCFrLztTLr2IDGzVOoVLq9X6e54ugE"
_DEFAULT_SHEET_NAME = "送信ログ"

# Message-ID 列: I 列（0始まり index=8）
# Sheets A1 表記: I
_MESSAGE_ID_COL_A1 = "I"
_MESSAGE_ID_COL_INDEX = 8  # 0-based

# 列インデックス（0始まり、A=0）
_COL = {
    "timestamp": 0,       # A
    "target_month": 1,    # B
    "office_id": 2,       # C
    "office_name": 3,     # D
    "channel": 4,         # E
    "filename": 5,        # F
    "sha256": 6,          # G
    "recipient": 7,       # H
    "message_id": 8,      # I
    "result": 9,          # J
    "error_detail": 10,   # K
    "from_address": 11,   # L
}

_JST = timezone(timedelta(hours=9))


class DeliveryLogger:
    """送信1件の結果を Google Sheets「送信ログ」シートに追記する。

    冪等キーは Message-ID（I列）。同一 Message-ID が既存行にある場合は skip する。

    Args:
        sheet_id: マスタDB の Google Sheets ID。
        sheet_name: 書込み先シート名。デフォルト「送信ログ」。
        credentials_path: SA 鍵 JSON ファイルパス。None なら local-dev-sa を
            優先し、利用できない環境では ADC を使う。
    """

    def __init__(
        self,
        sheet_id: str = _MASTER_DB_ID,
        sheet_name: str = _DEFAULT_SHEET_NAME,
        credentials_path: Optional[str] = None,
    ) -> None:
        self._sheet_id = sheet_id
        self._sheet_name = sheet_name
        self._credentials_path = credentials_path
        self._service = None  # 遅延初期化

    # ------------------------------------------------------------------
    # 内部: Sheets API client の遅延初期化
    # ------------------------------------------------------------------

    def _get_service(self):
        """Sheets API v4 サービスを遅延初期化して返す。"""
        if self._service is not None:
            return self._service

        if self._credentials_path is not None:
            # 明示的な credentials_path が指定された場合
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                self._credentials_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self._service = build(
                "sheets", "v4", credentials=creds, cache_discovery=False
            )
        else:
            # local-dev-sa 経由（既存スクリプト群と同一フロー）
            scripts_dir = os.path.expanduser("~/.claude/scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            try:
                from gcp_auth import get_sheets_service  # type: ignore[import]
            except ImportError:
                # Cloud Run 等、ローカル専用 gcp_auth がない環境では ADC を使う
                import google.auth
                from googleapiclient.discovery import build

                creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
                self._service = build(
                    "sheets", "v4", credentials=creds, cache_discovery=False
                )
            else:
                self._service = get_sheets_service()

        return self._service

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def is_duplicate(self, message_id: str) -> bool:
        """Message-ID が送信ログシートの I 列に既に存在するか確認する。

        Args:
            message_id: 確認する RFC 822 形式の Message-ID。

        Returns:
            既存行あり → True、なし → False。
        """
        if not message_id:
            return False

        service = self._get_service()
        range_name = f"{self._sheet_name}!{_MESSAGE_ID_COL_A1}:{_MESSAGE_ID_COL_A1}"
        try:
            resp = (
                service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self._sheet_id,
                    range=range_name,
                )
                .execute()
            )
        except Exception as exc:
            logger.warning("is_duplicate: Sheets API 取得失敗 (%s)。重複チェックをスキップします。", exc)
            return False

        rows = resp.get("values", [])
        # rows は [[value], [value], ...] 形式（ヘッダー行を含む）
        for row in rows:
            if row and row[0] == message_id:
                return True
        return False

    def append(
        self,
        result: DeliveryResult,
        office_id: str,
        from_address: str,
        extra: Optional[dict] = None,
    ) -> bool:
        """送信結果を送信ログシートに1行追記する。

        Args:
            result: DeliveryResult（T3 で定義。channel / recipient / success /
                    error / sent_at / message_id を含む）。
            office_id: マスタDB 事業所番号（C列）。
            from_address: 送信元メールアドレス（L列）。
            extra: 補助情報 dict。以下のキーを参照:
                - ``target_month`` (str): 対象月 YYYYMM（B列）。なければ空。
                - ``office_name`` (str): 事業所名 原本値（D列）。なければ空。
                - ``subject_or_filename`` (str): 添付ファイル名 / 件名（F列）。
                - ``sha256`` (str): 添付 PDF の SHA-256 全64文字（G列）。
                余ったキーは extra_json（廃止: 本スキーマには extra_json 列なし）
                として使用するか呼び出し側で処理すること。

        Returns:
            True: 追記成功。
            False: Message-ID 重複により skip（二重ログ防止）。

        Raises:
            Exception: Sheets API 呼び出し失敗時はそのまま再 raise する。
        """
        extra = extra or {}
        message_id = result.message_id or ""

        # 冪等性チェック: Message-ID が既存なら skip
        if message_id and self.is_duplicate(message_id):
            logger.info(
                "append: Message-ID が既存行と重複のため skip します。message_id=%s", message_id
            )
            return False

        # 送信日時: sent_at を JST に変換
        sent_at_jst = result.sent_at.astimezone(_JST)
        timestamp_str = sent_at_jst.isoformat()

        # 結果文字列
        result_str = "success" if result.success else "failed"

        # 12列の行データを構築（列順は setup_send_log_sheet.py の HEADERS と一致）
        row: list[str] = [""] * 12
        row[_COL["timestamp"]] = timestamp_str                          # A
        row[_COL["target_month"]] = str(extra.get("target_month", ""))  # B
        row[_COL["office_id"]] = str(office_id)                         # C
        row[_COL["office_name"]] = str(extra.get("office_name", ""))    # D
        row[_COL["channel"]] = result.channel.value                      # E
        row[_COL["filename"]] = str(extra.get("subject_or_filename", ""))  # F
        row[_COL["sha256"]] = str(extra.get("sha256", ""))              # G
        row[_COL["recipient"]] = result.recipient                        # H
        row[_COL["message_id"]] = message_id                            # I
        row[_COL["result"]] = result_str                                 # J
        row[_COL["error_detail"]] = result.error or ""                  # K
        row[_COL["from_address"]] = from_address                        # L

        service = self._get_service()
        range_name = f"{self._sheet_name}!A:L"
        service.spreadsheets().values().append(
            spreadsheetId=self._sheet_id,
            range=range_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        logger.info(
            "append: 送信ログ記録完了。office_id=%s channel=%s success=%s message_id=%s",
            office_id,
            result.channel.value,
            result.success,
            message_id or "(なし)",
        )
        return True
