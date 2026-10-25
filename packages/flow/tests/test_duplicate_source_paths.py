"""Every governed way of building a Replayer resolves one duplicate-check source.

Qualification, a run, a resume and the lifecycle proof must use the same
duplicate check, or a program is admitted under one check and run under
another. The source is resolved inside ``Replayer`` from the run's own effect
verifier (``resolve_duplicate_source``). These tests pin that:

- no construction path picks a source of its own (an AST scan of every
  ``Replayer(...)`` call);
- the governed paths all hand the Replayer their effect verifier;
- the same verifier gives the same source on every path;
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
    # Only the deployment passes the operator's switch (auto or screen),
    # never a source object.
    assert choosing == {("openadapt_flow/deployment.py", "build_replayer")}


def test_every_governed_path_hands_the_replayer_its_effect_verifier() -> None:
    calls = _replayer_calls()
    found = {(rel, fn) for rel, fn, _ in calls}
    missing = GOVERNED - found
    assert not missing, f"governed paths moved or renamed: {sorted(missing)}"
    for rel, fn, call in calls:
        if (rel, fn) not in GOVERNED:
            continue
        names = {k.arg for k in call.keywords}
        # ``**kwargs`` (qualify) must carry it; checked below by name.
        assert "effect_verifier" in names or None in names, (rel, fn)
    campaign = (FLOW / "openadapt_flow/qualify/campaign.py").read_text()
    assert "effect_verifier=verifier" in campaign


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
    ("verifier", "kind"), [(_fhir, FhirPatientSource), (_rest, RestPatientSource)]
)
def test_the_same_verifier_gives_the_same_source_on_every_path(verifier, kind) -> None:
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
        assert isinstance(replayer.duplicate_record_source, kind)
        assert replayer.duplicate_record_source.base_url == v.base_url


def test_no_record_channel_means_the_screen_unless_the_program_pins_record() -> None:
    assert resolve_duplicate_source(None) is None
    assert resolve_duplicate_source(_fhir(), "screen") is None
    table = TableSource([])
    assert resolve_duplicate_source(None, table) is table


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
    passed, read = _check("screen", "record")
    assert passed is False and read == []  # never the screen instead


def test_a_program_pinned_to_the_screen_reads_the_screen() -> None:
    table = TableSource([PatientRecord("1", "Ada", "Quill", "1984-03-07")])
    passed, read = _check(table, "screen")
    assert passed is True and read  # the list was read; the record wasn't asked


def test_unset_candidate_source_serializes_as_before() -> None:
    pc = Postcondition(
        kind=PostconditionKind.NO_DUPLICATE_CANDIDATE,
        text="x",
        candidate_rows="tr",
        candidate_params=("lname",),
    )
    assert "candidate_source" not in pc.model_dump()
