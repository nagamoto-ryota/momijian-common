"""Cloud Logging 構造化ログユーティリティのテスト"""

import json
import logging
import os

import pytest

from momijian_common.logger import CloudLoggingFormatter, setup_logger, _is_cloud_run


class TestCloudLoggingFormatter:
    """CloudLoggingFormatter のテスト"""

    def setup_method(self):
        self.formatter = CloudLoggingFormatter()

    def test_format_basic_info(self):
        """INFO レベルの基本フォーマット"""
        record = logging.LogRecord(
            name="test.module",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="テスト メッセージ",
            args=None,
            exc_info=None,
        )
        result = json.loads(self.formatter.format(record))

        assert result["severity"] == "INFO"
        assert result["message"] == "テスト メッセージ"
        assert result["logger"] == "test.module"
        assert result["logging.googleapis.com/sourceLocation"]["file"] == "test.py"
        assert result["logging.googleapis.com/sourceLocation"]["line"] == "42"

    def test_format_all_levels(self):
        """全ログレベルが正しく変換される"""
        level_map = {
            logging.DEBUG: "DEBUG",
            logging.INFO: "INFO",
            logging.WARNING: "WARNING",
            logging.ERROR: "ERROR",
            logging.CRITICAL: "CRITICAL",
        }
        for level, expected_severity in level_map.items():
            record = logging.LogRecord(
                name="test", level=level, pathname="", lineno=0,
                msg="msg", args=None, exc_info=None,
            )
            result = json.loads(self.formatter.format(record))
            assert result["severity"] == expected_severity

    def test_format_with_exception(self):
        """例外情報がJSON出力に含まれる"""
        try:
            raise ValueError("テスト例外")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="test.py", lineno=1,
            msg="エラー発生", args=None, exc_info=exc_info,
        )
        result = json.loads(self.formatter.format(record))

        assert "exception" in result
        assert "ValueError" in result["exception"]
        assert "テスト例外" in result["exception"]

    def test_format_japanese_message(self):
        """日本語メッセージが ensure_ascii=False で正しくJSON化される"""
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="利用者「田中太郎」の処理完了", args=None, exc_info=None,
        )
        raw = self.formatter.format(record)

        # ensure_ascii=False なので日本語がそのまま出力される
        assert "利用者「田中太郎」の処理完了" in raw
        # それでもvalidなJSON
        result = json.loads(raw)
        assert result["message"] == "利用者「田中太郎」の処理完了"


class TestIsCloudRun:
    """_is_cloud_run() のテスト"""

    def test_not_cloud_run(self, monkeypatch):
        """環境変数なし → False"""
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
        assert _is_cloud_run() is False

    def test_cloud_run_service(self, monkeypatch):
        """K_SERVICE あり → True"""
        monkeypatch.setenv("K_SERVICE", "my-service")
        monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
        assert _is_cloud_run() is True

    def test_cloud_run_job(self, monkeypatch):
        """CLOUD_RUN_JOB あり → True"""
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.setenv("CLOUD_RUN_JOB", "my-job")
        assert _is_cloud_run() is True


class TestSetupLogger:
    """setup_logger() のテスト"""

    def teardown_method(self):
        """テスト間でロガーのハンドラをリセット"""
        for name in list(logging.Logger.manager.loggerDict.keys()):
            if name.startswith("test_"):
                logger = logging.getLogger(name)
                logger.handlers.clear()

    def test_returns_logger(self):
        """Logger オブジェクトを返す"""
        logger = setup_logger("test_returns")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_returns"

    def test_default_level_info(self, monkeypatch):
        """デフォルトレベルは INFO"""
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        logger = setup_logger("test_default_level")
        assert logger.level == logging.INFO

    def test_explicit_level(self):
        """明示的レベル指定"""
        logger = setup_logger("test_explicit_level", level="DEBUG")
        assert logger.level == logging.DEBUG

    def test_env_level(self, monkeypatch):
        """LOG_LEVEL 環境変数からレベル設定"""
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        logger = setup_logger("test_env_level")
        assert logger.level == logging.WARNING

    def test_explicit_level_overrides_env(self, monkeypatch):
        """明示的指定が環境変数より優先"""
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        logger = setup_logger("test_override", level="DEBUG")
        assert logger.level == logging.DEBUG

    def test_no_duplicate_handlers(self):
        """同じ名前で2回呼んでもハンドラが重複しない"""
        logger1 = setup_logger("test_no_dup")
        handler_count = len(logger1.handlers)
        logger2 = setup_logger("test_no_dup")
        assert logger1 is logger2
        assert len(logger2.handlers) == handler_count

    def test_local_formatter(self, monkeypatch):
        """ローカル環境では通常フォーマッタ"""
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
        logger = setup_logger("test_local_fmt")
        handler = logger.handlers[0]
        assert not isinstance(handler.formatter, CloudLoggingFormatter)

    def test_cloud_formatter(self, monkeypatch):
        """Cloud Run 環境では CloudLoggingFormatter"""
        monkeypatch.setenv("K_SERVICE", "my-service")
        logger = setup_logger("test_cloud_fmt")
        handler = logger.handlers[0]
        assert isinstance(handler.formatter, CloudLoggingFormatter)

    def test_propagate_false(self):
        """ルートロガーへの伝搬が無効"""
        logger = setup_logger("test_propagate")
        assert logger.propagate is False
