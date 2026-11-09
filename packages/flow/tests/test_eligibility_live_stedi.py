"""Three-trial live Stedi TEST-mode qualification.

The request is Stedi's published Cigna dental mock (STC 35), so it uses no
real member data and Stedi documents mock checks as free.  Without a test key,
all three trials skip with one reproducible setup instruction; they must never
be reported as executed evidence.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

import pytest

from openadapt_flow.eligibility.artifact import (
    ArtifactEncryption,
    PracticeArtifactPolicy,
    all_confirmed,
    write_and_verify,
)
from openadapt_flow.eligibility.client import (
    ApplicationMode,
    BenefitSelection,
    EligibilityRequest,
    EligibilityStatus,
    StediAccountBoundary,
    StediEligibilityClient,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("STEDI_API_KEY"),
    reason=(
        "STEDI_API_KEY is absent: create a Stedi sandbox TEST key, then run "
        "`pytest -q tests/test_eligibility_live_stedi.py -rs`; no live "
        "eligibility evidence was collected"
    ),
)


@pytest.mark.parametrize("trial", [1, 2, 3])
def test_stedi_dental_mock_roundtrip_three_trials(tmp_path, trial):
    account = StediAccountBoundary(
        practice_account_id="openadapt-sandbox",
        application_mode=ApplicationMode.TEST,
    )
    client = StediEligibilityClient(account=account)
    request = EligibilityRequest(
        operation_id=f"stedi-cigna-dental-live-trial-{trial}",
        payer_id="62308",
        member_id="U3141592653",
        first_name="Jaguar",
        last_name="Dent",
        date_of_birth="19960505",
        provider_npi="1999999984",
        provider_organization="One",
        service_type_codes=["35"],
        date_of_service=datetime.now(timezone.utc).strftime("%Y%m%d"),
        benefit_selection=BenefitSelection(network_code="Y", coverage_level_code="IND"),
    )
    result = client.check(request)
    assert result.is_answer, result.reason
    assert result.status in (EligibilityStatus.ACTIVE, EligibilityStatus.INACTIVE)
    assert result.application_mode is ApplicationMode.TEST
    assert result.raw_271_sha256

    artifact_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    policy = PracticeArtifactPolicy(
        boundary_id=f"live-test-trial-{trial}",
        application_mode=ApplicationMode.TEST,
        encryption=ArtifactEncryption.APPLICATION_AES256_GCM,
        encryption_key_env="LIVE_TEST_ARTIFACT_KEY",
        retention_days=1,
    )
    artifact, verdicts = write_and_verify(
        result,
        tmp_path,
        request=request,
        policy=policy,
        env={"LIVE_TEST_ARTIFACT_KEY": artifact_key},
    )
    assert all_confirmed(verdicts), [v.reason for v in verdicts]
    assert artifact.raw_271_file.endswith(".enc")
