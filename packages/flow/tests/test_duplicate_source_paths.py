"""Every governed way of building a Replayer resolves one duplicate-check source.

Qualification, a run, a resume and the lifecycle proof must use the same
duplicate check, or a program is admitted under one check and run under
another. The source is resolved by ``Replayer`` from the run's own effect
verifier and the program's pin (``resolve_duplicate_source``). These tests
pin that:

- no construction path picks a source of its own (an AST scan of every
  ``Replayer(...)`` call);
- the governed paths all hand the Replayer their effect verifier;
- the same verifier gives the same source on every path;
- OpenEMR's REST adapter is used only by a program pinned to the record;
- resolution never crashes on a verifier it doesn't know;
- a program pinned to the record check fails closed without a channel.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.ir import Postcondition, PostconditionKind
from openadapt_flow.runtime.checks.duplicate_record import (
    FhirPatientSource,
    PatientRecord,
    RestPatientSource,
    TableSource,
    resolve_duplicate_source,
)
from openadapt_flow.runtime.effects import FhirEffectVerifier
from openadapt_flow.runtime.effects.rest import RestRecordVerifier

FLOW = Path(__file__).resolve().parents[1]
#: Governed construction paths: (file, enclosing function) of a Replayer(...)
#: call that runs a program against the system of record.
GOVERNED = {
    ("openadapt_flow/qualify/campaign.py", "_run"),
    ("benchmark/lifecycle_proof/run.py", "run"),
    ("benchmark/lifecycle_proof/run.py", "execute"),
    ("benchmark/lifecycle_proof/run.py", "_resume"),
    ("openadapt_flow/deployment.py", "build_replayer"),
}


def _replayer_calls() -> list[tuple[str, str, ast.Call]]:
    out = []
    for root in ("openadapt_flow", "benchmark"):
        for path in (FLOW / root).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = str(path.relative_to(FLOW))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "Replayer"
                    ):
                        out.append((rel, fn.name, node))
    return out


def test_no_construction_path_picks_its_own_duplicate_source() -> None:
    calls = _replayer_calls()
    assert calls, "the scan found no Replayer constructions"
    choosing = {
        (rel, fn)
        for rel, fn, call in calls
        if any(k.arg == "duplicate_record_source" for k in call.keywords)
    }
    assert choosing == set()


def test_every_governed_path_hands_the_replayer_its_effect_verifier() -> None:
    calls = _replayer_calls()
    found = {(rel, fn) for rel, fn, _ in calls}
    missing = GOVERNED - found
    assert not missing, f"governed paths moved or renamed: {sorted(missing)}"
    for rel, fn, call in calls:
        if (rel, fn) not in GOVERNED:
            continue
        names = {k.arg for k in call.keywords}
        # ``None`` is ``**kwargs`` (qualify), checked by name below.
        assert "effect_verifier" in names or None in names, (rel, fn)
    campaign = (FLOW / "openadapt_flow/qualify/campaign.py").read_text()
    assert "effect_verifier=verifier" in campaign
    # A qualification hook can't swap the duplicate check.
    assert "replayer_kwargs may not set duplicate_record_source" in campaign


class _Backend:
    url, page_title, page_count = "http://x/", "x", 1


def _fhir() -> FhirEffectVerifier:
    return FhirEffectVerifier(
        "https://emr.example.test/apis/default/fhir", access_token="t"
    )


def _rest() -> RestRecordVerifier:
    return RestRecordVerifier(
        "https://emr.example.test",
        records_path="/apis/default/api/patient?_limit=1000",
        records_key="data",
        headers={"Authorization": "Bearer t"},
    )


@pytest.mark.parametrize(
    ("verifier", "pinned", "kind"),
    [
        (_fhir, False, FhirPatientSource),
        (_fhir, True, FhirPatientSource),
        # OpenEMR's REST adapter only for a program pinned to the record.
        (_rest, False, type(None)),
        (_rest, True, RestPatientSource),
    ],
)
def test_the_same_verifier_gives_the_same_source_on_every_path(
    verifier: Any, pinned: bool, kind: type
) -> None:
    from openadapt_flow.deployment import build_replayer
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    v = verifier()
    direct = Replayer(_Backend(), vision=FakeVision(), effect_verifier=v)
    deployed = build_replayer(
        _Backend(),
        allow_egress=False,
        effect_verifier=v,
        api_actuator=None,
        durable=False,
        use_structural=False,
    )
    for replayer in (direct, deployed):
        source = replayer._duplicate_source(pinned)
        assert isinstance(source, kind)
        if source is not None:
            assert source.base_url == v.base_url


def test_resolution_is_tolerant_and_rest_is_opt_in() -> None:
    assert resolve_duplicate_source(None) is None
    table = TableSource([])
    assert resolve_duplicate_source(None, table) is table
    assert resolve_duplicate_source(_rest()) is None
    assert isinstance(
        resolve_duplicate_source(_rest(), pinned_record=True), RestPatientSource
    )

    class Double(RestRecordVerifier):
        """A test double that skips RestRecordVerifier.__init__."""

        def __init__(self) -> None:
            pass

    # No crash at construction; no source, so a pinned program fails closed.
    assert resolve_duplicate_source(Double(), pinned_record=True) is None

    class Wrapper:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

    assert isinstance(resolve_duplicate_source(Wrapper(_fhir())), FhirPatientSource)


def test_a_replayer_builds_with_an_unknown_verifier_double() -> None:
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    class Double(RestRecordVerifier):
        def __init__(self) -> None:
            pass

    replayer = Replayer(_Backend(), vision=FakeVision(), effect_verifier=Double())
    assert replayer._duplicate_source(True) is None


def _check(source: Any, pinned: Any) -> tuple[bool, list[Any]]:
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_no_duplicate_candidate import MARKER
    from tests.test_no_duplicate_candidate import _Backend as ListBackend
    from tests.test_replayer import FakeVision

    backend = ListBackend([], marker=True)  # the screen would pass
    replayer = Replayer(backend, vision=FakeVision(), duplicate_record_source=source)
    replayer._current_params = {"fname": "Ada", "lname": "Quill", "DOB": "1984-03-07"}
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text=MARKER,
        candidate_rows="tr.oneresult",
        candidate_frame_path=("#modalframe",),
        candidate_params=("lname", "DOB"),
        candidate_source=pinned,
    )
    return replayer._postcondition_passes(pc, b"", None, {}), backend.read


def test_a_program_pinned_to_the_record_fails_closed_without_a_channel() -> None:
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision

    passed, read = _check(None, "record")
    assert passed is False and read == []  # never the screen instead
    replayer = Replayer(_Backend(), vision=FakeVision())
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text="x",
        candidate_rows="tr",
        candidate_params=("lname",),
        candidate_source="record",
    )
    replayer._current_params = {}
    replayer._postcondition_passes(pc, b"", None, {})
    why = replayer._describe_postcondition(pc)
    # The reason the switch policy reads (#1517): a structured, stable phrase.
    assert why.startswith("no_duplicate_candidate (record check via none: unreadable")


def test_a_program_pinned_to_the_screen_reads_the_screen() -> None:
    table = TableSource([PatientRecord("1", "Ada", "Quill", "1984-03-07")])
    passed, read = _check(table, "screen")
    assert passed is True and read  # the list was read; the record wasn't asked


def test_unset_candidate_fields_serialize_as_before() -> None:
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text="x",
        candidate_rows="tr",
        candidate_params=("lname",),
    )
    dumped = pc.model_dump()
    assert "candidate_source" not in dumped
    assert "candidate_record_fields" not in dumped
