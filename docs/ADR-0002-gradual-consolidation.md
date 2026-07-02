# ADR-0002: 全社モノレポ化は見送り、段階集約（B案）を採る

- 状態: 採択
- 日付: 2026-07-02

## 背景

もみじ庵の自社アプリはリポジトリが増え続けており（新リポ作成は月 3〜6 件）、
「全社モノレポに統合すべきか」を検討した。実測で分かったことは次のとおり:

- **CI の重複は既に解決済み**: 全 Cloud Run アプリが momijian-common の
  reusable workflow（`reusable-test.yml` / `reusable-deploy.yml`）を薄い caller
  から呼ぶ構成に統一されており、CI 改善は 1 箇所で全プロジェクトに波及する。
  モノレポ化で新たに得られる CI 上のメリットは小さい。
- **新リポ作成は減速中**: 月 3〜6 件で、増加ペース自体が落ちている。
- **真の残存リスクは momijian-common のバージョンずれ**: 各アプリの
  requirements.txt は
  `momijian-common @ https://github.com/nagamoto-ryota/momijian-common/archive/master.tar.gz`
  という**ピン無し master 参照**であり、common 側の変更が「次に各アプリを
  ビルドした時」に暗黙に取り込まれる。アプリごとに取り込みタイミングが
  ばらけるため、参照ずれ起因の互換性障害が起こり得る。
- **一括移行は事故常連領域**: 本番デプロイ配線（GitHub Actions + WIF
  バインディング + デプロイ SA + Cloud Run サービス名）の張り替えは、
  過去に Permission denied・startup_failure・traffic 未切替等のトラブルを
  繰り返してきた領域（lessons/cicd-github.md 参照）。全アプリ分を一括で
  張り替えると、月次請求期間（毎月 1〜10 日）に壊した場合に業務が止まる。

## 決定

全社モノレポ化は**見送り**、以下の **B案: 段階集約** を採る。

- **Step 1（即時）**: momijian-common 参照の統一と、新リポ用テンプレートの整備。
  テンプレには薄い caller workflow・AGENTS.md・CLAUDE.md・requirements.txt
  （ピン無し master 参照であることの注意書き付き）・Dockerfile・smoke テスト・
  「テンプレで自動化できない残り儀式」チェックリスト（WIF/SA 紐付け・
  Cloud Run サービス初期作成・Secret 設定等）を含める。
- **Step 2（今後の新規アプリ）**: 新規アプリは原則、新リポを作らず
  **集約リポ `momijian-apps` に足す**。momijian-apps は初回の新規アプリ開発時に
  作成する（先行して空リポを作らない）。単独リポが必要な例外時のみ
  Step 1 のテンプレを使う。
- **Step 3（既存本番アプリ）**: 既存アプリの移行**専用**作業はしない。
  次の大改修が入った時に「ついで移行」で momijian-apps へ載せ替える。

### 併走時の衝突回避ルール（本決定に含む）

他セッション・Codex との並走を前提に、リポ操作は以下を守る:

1. `projects/active/`・`codex-workspace/` の共有作業コピーを直接編集しない。
   書き込みが要る時は scratchpad の使い捨て clone を使う。
2. 変更は必ず branch → PR → CI → auto-merge の経路に乗せる（直接 push しない）。
3. 着手前に対象リポの open PR と直近 push を確認し、被りがあれば後回しにする。
4. 1 リポずつ直列で進める（複数リポへの同時変更を並行させない）。

## 理由

- モノレポ化の主目的（CI 共通化・共通基盤の一元化）は reusable workflow と
  momijian-common で**既に達成済み**であり、残るメリットに対して一括移行の
  リスク（本番デプロイ配線の張り替え事故 × 月次請求期間の業務停止）が大きすぎる。
- 段階集約なら「新規の増殖を止める（Step 2）」効果は即時に得つつ、
  既存本番アプリには**移行そのものを目的とした変更を一切加えない**（Step 3）。
  ついで移行なら大改修時のテスト・検証と道連れにでき、移行単独の検証コストと
  事故リスクを負わない。
- 真の残存リスク（ピン無し master 参照によるバージョンずれ）は、モノレポ化
  ではなくテンプレの注意書き・common 側の互換性維持運用・将来のタグ/バージョン
  ピン導入で個別に対処できる問題であり、リポ統合の理由にならない。

## 影響 / トレードオフ

- リポジトリの複数体制は当面続く（一覧性・横断 grep の弱さは残る）。
- momijian-apps 作成後しばらくは「単独リポ組」と「集約リポ組」が併存し、
  デプロイ配線が 2 系統になる。ただしどちらも同じ reusable workflow を
  呼ぶため、CI ロジック自体は 1 箇所のまま。
- ピン無し master 参照は本 ADR では解消しない（注意書きによる可視化まで）。
  common に破壊的変更を入れる時は、利用側アプリへの波及を common 側 PR で
  意識する運用が引き続き必要。

## 再検討条件

以下のいずれかが起きたら本決定を見直す:

- 「ついで移行」が 12 ヶ月進まず、かつピン無し master 参照ずれ起因の
  本番障害が実際に発生した場合（→ タグピン導入 or 一括移行を再評価）。
- momijian-apps 運用開始後、集約リポ側で CI 時間・デプロイ独立性・
  権限分離に実害が出た場合（→ 分割方針へ戻すことも含め再評価）。
- GitHub / Google Cloud / Anthropic 等の公式推奨がモノレポ前提のツールチェーン
  に大きく寄り、独自の複数リポ運用がガラパゴス化する兆候が出た場合。
- 新リポ作成ペースが再加速し（月 10 件超等）、Step 2 の抑止だけでは
  管理が追いつかなくなった場合。

## 参照

- ADR の先例: nursing-jobs `docs/ADR-0001-monorepo.md`（Job 集約 monorepo の採択）
- デプロイ配線の事故史: `~/.claude/lessons/cicd-github.md`
- reusable workflow 実体: 本リポ `.github/workflows/reusable-test.yml` / `reusable-deploy.yml`
