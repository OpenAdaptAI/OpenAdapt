"""Every registered verdict site holds under every generic mutation.

A site an open pull request owns lists exactly the failures it has today
(``known``), with the owner in ``xfail``. The test fails on any other failure,
so a known bug can't hide a new one, and it fails when a known failure goes
away, so the list (and in the end the ``owned`` marker) has to come out in the
change that fixes it. That is a strict xfail, per case (FAIL_CLOSED.md).
"""

from __future__ import annotations

import pytest

from tests.failopen.harness import load_all, run_site

SPECS = {k: v for k, v in load_all().items() if v.kind in ("site", "owned")}


@pytest.mark.parametrize("spec_id", sorted(SPECS))
def test_site_holds(spec_id: str) -> None:
    spec = SPECS[spec_id]
    got = {f.key for f in run_site(spec)}
    new, fixed = sorted(got - spec.known), sorted(spec.known - got)
    assert not new, f"{spec_id}: new failures:\n" + "\n".join(new)
    assert not fixed, (
        f"{spec_id}: fixed, remove from known (and the owned marker when empty):\n"
        + "\n".join(fixed)
    )


def test_owned_sites_name_their_owner_and_failures() -> None:
    for spec in SPECS.values():
        if spec.kind == "owned":
            assert spec.xfail and "#" in spec.xfail and spec.known, spec.id
        else:
            assert not spec.known, spec.id


def test_a_site_that_ranks_unread_first_says_why() -> None:
    from openadapt_flow.verdict import Status

    for spec in load_all().values():
        if spec.precedence != (Status.REFUTED, Status.NOT_CHECKED):
            assert spec.notes.get("precedence"), spec.id
