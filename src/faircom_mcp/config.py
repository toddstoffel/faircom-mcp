from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from os import environ
from urllib.parse import urlparse

from faircom_mcp.errors import ConfigurationError
from faircom_mcp.security import SqlStatementPolicy, ToolGroupPolicy


def _parse_bool(value: str | bool | None, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ConfigurationError(
        "Invalid boolean value",
        details={"value": value},
    )


def _parse_csv_values(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()

    values = tuple(item.strip().upper() for item in raw_value.split(",") if item.strip())
    return values


@dataclass(slots=True)
class AuthConfig:
    username: str | None = None
    password: str | None = None
    token: str | None = None


@dataclass(slots=True)
class TransportConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(slots=True)
class SecurityConfig:
    sql_allowlist: tuple[str, ...] = ()
    sql_denylist: tuple[str, ...] = ()
    tool_group_allowlist: tuple[str, ...] = (
        "metadata",
        "query",
        "write",
        "admin",
        "diagnostics",
    )
    diagnostics_token: str | None = None
    diagnostics_enabled: bool = False

    def to_sql_policy(self) -> SqlStatementPolicy:
        return SqlStatementPolicy(
            allowlist=self.sql_allowlist,
            denylist=self.sql_denylist,
        )

    def to_tool_group_policy(self) -> ToolGroupPolicy:
        return ToolGroupPolicy(allowlist=self.tool_group_allowlist)


@dataclass(slots=True)
class ObservabilityConfig:
    enable_metrics: bool = True
    enable_tracing: bool = False


@dataclass(slots=True)
class AppConfig:
    faircom_api_base_url: str
    auth: AuthConfig
    transport: TransportConfig
    security: SecurityConfig = field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    tls_verify: bool = True


def _require_base_url(raw_value: str | None) -> str:
    if not raw_value:
        raise ConfigurationError(
            "Missing required environment variable",
            details={"name": "FAIRCOM_API_BASE_URL"},
        )

    parsed = urlparse(raw_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            "FAIRCOM_API_BASE_URL must be a valid HTTP/HTTPS URL",
            details={"value": raw_value},
        )

    return raw_value.rstrip("/")


def _build_auth_config(env: Mapping[str, str]) -> AuthConfig:
    token = env.get("FAIRCOM_API_TOKEN")
    username = env.get("FAIRCOM_API_USERNAME")
    password = env.get("FAIRCOM_API_PASSWORD")

    if token:
        return AuthConfig(token=token)

    if username and password:
        return AuthConfig(username=username, password=password)

    raise ConfigurationError(
        "Provide FAIRCOM_API_TOKEN or both FAIRCOM_API_USERNAME and FAIRCOM_API_PASSWORD",
    )


def _parse_port(raw_port: str | None) -> int:
    if not raw_port:
        return 8000

    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigurationError(
            "FAIRCOM_HTTP_PORT must be an integer",
            details={"value": raw_port},
        ) from exc

    if port < 1 or port > 65535:
        raise ConfigurationError(
            "FAIRCOM_HTTP_PORT must be between 1 and 65535",
            details={"value": port},
        )

    return port


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    env_values = env or environ

    base_url = _require_base_url(env_values.get("FAIRCOM_API_BASE_URL"))
    auth = _build_auth_config(env_values)

    host = env_values.get("FAIRCOM_HTTP_HOST", "127.0.0.1")
    port = _parse_port(env_values.get("FAIRCOM_HTTP_PORT"))
    tls_verify = _parse_bool(env_values.get("FAIRCOM_TLS_VERIFY"), default=True)
    security = SecurityConfig(
        sql_allowlist=_parse_csv_values(env_values.get("FAIRCOM_SQL_ALLOWLIST")),
        sql_denylist=_parse_csv_values(env_values.get("FAIRCOM_SQL_DENYLIST")),
        tool_group_allowlist=tuple(
            value.lower()
            for value in _parse_csv_values(env_values.get("FAIRCOM_TOOL_GROUP_ALLOWLIST"))
        )
        or SecurityConfig().tool_group_allowlist,
        diagnostics_token=env_values.get("FAIRCOM_DIAGNOSTICS_TOKEN"),
        diagnostics_enabled=_parse_bool(
            env_values.get("FAIRCOM_ENABLE_DIAGNOSTICS_UI"),
            default=False,
        ),
    )
    observability = ObservabilityConfig(
        enable_metrics=_parse_bool(
            env_values.get("FAIRCOM_ENABLE_METRICS"),
            default=True,
        ),
        enable_tracing=_parse_bool(
            env_values.get("FAIRCOM_ENABLE_TRACING"),
            default=False,
        ),
    )

    return AppConfig(
        faircom_api_base_url=base_url,
        auth=auth,
        transport=TransportConfig(host=host, port=port),
        security=security,
        observability=observability,
        tls_verify=tls_verify,
    )
