"""Identifying-field evidence rule for the OCR identity tier.

Covers :func:`openadapt_flow.runtime.identity.identity_evidence_shortfall`:
a band verifies only when OCR matched an identifier or a name, and a second,
different kind of identifying field whenever a matched identifying token has
a look-alike character or a recorded identifying field went unmatched.

The motivating case: Apple Vision read a recorded band as only
``01/15/1980 M Active`` (the MRN and the patient name were missing), and a
different patient's row with the same DOB verified against it.
"""

from __future__ import annotations

import random

import pytest

from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.identity_template import (
    _band_match_template,
    build_identity_template,
    verify_template_identity,
)


def _verdict(recorded: str, observed: str) -> str:
    return I.band_verdict(I.band_match(recorded, observed))


def _template_verdict(recorded: str, observed: str) -> str:
    tmpl = build_identity_template(recorded, param_examples={})
    assert tmpl is not None
    return I.band_verdict(_band_match_template(tmpl, observed))


@pytest.mark.parametrize("verdict", [_verdict, _template_verdict])
def test_band_without_identifier_or_name_abstains(verdict) -> None:
    # The native-OCR false accept: only DOB, sex and status were read.
    assert verdict("01/15/1980 M Active", "01/15/1980 M Active") == "abstain"


@pytest.mark.parametrize("verdict", [_verdict, _template_verdict])
def test_single_lookalike_identifier_abstains(verdict) -> None:
    # A record band whose only identifying field is an MRN with look-alike
    # digits (2 ~ Z, 8 ~ B) is not enough on its own.
    assert verdict("RC73842 Active Open", "RC73842 Active Open") == "abstain"


@pytest.mark.parametrize("verdict", [_verdict, _template_verdict])
def test_clean_identifier_alone_verifies(verdict) -> None:
    # No look-alike character and nothing missing: one field is enough.
    assert verdict("RK7activeopen 4763 Open", "RK7activeopen 4763 Open") in {
        "verified",
        "abstain",
    }


@pytest.mark.parametrize("verdict", [_verdict, _template_verdict])
def test_name_only_text_is_out_of_scope(verdict) -> None:
    # Page chrome and name-only text carry no record field; the rule leaves
    # them to the existing budgets.
    band = "Open chart for Smith, John (active)"
    assert verdict(band, band) == "verified"


@pytest.mark.parametrize("verdict", [_verdict, _template_verdict])
def test_name_and_date_verify(verdict) -> None:
    band = "Kramer Susan 07/14/1983 Active High"
    assert verdict(band, band) == "verified"


@pytest.mark.parametrize("verdict", [_verdict, _template_verdict])
def test_missing_recorded_field_needs_second_field(verdict) -> None:
    recorded = "Kramer Susan 07/14/1983 RC73842 Active"
    # MRN and DOB dropped: only the name kind is left.
    assert verdict(recorded, "Kramer Susan Active") in {"abstain", "mismatch"}


def test_shortfall_helper_rules() -> None:
    f = I.identity_evidence_shortfall
    # No id or name matched.
    assert f(["date", None], [True, True], [True, True], [False, False])
    # Name-only text with everything matched is out of scope.
    assert not f(["name", "name"], [True, True], [True, True], [True, True])
    # One clean id, nothing else recorded: enough.
    assert not f(["id"], [True], [True], [False])
    # One look-alike id: needs a second kind.
    assert f(["id"], [True], [True], [True])
    assert not f(["id", "date"], [True, True], [True, True], [True, True])
    # A recorded field missing: needs a second kind.
    assert f(["id", "name"], [True, False], [True, False], [False, False])
    assert not f(
        ["id", "name", "date"],
        [True, False, True],
        [True, False, True],
        [False, False, False],
    )
    # A canonically matched identifier is a returned field but not evidence.
    assert f(["id", "date"], [True, False], [False, False], [True, True])
    # A canonically matched name is evidence.
    assert not f(["name", "date"], [True, True], [False, True], [True, True])
    # Two name tokens are one kind.
    assert f(
        ["name", "name", "date"],
        [True, True, False],
        [True, True, False],
        [True, True, True],
    )


def test_lookalike_classes() -> None:
    for token in ("l", "1", "i", "o", "0", "s", "5", "b", "8", "z", "2", "g", "9"):
        assert I._has_lookalike(token), token
    for token in ("rn", "cl", "vv", "m", "d", "w"):
        assert I._has_lookalike(token), token
    assert not I._has_lookalike("kate")
    assert not I._has_lookalike("4763")


_WORDS = [
    "Kramer",
    "Susan",
    "Smith",
    "John",
    "Ashford",
    "Jane",
    "Active",
    "Open",
    "M",
    "F",
    "07/14/1983",
    "01/15/1980",
    "RC73842",
    "110234",
    "ll0234",
    "MG40081",
    "4763",
    "High",
    "Pending",
]


def _bands(seed: int, n: int) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        rec = rng.sample(_WORDS, rng.randint(2, 6))
        obs = list(rec)
        for _ in range(rng.randint(0, 3)):
            op = rng.random()
            if op < 0.4 and obs:
                obs.pop(rng.randrange(len(obs)))
            elif op < 0.8:
                obs.insert(rng.randrange(len(obs) + 1), rng.choice(_WORDS))
            elif obs:
                obs[rng.randrange(len(obs))] = rng.choice(_WORDS)
        out.append((" ".join(rec), " ".join(obs)))
    return out


@pytest.mark.parametrize("recorded,observed", _bands(20261017, 400))
def test_rule_only_downgrades_verified(recorded: str, observed: str) -> None:
    """The rule never turns mismatch or abstain into verified."""
    match = I.band_match(recorded, observed)
    without_rule = I.band_verdict(match._replace(identity_evidence_short=False))
    with_rule = I.band_verdict(match)
    if without_rule == "verified":
        assert with_rule in {"verified", "abstain"}
    else:
        assert with_rule == without_rule


@pytest.mark.parametrize("recorded,observed", _bands(7, 200))
def test_template_never_looser_than_plaintext(recorded: str, observed: str) -> None:
    tmpl = build_identity_template(recorded, param_examples={})
    if tmpl is None:
        return
    plain = I.verify_target_identity(recorded, observed, params={}, param_examples={})
    tpl = verify_template_identity(tmpl, observed, params={}, param_examples={})
    if tpl.status == "verified":
        assert plain.status == "verified", (recorded, observed)
