"""The Medplum (FHIR R4) new-patient profile for the production configuration.

The pieces benchmark/lifecycle_proof needs that are specific to Medplum: its
contract (benchmark/fhir_medplum/verify.yaml), its taught program, its
dedicated user, its test copy and how the gate checks who is signed in.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from benchmark.lifecycle_proof.run import AppProfile

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
SOURCE = Path(
    os.environ.get(
        "MEDPLUM_SOURCE_BUNDLE",
        Path.home() / "oa/scratch/second-installation/medplum/bundle",
    )
)


class MedplumSignedInUser:
    """P2 for Medplum: the signed-in user's email, from the app's own session.

    The page asks the server who it is with the token the web app holds; the
    frame is not used.
    """

    kind = "login_id"

    def __init__(self, page: Any, expected: str, server: str) -> None:
        self._page = page
        self._expected = expected
        self._server = server

    def reference(self, key: bytes) -> str:
        from openadapt_flow.approval_mode.gate import dedicated_user_reference

        return dedicated_user_reference(self._expected, key)

    def check(self, frame_png: bytes) -> str:
        try:
            email = self._page.evaluate(
                """async (server) => {
                  const active = JSON.parse(localStorage.getItem('activeLogin') || 'null');
                  if (!active || !active.accessToken) return null;
                  const r = await fetch(server + 'auth/me', {
                    headers: {Authorization: 'Bearer ' + active.accessToken}});
                  const j = await r.json();
                  return j && j.user ? j.user.email : null;
                }""",
                self._server,
            )
        except Exception:  # noqa: BLE001 - an unreadable session is not a match
            return "unreadable"
        if not isinstance(email, str) or not email:
            return "unreadable"
        return "match" if email == self._expected else "mismatch"


class MedplumTestCopy:
    """The fixture as the configuration's test copy."""

    def __init__(self) -> None:
        from benchmark.fhir_medplum import qualify_hooks

        self.hooks = qualify_hooks
        self.fx = qualify_hooks.fixture()

    def point_hooks_here(self) -> None:
        os.environ.setdefault("MEDPLUM_UI_URL", self.fx.ui_url)
        os.environ.setdefault("MEDPLUM_FHIR_URL", self.fx.fhir_url)
        self.hooks.record_token()

    def dump(self) -> set[str]:
        """The patients now; ``restore`` deletes any patient created since."""

        return set(self.fx.patient_ids(token=self.fx.client_token("harness")))

    def restore(self, before: set[str]) -> None:
        token = self.fx.client_token("harness")
        for pid in set(self.fx.patient_ids(token=token)) - before:
            self.fx.delete_patient(pid, token=token)

    def reset_for_admission(self) -> None:
        self.hooks.reset()


def _signed_in(page: Any, user: str) -> MedplumSignedInUser:
    from benchmark.fhir_medplum.qualify_hooks import fixture

    return MedplumSignedInUser(page, user, fixture().base_url + "/")


def _actor() -> str:
    from benchmark.fhir_medplum.qualify_hooks import ACTOR_EMAIL

    return ACTOR_EMAIL


FHIR = AppProfile(
    name="fhir",
    verify_yaml=FLOW / "benchmark/fhir_medplum/verify.yaml",
    source=SOURCE,
    dedicated_user=_actor(),
    workflow_id="workflow-medplum-new-patient",
    make_app=MedplumTestCopy,
    signed_in_user=_signed_in,
)
