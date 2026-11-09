"""Live end-to-end test of the FHIR :class:`FhirEffectVerifier` against a REAL
local OpenEMR — closing PR #63's one honest caveat.

PR #63 shipped the OpenEMR FHIR R4 verifier
(``openadapt_flow/runtime/effects/fhir.py``) but exercised it only against a
faithful in-repo fake (``tests/_fhir_fake.py``), because the repo's OpenEMR
harness targets the public demo *vision-only* and never stands up a FHIR
endpoint to write to and read back. This test removes the fake from the loop:
it writes a resource through a REAL OpenEMR's API and has the SAME verifier
independently read it back out of the system of record over FHIR R4.

Bring the instance up first (see ``benchmark/openemr_live/README.md``)::

    docker compose -f benchmark/openemr_live/docker-compose.yml up -d
    eval "$(benchmark/openemr_live/setup.sh)"      # exports the env below
    .venv/bin/pytest tests/test_effect_fhir_live_openemr.py -v

The whole module is skipped unless ``OPENEMR_FHIR_BASE_URL`` is set, so normal
CI (which runs the fake-backed contract tests in ``test_effect_fhir.py``)
never touches it. Env consumed:

- ``OPENEMR_FHIR_BASE_URL``  — e.g. ``https://localhost:9390/apis/default/fhir``
- ``OPENEMR_FHIR_TOKEN``     — OAuth2 bearer access token (Patient read+write)
- ``OPENEMR_FHIR_VERIFY_TLS``— ``0`` to accept OpenEMR's self-signed localhost
  cert (default: verify)

Honest scope of the "real write":

- The write is a **FHIR Patient POST** (``POST {base}/Patient``), a real
  clinical resource created in OpenEMR's system of record, then read back
  through the FHIR search API by the verifier under test. This is an API
  write, not a GUI-driven one: OpenEMR's FHIR API exposes **Observation as
  read-only** (there is no ``user/Observation.write`` scope), so the
  note-as-Observation write the fake models cannot be created over FHIR on a
  stock OpenEMR. Driving the OpenEMR *GUI* to make the write and reading it
  back over FHIR would be the most honest path of all; it is not attempted
  here (browser-driving the dense OpenEMR UI reliably is a separate effort).
  The point this test proves is the one PR #63 could not: the verifier's
  verdicts (CONFIRMED / REFUTED / INDETERMINATE) are correct against a REAL
  FHIR server, not a fake.
"""

from __future__ import annotations

import os
import uuid

import pytest

from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    FhirEffectVerifier,
    Verdict,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENEMR_FHIR_BASE_URL"),
    reason=(
        "live OpenEMR FHIR: set OPENEMR_FHIR_BASE_URL (+ OPENEMR_FHIR_TOKEN "
        "+ OPENEMR_FHIR_VERIFY_TLS). See benchmark/openemr_live/README.md."
    ),
)

#: Flat Effect key -> dotted FHIRPath-lite for an OpenEMR FHIR ``Patient``.
#: ``name`` is a list, so ``name.family`` reads the first name's family and
#: ``name.given`` its first given (``extract_path`` takes a list's head).
PATIENT_PATHS = {
    "id": "id",
    "family": "name.family",
    "given": "name.given",
    "gender": "gender",
    "birthdate": "birthDate",
}


def _env() -> tuple[str, str | None, bool]:
    base = os.environ["OPENEMR_FHIR_BASE_URL"]
    token = os.environ.get("OPENEMR_FHIR_TOKEN")
    verify = os.environ.get("OPENEMR_FHIR_VERIFY_TLS", "1") not in (
        "0",
        "false",
        "False",
        "no",
    )
    return base, token, verify


@pytest.fixture
def live():
    base, token, verify = _env()
    import requests

    session = requests.Session()
    session.verify = verify
    if not verify:
        # Quiet the self-signed-cert warning for localhost.
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        yield base, token, verify, session
    finally:
        session.close()


def _create_patient(
    base: str, token: str | None, session, *, family: str, given: str
) -> str:
    """Create a real Patient via ``POST {base}/Patient``; return its FHIR id."""
    headers = {"Content-Type": "application/fhir+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resource = {
        "resourceType": "Patient",
        "name": [{"use": "official", "family": family, "given": [given]}],
        "gender": "male",
        "birthDate": "1980-01-01",
    }
    resp = session.post(f"{base}/Patient", json=resource, headers=headers, timeout=30)
    assert resp.status_code in (200, 201), (
        f"Patient create failed: HTTP {resp.status_code} {resp.text[:400]}"
    )
    body = resp.json()
    # OpenEMR returns {"uuid": ...} on create; be liberal about the shape.
    puuid = body.get("uuid") or body.get("id")
    assert puuid, f"no patient id in create response: {body}"
    return puuid


