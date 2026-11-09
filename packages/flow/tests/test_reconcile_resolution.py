"""One resolution rule for every judged slot of a reconciliation.

Review round 5 (PR 2004): each earlier false CONFIRMED was one instance of
one class: which record or value is "the" one when the read holds several.
``openadapt_flow.reconcile.resolve`` is the single rule; these tests check
it at every site and, as a property over generated reads, that a judged slot
with two or more distinct candidates never yields CONFIRMED.
"""

from __future__ import annotations

import copy
import importlib.util
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from openadapt_flow.reconcile import ReconcileSpec, reconcile, x12
from openadapt_flow.reconcile.resolve import Ambiguous, is_ambiguous, resolve
from openadapt_flow.runtime.effects.adapter import (
    VerifierAdapterBase,
    register_verifier_factory,
)
from openadapt_flow.runtime.effects.effect import EffectState
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier, extract_all
from openadapt_flow.verification import VerificationTier
from openadapt_flow.verifier import Outcome

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent / "benchmark" / "reconcile"
_spec = importlib.util.spec_from_file_location(
    "x12_fixtures", _BENCH / "x12_fixtures.py"
)
fx = importlib.util.module_from_spec(_spec)
sys.modules["x12_fixtures"] = fx
_spec.loader.exec_module(fx)  # type: ignore[union-attr]

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
#: The shipped claim spec's status rule, so these tests judge what ships.
SHIPPED_ACCEPT = yaml.safe_load((_BENCH / "claim.reconcile.yaml").read_text())["legs"][
    "clearinghouse"
]["accept"]


# -- the rule ------------------------------------------------------------------


def test_resolve_collapses_identical_and_flags_conflicts() -> None:
    assert resolve([]) is None
    assert resolve(["A1", "A1"]) == "A1" and not is_ambiguous(resolve(["A1", "A1"]))
    both = resolve(["A1", "A7"])
    assert is_ambiguous(both) and set(both.candidates) == {"A1", "A7"}
    # A record without the value is not one with it.
    assert is_ambiguous(resolve([None, "A1"]))
    # Nested ambiguity flattens; the marker quotes no candidate.
    assert set(resolve([both, "A3"]).candidates) == {"A1", "A7", "A3"}
    assert "A1" not in str(both) and both != "A1"
    assert copy.deepcopy(both) is both


# -- X12 repeated segments (finding 3) ------------------------------------------


def _status_body(**kw: Any) -> list[str]:
    return fx.status_set([fx.ClaimStatus("C1", "M1", "99213", "2026-10-01", **kw)])


def _insert_after(body: list[str], prefix: str, segment: str) -> list[str]:
    at = next(i for i, s in enumerate(body) if s.startswith(prefix))
    return body[: at + 1] + [segment] + body[at + 1 :]


@pytest.mark.parametrize(
    ("prefix", "repeat", "slot"),
    [
        ("STC*", "STC*A7:21*20261102**125.00", "status_category"),
        ("REF*1K*", "REF*1K*PCN-OTHER", "payer_claim_number"),
        ("DTP*472*", "DTP*472*D8*20261002", "service_date"),
        ("SVC*", "SVC*HC:99214*125.00*0", "service_code"),
        ("NM1*IL*", "NM1*IL*1*SYNTH*MEMBER****MI*M2", "member_id"),
    ],
)
def test_conflicting_277_repeats_are_ambiguous(prefix, repeat, slot) -> None:
    body = _status_body(payer_claim_number="PCN1")
    (record,) = x12.parse(
        fx.interchange("277", [_insert_after(body, prefix, repeat)], control="7")
    )
    assert is_ambiguous(record[slot]), record[slot]


def test_stc_conflict_is_not_first_wins() -> None:
    # The reviewer's probe: STC A1:20 then STC A7:21 on one claim.
    body = _insert_after(_status_body(), "STC*", "STC*A7:21*20261102**125.00")
    (record,) = x12.parse(fx.interchange("277", [body], control="8"))
    assert record["status_category"] != "A1"
    assert set(record["status_category"].candidates) == {"A1", "A7"}
    # Same category, other code: only the code is ambiguous.
    body = _insert_after(_status_body(), "STC*", "STC*A1:19*20261102**125.00")
    (record,) = x12.parse(fx.interchange("277", [body], control="9"))
    assert record["status_category"] == "A1" and is_ambiguous(record["status_code"])


