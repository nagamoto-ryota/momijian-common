"""Sentry 初期化 + あおい通知送信ヘルパー (Phase 0)

- sentry_init: SENTRY_DSN 環境変数で Sentry SDK を初期化 (未設定時は no-op)
- notify_error_to_aoi: あおいの /notify/error エンドポイントにエラー通知を送信
"""
from __future__ import annotations
import logging
import os
from typing import Optional
import requests

from .errors import ErrorReport

logger = logging.getLogger(__name__)


def sentry_init(app_name: str, environment: Optional[str] = None) -> bool:
    """Sentry を初期化する。SENTRY_DSN 未設定時は no-op。

    Returns:
        True if Sentry was initialized, False otherwise.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.warning(f"[sentry_init] SENTRY_DSN 未設定のため Sentry を無効化: app={app_name}")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.stdlib import StdlibIntegration
    except ImportError:
        logger.warning(f"[sentry_init] sentry-sdk 未インストール: app={app_name}")
        return False

    integrations = [StdlibIntegration()]
    # Flask integration は optional
    try:
        from sentry_sdk.integrations.flask import FlaskIntegration
        integrations.append(FlaskIntegration())
    except Exception:
        pass
    # FastAPI integration は optional
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        integrations.append(FastApiIntegration())
    except Exception:
        pass

    k_service = os.environ.get("K_SERVICE", "")
    cloud_run_job = os.environ.get("CLOUD_RUN_JOB", "")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    def _before_send(event, hint):
        # DRY_RUN 時は送信しない
        if dry_run:
            return None
        return event

    sentry_sdk.init(
        dsn=dsn,
        environment=environment or ("cloud-run" if k_service else "local"),
        integrations=integrations,
        before_send=_before_send,
        traces_sample_rate=0.0,  # パフォーマンストレースは無効(コスト節約)
        profiles_sample_rate=0.0,
    )
    sentry_sdk.set_tag("app", app_name)
    if k_service:
        sentry_sdk.set_tag("k_service", k_service)
    if cloud_run_job:
        sentry_sdk.set_tag("cloud_run_job", cloud_run_job)

    logger.info(f"[sentry_init] 初期化完了: app={app_name}, env={environment}")
    return True


def notify_error_to_aoi(
    report: ErrorReport,
    app_name: str,
    task_id: Optional[str] = None,
    affected_items: Optional[list[str]] = None,
    timeout: float = 5.0,
) -> bool:
    """あおいの /notify/error エンドポイントにエラー通知を送信する。

    AOI_NOTIFY_URL / AOI_NOTIFY_TOKEN が未設定なら no-op。
    通信失敗してもアプリを落とさない(False返却のみ)。

    Returns:
        True if notification was sent successfully, False otherwise.
    """
    url = os.environ.get("AOI_NOTIFY_URL", "").strip()
    token = os.environ.get("AOI_NOTIFY_TOKEN", "").strip()
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        logger.info(f"[notify_error_to_aoi] DRY_RUN: skipped (app={app_name}, category={report.category})")
        return False

    if not url or not token:
        logger.warning(
            f"[notify_error_to_aoi] AOI_NOTIFY_URL/TOKEN 未設定のため通知スキップ: "
            f"app={app_name}, category={report.category}"
        )
        return False

    payload = {
        "app_name": app_name,
        "task_id": task_id or "",
        "category": report.category,
        "user_message": report.user_message,
        "recovery_hint": report.recovery_hint,
        "target": report.target,
        "affected_items": affected_items or [],
    }
    endpoint = f"{url.rstrip('/')}/notify/error"

    for attempt in range(2):  # リトライ1回 (初回 + 1回)
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                headers={"X-Notify-Token": token, "Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                logger.info(f"[notify_error_to_aoi] 送信成功: app={app_name}, category={report.category}")
                return True
            logger.warning(
                f"[notify_error_to_aoi] 送信失敗 status={resp.status_code}: {resp.text[:200]}"
            )
        except Exception as e:
            logger.warning(f"[notify_error_to_aoi] 送信例外 (attempt {attempt+1}/2): {e}")
    return False
