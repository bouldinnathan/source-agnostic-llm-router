"""Exercise the shipped status-page JavaScript without a browser dependency."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_status_page_client_flows() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the status-page client regression harness")
    script = Path(__file__).with_name("status_page_client.cjs")
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS publicReadiness" in result.stdout
    assert "PASS lockLateResponsesAndRejectedKeys" in result.stdout
    assert "PASS safeRouterAndBackendLinks" in result.stdout
    assert "PASS selfTestIsExplicitAndIndependent" in result.stdout
    assert "PASS selfTestFailuresAndSafeRendering" in result.stdout
    assert "PASS selfTestPrivacyAndRaceGuards" in result.stdout
    assert "PASS selfTestAuthenticationFailure" in result.stdout
