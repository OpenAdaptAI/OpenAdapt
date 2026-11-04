"""ensure_record ``operations:``: what one write may also do, and what still refuses.

Two parts. The judgment on snapshots, for each rule and each way it must
still refuse. Then real Medplum runs: the write leaderboard's frozen server
history (benchmark/fhir_medplum/leaderboard_rescore), replayed through the
verifier's own read path. Those tests use only runs the live verifier judged,
whose verdicts the replay reproduces exactly.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from openadapt_flow.ensure_record import (
    Baseline,
    Change,
    EnsureContract,
    holds_only_task_values,
    judge,
)

BOT = "Practitioner/bot"
OTHER = "Practitioner/clinician"
READER = "ClientApplication/someone"
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
PARAMS = {
    "given": "Ada",
    "family": "Quill",
    "birthDate": "1984-03-07",
    "email": "ada.quill@example.test",
    "phone": "617-555-0142",
}


def contract(**operations) -> EnsureContract:
    return EnsureContract.model_validate(
        {
            "resource_type": "Patient",
            "base_url": "http://fhir.test/fhir/R4",
            "self": [BOT],
            "fields": {
                "given": {"path": "name.given"},
                "family": {"path": "name.family"},
                "birthDate": {"path": "birthDate", "compare": "date"},
                "email": {"path": "telecom[system=email].value", "compare": "casefold"},
                "phone": {"path": "telecom[system=phone].value", "compare": "digits"},
                "gender": {"path": "gender"},
            },
            "identity": [["given", "family", "birthDate"], ["email"]],
            "search": {
                "given": "given",
                "family": "family",
                "birthDate": "birthdate",
                "email": "email",
            },
            "closed": True,
            "settle_s": 1,
            "operations": operations,
        }
    )


def patient(pid: str, **over) -> dict:
    res = {
        "resourceType": "Patient",
        "id": pid,
        "meta": {"versionId": "1", "lastUpdated": "2026-10-28T12:00:05Z"},
        "name": [{"given": ["Ada"], "family": "Quill"}],
        "birthDate": "1984-03-07",
        "telecom": [
            {"system": "phone", "value": "(617) 555-0142"},
            {"system": "email", "value": "ada.quill@example.test"},
        ],
    }
    res.update(over)
    return {k: v for k, v in res.items() if v is not None}


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def base(*matches: dict) -> Baseline:
    return Baseline(started=T0, matches={m["id"]: m for m in matches})


def written(ref: str, *steps: tuple, bodies: list, reads=(), prior=None) -> Change:
    """``steps`` are (seconds, action, actor); one version per create or update."""

    events = [(_t(s), a, frozenset({who})) for s, a, who in steps]
    return Change(
        ref=ref,
        actions={a for _, a, _ in events},
        authors={w for _, _, who in events for w in who},
        events=events,
        reads=[(_t(s), frozenset({who})) for s, who in reads],
        versions=len(bodies),
        bodies=bodies,
        prior=prior,
    )


CONSISTENT = {"correction": {"between": "consistent"}}
TRANSIENT = {"cardinality": {"own_transient": True}}


# -- a record saved in steps -----------------------------------------------------------


def stepped(*first: dict) -> dict[str, Change]:
    """The agent created Patient/x as ``first`` versions, then completed it."""

    steps = [(1 + i, "C" if i == 0 else "U", BOT) for i in range(len(first) + 1)]
    return {"Patient/x": written("Patient/x", *steps, bodies=[*first, patient("x")])}


def test_a_record_saved_in_steps_counts_as_one_write_when_consistent():
    x = patient("x")
    partial = [{"resourceType": "Patient", "id": "x"}, patient("x", birthDate=None)]
    r = judge(contract(**CONSISTENT), PARAMS, base(), stepped(*partial), {"x": x}, x)
    assert (r.outcome, r.record_id) == ("created_verified", "x")
    assert r.evidence["corrected"] is True
    # The rule as it was refuses the same record: its first version is no one.
    old = judge(contract(), PARAMS, base(), stepped(*partial), {"x": x}, x)
    assert old.outcome == "needs_review"
    assert "was not the person" in old.reasons[0]


@pytest.mark.parametrize(
    "first",
    [
        {"birthDate": "1984-07-03"},  # another person's birth date, for a moment
        {"telecom": [{"system": "phone", "value": "617-555-0199"}]},  # a wrong phone
        {"gender": "female"},  # a value the task didn't give
        {"name": [{"given": ["Ada", "Bee"], "family": "Quill"}]},  # one name too many
        {"extension": [{"url": "urn:x", "valueString": "y"}]},  # an element too many
    ],
)
def test_a_version_holding_any_other_value_still_refuses(first):
    x = patient("x")
    bad = {"resourceType": "Patient", "id": "x", **first}
    r = judge(contract(**CONSISTENT), PARAMS, base(), stepped(bad), {"x": x}, x)
    assert r.outcome == "needs_review"
    assert "held a value the task didn't give" in r.reasons[0]


def test_a_person_version_with_a_wrong_phone_passes_person_but_not_consistent():
    # ``person`` checks identity only; ``consistent`` checks every value.
    x = patient("x")
    wrong = patient("x", telecom=[{"system": "phone", "value": "617-555-0199"}])
    assert judge(contract(), PARAMS, base(), stepped(wrong), {"x": x}, x).outcome == (
        "created_verified"
    )
    r = judge(contract(**CONSISTENT), PARAMS, base(), stepped(wrong), {"x": x}, x)
    assert r.outcome == "needs_review"


@pytest.mark.parametrize(
    "first, outcome",
    [
        ({"resourceType": "Patient", "id": "x"}, "created_verified"),  # saved in steps
        (
            patient("x", telecom=[{"system": "phone", "value": "617-555-0199"}]),
            "created_verified",
        ),
        (
            {"resourceType": "Patient", "id": "x", "birthDate": "1984-07-03"},
            "needs_review",
        ),
        (
            patient("x", birthDate="1984-07-03", telecom=[]),
            "needs_review",
        ),  # someone else
    ],
)
def test_either_is_never_stricter_than_person_and_accepts_steps(first, outcome):
    x = patient("x")
    either = {"correction": {"between": "either"}}
    r = judge(contract(**either), PARAMS, base(), stepped(first), {"x": x}, x)
    assert r.outcome == outcome, r.reasons
    if judge(contract(), PARAMS, base(), stepped(first), {"x": x}, x).outcome == (
        "created_verified"
    ):
        assert r.outcome == "created_verified"


def test_consistent_still_needs_every_other_correction_condition():
    x = patient("x")
    partial = {"resourceType": "Patient", "id": "x"}
    # Someone else read the record between the agent's writes.
    changes = stepped(partial)
    changes["Patient/x"].reads = [(_t(1.5), frozenset({OTHER}))]
    r = judge(contract(**CONSISTENT), PARAMS, base(), changes, {"x": x}, x)
    assert r.reasons == ["another actor read the record between the agent's writes"]
    # A version the trail didn't log.
    changes = stepped(partial)
    changes["Patient/x"].versions = 3
    assert judge(
        contract(**CONSISTENT), PARAMS, base(), changes, {"x": x}, x
    ).outcome == ("needs_review")
    # Corrections turned off by either switch.
    for c in (
        contract(correction={"accept": False, "between": "consistent"}),
        contract(**CONSISTENT).model_copy(update={"accept_corrections": False}),
    ):
        assert judge(c, PARAMS, base(), stepped(partial), {"x": x}, x).outcome == (
            "needs_review"
        )


def test_holds_only_task_values_on_a_void_version_is_false():
    c = EnsureContract.model_validate(
        {**contract().model_dump(by_alias=True), "void": ['gender="unknown"']}
    )
    assert holds_only_task_values(c, {"resourceType": "Patient"}, PARAMS)
    assert not holds_only_task_values(
        c, {"resourceType": "Patient", "gender": "unknown"}, PARAMS
    )


# -- a second record the agent created and deleted again ----------------------------------


def double_submit(**over) -> dict[str, Change]:
    keep = written("Patient/x", (1, "C", BOT), bodies=[patient("x")])
    gone = written(
        "Patient/y",
        (2, "C", BOT),
        (4, over.pop("deleted_by", "D"), over.pop("by", BOT)),
        bodies=over.pop("bodies", [patient("y")]),
        reads=over.pop("reads", ()),
    )
    gone.actions.add("D")
    for k, v in over.items():
        setattr(gone, k, v)
    return {"Patient/x": keep, "Patient/y": gone}


def test_a_double_submit_the_agent_cleaned_up_is_one_record():
    x = patient("x")
    r = judge(contract(**TRANSIENT), PARAMS, base(), double_submit(), {"x": x}, x)
    assert (r.outcome, r.record_id) == ("created_verified", "x")
    assert r.evidence["transient_records"] == 1
    assert "deleted 1 record(s) it had created by mistake" in r.reasons[0]
    off = judge(contract(), PARAMS, base(), double_submit(), {"x": x}, x)
    assert "the agent created 2 Patient records, not one" in off.reasons


@pytest.mark.parametrize(
    "over",
    [
        {"reads": [(3, OTHER)]},  # someone opened the duplicate
        {"by": OTHER},  # someone else deleted it
        {"bodies": [patient("y", birthDate="1984-07-03")]},  # it held a wrong value
        {"versions": 2},  # a version the trail didn't log
        {"attributed": False},
    ],
)
def test_a_duplicate_anyone_else_could_have_used_still_refuses(over):
    x = patient("x")
    r = judge(contract(**TRANSIENT), PARAMS, base(), double_submit(**over), {"x": x}, x)
    assert r.outcome == "needs_review"


def test_a_duplicate_still_there_is_a_duplicate():
    x, y = patient("x"), patient("y")
    changes = {
        "Patient/x": written("Patient/x", (1, "C", BOT), bodies=[x]),
        "Patient/y": written("Patient/y", (2, "C", BOT), bodies=[y]),
    }
    r = judge(contract(**TRANSIENT), PARAMS, base(), changes, {"x": x, "y": y}, None)
    assert r.outcome == "needs_review"


# -- bookkeeping on another record --------------------------------------------------------


BOOK = {
    "bookkeeping": [{"resource_type": "Practitioner", "paths": ["meta", "extension"]}]
}
DOC = {
    "resourceType": "Practitioner",
    "id": "d",
    "meta": {"versionId": "1"},
    "name": [{"family": "Osei"}],
}


def touched(after: dict, *, ref="Practitioner/d", prior=DOC, actions=("U",)) -> Change:
    c = written(ref, *[(2, a, BOT) for a in actions], bodies=[after], prior=prior)
    return c


def created_with(other: Change) -> dict[str, Change]:
    return {
        "Patient/x": written("Patient/x", (1, "C", BOT), bodies=[patient("x")]),
        other.ref: other,
    }


def test_an_update_of_bookkeeping_elements_only_is_not_a_write():
    x = patient("x")
    after = {
        **DOC,
        "meta": {"versionId": "2", "lastUpdated": "2026-10-28T12:00:02Z"},
        "extension": [
            {"url": "urn:app:last-seen", "valueInstant": "2026-10-28T12:00:02Z"}
        ],
    }
    r = judge(
        contract(**BOOK), PARAMS, base(), created_with(touched(after)), {"x": x}, x
    )
    assert (r.outcome, r.evidence["bookkeeping_writes"]) == ("created_verified", 1)
    assert judge(
        contract(), PARAMS, base(), created_with(touched(after)), {"x": x}, x
    ).outcome == ("needs_review")


@pytest.mark.parametrize(
    "after, prior",
    [
        ({**DOC, "name": [{"family": "Lind"}]}, DOC),  # a real change
        (
            {**DOC, "meta": {"versionId": "2", "tag": [{"code": "hidden"}]}},
            DOC,
        ),  # a label
        ({**DOC, "meta": {"versionId": "2"}}, None),  # no version before the window
    ],
)
def test_bookkeeping_excuses_nothing_else(after, prior):
    x = patient("x")
    r = judge(
        contract(**BOOK),
        PARAMS,
        base(),
        created_with(touched(after, prior=prior)),
        {"x": x},
        x,
    )
    assert r.outcome == "needs_review"


def test_bookkeeping_never_excuses_the_record_or_the_person():
    book = {"bookkeeping": [{"resource_type": "Patient", "paths": ["meta"]}]}
    x = patient("x")
    touched_x = touched({**x, "meta": {"versionId": "2"}}, ref="Patient/x", prior=x)
    # A found record the agent touched, even in meta only, is a write.
    r = judge(
        contract(**book), PARAMS, base(x), {"Patient/x": touched_x}, {"x": x}, None, x
    )
    assert r.outcome == "needs_review"


@pytest.mark.parametrize(
    "path", ["meta.security", "meta.tag.code", "id", "telecom[system=x]", "meta*"]
)
def test_a_bookkeeping_path_that_could_hide_a_change_is_refused(path):
    with pytest.raises(Exception):
        contract(bookkeeping=[{"resource_type": "Practitioner", "paths": [path]}])


# -- real Medplum runs, from the leaderboard's frozen history ------------------------------

RESCORE = (
    Path(__file__).resolve().parents[1] / "benchmark/fhir_medplum/leaderboard_rescore"
)


@lru_cache(maxsize=None)
def _stack(agent: str):
    from benchmark.fhir_medplum.leaderboard_rescore import rescore

    times = json.loads((RESCORE / "inputs/times.json").read_text())
    return rescore, rescore.Stack(agent, times), times


def replay(key: str, which: str) -> dict:
    rescore, stack, times = _stack(key.split("/")[0])
    rows = {
        f"{key.split('/')[0]}/runs/{r['_name']}": r
        for r in rescore.load_rows(key.split("/")[0])
    }
    row = rows[key]
    row["_key"] = key
    suite = yaml.safe_load(rescore.CASES.read_text())
    case = {c["id"]: c for c in suite["cases"]}[row["id"]]
    return rescore.judge_row(stack, row, times[key], case, suite, which) | {
        "live": row["verifier"]
    }


@pytest.mark.parametrize(
    "key",
    [
        "claude/runs/r1-P05",  # created as {}, completed in three updates
        "claude/runs/r1-P11",  # created as {}, completed in two updates
        "claude/runs/r1-P12",  # created with the name only, completed
    ],
)
def test_medplum_a_new_patient_saved_in_steps_is_verified(key):
    old, new = replay(key, "main"), replay(key, "operations")
    assert old["outcome"] == old["live"]["outcome"] == "needs_review"
    assert (new["outcome"], new["evidence"]["corrected"]) == ("created_verified", True)


@pytest.mark.parametrize(
    "key",
    [
        "claude/runs/r1-P04",  # first saved with the wrong birth date
        "openai/runs/r1-A04",  # an allergy first saved on the wrong patient
        "openai/runs/r1-A05",  # first saved with the wrong substance code
        "openai/runs/r1-AP11",  # an appointment first booked for the wrong patient
    ],
)
def test_medplum_a_wrong_first_version_still_needs_review(key):
    r = replay(key, "operations")
    assert r["outcome"] == "needs_review"
    assert any("held a value the task didn't give" in x for x in r["reasons"])


def test_medplum_a_lost_response_resubmitted_and_cleaned_up_is_one_record():
    old, new = (
        replay("claude/runs/r1-P08", "main"),
        replay("claude/runs/r1-P08", "operations"),
    )
    assert "the agent created 2 Patient records, not one" in old["reasons"]
    assert (new["outcome"], new["evidence"]["transient_records"]) == (
        "created_verified",
        1,
    )


@pytest.mark.parametrize(
    "key",
    [
        "claude/runs/r1-P09",  # said done; another patient was changed too
        "openai/runs/r1-A07",  # said done; a duplicate allergy is still there
        "openai/runs/r1-AP05",  # said done; two appointments
        "openai/runs/r1-A08",  # said done; another patient was changed too
        "openai/runs/r1-P05",  # said done; the phone is wrong
    ],
)
def test_medplum_misreports_are_still_flagged(key):
    assert replay(key, "operations")["outcome"] == "needs_review"


def test_operations_contracts_are_the_public_contracts_plus_the_medplum_spec():
    public = RESCORE.parent / "public" / "contracts"
    for kind in ("patient", "allergy", "appointment"):
        a = yaml.safe_load((public / f"{kind}.yaml").read_text())
        b = yaml.safe_load(
            (RESCORE / "contracts_operations" / f"{kind}.yaml").read_text()
        )
        assert set(b) - set(a) == {"semantics"}
        assert {k: v for k, v in b.items() if k != "semantics"} == a


def test_a_contract_loads_its_pinned_app_semantics(tmp_path, monkeypatch):
    from openadapt_flow.ensure_record import EnsureRecordError, load_contract

    for k in ("BENCH_FHIR_URL", "BENCH_AGENT_REF"):
        monkeypatch.setenv(k, "http://x/fhir" if k == "BENCH_FHIR_URL" else BOT)
    c = load_contract(RESCORE / "contracts_operations" / "allergy.yaml")
    assert c.operations.correction.between == "consistent"
    assert c.operations.cardinality.own_transient is True
    text = (RESCORE / "contracts_operations" / "allergy.yaml").read_text()
    spec = RESCORE.parent / "app_semantics" / "medplum.yaml"
    bad = [
        text.replace("version: 1}", "version: 2}"),  # a pin the file doesn't match
        text.replace("app: medplum", "app: openemr"),
        text + "operations: {}\n",  # two sources
        text.replace("../../app_semantics/medplum.yaml", str(tmp_path / "none.yaml")),
    ]
    for i, body in enumerate(bad):
        f = (RESCORE / "contracts_operations") / f"_tmp_{i}.yaml"
        try:
            f.write_text(body)
            with pytest.raises(EnsureRecordError):
                load_contract(f)
        finally:
            f.unlink()
    assert spec.exists()


def test_a_bookkeeping_time_stamp_must_be_near_the_versions_own_time():
    book = {
        "bookkeeping": [
            {
                "resource_type": "Practitioner",
                "paths": ["meta", "extension"],
                "now_checks": True,
            }
        ]
    }
    x = patient("x")

    def stamped(at: str) -> Change:
        after = {
            **DOC,
            "meta": {"versionId": "2", "lastUpdated": "2026-10-28T12:00:02Z"},
            "extension": [{"url": "urn:app:last-seen", "valueInstant": at}],
        }
        return touched(after)

    ok = judge(
        contract(**book),
        PARAMS,
        base(),
        created_with(stamped("2026-10-28T12:00:01Z")),
        {"x": x},
        x,
    )
    assert ok.outcome == "created_verified"
    late = judge(
        contract(**book),
        PARAMS,
        base(),
        created_with(stamped("2025-01-01T00:00:00Z")),
        {"x": x},
        x,
    )
    assert late.outcome == "needs_review"


# -- pairs: every fault family with a legitimate variant that must confirm --------------


def _pair_cases() -> list[
    tuple[str, str, dict, dict, Baseline, dict, dict | None, str]
]:
    """(family, variant, operations, changes, baseline, after, created, outcome)."""

    x = patient("x")
    partial = {"resourceType": "Patient", "id": "x"}
    wrong = {"resourceType": "Patient", "id": "x", "birthDate": "1984-07-03"}
    meta_only = {
        **DOC,
        "meta": {"versionId": "2", "lastUpdated": "2026-10-28T12:00:02Z"},
    }
    renamed = {**DOC, "name": [{"family": "Lind"}]}
    other = patient(
        "o", name=[{"given": ["Bo"], "family": "Ng"}], birthDate="1950-01-01"
    )
    o_edit = written(
        "Patient/o", (2, "U", BOT), bodies=[{**other, "gender": "male"}], prior=other
    )
    ops = {**CONSISTENT, **TRANSIENT, **BOOK}
    one = {"Patient/x": written("Patient/x", (1, "C", BOT), bodies=[x])}
    return [
        (
            "wrong value",
            "fault: a wrong first version, corrected",
            ops,
            stepped(wrong),
            base(),
            {"x": x},
            x,
            "needs_review",
        ),
        (
            "wrong value",
            "positive: saved in steps, then completed",
            ops,
            stepped(partial),
            base(),
            {"x": x},
            x,
            "created_verified",
        ),
        (
            "duplicate",
            "fault: a second record left",
            ops,
            {
                **one,
                "Patient/y": written("Patient/y", (2, "C", BOT), bodies=[patient("y")]),
            },
            base(),
            {"x": x, "y": patient("y")},
            None,
            "needs_review",
        ),
        (
            "duplicate",
            "positive: the agent's own double submit, cleaned up",
            ops,
            double_submit(),
            base(),
            {"x": x},
            x,
            "created_verified",
        ),
        (
            "collateral",
            "fault: another patient edited",
            ops,
            {**one, "Patient/o": o_edit},
            base(),
            {"x": x},
            x,
            "needs_review",
        ),
        (
            "collateral",
            "positive: a version bump on another record",
            ops,
            created_with(touched(meta_only)),
            base(),
            {"x": x},
            x,
            "created_verified",
        ),
        (
            "collateral",
            "fault: a real change on that record",
            ops,
            created_with(touched(renamed)),
            base(),
            {"x": x},
            x,
            "needs_review",
        ),
        (
            "audit rows",
            "positive: another user's write elsewhere",
            ops,
            {
                **one,
                "Patient/o": written(
                    "Patient/o", (2, "U", OTHER), bodies=[other], prior=other
                ),
            },
            base(),
            {"x": x},
            x,
            "created_verified",
        ),
        (
            "audit rows",
            "fault: another user edits the created record",
            ops,
            {
                "Patient/x": written(
                    "Patient/x", (1, "C", BOT), (2, "U", OTHER), bodies=[x, x]
                )
            },
            base(),
            {"x": x},
            x,
            "needs_review",
        ),
    ]


@pytest.mark.parametrize(
    "family, variant, ops, changes, baseline, after, created, outcome",
    _pair_cases(),
    ids=[f"{c[0]}: {c[1]}" for c in _pair_cases()],
)
def test_pair(family, variant, ops, changes, baseline, after, created, outcome):
    r = judge(contract(**ops), PARAMS, baseline, changes, after, created)
    assert r.outcome == outcome, r.reasons


def test_every_fault_family_has_a_positive_that_confirms():
    cases = _pair_cases()
    families = {c[0] for c in cases}
    for fam in families:
        outcomes = {c[7] for c in cases if c[0] == fam}
        assert outcomes == {"needs_review", "created_verified"}, fam


def test_the_frozen_inputs_are_the_leaderboards_rows():
    for agent in ("claude", "openai"):
        with gzip.open(RESCORE / f"inputs/rows-{agent}.jsonl.gz", "rt") as fh:
            rows = [json.loads(line) for line in fh]
        assert len(rows) == 111
        assert all(
            r["_source"].startswith("04e76283a03c37a18e47887c1954728812d21533:")
            for r in rows
        )
