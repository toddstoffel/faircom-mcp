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

    if action in {"createOutput", "alterOutput", "deleteOutput"}:
        output_name = transformed.get("outputName")
        if not isinstance(output_name, str) or not output_name.strip():
            connector_name = transformed.get("connectorName")
            if isinstance(connector_name, str) and connector_name.strip():
                transformed["outputName"] = connector_name.strip()

        transformed.pop("connectorName", None)

    service_name = transformed.get("serviceName")
    if (
        action in {"createInput", "alterInput", "createOutput", "alterOutput"}
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
        # The Edge admin UI treats this as a required dropdown even though the
        # JSON API docs list a "zeroBased" default; set it explicitly on create
        # so connectors don't show up as invalid/unset in the UI.
        if action == "createInput" and "modbusDataAddressType" not in settings:
            settings["modbusDataAddressType"] = "zeroBased"
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
        return self._client.hub_action(
            "createOutput",
            transform_connector_request("createOutput", payload),
        )

    def alter_output(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action(
            "alterOutput",
            transform_connector_request("alterOutput", payload),
        )

    def delete_output(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("deleteOutput", payload)

    # Per https://documentation.faircom.com/en_US/integration-tables-api-actions,
    # transforms are configured as "transformSteps" on an integration table —
    # there is no standalone "transform" hub action.
    def list_integration_tables(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("listIntegrationTables", payload)

    def describe_integration_tables(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.hub_action("describeIntegrationTables", payload)

    def create_integration_table(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("createIntegrationTable", payload)

    def alter_integration_table(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("alterIntegrationTable", payload)

    def delete_integration_tables(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("deleteIntegrationTables", payload)

    def test_integration_table_transform_steps(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("testIntegrationTableTransformSteps", payload)

    def copy_integration_table_transform_steps(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("copyIntegrationTableTransformSteps", payload)

    def rerun_integration_table_transform_steps(self, payload: Mapping[str, Any]) -> Any:
        return self._client.hub_action("rerunIntegrationTableTransformSteps", payload)

    # Per https://documentation.faircom.com/en_US/code-packages-api-actions, code
    # packages are managed through the "admin" API, not raw SQL against system tables.
    def create_code_package(self, payload: Mapping[str, Any]) -> Any:
        return self._client.admin_action("createCodePackage", payload)

    def alter_code_package(self, payload: Mapping[str, Any]) -> Any:
        return self._client.admin_action("alterCodePackage", payload)

    def clone_code_package(self, payload: Mapping[str, Any]) -> Any:
        return self._client.admin_action("cloneCodePackage", payload)

    def revert_code_package(self, payload: Mapping[str, Any]) -> Any:
        return self._client.admin_action("revertCodePackage", payload)

    def list_code_packages(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.admin_action("listCodePackages", payload)

    def describe_code_packages(self, payload: Mapping[str, Any]) -> Any:
        return self._client.admin_action("describeCodePackages", payload)

    def list_code_package_history(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.admin_action("listCodePackageHistory", payload)

    def describe_code_package_history(self, payload: Mapping[str, Any]) -> Any:
        return self._client.admin_action("describeCodePackageHistory", payload)

    # Per https://documentation.faircom.com/en_US/apis/faircom-edge-apis, MQTT delivery
    # runs through the "mq" API's topic actions, not through createOutput/alterOutput.
    # configureTopic is an upsert (create-or-update), unlike createInput/createOutput.
    def configure_topic(self, payload: Mapping[str, Any]) -> Any:
        return self._client.mq_action("configureTopic", payload)

    def delete_topic(self, payload: Mapping[str, Any]) -> Any:
        return self._client.mq_action("deleteTopic", payload)

    def describe_topics(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.mq_action("describeTopics", payload)

    def list_topics(self, payload: Mapping[str, Any] | None = None) -> Any:
        return self._client.mq_action("listTopics", payload)
