from __future__ import annotations

import json
from collections.abc import Mapping
from threading import RLock
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
        auth: AuthConfig | None = None,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        tls_verify: bool = True,
        timeout_seconds: float = 10.0,
        max_read_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_auth = self._resolve_auth(auth, username=username, password=password, token=token)
        self._max_read_retries = max(0, max_read_retries)
        self._auth = resolved_auth
        self._session_lock = RLock()
        self._session_auth_token: str | None = resolved_auth.token
        client_auth, token_header = self._build_http_auth(resolved_auth)

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
                details: dict[str, object] = {
                    "method": method_upper,
                    "path": path,
                    "status_code": response.status_code,
                    "body": response.text[:2000],
                }
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = None

                if isinstance(error_payload, dict):
                    error_code = self._coerce_error_code(error_payload.get("errorCode"))
                    if error_code is not None:
                        details["errorCode"] = error_code
                    error_message = error_payload.get("errorMessage")
                    if isinstance(error_message, str):
                        details["errorMessage"] = error_message
                    debug_info = error_payload.get("debugInfo")
                    request_info = (
                        debug_info.get("request") if isinstance(debug_info, dict) else None
                    )
                    if isinstance(request_info, dict):
                        details["request"] = request_info
                        request_action = request_info.get("action")
                        request_api = request_info.get("api")
                        if isinstance(request_action, str):
                            details["request_action"] = request_action
                        if isinstance(request_api, str):
                            details["request_api"] = request_api

                raise UpstreamAPIError(
                    "FairCom API returned an error",
                    details=details,
                    retryable=(response.status_code >= 500 or details.get("errorCode") == 12031),
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
        return self.json_action("db", action, payload, path=path)

    def json_action(
        self,
        api: str,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        path: str = "/api/v1/action",
        include_auth_token: bool = True,
    ) -> Any:
        body: dict[str, Any] = {
            "api": api,
            "action": action,
        }
        if payload:
            body["params"] = dict(payload)
        auth_token = self._get_action_auth_token() if include_auth_token else None
        if auth_token is not None:
            body["authToken"] = auth_token
        try:
            return self.request_json("POST", path, json_body=body, idempotent=False)
        except UpstreamAPIError as exc:
            if not include_auth_token or not self._is_session_expired_error(exc):
                raise
            self._invalidate_session_auth_token()
            refreshed_body = dict(body)
            refreshed_body["authToken"] = self._get_action_auth_token()
            return self.request_json("POST", path, json_body=refreshed_body, idempotent=False)

    def admin_action(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        path: str = "/api/v1/action",
    ) -> Any:
        return self.json_action("admin", action, payload, path=path)

    def hub_action(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        path: str = "/api/v1/action",
    ) -> Any:
        return self.json_action("hub", action, payload, path=path)

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

    @staticmethod
    def _resolve_auth(
        auth: AuthConfig | None,
        *,
        username: str | None,
        password: str | None,
        token: str | None,
    ) -> AuthConfig:
        if auth is not None:
            return auth

        if token is not None:
            return AuthConfig(token=token)

        if username is not None or password is not None:
            return AuthConfig(username=username, password=password)

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

            payload = self.json_action(
                "admin",
                "createSession",
                {
                    "username": self._auth.username,
                    "password": self._auth.password,
                    "defaultApi": "db",
                    "defaultDebug": "none",
                },
                include_auth_token=False,
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

    def _invalidate_session_auth_token(self) -> None:
        if self._auth.token:
            return
        with self._session_lock:
            self._session_auth_token = None

    @staticmethod
    def _is_session_expired_error(exc: UpstreamAPIError) -> bool:
        direct_error_code = FaircomAPIClient._coerce_error_code(exc.details.get("errorCode"))
        if direct_error_code == 12031:
            return True

        raw_body = exc.details.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            return False
        try:
            body_payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return False
        if not isinstance(body_payload, dict):
            return False
        body_error_code = FaircomAPIClient._coerce_error_code(body_payload.get("errorCode"))
        return body_error_code == 12031

    @staticmethod
    def _coerce_error_code(value: object) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _raise_if_api_error(payload: Any, *, method: str, path: str) -> None:
        if not isinstance(payload, dict):
            return

        error_code = payload.get("errorCode")
        if not isinstance(error_code, int):
            return
        if error_code == 0:
            return

        request_info = (
            payload.get("debugInfo", {}).get("request")
            if isinstance(payload.get("debugInfo"), dict)
            else None
        )
        request_action: str | None = None
        request_api: str | None = None
        if isinstance(request_info, dict):
            raw_action = request_info.get("action")
            raw_api = request_info.get("api")
            request_action = raw_action if isinstance(raw_action, str) else None
            request_api = raw_api if isinstance(raw_api, str) else None

        connector_actions = {
            "listInputs",
            "describeInputs",
            "createInput",
            "alterInput",
            "deleteInput",
            "listOutputs",
            "describeOutputs",
            "createOutput",
            "alterOutput",
            "deleteOutput",
            "listTransforms",
            "describeTransforms",
            "createTransform",
            "alterTransform",
            "deleteTransform",
        }
        hint: str | None = None
        if request_action in connector_actions:
            hint = (
                "Connector action failed upstream. Verify FAIRCOM_API_BASE_URL points to the "
                "correct FairCom JSON API endpoint, confirm the authenticated account can execute "
                "connector admin actions, and validate listInputs directly against the upstream "
                "API."
            )

        raise UpstreamAPIError(
            "FairCom API returned an application error",
            details={
                "method": method,
                "path": path,
                "errorCode": error_code,
                "errorMessage": payload.get("errorMessage"),
                "request": request_info,
                "request_api": request_api,
                "request_action": request_action,
            },
            retryable=False,
            hint=hint,
        )


def create_client(config: AppConfig) -> FaircomAPIClient:
    return FaircomAPIClient.from_config(config)
