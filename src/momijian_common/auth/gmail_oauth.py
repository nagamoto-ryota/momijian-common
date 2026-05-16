"""Gmail OAuth2 helper.

Phase 2 で teikyohyou-send / EmailRegistration / BounceMonitor から共通利用される。

提供関数:
- setup_flow(client_secret_path): CLI でブラウザ認証 → refresh_token 取得 → JSON を stdout に出力
- get_gmail_service(scopes, secret_name, project_id): Secret Manager から OAuth creds を取得して
  Gmail API service を返す（lru_cache 付き）

Secret JSON 形式（Secret Manager に登録する値）:
    {"client_id": "...", "client_secret": "...", "refresh_token": "..."}

再認証が必要な場合（refresh token 失効）:
    python -m momijian_common.auth.gmail_oauth setup path/to/client_secret.json
    → stdout の JSON を gcloud secrets versions add で更新する

参考プラン: ~/.claude/plans/40-45-fax-luminous-waffle.md
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import googleapiclient.discovery

# Gmail API スコープ（デフォルト）
_DEFAULT_SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)

# gcp_auth.py の標準配置パス
_GCP_AUTH_PATH = Path.home() / ".claude" / "scripts" / "gcp_auth.py"

# GCP プロジェクト ID デフォルト
_DEFAULT_PROJECT_ID = "pdf-automation-487602"


# ---------------------------------------------------------------------------
# 内部: gcp_auth.get_secret() の動的ロード（sys.path 非汚染）
# ---------------------------------------------------------------------------

def _load_get_secret():
    """~/.claude/scripts/gcp_auth.py から get_secret を動的ロードして返す。

    失敗した場合は google.cloud.secretmanager を直接使うフォールバックを返す。
    動的ロードは sys.path を汚染しない importlib.util.spec_from_file_location を使う。
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_gcp_auth_dynamic", str(_GCP_AUTH_PATH)
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location returned None")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        return module.get_secret
    except Exception as e:
        # フォールバック: google-cloud-secret-manager を直接使う
        def _fallback_get_secret(secret_name: str, project_id: str = _DEFAULT_PROJECT_ID) -> str:
            """gcp_auth.py ロード失敗時のフォールバック。google-cloud-secret-manager を直接使う。"""
            try:
                from google.cloud import secretmanager  # type: ignore
            except ImportError:
                raise ImportError(
                    f"gcp_auth.py のロードに失敗し ({e})、"
                    "google-cloud-secret-manager もインストールされていません。\n"
                    "pip install google-cloud-secret-manager を実行してください。"
                )
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            return response.payload.data.decode("utf-8").strip()

        return _fallback_get_secret


# モジュールロード時に一度だけ解決する
_get_secret_fn = _load_get_secret()


# ---------------------------------------------------------------------------
# setup_flow: 一回限りの CLI ブラウザ認証
# ---------------------------------------------------------------------------

