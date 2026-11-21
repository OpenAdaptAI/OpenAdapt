"""The harness catches every historical bug and passes every fix.

Each bug is real code from this repository: vendored pre-fix copies under
``selftest/`` (commit and lines in their first line) or main itself. The fix
is the vendored post-fix copy, or main after this change. Kill criteria 1 and
2 of the design: at least 8 bugs caught, all from real code, and no post-fix
copy flagged.
"""

from __future__ import annotations

import pytest

from tests.failopen.harness import load_all, run_site
from tests.failopen.selftest.specs import PAIRS

REGISTRY = load_all()


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_pre_fix_is_flagged(name: str) -> None:
    pre, _post, catches = PAIRS[name]
    failures = run_site(REGISTRY[pre])
    caught_by = {f.mutator for f in failures}
    assert caught_by & catches, (
        f"{name}: expected one of {sorted(catches)} to flag the pre-fix code, "
        f"got {sorted(caught_by) or 'nothing'}"
    )


@pytest.mark.parametrize("name", sorted(n for n, p in PAIRS.items() if p[1]))
def test_post_fix_holds(name: str) -> None:
    _pre, post, _catches = PAIRS[name]
    assert post is not None
    failures = run_site(REGISTRY[post])
    assert not failures, "\n".join(str(f) for f in failures)


def test_enough_real_bugs_are_caught() -> None:
    caught = [n for n, (pre, _, _) in PAIRS.items() if run_site(REGISTRY[pre])]
    assert len(caught) >= 8, caught
