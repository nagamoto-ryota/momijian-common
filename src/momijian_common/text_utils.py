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
    "伹": "但",  # U+4F39 異体字（例: 伹住節子）
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

    .. deprecated::
        識別目的（マッチングキー生成）の場合は ``to_match_key()`` を使用してください。
        本関数は変換結果を実データとして保存・表示する用途を含意しがちですが、
        人名・固有名詞は本人の所有物であり書き換え禁止です。
        既存呼び出し箇所は後方互換のため動作は維持されます。

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


def to_match_key(text: str) -> str:
    """マッチング用の照合鍵（match key）を生成する。

    異体字・全角半角・前後空白などのデータソース表記ゆれを吸収して
    1つの内部キーに収束させる。**戻り値は識別/比較目的に限定し、
    実データとして保存・表示してはならない**。

    人名・固有名詞は本人の所有物であり、システム側で書き換えてはならない。
    マスタDB側の表記、CSV側の表記を**それぞれ原文のまま保存**し、
    本関数の戻り値は突合時の一時的な比較キーとしてのみ使用すること。

    Examples:
        >>> to_match_key("伹住 節子") == to_match_key("但住 節子")
        True
        >>> # マスタDBには "伹住 節子" を、CSV側には "但住 節子" を、それぞれ原文で保存。
        >>> # 突合時に両方を to_match_key() に通してキー比較する。

    Args:
        text: 照合対象のテキスト

    Returns:
        照合鍵文字列（保存・表示禁止）
    """
    # 現状は normalize_japanese と同じ実装で問題なし。
    # 将来 match_key 専用の追加正規化（スペース除去等）が必要になったらここで分岐。
    return normalize_japanese(text)
