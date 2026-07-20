from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from faircom_mcp.config import AppConfig, AuthConfig
from faircom_mcp.errors import ConfigurationError, TransportError, UpstreamAPIError

IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}


class FaircomAPIClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth: AuthConfig,
        tls_verify: bool = True,
        timeout_seconds: float = 10.0,
        max_read_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_read_retries = max(0, max_read_retries)
        client_auth, token_header = self._build_auth(auth)

        headers: dict[str, str] = {"Accept": "application/json"}
        if token_header is not None:
            headers["Authorization"] = token_header

        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            verify=tls_verify,
            auth=client_auth,
            transport=transport,
            headers=headers,
        )

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        timeout_seconds: float = 10.0,
        max_read_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> FaircomAPIClient:
        return cls(
            base_url=config.faircom_api_base_url,
            auth=config.auth,
            tls_verify=config.tls_verify,
            timeout_seconds=timeout_seconds,
            max_read_retries=max_read_retries,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FaircomAPIClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        return self._request_json_internal(
            method,
            path,
            params=params,
            json_body=json_body,
            idempotent=idempotent,
        )

    def _request_json_internal(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        method_upper = method.upper()
        can_retry = self._is_retryable_read(method_upper, idempotent=idempotent)
        attempts = self._max_read_retries + 1 if can_retry else 1

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(
                    method_upper,
                    path,
                    params=dict(params) if params is not None else None,
                    json=dict(json_body) if json_body is not None else None,
                )
            except httpx.TimeoutException as exc:
                if can_retry and attempt < attempts:
                    continue
                raise TransportError(
                    "FairCom API request timed out",
                    details={"method": method_upper, "path": path},
                    retryable=can_retry,
                ) from exc
            except httpx.RequestError as exc:
                if can_retry and attempt < attempts:
                    continue
                raise TransportError(
                    "FairCom API request failed",
                    details={"method": method_upper, "path": path, "error": str(exc)},
                    retryable=can_retry,
                ) from exc

            if response.status_code >= 500 and can_retry and attempt < attempts:
                continue

            if response.status_code >= 400:
                raise UpstreamAPIError(
                    "FairCom API returned an error",
                    details={
                        "method": method_upper,
                        "path": path,
                        "status_code": response.status_code,
                        "body": response.text[:2000],
                    },
                    retryable=response.status_code >= 500,
                )

            try:
                return response.json()
            except ValueError as exc:
                raise UpstreamAPIError(
                    "FairCom API returned non-JSON response",
                    details={
                        "method": method_upper,
                        "path": path,
                        "status_code": response.status_code,
                    },
                    retryable=False,
                ) from exc

        raise UpstreamAPIError(
            "FairCom API request exhausted retry attempts",
            details={"method": method_upper, "path": path},
            retryable=False,
        )

    def post_action(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        path: str = "/api/v1/action",
    ) -> Any:
        body: dict[str, Any] = {"action": action}
        if payload:
            body["payload"] = dict(payload)
        return self.request_json("POST", path, json_body=body, idempotent=False)

    @staticmethod
    def _is_retryable_read(method: str, *, idempotent: bool | None) -> bool:
        if idempotent is not None:
            return idempotent
        return method in IDEMPOTENT_METHODS

    @staticmethod
    def _build_auth(auth: AuthConfig) -> tuple[httpx.Auth | None, str | None]:
        if auth.token:
            return None, f"Bearer {auth.token}"

        if auth.username and auth.password:
            return httpx.BasicAuth(auth.username, auth.password), None

        raise ConfigurationError(
            "Auth configuration is missing required credentials",
        )


def create_client(config: AppConfig) -> FaircomAPIClient:
    return FaircomAPIClient.from_config(config)
