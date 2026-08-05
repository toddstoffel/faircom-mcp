from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from faircom_mcp.api.client import FaircomAPIClient


class ConnectorAdapter:
    def __init__(self, client: FaircomAPIClient) -> None:
        self._client = client

    def list_inputs(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("listInputs", payload)

    def describe_inputs(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("describeInputs", payload)

    def create_input(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("createInput", payload)

    def alter_input(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("alterInput", payload)

    def delete_input(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("deleteInput", payload)

    def list_outputs(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("listOutputs", payload)

    def describe_outputs(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("describeOutputs", payload)

    def create_output(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("createOutput", payload)

    def alter_output(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("alterOutput", payload)

    def delete_output(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("deleteOutput", payload)
