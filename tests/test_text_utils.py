"""日本語テキスト正規化ユーティリティのテスト"""

import pytest

from momijian_common.text_utils import normalize_japanese, to_match_key


class TestNormalizeJapanese:
    """normalize_japanese() のテスト"""

    # --- 基本動作 ---

    def test_empty_string(self):
        """空文字列はそのまま返す"""
        assert normalize_japanese("") == ""

    def test_none_returns_none(self):
        """None はそのまま返す"""
        assert normalize_japanese(None) is None

    def test_no_change_needed(self):
        """変換不要な文字列はそのまま"""
        assert normalize_japanese("田中太郎") == "田中太郎"

    # --- 旧字体 → 新字体変換（人名） ---

    def test_taka(self):
        """髙 → 高（はしご高）"""
        assert normalize_japanese("髙橋") == "高橋"

    def test_saki(self):
        """﨑 → 崎（立崎）"""
        assert normalize_japanese("山﨑") == "山崎"

    def test_sai_variant1(self):
        """齋 → 斎"""
        assert normalize_japanese("齋藤") == "斎藤"

    def test_sai_variant2(self):
        """齊 → 斎"""
        assert normalize_japanese("齊藤") == "斎藤"

    def test_hen_variant1(self):
        """邊 → 辺"""
        assert normalize_japanese("渡邊") == "渡辺"

    def test_hen_variant2(self):
        """邉 → 辺"""
        assert normalize_japanese("渡邉") == "渡辺"

    def test_sawa(self):
        """澤 → 沢"""
        assert normalize_japanese("澤田") == "沢田"

    def test_hama_variant1(self):
        """濱 → 浜"""
        assert normalize_japanese("濱田") == "浜田"

    def test_hama_variant2(self):
        """濵 → 浜"""
        assert normalize_japanese("濵田") == "浜田"

    def test_megumi(self):
        """惠 → 恵"""
        assert normalize_japanese("惠子") == "恵子"

    def test_osamu(self):
        """收 → 収"""
        assert normalize_japanese("收") == "収"

    # --- 旧字体 → 新字体変換（地名・事業所名） ---

    def test_hiro(self):
        """廣 → 広"""
        assert normalize_japanese("廣島") == "広島"

    def test_kuni(self):
        """國 → 国"""
        assert normalize_japanese("國分") == "国分"

    def test_sakura(self):
        """櫻 → 桜"""
        assert normalize_japanese("櫻井") == "桜井"

    def test_ryu(self):
        """龍 → 竜"""
        assert normalize_japanese("龍太郎") == "竜太郎"

    def test_se(self):
        """瀨 → 瀬"""
        assert normalize_japanese("瀨戸") == "瀬戸"

    def test_en(self):
        """圓 → 円"""
        assert normalize_japanese("圓山") == "円山"

    # --- 複合変換 ---

    def test_multiple_old_kanji(self):
        """1つの文字列に複数の旧字体が含まれる場合"""
        assert normalize_japanese("髙橋惠子") == "高橋恵子"

    def test_real_world_care_name(self):
        """介護現場で実際にある名前パターン"""
        assert normalize_japanese("渡邊　惠美子") == "渡辺 恵美子"

    # --- NFKC 正規化 ---

    def test_fullwidth_to_halfwidth_numbers(self):
        """全角数字 → 半角"""
        assert normalize_japanese("１２３") == "123"

    def test_fullwidth_to_halfwidth_alpha(self):
        """全角英字 → 半角"""
        assert normalize_japanese("ＡＢＣ") == "ABC"

    def test_strip_whitespace(self):
        """前後の空白を除去"""
        assert normalize_japanese("  田中太郎  ") == "田中太郎"

    def test_fullwidth_space_in_name(self):
        """全角スペースはNFKCで半角になる"""
        result = normalize_japanese("田中　太郎")
        assert result == "田中 太郎"

    # --- 組み合わせ ---

    def test_combined_nfkc_and_kanji(self):
        """NFKC正規化と旧字体変換の組み合わせ"""
        assert normalize_japanese("　髙橋　惠子　") == "高橋 恵子"


class TestToMatchKey:
    """to_match_key() のテスト

    NOTE: 戻り値は識別/比較目的のみ。実データとして保存・表示禁止。
    マスタDB/CSVの原文は必ず別フィールドに保持し、
    突合時の一時キーとしてのみ本関数を使用すること。
    """

    # --- 基本動作（normalize_japanese と同じ動作を確認）---

    def test_nfkc_normalization(self):
        """全角数字 → 半角（NFKC正規化が適用される）"""
        assert to_match_key("１２３") == "123"

    def test_strip_whitespace(self):
        """前後の空白を除去"""
        assert to_match_key("  田中太郎  ") == "田中太郎"

    def test_old_kanji_conversion(self):
        """旧字体 → 新字体変換（髙→高）"""
        assert to_match_key("髙橋") == "高橋"

    # --- identity property: 異体字ペアが同じキーを返す ---
    # 保存禁止: 以下のアサートは「同じキーになる」ことだけを検証。
    # 実際のマスタDB/CSVにはそれぞれ原文を保存すること。

    def test_identity_tanisumi(self):
        """伹住（U+4F39）と但住（U+4F46）が同じキーになる（nursing-sync 伹住節子さんの実例）"""
        # 保存禁止: to_match_key の戻り値は突合用の一時キー
        assert to_match_key("伹住 節子") == to_match_key("但住 節子")

    def test_identity_hama(self):
        """濱（旧字体）と浜（新字体）が同じキーになる"""
        assert to_match_key("濱田") == to_match_key("浜田")

    def test_identity_saki(self):
        """﨑（立崎）と崎が同じキーになる"""
        assert to_match_key("山﨑") == to_match_key("山崎")

    def test_identity_megumi(self):
        """惠（旧字体）と恵（新字体）が同じキーになる"""
        assert to_match_key("惠子") == to_match_key("恵子")

    # --- エッジケース ---

    def test_empty_string(self):
        """空文字列はそのまま返す"""
        assert to_match_key("") == ""

    def test_none_returns_none(self):
        """None はそのまま返す（normalize_japanese と同じ挙動）"""
        assert to_match_key(None) is None
