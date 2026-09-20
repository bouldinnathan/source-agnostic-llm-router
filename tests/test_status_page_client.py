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
    assert "PASS topbarVersionTracksCurrentSnapshot" in result.stdout
    assert "PASS lockLateResponsesAndRejectedKeys" in result.stdout
    assert "PASS safeRouterAndBackendLinks" in result.stdout
    assert "PASS selfTestIsExplicitAndIndependent" in result.stdout
    assert "PASS selfTestFailuresAndSafeRendering" in result.stdout
    assert "PASS selfTestPrivacyAndRaceGuards" in result.stdout
    assert "PASS selfTestAuthenticationFailure" in result.stdout
    assert "PASS savedHostLifecycle" in result.stdout
    assert "PASS savedHostsRestoreAndRequireAuthentication" in result.stdout
    assert "PASS savedHostFailuresAndSafeRendering" in result.stdout
    assert "PASS savedHostPrivacyAndRaceGuards" in result.stdout
    assert "PASS savedHostAuthRejectionAndTimeout" in result.stdout
    assert "PASS savedHostModelCatalogs" in result.stdout
    assert "PASS savedHostCatalogEmptyErrorTruncatedAndLegacy" in result.stdout
    assert "PASS savedHostCatalogEscapingAndValidation" in result.stdout
    assert "PASS savedHostCatalogStaleAndPrivate" in result.stdout
    assert "PASS publicCachedSummary" in result.stdout
    assert "PASS urlKeyBootstrapAndImmediateScrub" in result.stdout
    assert "PASS urlKeyInvalidAmbiguousAndCleanupFailure" in result.stdout
    assert "PASS urlKeyAuthenticationFailureAndPageRestore" in result.stdout
    assert "PASS liveFragmentKeyUnlockAndNavigation" in result.stdout
    assert "PASS liveFragmentKeyInvalidAndCleanupFailure" in result.stdout
    assert "PASS liveFragmentKeyCancelsOldSession" in result.stdout
    assert "PASS performanceIsPassiveAndPerDeployment" in result.stdout
    assert "PASS performanceUnknownZeroAndMissingTimings" in result.stdout
    assert "PASS performanceEmptyOlderAndUnavailableStorage" in result.stdout
    assert "PASS performanceEscapingAndPrivateStateClearing" in result.stdout
    assert "PASS performanceLateBodyAndNewSessionGuards" in result.stdout
    assert "PASS savedHostRoutingStatesAndSafeDetails" in result.stdout
    assert "PASS savedHostSaveEnrollmentAndRouterRefresh" in result.stdout
    assert "PASS savedHostSnapshotPollingIsAuthenticatedAndReadOnly" in result.stdout
    assert "PASS savedHostEnrollmentMutationRaceGuards" in result.stdout
    assert "PASS collapsiblePanelsAndSavedHostResults" in result.stdout
    assert "PASS trafficTilesChartAndWindows" in result.stdout
    assert "PASS routingSettingsCheckboxesAndRaces" in result.stdout
    assert "PASS updatesRequireExplicitAuthenticatedClick" in result.stdout
    assert "PASS updatesReconnectWithoutRepostingOrOldSuccess" in result.stdout
    assert "PASS updatesBusyFailuresAndBoundedWaiting" in result.stdout
    assert "PASS updateAuthenticationAndRacePrivacy" in result.stdout
    assert "PASS updatesVisibilityAndReloadAreReadOnly" in result.stdout
    assert "PASS updateStatusValidationAndSafeRendering" in result.stdout
    assert "PASS inferenceExplicitConsentAndNoPassiveRequests" in result.stdout
    assert "PASS inferenceProgressAndSafePerBackendResults" in result.stdout
    assert "PASS inferenceNoRepeatedPostOrHistoricalSuccess" in result.stdout
    assert "PASS inferenceBusyCooldownAndPreparationFailures" in result.stdout
    assert "PASS inferencePrivacyAuthenticationAndLateBodies" in result.stdout
    assert "PASS inferenceVisibilityAndTimeoutRecovery" in result.stdout
