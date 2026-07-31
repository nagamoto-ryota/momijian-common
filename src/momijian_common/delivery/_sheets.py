"""Delivery modules shared Sheets API authentication."""

from __future__ import annotations

import os
import sys


_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheets_service() -> object:
    """Build a Sheets service using local SA credentials first, then ADC."""
    scripts_dir = os.path.expanduser("~/.claude/scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from gcp_auth import get_sheets_service as get_local_sheets_service
    except ImportError:
        return _get_adc_sheets_service()

    try:
        return get_local_sheets_service()
    except FileNotFoundError:
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