def setup_flow(client_secret_path: str) -> None:
    """One-time CLI: client_secret.json からブラウザ認証 → refresh_token を取得し、
    {"client_id", "client_secret", "refresh_token"} の JSON を stdout に出力する。

    user が手動で以下のコマンドで Secret Manager に登録する:
        python -m momijian_common.auth.gmail_oauth setup path/to/client_secret.json \\
          | gcloud secrets create momijian-gmail-oauth-creds \\
              --data-file=- --project=pdf-automation-487602

    または既存シークレットを更新する場合:
        python -m momijian_common.auth.gmail_oauth setup path/to/client_secret.json \\
          | gcloud secrets versions add momijian-gmail-oauth-creds \\
              --data-file=- --project=pdf-automation-487602

    scopes: gmail.modify 固定（メール読取・ラベル操作に必要な最小スコープ）
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        print(
            "ERROR: google-auth-oauthlib がインストールされていません。\n"
            "  pip install google-auth-oauthlib",
            file=sys.stderr,
        )
        sys.exit(1)

    secret_file = Path(client_secret_path).expanduser().resolve()
    if not secret_file.exists():
        print(f"ERROR: client_secret.json が見つかりません: {secret_file}", file=sys.stderr)
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(secret_file),
        scopes=list(_DEFAULT_SCOPES),
    )
    creds = flow.run_local_server(port=0)

    output = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
    }
    # stdout に出力（user が gcloud secrets ... | で受け取る）
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# get_gmail_service: Secret Manager から creds を取得して Gmail service を返す
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def get_gmail_service(
    scopes: tuple[str, ...] = _DEFAULT_SCOPES,
    secret_name: str = "momijian-gmail-oauth-creds",
    project_id: str = _DEFAULT_PROJECT_ID,
) -> "googleapiclient.discovery.Resource":
    """Secret Manager から refresh_token JSON を取得 → Credentials を構築 → Gmail API service を返す。

    Secret JSON 形式:
        {"client_id": "...", "client_secret": "...", "refresh_token": "..."}

    キャッシュキーは (scopes, secret_name, project_id)。同 scope での再取得は無料。

    Raises:
        RuntimeError: refresh_token 失効時（再認証手順を案内するメッセージ付き）
        RuntimeError: Secret Manager 取得失敗時
        ImportError: google-api-python-client が未インストール時

    使い方:
        from momijian_common.auth.gmail_oauth import get_gmail_service
        svc = get_gmail_service()
        profile = svc.users().getProfile(userId="me").execute()
        # → {"emailAddress": "office@momijian.co", ...}
    """
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.exceptions import RefreshError  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as e:
        raise ImportError(
            f"必要なライブラリが不足しています: {e}\n"
            "pip install google-api-python-client google-auth を実行してください。"
        ) from e

    # Secret Manager から JSON 取得
    secret_json_str = _get_secret_fn(secret_name, project_id)
    try:
        creds_data = json.loads(secret_json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Secret '{secret_name}' の JSON パースに失敗しました: {e}\n"
            "Secret Manager に登録されている値を確認してください。"
        ) from e

    # Credentials を構築（refresh_token のみで build 可）
    try:
        creds = Credentials.from_authorized_user_info(
            {
                "client_id": creds_data["client_id"],
                "client_secret": creds_data["client_secret"],
                "refresh_token": creds_data["refresh_token"],
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": list(scopes),
            }
        )
    except KeyError as e:
        raise RuntimeError(
            f"Secret '{secret_name}' に必要なキーが見つかりません: {e}\n"
            "Secret JSON には client_id / client_secret / refresh_token が必要です。"
        ) from e

    # サービス構築（token が有効かは API コール時に検証される）
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except RefreshError as e:
        raise RuntimeError(
            "Gmail OAuth2 の refresh_token が失効しています。\n"
            "以下の手順で再認証してください:\n"
            "\n"
            "1. GCP Console で client_secret.json をダウンロード\n"
            "   https://console.cloud.google.com/apis/credentials?project=pdf-automation-487602\n"
            "\n"
            "2. 再認証を実行:\n"
            "   python -m momijian_common.auth.gmail_oauth setup ~/Downloads/client_secret_*.json\n"
            "\n"
            "3. 出力 JSON を Secret Manager に登録:\n"
            "   ... | gcloud secrets versions add momijian-gmail-oauth-creds \\\n"
            "         --data-file=- --project=pdf-automation-487602\n"
        ) from e

    return service


# ---------------------------------------------------------------------------
# get_dwd_gmail_service: DWD impersonation で任意ユーザーの Gmail service を返す
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=32)
def get_dwd_gmail_service(
    impersonate_email: str,
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.send",),
    sa_email: str | None = None,
) -> "googleapiclient.discovery.Resource":
    """DWD (Domain-Wide Delegation) で指定ユーザーに impersonate した Gmail service を返す。

    aoi/services/gmail_service.py の _get_gmail_service と同じ IAM Signer パターン。
    Cloud Run の ADC (compute_engine.Credentials) は with_subject 不可のため、
    IAM Credentials API 経由で SA → user の委任 JWT に署名する。

    Args:
        impersonate_email: 委任先ユーザーの email (例: takada@momijian.co)
        scopes: Gmail スコープ。送信用途は gmail.send のみで十分。
            キャッシュキーは tuple 型であること（list 不可）。
        sa_email: SA email。省略時は環境変数 SERVICE_ACCOUNT_EMAIL から取得。
            それも未設定なら ADC の signer_email から自動解決する。

    Raises:
        ValueError: impersonate_email が空の場合
        google.auth.exceptions.RefreshError: DWD 設定不備 / scope 未登録時。
            Admin Console > セキュリティ > API制御 > ドメイン全体の委任 で
            SA Client ID に対象 scope が登録されているか確認してください。

    Notes:
        - lru_cache(maxsize=32): 17名 + バッファ分をキャッシュ
        - キャッシュキーは (impersonate_email, scopes, sa_email) の3-tuple
    """
    if not impersonate_email:
        raise ValueError("impersonate_email は空にできません")

    try:
        from google.auth import default as google_auth_default, iam
        from google.auth.transport import requests as google_requests
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as _build
    except ImportError as e:
        raise ImportError(
            f"必要なライブラリが不足しています: {e}\n"
            "pip install google-api-python-client google-auth を実行してください。"
        ) from e

    # SA email の解決
    resolved_sa_email = sa_email or os.environ.get("SERVICE_ACCOUNT_EMAIL", "")

    # ADC から基本認証情報を取得
    source_credentials, _ = google_auth_default()

    # SA email が未設定の場合は ADC の signer_email から解決を試みる
    if not resolved_sa_email:
        resolved_sa_email = getattr(source_credentials, "service_account_email", "")
    if not resolved_sa_email:
        raise ValueError(
            "SA email を解決できません。環境変数 SERVICE_ACCOUNT_EMAIL を設定するか、"
            "sa_email 引数を指定してください。"
        )

    # IAM signer を使って SA として JWT に署名できるようにする
    signer = iam.Signer(
        request=google_requests.Request(),
        credentials=source_credentials,
        service_account_email=resolved_sa_email,
    )

    # DWD 用の SA 認証情報を構築（subject = 委任先ユーザー）
    credentials = service_account.Credentials(
        signer=signer,
        service_account_email=resolved_sa_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=list(scopes),
        subject=impersonate_email,
    )

    return _build("gmail", "v1", credentials=credentials, cache_discovery=False)


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python -m momijian_common.auth.gmail_oauth setup <path/to/client_secret.json>",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "setup":
        if len(sys.argv) < 3:
            print("ERROR: setup コマンドには client_secret.json のパスが必要です。", file=sys.stderr)
            print(
                "Usage: python -m momijian_common.auth.gmail_oauth setup <path/to/client_secret.json>",
                file=sys.stderr,
            )
            sys.exit(1)
        setup_flow(sys.argv[2])
    else:
        print(f"ERROR: 不明なコマンド: {cmd}", file=sys.stderr)
        print(
            "Usage: python -m momijian_common.auth.gmail_oauth setup <path/to/client_secret.json>",
            file=sys.stderr,
        )
        sys.exit(1)
