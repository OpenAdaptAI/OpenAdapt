"""The openIMIS claims-intake profile for the production configuration.

The pieces benchmark/lifecycle_proof needs that are specific to openIMIS:
its contract (verify.claims.yaml), its taught program, its user, its test
copy and how the gate checks who is signed in. Everything else is shared.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from benchmark.lifecycle_proof.run import AppProfile

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
#: The program taught on the fixture (scripts/openimis_claims_demo.py record
#: + compile); outside the repository.
SOURCE = Path(
    os.environ.get(
        "OPENIMIS_SOURCE_BUNDLE",
        Path.home() / "oa/scratch/second-installation/openimis/bundle",
    )
)
#: The demo dataset's actor. openIMIS ships no separate service account in
#: the demo data, so the dedicated user is the fixture's one actor.
ACTOR = "Admin"


class OpenIMISSignedInUser:
    """P2 for openIMIS: the signed-in login, from the app's own API.

    The page asks openIMIS who the current user is with its own session; the
    frame is not used.
    """

    kind = "login_id"

    def __init__(self, page: Any, expected: str) -> None:
        self._page = page
        self._expected = expected

    def reference(self, key: bytes) -> str:
        from openadapt_flow.approval_mode.gate import dedicated_user_reference

        return dedicated_user_reference(self._expected, key)

    def check(self, frame_png: bytes) -> str:
        try:
            login = self._page.evaluate(
                """async () => {
                  const r = await fetch('/api/graphql', {
                    method: 'POST', credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({query: '{ me { username } }'})});
                  const j = await r.json();
                  return j && j.data && j.data.me ? j.data.me.username : null;
                }"""
            )
        except Exception:  # noqa: BLE001 - an unreadable session is not a match
            return "unreadable"
        if not isinstance(login, str) or not login:
            return "unreadable"
        return "match" if login == self._expected else "mismatch"


class OpenIMISTestCopy:
    """The fixture as the configuration's test copy."""

    def __init__(self) -> None:
        from benchmark.openimis_claims import qualify_hooks

        self.hooks = qualify_hooks
        self.fx = qualify_hooks.fixture()

    def point_hooks_here(self) -> None:
        os.environ.setdefault("OPENIMIS_FRONT_URL", self.fx.front_url)
        os.environ.setdefault("OPENIMIS_DB_PORT", str(self.fx.db_port))
        self.hooks.ensure_claims_oracle()

    def live_claim_ids(self) -> set[str]:
        out = self.fx._psql(
            'SELECT "ClaimID" FROM "tblClaim" WHERE "ValidityTo" IS NULL;'
        )
        return {line.strip() for line in out.splitlines() if line.strip().isdigit()}

    def dump(self) -> set[str]:
        """The live claims now; ``restore`` voids any claim entered since."""

        return self.live_claim_ids()

    def restore(self, before: set[str]) -> None:
        added = sorted(self.live_claim_ids() - before, key=int)
        if added:
            self.fx._psql(
                'UPDATE "tblClaim" SET "ValidityTo" = now() WHERE "ClaimID" IN ('
                + ",".join(added)
                + ");"
            )

    def reset_for_admission(self) -> None:
        self.hooks.reset()


OPENIMIS = AppProfile(
    name="openimis",
    verify_yaml=FLOW / "benchmark/openimis_claims/verify.claims.yaml",
    source=SOURCE,
    dedicated_user=ACTOR,
    workflow_id="workflow-openimis-claims-intake",
    make_app=OpenIMISTestCopy,
    signed_in_user=lambda page, user: OpenIMISSignedInUser(page, user),
)
