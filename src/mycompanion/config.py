"""構成情報(環境変数)と、エンジンの設定値(既定値)。

- 環境ごとの値(Azure のアカウント、リソース名、エンドポイントなど)は、コードに書かず、
  環境変数から読む(本体仕様書 §11.5.9)。変数名の一覧は `.env.example` を正とする。
- エンジンの設定値と既定値は、本体仕様書 §12「設定値の既定」に対応する。
- 必須の変数が欠けている場合は、変数名だけを示してエラーにする(値はログに出さない)。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigError(RuntimeError):
    """構成情報の不足・不正。メッセージには変数名だけを含め、値は含めない。"""


def _get(env: Mapping[str, str], name: str) -> str | None:
    """環境変数を読む。空文字は未設定として扱う。"""
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require(env: Mapping[str, str], names: list[str], component: str) -> dict[str, str]:
    """必須の環境変数をまとめて読む。欠けているものは、変数名を列挙してエラーにする。"""
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = _get(env, name)
        if value is None:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise ConfigError(
            f"{component} に必要な環境変数が設定されていません: {', '.join(missing)}"
            "(.env.example を参照)"
        )
    return values


# ---------------------------------------------------------------------------
# 環境変数(構成情報)
# ---------------------------------------------------------------------------

# LLM の役割(§11.1)と、デプロイ名を持つ環境変数の対応
LLM_ROLES = ("emotion", "planner", "memory_write", "draft", "filter", "consolidation", "embedding")
LlmRole = Literal["emotion", "planner", "memory_write", "draft", "filter", "consolidation", "embedding"]


@dataclass(frozen=True)
class AzureOpenAIConfig:
    """Azure OpenAI の接続先。認証は Entra ID(API キーは使わない)。"""

    endpoint: str
    deployments: Mapping[str, str]  # 役割 → デプロイ名(未設定の役割は含まない)
    # デプロイ名 → モデル名と版(例: gpt-6.2-sol:2026-11-17)。Bicep のデプロイの一覧から作る
    models: Mapping[str, str] = field(default_factory=dict)

    def deployment(self, role: LlmRole) -> str:
        name = self.deployments.get(role)
        if not name:
            raise ConfigError(
                f"LLM の役割 {role} のデプロイ名が設定されていません: "
                f"AZURE_OPENAI_DEPLOYMENT_{role.upper()}"
            )
        return name

    def model_id(self, role: LlmRole) -> str:
        """記録に使うモデルの識別子。`モデル名:版@デプロイ名`。

        モデルが分からないときは、推測した名前を付けず、デプロイ名だけにする
        (コードの既定値を付けると、実際のデプロイと食い違うことがあるため)。
        """
        deployment = self.deployment(role)
        model = self.models.get(deployment)
        return f"{model}@{deployment}" if model else deployment

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AzureOpenAIConfig:
        env = os.environ if env is None else env
        values = _require(env, ["AZURE_OPENAI_ENDPOINT"], "Azure OpenAI")
        deployments = {}
        for role in LLM_ROLES:
            name = _get(env, f"AZURE_OPENAI_DEPLOYMENT_{role.upper()}")
            if name:
                deployments[role] = name
        return cls(
            endpoint=values["AZURE_OPENAI_ENDPOINT"],
            deployments=deployments,
            models=parse_models(_get(env, "AZURE_OPENAI_MODELS") or ""),
        )


def parse_models(value: str) -> dict[str, str]:
    """`デプロイ名=モデル名:版` をカンマで区切った値を読む。形の崩れた項目は捨てる。"""
    models = {}
    for item in value.split(","):
        name, sep, model = item.strip().partition("=")
        if sep and name.strip() and model.strip():
            models[name.strip()] = model.strip()
    return models


@dataclass(frozen=True)
class PostgresConfig:
    """PostgreSQL の接続先。認証は Entra ID のトークン(パスワードは使わない)。"""

    host: str
    port: int
    database: str
    user: str
    sslmode: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PostgresConfig:
        env = os.environ if env is None else env
        values = _require(env, ["POSTGRES_HOST", "POSTGRES_DATABASE", "POSTGRES_USER"], "PostgreSQL")
        port_text = _get(env, "POSTGRES_PORT") or "5432"
        if not port_text.isdigit():
            raise ConfigError("環境変数 POSTGRES_PORT は数値で指定してください")
        return cls(
            host=values["POSTGRES_HOST"],
            port=int(port_text),
            database=values["POSTGRES_DATABASE"],
            user=values["POSTGRES_USER"],
            sslmode=_get(env, "POSTGRES_SSLMODE") or "require",
        )


@dataclass(frozen=True)
class CosmosConfig:
    """Cosmos DB for NoSQL の接続先。認証は Entra ID。"""

    endpoint: str
    database: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CosmosConfig:
        env = os.environ if env is None else env
        values = _require(env, ["COSMOS_ENDPOINT", "COSMOS_DATABASE"], "Cosmos DB")
        return cls(endpoint=values["COSMOS_ENDPOINT"], database=values["COSMOS_DATABASE"])


@dataclass(frozen=True)
class AuthConfig:
    """一般ユーザーの認証(Entra External ID の外部テナント。§13.4)。"""

    tenant_id: str
    authority: str  # https://<サブドメイン>.ciamlogin.com/
    audience: str  # API のアプリ登録
    scope: str  # API が公開するスコープ(完全な形式)

    @property
    def scope_name(self) -> str:
        """トークンの `scp` に入る短い名前(最後の `/` より後ろ)。"""
        return self.scope.rsplit("/", 1)[-1]

    @property
    def metadata_url(self) -> str:
        """OpenID Connect のメタデータの URL。"""
        return f"{self.authority.rstrip('/')}/{self.tenant_id}/v2.0/.well-known/openid-configuration"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthConfig:
        env = os.environ if env is None else env
        values = _require(
            env,
            ["ENTRA_EXTERNAL_TENANT_ID", "ENTRA_EXTERNAL_AUTHORITY", "ENTRA_API_AUDIENCE", "ENTRA_API_SCOPE"],
            "認証(Entra External ID)",
        )
        return cls(
            tenant_id=values["ENTRA_EXTERNAL_TENANT_ID"],
            authority=values["ENTRA_EXTERNAL_AUTHORITY"],
            audience=values["ENTRA_API_AUDIENCE"],
            scope=values["ENTRA_API_SCOPE"],
        )


@dataclass(frozen=True)
class OperatorAuthConfig:
    """運営者の認証(社内向けの Entra ID。今の Azure のテナント。§13.7、§15.1 の M9)。

    一般ユーザーの外部テナントとは別のテナント・別のアプリ。値は環境変数で渡す(コードに書かない)。
    """

    tenant_id: str
    audience: str  # 運営者の API のアプリ登録(アプリ ID の URI か、クライアント ID)
    scope: str  # 画面が要求するスコープ(完全な形式)
    # トークンの amr に "mfa" があることを求める(テナントで MFA を必須にしたうえでの、念のための確認)
    require_mfa_claim: bool = False

    @property
    def scope_name(self) -> str:
        return self.scope.rsplit("/", 1)[-1]

    @property
    def metadata_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0/.well-known/openid-configuration"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OperatorAuthConfig | None:
        """設定がなければ None(運営者の API は使えない)。一部だけ設定されていれば、エラーにする。"""
        env = os.environ if env is None else env
        names = ["OPS_ENTRA_TENANT_ID", "OPS_ENTRA_API_AUDIENCE", "OPS_ENTRA_API_SCOPE"]
        if all(_get(env, name) is None for name in names):
            return None
        values = _require(env, names, "運営者の認証(社内向けの Entra ID)")
        return cls(
            tenant_id=values["OPS_ENTRA_TENANT_ID"],
            audience=values["OPS_ENTRA_API_AUDIENCE"],
            scope=values["OPS_ENTRA_API_SCOPE"],
            require_mfa_claim=(_get(env, "OPS_REQUIRE_MFA_CLAIM") or "").lower() in ("1", "true", "yes"),
        )


# ダッシュボードの対象のリソース(Azure のリソース ID。IaC が環境変数に設定する)。名前 → 環境変数
MONITOR_RESOURCE_ENV = {
    "api": "OPS_MONITOR_API_APP_ID",  # Container Apps(API)
    "worker": "OPS_MONITOR_WORKER_JOB_ID",  # Container Apps のジョブ(ワーカー)
    "postgres": "OPS_MONITOR_POSTGRES_ID",
    "cosmos": "OPS_MONITOR_COSMOS_ID",
    "logs": "OPS_MONITOR_LOG_WORKSPACE_ID",  # Log Analytics
    "openai": "OPS_MONITOR_OPENAI_ID",
}


@dataclass(frozen=True)
class OperatorMonitorConfig:
    """ダッシュボード(Azure Monitor のメトリック。§15.1 の M9)。設定のないリソースは「未設定」と表示する。

    リソース ID はサブスクリプションの ID を含むので、ログ・応答には出さない。
    """

    resources: dict[str, str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OperatorMonitorConfig | None:
        env = os.environ if env is None else env
        resources = {name: value for name, var in MONITOR_RESOURCE_ENV.items() if (value := _get(env, var))}
        for value in resources.values():
            if not value.startswith("/subscriptions/"):
                raise ConfigError("ダッシュボードの対象のリソース ID の形式が正しくありません(OPS_MONITOR_*)")
        return cls(resources) if resources else None


@dataclass(frozen=True)
class CliConfig:
    """CLI(API を呼ぶ薄いクライアント。§11.5.8)。"""

    api_url: str
    client_id: str | None
    authority: str | None
    tenant_id: str | None
    api_scope: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, api_url: str | None = None) -> CliConfig:
        env = os.environ if env is None else env
        url = api_url or _get(env, "MYCOMPANION_API_URL")
        if not url:
            raise ConfigError(
                "API の URL が設定されていません: 環境変数 MYCOMPANION_API_URL、または --api-url で指定してください"
            )
        return cls(
            api_url=url.rstrip("/"),
            client_id=_get(env, "MYCOMPANION_CLI_CLIENT_ID"),
            authority=_get(env, "ENTRA_EXTERNAL_AUTHORITY"),
            tenant_id=_get(env, "ENTRA_EXTERNAL_TENANT_ID"),
            api_scope=_get(env, "ENTRA_API_SCOPE"),
        )


def environment_name(env: Mapping[str, str] | None = None) -> str:
    """実行環境の名前(dev / stg / prod)。ログとメトリクスに付ける。"""
    env = os.environ if env is None else env
    return _get(env, "MYCOMPANION_ENV") or "dev"


# ---------------------------------------------------------------------------
# エンジンの設定値(本体仕様書 §12「設定値の既定」)
# ---------------------------------------------------------------------------


class EngineSettings(BaseModel):
    """エンジンの設定値。既定値は仕様書 §12 の表に対応する。

    安全に関わる上限(無言の連続回数など)は、読み込み時に検証し、違反は起動エラーとする(§8.4.2 R1)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 表示・フィルタ
    debug: bool = False
    filter_mode: Literal["on", "off", "both"] = "on"
    # 応答の作り方(§8.1。2026-11-20 の決定で single を既定にした)。
    # single: 感情推定と Planner のあとに、応答方針を指示に含めて、1回の呼び出しで応答を作る(感情フィルタを通さない。
    #         危機フローだけは、安全のため感情フィルタを通す)。
    # two_stage: 下書き(感情の情報なし。投機実行)→ 感情フィルタ。検証・デバッグ用に残す(ターンごとに選べる)
    compose_mode: Literal["single", "two_stage"] = "single"

    # 無言(§8.4)
    silence_enabled: bool = True
    silence_render: Literal["dots", "none"] = "dots"
    silence_max_consecutive: int = Field(default=2, ge=0, le=2)  # 上限2固定(R1)
    anger_no_silence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    speculative_draft: bool = True
    # 応答モードを確率的に決めるか。False は決定論モード(しきい値のみ)。True のときは乱数シードを保存する
    silence_gate_stochastic: bool = False

    # 関係値・同期(§6.2、§6.3.7)
    sync_max: float = Field(default=0.5, ge=0.0, le=1.0)
    sync_curve_p: float = Field(default=1.0, gt=0.0)
    relationship_delta_max_per_turn: float = Field(default=0.5, ge=0.0)
    relationship_delta_max_per_session: float = Field(default=2.0, ge=0.0)
    relationship_decay_enabled: bool = False
    relationship_modulation_max: float = Field(default=0.6, ge=0.0, le=1.0)

    # イベントルール(§7.1 の④)
    event_rules_enabled: bool = True  # false なら、第4の項を計算しない(切り戻し用)
    event_act_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)  # 発話行為を発火させる確信度の下限
    event_saturation_decay: float = Field(default=0.5, ge=0.0, le=1.0)  # κ。同じルールの連続発火の逓減率
    event_saturation_reset_turns: int = Field(default=6, ge=1)  # この間発火しなければ、逓減の段数を戻す
    event_delta_max_per_turn: float = Field(default=0.25, ge=0.0, le=1.0)  # 1ターン・感情ごとの上限
    event_delta_max_per_session: float = Field(default=0.6, ge=0.0)  # 1セッション・感情ごとの累計の上限
    session_gap_min_days_floor: int = Field(default=3, ge=1)  # 「久しぶり」の日数の下限(ペルソナで下げられない)
    negative_act_no_silence_turns: int = Field(default=1, ge=1)  # R9: 敵意を受けたターンから、無言にしないターン数

    # 好き嫌いシート由来の話題ルール(§7.1 の③。全キャラクタ共通)
    sheet_rules_enabled: bool = True  # false なら、シート由来のルールを評価しない(切り戻し用)
    sheet_relevance_min: float = Field(default=0.5, ge=0.0, le=1.0)  # 発火させる関連度の下限
    sheet_like_polarity_min: float = Field(default=0.3, ge=-1.0, le=1.0)  # 好き: 文脈の極性の下限
    sheet_risk_threshold: float = Field(default=0.5, ge=0.0, le=1.0)  # 好き: 性的・暴力のリスクがこれ以上なら止める
    dislike_max_per_session: int = Field(default=5, ge=1)  # 嫌いなもの: 1セッションの発火の回数
    dislike_max_rise_per_session: float = Field(default=0.5, ge=0.0, le=1.0)  # 嫌いなもの: 感情ごとの累計
    disliked_activity_max_rise_per_session: float = Field(default=0.8, ge=0.0, le=1.0)  # 嫌いなこと: 同
    avoid_threshold: float = Field(default=0.5, ge=0.0, le=1.0)  # 回避フラグを立てる、感情の値
    avoid_rearm_below: float = Field(default=0.3, ge=0.0, le=1.0)  # これを下回るまで、再び立てない
    steer_away_max_per_session: int = Field(default=1, ge=0)  # 話題を変える回数(1セッション)

    # トピック別の時系列記憶(§9.13)
    topic_memory_enabled: bool = True  # false なら、トピックを作らず、使わない(切り戻し用)
    topic_candidate_k: int = Field(default=5, ge=1)  # 記憶1件につき、照合の候補にする既存のトピックの数
    topic_establish_min_days: int = Field(default=3, ge=1)  # 確立: 異なる日に現れた回数(昇格と同じ)
    topic_entry_min_grounding: float = Field(default=0.35, ge=0.0, le=1.0)  # 項目と根拠の記憶の、類似度の下限
    # 発話とトピックが「合う」類似度。実測(text-embedding-3-large): 関係のある話題 0.46、関係のない話題 0.24 以下
    topic_query_min_similarity: float = Field(default=0.40, ge=0.0, le=1.0)
    topic_timeline_max_entries: int = Field(default=5, ge=1)  # プロンプトに入れる項目の数
    topic_timeline_char_budget: int = Field(default=600, ge=100)  # 同、文字数(約400トークン)
    topic_dormant_days: int = Field(default=180, ge=1)  # 最後の項目から、休止にするまでの日数
    # 統合: 名前・別名・説明の埋め込みが、これ以上近い組を候補にして、LLM が同じかを判定する(日次整理のたび)
    topic_merge_similarity: float = Field(default=0.88, ge=0.0, le=1.0)
    topic_merge_max_pairs: int = Field(default=3, ge=0)  # 1回の整理で、判定する組の数
    # 予定の「その後どうなった?」(フォローアップ)。対象は、時期が過ぎて、結果を聞いていない予定だけ
    topic_followup_enabled: bool = True
    topic_followup_window_days: int = Field(default=14, ge=1)  # 予定の時期が過ぎてから、聞いてよい期間
    topic_followup_cooldown_days: int = Field(default=14, ge=0)  # 同じトピックで、次に聞くまでの間隔
    topic_followup_max_per_session: int = Field(default=1, ge=0)
    topic_followup_weight: float = Field(default=0.3, ge=0.0)  # b6: 経過の適合(過ぎてからの日数が短いほど高い)

    # 関係をたどる連想(§9.13 の第3段階)
    association_enabled: bool = True  # false なら、キャラクタは連想で話題を移さない(切り戻し用)
    association_weight: float = Field(default=0.5, ge=0.0)  # b7: 話題のスコアへの、連想の強さの重み

    # 個人情報(§11.3、§13.3)
    pii_mask_contacts: bool = True  # 電話番号・メールアドレス・郵便番号を、LLM に送る前に置き換える
    pii_mask_banchi: bool = True  # 番地まで書かれた住所の、番地以降を置き換える(町名までは残す)
    audit_log_retention_days: int = Field(default=365, ge=1)  # 監査ログの保持期間(法務の確認で変わりうる)

    # 記憶の整理(§9)
    memory_consolidation_time: str = "04:00"
    memory_scheduler_scan_interval_minutes: int = Field(default=60, ge=1)
    memory_catchup_max_days: int = Field(default=31, ge=1)
    session_idle_timeout_minutes: int = Field(default=30, ge=1)
    # 画面を開いたときに表示する、これまでの会話のターン数(終わった会話も含む。2026-11-23 のユーザーの指示)
    conversation_restore_turns: int = Field(default=20, ge=1, le=100)
    memory_monthly_summary_enabled: bool = True
    memory_monthly_day: int = Field(default=1, ge=1, le=28)
    memory_medium_retention_days: int = Field(default=90, ge=45)  # 45日未満は設定不可
    memory_promotion_importance: float = Field(default=0.8, ge=0.0, le=1.0)
    memory_promotion_min_days: int = Field(default=3, ge=1)
    memory_promotion_min_refs: int = Field(default=3, ge=1)
    turns_retention_days: int = Field(default=30, ge=1)

    # LLM のモデルは、Bicep のデプロイで決め、デプロイ名とモデル名・版を環境変数で渡す(§11.1、§11.5.9)。
    # 記録に使う名前は AzureOpenAIConfig.model_id。ここには持たない(実際のデプロイと食い違うため)
    # 埋め込み(§11.6)。検索で比べてよい埋め込みかの判定に使う
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = Field(default=1536, ge=1)
    llm_data_residency: Literal["apac_data_zone"] = "apac_data_zone"  # global は禁止(方針B)

    # 認証・入力の制限(§13.4)
    access_token_ttl_minutes: int = Field(default=30, ge=1)
    link_code_ttl_minutes: int = Field(default=10, ge=1)
    link_code_max_attempts: int = Field(default=5, ge=1)
    link_code_issue_limit_per_hour: int = Field(default=5, ge=1)
    max_input_chars: int = Field(default=2000, ge=1)
    rate_limit_turns_per_minute: int = Field(default=10, ge=1)
    rate_limit_turns_per_day: int = Field(default=300, ge=1)

    # スクリーニング(§13.5)
    screening_mode: Literal["annotate_first"] = "annotate_first"
    throttle_serious_events_per_day: int = Field(default=3, ge=1)
    throttle_duration_minutes: int = Field(default=60, ge=1)
    security_events_retention_days: int = Field(default=90, ge=1)
    age_flag_default_under_18: bool = True

    # 人格への要求(§8.8)
    persona_request_count_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    persona_pressure_window_hours: int = Field(default=24, ge=1)
    persona_pressure_notice_threshold: int = Field(default=5, ge=1)
    persona_pressure_cooldown_threshold: int = Field(default=3, ge=1)
    persona_cooldown_minutes: int = Field(default=10, ge=1)
    default_adaptability: float = Field(default=0.5, ge=0.0, le=1.0)

    # 想起・再活性化(§9.9、§9.10)
    recall_activation_half_life_days: float = Field(default=14, gt=0)
    recall_weight_user: float = Field(default=1.0, ge=0.0)
    recall_weight_character: float = Field(default=0.5, ge=0.0)
    recall_character_requires_engagement: bool = True
    activation_preference_gain: float = Field(default=0.5, ge=0.0)
    memory_preference_weight: float = Field(default=0.2, ge=0.0)  # λ5
    memory_activation_weight: float = Field(default=0.2, ge=0.0)  # λ6
    reactivation_threshold_medium: float = 1.5
    reactivation_threshold_long: float = 2.2
    reactivation_daily_cap: int = Field(default=5, ge=0)
    reactivation_cooldown_days: int = Field(default=7, ge=0)
    working_set_size: int = Field(default=5, ge=0)
    working_set_ttl_turns: int = Field(default=10, ge=1)

    # 話題選択(§8.7)
    topic_selection_temperature: float = Field(default=0.7, gt=0.0)
    proactive_mention_max_per_session: int = Field(default=2, ge=0)
    proactive_mention_cooldown_days: int = Field(default=7, ge=0)
    topic_initiative_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # 無入力時の自発的な発話(§8.6、§15.1 の M8)。ユーザー設定 proactive_enabled(既定オフ)のときだけ動く
    proactive_enabled: bool = True  # false なら、全体で止める(切り戻し用)
    proactive_quiet_start: str = "21:00"  # 静穏時間帯の始まり(ユーザーのローカル時刻)
    proactive_quiet_end: str = "09:00"  # 静穏時間帯の終わり
    # 間隔 = 基準 × (2 − initiative) × 関係値の補正。2026-11-21 に 24 → 12 時間(ユーザーの決定。全体を50%に)
    proactive_base_hours: float = Field(default=12.0, gt=0.0)
    # 同じローカル日の2回目以降(その日すでに話しかけたあと)の間隔 = 基準 × (2 − initiative)。関係値の補正はしない
    # (2026-11-21 のユーザーの決定。案A。ユーザーから始めた会話のあとも、こちらを使う)
    proactive_repeat_base_hours: float = Field(default=3.0, gt=0.0)
    proactive_relationship_boost: float = Field(default=0.2, ge=0.0, lt=1.0)  # 関係値 +20 で間隔を短くする割合
    # 1日の上限は、キャラクタごと。自発性で1〜3回(下限 + round((上限 − 下限) × initiative))。2026-11-21 のユーザーの決定
    proactive_daily_min: int = Field(default=1, ge=0)
    proactive_daily_max: int = Field(default=3, ge=0)
    proactive_max_unanswered: int = Field(default=1, ge=1)  # 返事のない自発発話の連続の上限(追撃の上限)
    proactive_crisis_hold_hours: int = Field(default=72, ge=0)  # 危機の判定のあと、話しかけない時間
    proactive_reply_window_hours: int = Field(default=48, ge=1)  # 返事を待つあいだ、セッションを閉じない時間
    proactive_negative_valence: float = Field(default=-0.5, le=0.0)  # 最後の気分がこれ以下なら話しかけない
    proactive_anger_level: float = Field(default=0.5, ge=0.0, le=1.0)  # 最後の怒りがこれ以上なら話しかけない
    # 負の懸念の「その後」(topic-timeline-memory.md の Q1 の案 b。自発発話でだけ)
    proactive_concern_enabled: bool = True
    proactive_concern_min_mentions: int = Field(default=2, ge=1)  # ユーザーが自分から話した回数(日数)
    proactive_concern_after_days: int = Field(default=14, ge=1)  # 最後に話してから、聞くまでの日数
    proactive_concern_window_days: int = Field(default=30, ge=1)  # これより古い懸念は、掘り返さない
    # 類似による連想(Q16 を自発発話でだけ解除)
    proactive_similar_enabled: bool = True
    proactive_similar_min: float = Field(default=0.6, ge=0.0, le=1.0)  # 埋め込みの類似度の下限

    # baseline・安全(§4.2)
    baseline_min_sessions: int = Field(default=3, ge=1)
    baseline_min_days: int = Field(default=7, ge=1)
    safety_exempt_hours: int = Field(default=72, ge=1)

    @model_validator(mode="after")
    def _check_consolidation_time(self) -> EngineSettings:
        # 「HH:MM」の形式であることを確認する
        for name in ("memory_consolidation_time", "proactive_quiet_start", "proactive_quiet_end"):
            parts = str(getattr(self, name)).split(":")
            ok = len(parts) == 2 and all(p.isdigit() for p in parts) and int(parts[0]) < 24 and int(parts[1]) < 60
            if not ok:
                raise ValueError(f"{name} は HH:MM の形式で指定してください")
        return self
