import pytest

from faircom_mcp.config import load_config
from faircom_mcp.errors import ConfigurationError


def test_load_config_accepts_token_auth() -> None:
    config = load_config(
        {
            "FAIRCOM_API_BASE_URL": "https://example.test/api",
            "FAIRCOM_API_TOKEN": "abc123",
        }
    )

    assert config.faircom_api_base_url == "https://example.test/api"
    assert config.auth.token == "abc123"
    assert config.auth.username is None
    assert config.tls_verify is True


def test_load_config_accepts_username_password_auth() -> None:
    config = load_config(
        {
            "FAIRCOM_API_BASE_URL": "https://example.test",
            "FAIRCOM_API_USERNAME": "user",
            "FAIRCOM_API_PASSWORD": "pass",
            "FAIRCOM_TLS_VERIFY": "false",
            "FAIRCOM_HTTP_PORT": "9001",
            "FAIRCOM_SQL_ALLOWLIST": "SELECT,WITH",
            "FAIRCOM_SQL_DENYLIST": "DROP,TRUNCATE",
        }
    )

    assert config.auth.username == "user"
    assert config.auth.password == "pass"
    assert config.auth.token is None
    assert config.tls_verify is False
    assert config.transport.port == 9001
    assert config.security.sql_allowlist == ("SELECT", "WITH")
    assert config.security.sql_denylist == ("DROP", "TRUNCATE")


def test_load_config_fails_without_base_url() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_config(
            {
                "FAIRCOM_API_TOKEN": "abc123",
            }
        )

    assert exc.value.details["name"] == "FAIRCOM_API_BASE_URL"


def test_load_config_fails_without_auth() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_config(
            {
                "FAIRCOM_API_BASE_URL": "https://example.test",
            }
        )

    assert "FAIRCOM_API_TOKEN" in str(exc.value)


def test_load_config_fails_with_invalid_port() -> None:
    with pytest.raises(ConfigurationError):
        load_config(
            {
                "FAIRCOM_API_BASE_URL": "https://example.test",
                "FAIRCOM_API_TOKEN": "abc123",
                "FAIRCOM_HTTP_PORT": "99999",
            }
        )


def test_load_config_uses_secure_tls_default() -> None:
    config = load_config(
        {
            "FAIRCOM_API_BASE_URL": "https://example.test",
            "FAIRCOM_API_TOKEN": "abc123",
        }
    )

    assert config.tls_verify is True