def test_identical_repeats_collapse() -> None:
    body = _status_body(payer_claim_number="PCN1")
    for prefix in ("STC*", "REF*1K*", "DTP*472*", "SVC*"):
        seg = next(s for s in body if s.startswith(prefix))
        body = _insert_after(body, prefix, seg)
    (record,) = x12.parse(fx.interchange("277", [body], control="10"))
    assert record["status_category"] == "A1"
    assert record["payer_claim_number"] == "PCN1"
    assert record["service_date"] == "2026-10-01"
    assert record["service_code"] == "99213"
    assert record["service_count"] == 2


@pytest.mark.parametrize(
    ("segment", "slot"),
    [
        ("HCR*A3", "decision"),
        ("REF*BB*AUTH-OTHER", "auth_number"),
        ("HCR*A1*AUTH-OTHER", "auth_number"),
        ("DTP*472*D8*20261121", "service_date"),
    ],
)
def test_conflicting_278_repeats_are_ambiguous(segment, slot) -> None:
    body = fx.review_set(
        fx.Review("PA-1", "M1", "97110", "2026-11-20", auth_number="AUTH9")
    )
    body = _insert_after(body, "UM*", segment)
    (record,) = x12.parse(fx.interchange("278", [body], control="11"))
    assert is_ambiguous(record[slot]), record[slot]


def test_the_278_event_and_service_hcr_agree() -> None:
    # The fixture writes the same HCR at the event and its service level.
    body = fx.review_set(
        fx.Review("PA-1", "M1", "97110", "2026-11-20", auth_number="AUTH9")
    )
    (record,) = x12.parse(fx.interchange("278", [body], control="12"))
    assert record["decision"] == "A1" and record["auth_number"] == "AUTH9"


# -- the 277 status category (finding 2) ----------------------------------------


def _claim_world(tmp: Path, statuses: list[list[str]]) -> ReconcileSpec:
    db = tmp / "e.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (submission_id, member_id, service_code, service_date)")
    con.execute("INSERT INTO t VALUES ('C1', 'M1', '99213', '2026-10-01')")
    con.commit()
    con.close()
    inbox = tmp / "in"
    inbox.mkdir(exist_ok=True)
    for old in inbox.iterdir():
        old.unlink()
    for i, body in enumerate(statuses, start=1):
        (inbox / f"{i}.x12").write_text(
            fx.interchange("277", [body], control=str(i), date=f"202611{i:02d}")
        )
    same = {
        k: {"param": f"ehr.{k}"} for k in ("member_id", "service_code", "service_date")
    }
    return ReconcileSpec.model_validate(
        {
            "unit": "claim",
            "anchor": "ehr",
            "legs": {
                "ehr": {
                    "role": "ehr",
                    "verifier": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": "SELECT * FROM t",
                    },
                    "identity": {"submission_id": {"param": "submission_id"}},
                    "provides": {
                        "member_id": "member_id",
                        "service_code": "service_code",
                        "service_date": "service_date",
                    },
                },
                "clearinghouse": {
                    "role": "clearinghouse",
                    "verifier": {
                        "kind": "x12",
                        "options": {"root": str(inbox), "transaction": "277"},
                    },
                    "identity": {"trace": {"param": "submission_id"}},
                    "compare": same,
                    "duplicate_key": same,
                    "group_by": ["trace"],
                    "latest_by": "sent_at",
                    "distinct_by": ["payer_claim_number"],
                    "accept": SHIPPED_ACCEPT,
                },
            },
        }
    )


def _breaks(result) -> set[tuple[str, str, str]]:
    return {(b.kind, b.leg, b.outcome.value) for b in result.breaks}


def test_shipped_claim_spec_requires_an_accepted_status() -> None:
    rule = SHIPPED_ACCEPT["status_category"]
    assert {"A3", "A4", "A6", "A7", "A8"} <= set(rule["refute"])
    assert "A1" in rule["values"] and "A7" not in rule["values"]
    ReconcileSpec.load(_BENCH / "claim.reconcile.yaml")  # still a valid spec


@pytest.mark.parametrize(
    ("category", "outcome"),
    [
        ("A1", Outcome.CONFIRMED),
        ("A7", Outcome.REFUTED),  # rejected for invalid information
        ("A6", Outcome.REFUTED),  # rejected for missing information
        ("A3", Outcome.REFUTED),  # returned as unprocessable
        ("E0", Outcome.UNCERTAIN),  # neither accepted nor rejected
        ("", Outcome.UNCERTAIN),  # no category at all
    ],
)
def test_a_rejected_claim_is_never_confirmed(tmp_path, category, outcome) -> None:
    spec = _claim_world(tmp_path, [_status_body(category=category, status="21")])
    result = reconcile(spec, {"submission_id": "C1"}, now=NOW)
    assert result.outcome is outcome, result.reasons
    if outcome is not Outcome.CONFIRMED:
        assert ("not_accepted", "clearinghouse", outcome.value) in _breaks(result)