def _verifier(base, token, verify, session, *, family):
    return FhirEffectVerifier(
        base,
        resource_type="Patient",
        search_params={"family": family},
        field_paths=PATIENT_PATHS,
        access_token=token,
        session=session,
        verify_tls=verify,
        timeout_s=15.0,
    )


def test_live_reachable(live):
    """Sanity: the verifier can READ the real FHIR system of record."""
    base, token, verify, session = live
    v = _verifier(base, token, verify, session, family="AnyFamily")
    before = v.capture_pre_state()
    assert before.reachable, (
        "live OpenEMR FHIR unreachable/unauthorized -- check base URL + token"
    )


def test_live_record_written_confirmed(live):
    """Real write -> independent FHIR read-back -> CONFIRMED.

    Snapshot the (empty) pre-state for a unique family name, POST a real
    Patient, then have the verifier read the system of record back and confirm
    exactly one matching record landed.
    """
    base, token, verify, session = live
    family = f"Zztest{uuid.uuid4().hex[:12]}"
    given = f"Given{uuid.uuid4().hex[:8]}"
    v = _verifier(base, token, verify, session, family=family)

    before = v.capture_pre_state()
    assert before.reachable
    assert before.records == [], "unique family should not pre-exist"

    _create_patient(base, token, session, family=family, given=given)

    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"family": family},
        expected_count=1,
        timeout_s=15.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.observed_count == 1


def test_live_field_equals_reads_back_given(live):
    """Real write -> FHIR read-back of a specific field value -> CONFIRMED."""
    base, token, verify, session = live
    family = f"Zztest{uuid.uuid4().hex[:12]}"
    given = f"Given{uuid.uuid4().hex[:8]}"
    v = _verifier(base, token, verify, session, family=family)

    before = v.capture_pre_state()
    _create_patient(base, token, session, family=family, given=given)

    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"family": family},
        field="given",
        value=given,
        timeout_s=15.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.observed_value == given


def test_live_field_equals_wrong_value_refuted(live):
    """A deliberately-wrong expectation against a REAL record -> REFUTED.

    The Patient exists and its ``given`` is read back correctly, but we assert
    the wrong value; the system of record affirmatively contradicts it.
    """
    base, token, verify, session = live
    family = f"Zztest{uuid.uuid4().hex[:12]}"
    given = f"Given{uuid.uuid4().hex[:8]}"
    v = _verifier(base, token, verify, session, family=family)

    before = v.capture_pre_state()
    _create_patient(base, token, session, family=family, given=given)

    eff = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"family": family},
        field="given",
        value="DefinitelyNotTheGivenName",
        timeout_s=3.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED, verdict.reason
    assert verdict.observed_value == given


def test_live_absent_record_refuted(live):
    """An expected write that never happened -> REFUTED (missing/phantom).

    Nothing is created for this unique family, so the real FHIR server returns
    zero matches and the ``record_written`` contract is affirmatively refuted.
    """
    base, token, verify, session = live
    family = f"Zznever{uuid.uuid4().hex[:12]}"
    v = _verifier(base, token, verify, session, family=family)

    before = v.capture_pre_state()
    assert before.reachable and before.records == []

    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"family": family},
        expected_count=1,
        timeout_s=3.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.REFUTED, verdict.reason
    assert verdict.observed_count == 0


def test_live_bad_token_is_indeterminate_not_absent(live):
    """A bad/expired token against the REAL server -> INDETERMINATE (HALT).

    OpenEMR returns 401 for a bad bearer token; the verifier must read that as
    *unreadable* (INDETERMINATE -> HALT), NEVER as "record absent". This is the
    security-critical property: an expired token can never be mistaken for a
    clean, empty system of record.
    """
    base, _token, verify, session = live
    family = f"Zztest{uuid.uuid4().hex[:12]}"
    v = FhirEffectVerifier(
        base,
        resource_type="Patient",
        search_params={"family": family},
        field_paths=PATIENT_PATHS,
        access_token="THIS-IS-NOT-A-VALID-TOKEN",
        session=session,
        verify_tls=verify,
        timeout_s=3.0,
    )
    before = v.capture_pre_state()
    assert not before.reachable, "401 must read as unreadable, not empty"

    eff = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"family": family},
        timeout_s=1.0,
    )
    verdict = v.verify(eff, before)
    assert verdict.verdict is Verdict.INDETERMINATE, verdict.reason
