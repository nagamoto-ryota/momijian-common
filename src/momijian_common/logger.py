"""Cloud Logging 構造化ログユーティリティ

Cloud Run 環境では JSON 構造化ログを出力し、
ローカル環境では人間が読みやすいフォーマットで出力する。
"""

import json
import logging
import os
import sys


class CloudLoggingFormatter(logging.Formatter):
    """Cloud Logging 用 JSON フォーマッター

    Cloud Run 上で stdout/stderr に出力された JSON は
    Cloud Logging が自動でパースし、severity や sourceLocation を認識する。
    """

    LEVEL_MAP = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "severity": self.LEVEL_MAP.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": str(record.lineno),
                "function": record.funcName,
            },
            "logger": record.name,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)


def _is_cloud_run() -> bool:
    """Cloud Run 環境かどうかを判定"""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))


def setup_logger(name: str, level: str | None = None) -> logging.Logger:
    """ロガーを設定して返す

    Args:
        name: ロガー名（通常 __name__）
        level: ログレベル文字列（"DEBUG", "INFO" 等）。
               省略時は LOG_LEVEL 環境変数 → デフォルト "INFO"。

    Returns:
        設定済みの logging.Logger
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    log_level = level or os.environ.get("LOG_LEVEL", "INFO")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)

    if _is_cloud_run():
        handler.setFormatter(CloudLoggingFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    logger.addHandler(handler)
    logger.propagate = False

    return logger