def test_a_rejection_after_an_acknowledgement_is_judged(tmp_path) -> None:
    spec = _claim_world(
        tmp_path, [_status_body(category="A1"), _status_body(category="A7")]
    )
    result = reconcile(spec, {"submission_id": "C1"}, now=NOW)
    assert result.outcome is Outcome.REFUTED


def test_conflicting_stc_in_one_277_is_uncertain(tmp_path) -> None:
    body = _insert_after(_status_body(), "STC*", "STC*A7:21*20261102**125.00")
    result = reconcile(_claim_world(tmp_path, [body]), {"submission_id": "C1"}, now=NOW)
    assert result.outcome is Outcome.UNCERTAIN, result.reasons


def test_fixture_set_plants_rejected_returned_and_unknown_277s() -> None:
    import json

    units = json.loads((_BENCH / "fixtures.json").read_text())["units"]
    faults = {u["fault"]: u["expected"] for u in units}
    assert faults["rejected_clearinghouse"] == [
        ["not_accepted", "clearinghouse", "REFUTED"]
    ]
    assert faults["returned_clearinghouse"] == [
        ["not_accepted", "clearinghouse", "REFUTED"]
    ]
    assert faults["unknown_status_clearinghouse"] == [
        ["not_accepted", "clearinghouse", "UNCERTAIN"]
    ]


# -- FHIR list values ------------------------------------------------------------


def test_fhir_lists_fan_out_and_resolve() -> None:
    claim = {
        "resourceType": "Claim",
        "id": "c1",
        "item": [
            {"productOrService": {"coding": [{"code": "99213"}]}},
            {"productOrService": {"coding": [{"code": "99214"}]}},
        ],
    }
    path = "item.productOrService.coding.code"
    assert extract_all(claim, path) == ["99213", "99214"]
    reader = FhirEffectVerifier(
        "http://fhir.test/fhir/R4",
        resource_type="Claim",
        field_paths={"id": "id", "service_code": path},
        access_token="synthetic-test-token",
    )
    assert reader._flatten(claim)["service_code"] == "99213"  # engine default
    reader.value_resolver = resolve
    assert is_ambiguous(reader._flatten(claim)["service_code"])
    claim["item"][1] = copy.deepcopy(claim["item"][0])
    assert reader._flatten(claim)["service_code"] == "99213"


# -- the property over generated reads (finding 4) ---------------------------------

_STORE: dict[str, list[dict[str, Any]]] = {}


class _Memory(VerifierAdapterBase):
    """A store held in memory: the read is exactly the generated records."""

    substrate = "memory"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, key: str) -> None:
        self.key = key

    def capture_pre_state(self, context: Any = None) -> EffectState:
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=copy.deepcopy(_STORE[self.key]),
        )

    def verify(self, expected: Any, before: EffectState, context: Any = None) -> Any:
        raise NotImplementedError


register_verifier_factory(
    "memory-test",
    lambda cfg, params=None: _Memory(str(cfg.options["key"])),
    replace=True,
)

SAME = {
    "member_id": {"param": "ehr.member_id"},
    "service_code": {"param": "ehr.service_code"},
    "service_date": {"param": "ehr.service_date"},
}
SPEC = ReconcileSpec.model_validate(
    {
        "unit": "claim",
        "anchor": "ehr",
        "legs": {
            "ehr": {
                "role": "ehr",
                "verifier": {"kind": "memory-test", "options": {"key": "ehr"}},
                "identity": {
                    "submission_id": {"param": "submission_id"},
                    "use": "claim",
                },
                "join": {"member_id": {"via": "patient_ref", "field": "member_id"}},
                "provides": {
                    k: k
                    for k in (
                        "member_id",
                        "service_code",
                        "service_date",
                        "submitted_at",
                    )
                },
            },
            "clearinghouse": {
                "role": "clearinghouse",
                "verifier": {"kind": "memory-test", "options": {"key": "ch"}},
                "identity": {"trace": {"param": "submission_id"}},
                "compare": SAME,
                "duplicate_key": SAME,
                "group_by": ["trace"],
                "latest_by": "sent_at",
                "distinct_by": ["payer_claim_number"],
                "accept": SHIPPED_ACCEPT,
                "due": {"after": "ehr.submitted_at", "within_s": 259200},
            },
        },
    }
)

