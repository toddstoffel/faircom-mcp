from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
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
        self._auth = auth
        self._session_lock = Lock()
        self._session_auth_token: str | None = auth.token
        client_auth, token_header = self._build_http_auth(auth)

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
                payload = response.json()
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

            self._raise_if_api_error(payload, method=method_upper, path=path)
            return payload

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
        body: dict[str, Any] = {
            "api": "db",
            "action": action,
        }
        if payload:
            body["params"] = dict(payload)
        auth_token = self._get_action_auth_token()
        if auth_token:
            body["authToken"] = auth_token
        return self.request_json("POST", path, json_body=body, idempotent=False)

    @staticmethod
    def _is_retryable_read(method: str, *, idempotent: bool | None) -> bool:
        if idempotent is not None:
            return idempotent
        return method in IDEMPOTENT_METHODS

    @staticmethod
    def _build_http_auth(auth: AuthConfig) -> tuple[httpx.Auth | None, str | None]:
        if auth.token:
            return None, None

        if auth.username and auth.password:
            return None, None

        raise ConfigurationError(
            "Auth configuration is missing required credentials",
        )

    def _get_action_auth_token(self) -> str:
        if self._session_auth_token:
            return self._session_auth_token

        # Session creation is required for username/password mode.
        with self._session_lock:
            if self._session_auth_token:
                return self._session_auth_token

            if not self._auth.username or not self._auth.password:
                raise ConfigurationError(
                    "Auth configuration is missing required credentials",
                )

            payload = self.request_json(
                "POST",
                "/api/v1/action",
                json_body={
                    "api": "admin",
                    "action": "createSession",
                    "params": {
                        "username": self._auth.username,
                        "password": self._auth.password,
                        "defaultApi": "db",
                        "defaultDebug": "none",
                    },
                },
                idempotent=False,
            )
            token = payload.get("authToken") if isinstance(payload, dict) else None
            if not token:
                raise UpstreamAPIError(
                    "FairCom createSession did not return authToken",
                    details={"path": "/api/v1/action", "action": "createSession"},
                    retryable=False,
                )
            self._session_auth_token = str(token)
            return self._session_auth_token

    @staticmethod
    def _raise_if_api_error(payload: Any, *, method: str, path: str) -> None:
        if not isinstance(payload, dict):
            return

        error_code = payload.get("errorCode")
        if not isinstance(error_code, int):
            return
        if error_code == 0:
            return

        raise UpstreamAPIError(
            "FairCom API returned an application error",
            details={
                "method": method,
                "path": path,
                "errorCode": error_code,
                "errorMessage": payload.get("errorMessage"),
                "request": payload.get("debugInfo", {}).get("request")
                if isinstance(payload.get("debugInfo"), dict)
                else None,
            },
            retryable=False,
        )


def create_client(config: AppConfig) -> FaircomAPIClient:
    return FaircomAPIClient.from_config(config)
