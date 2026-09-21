import pytest

from app.config import Settings, _check_placeholder_secrets


def test_placeholder_secret_key_warns_in_development(caplog):
    settings = Settings(environment="development", secret_key="change-me")
    with caplog.at_level("WARNING"):
        _check_placeholder_secrets(settings)
    assert "SECRET_KEY" in caplog.text


def test_placeholder_secret_key_aborts_in_production():
    settings = Settings(environment="production", secret_key="change-me")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _check_placeholder_secrets(settings)


def test_real_secret_key_starts_clean_in_production():
    settings = Settings(
        environment="production",
        secret_key="a-real-random-value",
        default_admin_password="a-real-strong-password",
        s3_enabled=False,
    )
    _check_placeholder_secrets(settings)  # must not raise