#: Every field each record's selection or verdict reads (the judged slots).
EHR_CLAIM_SLOTS = [
    "submission_id",
    "use",
    "patient_ref",
    "service_code",
    "service_date",
    "submitted_at",
]
PATIENT_SLOTS = ["resourceType", "id", "member_id"]
#: A status the group no longer judges still decides which one is latest.
HISTORY_SLOTS = ["trace", "sent_at", "payer_claim_number"]
LATEST_SLOTS = [
    *HISTORY_SLOTS,
    "member_id",
    "service_code",
    "service_date",
    "status_category",
]

text = st.text(alphabet="ABCDEFGHJKMNPQRSTUVWXYZ0123456789", min_size=1, max_size=6)


@st.composite
def worlds(draw: Any) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """A clean read of one claim: the EHR claim and patient, the claim's 277
    history (one to three statuses, the latest accepted) and other claims."""

    code, day, member = draw(text), draw(text), draw(text)
    ehr = [
        {
            "resourceType": "Claim",
            "id": "c1",
            "submission_id": "S1",
            "use": "claim",
            "patient_ref": "Patient/p1",
            "service_code": code,
            "service_date": day,
            "submitted_at": "2026-10-30T09:00:00Z",
        },
        {"resourceType": "Patient", "id": "p1", "member_id": member},
    ]
    accepted = SHIPPED_ACCEPT["status_category"]["values"]
    k = draw(st.integers(1, 3))
    history = [
        {
            "id": f"h{i}",
            "trace": "S1",
            "member_id": member,
            "service_code": code,
            "service_date": day,
            "status_category": draw(st.sampled_from(accepted)),
            "sent_at": f"2026-11-0{i + 1}T10:00:00Z",
            "payer_claim_number": "PCN1",
        }
        for i in range(k)
    ]
    others = [
        {
            "id": f"o{i}",
            "trace": f"OTHER{i}",
            "member_id": draw(text),
            # Another claim's service: never this unit's duplicate key.
            "service_code": "other-" + draw(text),
            "service_date": draw(text),
            "status_category": "A1",
            "sent_at": "2026-11-01T10:00:00Z",
            "payer_claim_number": f"PCNO{i}",
        }
        for i in range(draw(st.integers(0, 3)))
    ]
    return {"ehr": ehr, "ch": history + others}, k


def _other(draw: Any, value: Any) -> Any:
    return draw(text.filter(lambda t: t != str(value)))


@st.composite
def conflicted(draw: Any) -> tuple[dict, dict, str]:
    """A clean world, and the same world with one judged slot given two or
    more distinct candidates."""

    world, k = draw(worlds())
    bad = copy.deepcopy(world)
    how = draw(
        st.sampled_from(
            ["value", "join", "second_record", "tie", "distinct", "history_value"]
        )
    )
    if how == "value":
        # One record's judged slot reads two different values.
        target = draw(st.sampled_from(["claim", "patient", "latest"]))
        if target == "claim":
            record, slot = bad["ehr"][0], draw(st.sampled_from(EHR_CLAIM_SLOTS))
        elif target == "patient":
            record, slot = bad["ehr"][1], draw(st.sampled_from(PATIENT_SLOTS))
        else:
            record, slot = bad["ch"][k - 1], draw(st.sampled_from(LATEST_SLOTS))
        record[slot] = resolve([record[slot], _other(draw, record[slot])])
    elif how == "history_value":
        # A status no longer judged still holds a slot that picks the latest.
        record = bad["ch"][draw(st.integers(0, k - 1))]
        slot = draw(st.sampled_from(HISTORY_SLOTS))
        record[slot] = resolve([record[slot], _other(draw, record[slot])])
    elif how == "join":
        # Two different records under the reference the claim joins through.
        twin = dict(bad["ehr"][1], member_id=_other(draw, bad["ehr"][1]["member_id"]))
        bad["ehr"].insert(draw(st.integers(0, 2)), twin)
    elif how == "second_record":
        # A second, different record the identity selects.
        leg = draw(st.sampled_from(["ehr", "ch"]))
        source = bad["ehr"][0] if leg == "ehr" else bad["ch"][k - 1]
        field = draw(
            st.sampled_from(
                sorted(
                    k_
                    for k_ in source
                    # A twin that joins elsewhere is an unreadable join, not a twin.
                    if k_ not in ("id", "resourceType", "patient_ref")
                )
            )
        )
        twin = dict(source, id="twin")
        if field not in ("submission_id", "use", "trace", "sent_at"):
            twin[field] = _other(draw, source[field])
        bad[leg].insert(draw(st.integers(0, len(bad[leg]))), twin)
    elif how == "tie":
        # Another status at the latest time that says something else.
        latest = bad["ch"][k - 1]
        field = draw(st.sampled_from(["status_category", "member_id", "service_code"]))
        twin = dict(latest, id="tie")
        twin[field] = _other(draw, latest[field])
        bad["ch"].insert(draw(st.integers(0, len(bad["ch"]))), twin)
    else:
        # A status in the history with another payer claim number.
        record = bad["ch"][draw(st.integers(0, k - 1))]
        twin = dict(record, id="split", payer_claim_number=_other(draw, "PCN1"))
        twin["sent_at"] = draw(st.sampled_from([r["sent_at"] for r in bad["ch"][:k]]))
        bad["ch"].insert(draw(st.integers(0, len(bad["ch"]))), twin)
    return world, bad, how


