"""Local integration harness sanity checks."""

from pathlib import Path

import pytest


@pytest.mark.integration
def test_local_harness_smoke() -> None:
    script = Path("tests/integration/test_with_edge.sh")
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "pytest -m edge_integration" in text
