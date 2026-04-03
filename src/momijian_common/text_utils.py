"""テキスト処理ユーティリティ

日本語テキストの正規化（旧字体 → 新字体変換）等を提供。
介護業界では利用者名・事業所名に旧字体が頻出するため、
マッチング前の正規化が必須。
"""

import re
import unicodedata

# 旧字体 → 新字体 変換マップ（包括版）
_OLD_KANJI_MAP: dict[str, str] = {
    # 人名で頻出
    "惠": "恵",
    "髙": "高",
    "﨑": "崎",
    "齋": "斎",
    "齊": "斎",
    "邊": "辺",
    "邉": "辺",
    "澤": "沢",
    "濱": "浜",
    "濵": "浜",
    "藪": "薮",
    "收": "収",
    # 地名・事業所名で頻出
    "廣": "広",
    "國": "国",
    "鷗": "鴎",
    "櫻": "桜",
    "條": "条",
    "龍": "竜",
    "瀨": "瀬",
    "亞": "亜",
    "圓": "円",
    "藏": "蔵",
    "德": "徳",
}

# 正規化用コンパイル済みパターン
_OLD_KANJI_PATTERN = re.compile("|".join(re.escape(k) for k in _OLD_KANJI_MAP))


def normalize_japanese(text: str) -> str:
    """日本語テキストを正規化する

    - Unicode NFKC 正規化（全角英数 → 半角、等）
    - 旧字体 → 新字体変換
    - 前後の空白除去

    Args:
        text: 正規化対象のテキスト

    Returns:
        正規化済みテキスト
    """
    if not text:
        return text

    # NFKC 正規化（全角→半角、互換文字統一）
    text = unicodedata.normalize("NFKC", text.strip())

    # 旧字体 → 新字体
    text = _OLD_KANJI_PATTERN.sub(lambda m: _OLD_KANJI_MAP[m.group()], text)

    return text
