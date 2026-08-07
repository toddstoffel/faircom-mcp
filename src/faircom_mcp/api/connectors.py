from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from faircom_mcp.api.client import FaircomAPIClient

_MODBUS_SETTINGS_KEYS = {
    "modbusProtocol",
    "modbusServer",
    "modbusServerPort",
    "modbusDataAddressType",
    "modbusSerialPort",
    "modbusBaudRate",
    "modbusDataBits",
    "modbusParity",
    "modbusStopBits",
    "modbusTimeoutMs",
    "modbusRetryCount",
    "modbusByteOrder",
    "modbusWordOrder",
    "transformName",
    "disableTransformSteps",
    "dataCollectionIntervalMilliseconds",
    "enabled",
    "description",
    "propertyMapList",
}

_MODBUS_SETTINGS_MIRROR_ONLY_KEYS = {
    "transformName",
    "dataCollectionIntervalMilliseconds",
    "disableTransformSteps",
    "enabled",
    "description",
}


def transform_connector_request(
    action: str,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if payload is None:
        return None

    transformed: dict[str, Any] = dict(payload)

    if action in {"listInputs", "describeInputs"}:
        if "inputNameLike" not in transformed and "connectorNameLike" in transformed:
            transformed["inputNameLike"] = transformed.pop("connectorNameLike")
        if "inputNames" not in transformed and "connectorNames" in transformed:
            transformed["inputNames"] = transformed.pop("connectorNames")
        if "inputNames" not in transformed and "inputName" in transformed:
            input_name = transformed.pop("inputName")
            if isinstance(input_name, str):
                input_name = input_name.strip()
                transformed["inputNames"] = [input_name] if input_name else []
            elif isinstance(input_name, list):
                transformed["inputNames"] = input_name
        return transformed

    if action in {"createInput", "alterInput", "deleteInput"}:
        input_name = transformed.get("inputName")
        if not isinstance(input_name, str) or not input_name.strip():
            connector_name = transformed.get("connectorName")
            if isinstance(connector_name, str) and connector_name.strip():
                transformed["inputName"] = connector_name.strip()

        transformed.pop("connectorName", None)

    service_name = transformed.get("serviceName")
    if (
        action in {"createInput", "alterInput"}
        and isinstance(service_name, str)
        and service_name.strip().lower() == "modbus"
    ):
        existing_settings = transformed.get("settings")
        settings = dict(existing_settings) if isinstance(existing_settings, Mapping) else {}
        for key in list(transformed.keys()):
            if key in _MODBUS_SETTINGS_KEYS:
                settings[key] = transformed[key]
                if key not in _MODBUS_SETTINGS_MIRROR_ONLY_KEYS:
                    transformed.pop(key, None)
        transformed["settings"] = settings

    return transformed


class ConnectorAdapter:
    def __init__(self, client: FaircomAPIClient) -> None:
        self._client = client

    def list_inputs(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action(
            "listInputs",
            transform_connector_request("listInputs", payload),
        )

    def describe_inputs(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action(
            "describeInputs", transform_connector_request("describeInputs", payload)
        )

    def create_input(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action(
            "createInput",
            transform_connector_request("createInput", payload),
        )

    def alter_input(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action(
            "alterInput",
            transform_connector_request("alterInput", payload),
        )

    def delete_input(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action(
            "deleteInput",
            transform_connector_request("deleteInput", payload),
        )

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

    def list_transforms(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("listTransforms", payload)

    def describe_transforms(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("describeTransforms", payload)

    def create_transform(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("createTransform", payload)

    def alter_transform(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("alterTransform", payload)

    def delete_transform(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("deleteTransform", payload)
