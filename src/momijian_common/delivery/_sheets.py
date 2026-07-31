"""Delivery modules shared Sheets API authentication."""

from __future__ import annotations

import logging
import os
import sys


logger = logging.getLogger(__name__)

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheets_service() -> object:
    """Build a Sheets service using local SA credentials first, then ADC."""
    scripts_dir = os.path.expanduser("~/.claude/scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from gcp_auth import get_sheets_service as get_local_sheets_service
    except ImportError as exc:
        # Cloud Run では gcp_auth が存在しないため ADC が正常経路。
        # PC で google-auth 系の破損によりここへ落ちた場合も気づけるよう警告を残す
        logger.warning("gcp_auth を import できないため ADC にフォールバック: %s", exc)
        return _get_adc_sheets_service()

    try:
        return get_local_sheets_service()
    except FileNotFoundError as exc:
        # PC で SA 鍵が欠損・失効している場合。別 identity（ADC）での書き込みに
        # 無音で切り替わると鍵の腐りが発覚しないため必ず警告する
        logger.warning("SA 鍵が見つからないため ADC にフォールバック: %s", exc)
        return _get_adc_sheets_service()


def _get_adc_sheets_service() -> object:
    """Build a Sheets service from Application Default Credentials."""
    import google.auth
    from googleapiclient.discovery import build

    creds, _ = google.auth.default(scopes=_SHEETS_SCOPES)
    return build(
        "sheets",
        "v4",
        credentials=creds,
        cache_discovery=False,
    )
