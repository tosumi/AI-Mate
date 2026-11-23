"""構成情報と設定値のテスト(本体仕様書 §11.5.9、§12)。"""

import pytest
from pydantic import ValidationError

from mycompanion.config import (
    AuthConfig,
    AzureOpenAIConfig,
    ConfigError,
    EngineSettings,
    PostgresConfig,
)


def test_missing_variables_are_reported_by_name_only():
    env = {"POSTGRES_HOST": "db.example.invalid", "POSTGRES_DATABASE": " "}
    with pytest.raises(ConfigError) as info:
        PostgresConfig.from_env(env)
    message = str(info.value)
    # 欠けている変数名は出すが、設定済みの値は出さない
    assert "POSTGRES_DATABASE" in message and "POSTGRES_USER" in message
    assert "db.example.invalid" not in message


def test_postgres_defaults():
    config = PostgresConfig.from_env(
        {"POSTGRES_HOST": "h", "POSTGRES_DATABASE": "d", "POSTGRES_USER": "u"}
    )
    assert (config.port, config.sslmode) == (5432, "require")


def test_openai_deployment_missing_role():
    config = AzureOpenAIConfig.from_env(
        {"AZURE_OPENAI_ENDPOINT": "https://x.example.invalid/", "AZURE_OPENAI_DEPLOYMENT_EMOTION": "emo"}
    )
    assert config.deployment("emotion") == "emo"
    with pytest.raises(ConfigError, match="AZURE_OPENAI_DEPLOYMENT_DRAFT"):
        config.deployment("draft")


def test_openai_model_id_uses_deployed_model():
    # 記録のモデル名は、デプロイ名に対応するモデル名と版(Bicep から渡す)。分からなければデプロイ名だけ
    config = AzureOpenAIConfig.from_env(
        {
            "AZURE_OPENAI_ENDPOINT": "https://x.example.invalid/",
            "AZURE_OPENAI_DEPLOYMENT_DRAFT": "gpt-6-2-sol",
            "AZURE_OPENAI_DEPLOYMENT_EMOTION": "mini",
            "AZURE_OPENAI_MODELS": "gpt-6-2-sol=gpt-6.2-sol:2026-11-17, broken, =x,other=gpt-6.1-sol:2026-09-29",
        }
    )
    assert config.model_id("draft") == "gpt-6.2-sol:2026-11-17@gpt-6-2-sol"
    assert config.model_id("emotion") == "mini"
    assert set(config.models) == {"gpt-6-2-sol", "other"}


def test_auth_scope_name_and_metadata_url():
    config = AuthConfig(
        tenant_id="tid",
        authority="https://sub.ciamlogin.com/",
        audience="aud",
        scope="api://aud/companion.access",
    )
    assert config.scope_name == "companion.access"
    assert config.metadata_url == "https://sub.ciamlogin.com/tid/v2.0/.well-known/openid-configuration"


def test_engine_settings_defaults_match_spec():
    s = EngineSettings()
    assert s.silence_max_consecutive == 2
    assert s.memory_medium_retention_days == 90
    assert s.embedding_dimensions == 1536
    assert s.safety_exempt_hours == 72
    assert s.topic_initiative_threshold == 0.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"silence_max_consecutive": 3},  # R1: 上限は2
        {"memory_medium_retention_days": 44},  # 45日未満は不可
        {"llm_data_residency": "global"},  # 方針B: global は禁止
        {"memory_consolidation_time": "25:00"},
        {"unknown_key": 1},
    ],
)
def test_engine_settings_reject_invalid_values(overrides):
    with pytest.raises(ValidationError):
        EngineSettings(**overrides)