#: What each kind of conflict must give: ambiguity is never a refutation;
#: a second definite record is the duplicate policy's REFUTED.
EXPECTED = {
    "value": Outcome.UNCERTAIN,
    "history_value": Outcome.UNCERTAIN,
    "join": Outcome.UNCERTAIN,
    "second_record": Outcome.REFUTED,
    "tie": Outcome.REFUTED,
    "distinct": Outcome.REFUTED,
}


def _run(world: dict[str, list[dict[str, Any]]]) -> Outcome:
    _STORE.clear()
    _STORE.update(world)
    return reconcile(SPEC, {"submission_id": "S1"}, now=NOW).outcome


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(conflicted())
def test_a_judged_slot_with_two_candidates_is_never_confirmed(case) -> None:
    clean, bad, how = case
    assert _run(clean) is Outcome.CONFIRMED  # the property isn't vacuous
    outcome = _run(bad)
    assert outcome is not Outcome.CONFIRMED
    assert outcome is EXPECTED[how], how


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    prefix=st.sampled_from(["STC*", "REF*1K*", "DTP*472*", "SVC*", "NM1*IL*"]),
    same=st.booleans(),
    category=st.sampled_from(["A0", "A1", "A2", "F1", "P1"]),
    other=st.sampled_from(["A1", "A3", "A7", "F1", "E0"]),
)
def test_x12_repeats_resolve_by_one_rule(prefix, same, category, other) -> None:
    body = _status_body(category=category, payer_claim_number="PCN1")
    seg = next(s for s in body if s.startswith(prefix))
    if not same:
        swap: dict[str, tuple[str, str]] = {
            "STC*": (f"STC*{category}:", f"STC*{other}9:"),
            "REF*1K*": ("PCN1", "PCN2"),
            "DTP*472*": ("20261001", "20261003"),
            "SVC*": ("HC:99213", "HC:99215"),
            "NM1*IL*": ("*MI*M1", "*MI*M2"),
        }
        old, new = swap[prefix]
        seg = seg.replace(old, new)
    body = _insert_after(body, prefix, seg)
    with tempfile.TemporaryDirectory() as tmp:
        result = reconcile(
            _claim_world(Path(tmp), [body]), {"submission_id": "C1"}, now=NOW
        )
    if same:
        assert result.outcome is Outcome.CONFIRMED, result.reasons
    else:
        assert result.outcome is not Outcome.CONFIRMED


def test_ambiguity_never_downgrades_a_definite_refutation() -> None:
    record = {
        "id": "h0",
        "trace": "S1",
        "member_id": "M",
        "service_code": "C",
        "service_date": "D",
        "status_category": "A1",
        "sent_at": "2026-11-01T10:00:00Z",
        "payer_claim_number": "PCN1",
    }
    ehr = [
        {
            "resourceType": "Claim",
            "id": "c1",
            "submission_id": "S1",
            "use": "claim",
            "patient_ref": "Patient/p1",
            "service_code": "C",
            "service_date": "D",
            "submitted_at": "2026-10-30T09:00:00Z",
        },
        {"resourceType": "Patient", "id": "p1", "member_id": "M"},
    ]
    # Two definite records at one time, and a third whose trace is ambiguous.
    twin = dict(record, id="h1", payer_claim_number="PCN2")
    loose = dict(record, id="h2", trace=Ambiguous(["S1", "S9"]))
    assert _run({"ehr": ehr, "ch": [record, twin, loose]}) is Outcome.REFUTED
