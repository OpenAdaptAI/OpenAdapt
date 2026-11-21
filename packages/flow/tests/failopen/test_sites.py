"""Every registered verdict site holds under every generic mutation.

A site an open pull request owns is ``xfail(strict=True)`` with that pull
request in the reason: when its fix lands the test passes unexpectedly, and
the marker has to come out in the same change (FAIL_CLOSED.md, adoption).
"""

from __future__ import annotations

import pytest

from tests.failopen.harness import load_all, run_site

SPECS = {k: v for k, v in load_all().items() if v.kind in ("site", "owned")}


def _param(spec_id: str) -> object:
    spec = SPECS[spec_id]
    marks = [pytest.mark.xfail(strict=True, reason=spec.xfail)] if spec.xfail else []
    return pytest.param(spec_id, marks=marks, id=spec_id)


@pytest.mark.parametrize("spec_id", [_param(k) for k in sorted(SPECS)])
def test_site_holds(spec_id: str) -> None:
    failures = run_site(SPECS[spec_id])
    assert not failures, "\n".join(str(f) for f in failures)


def test_owned_sites_name_their_owner() -> None:
    for spec in SPECS.values():
        if spec.kind == "owned":
            assert spec.xfail and ("#" in spec.xfail), spec.id
