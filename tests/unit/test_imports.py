from faircom_mcp import (
    api,
    config,
    errors,
    logging_utils,
    observability,
    security,
    tools,
    transports,
)


def test_package_imports() -> None:
    assert api is not None
    assert config is not None
    assert errors is not None
    assert logging_utils is not None
    assert observability is not None
    assert transports is not None
    assert tools is not None
    assert security is not None
