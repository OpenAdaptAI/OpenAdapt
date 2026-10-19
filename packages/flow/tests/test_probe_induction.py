"""``induce --probe-on``: settle a recording disagreement on a test copy.

The test copy is MockMed's transactional server
(:mod:`openadapt_flow.mockmed.fault_server`): a real HTTP store that
``POST /api/reset`` restores and ``GET /api/db`` reads. Probe runs are scored
by a ``serve-reward`` contract whose oracle is the ``rest`` recipe over that
store, so a run counts only when the judge ``serve-reward`` uses reads the
expected record back.

The screen side is a scripted MockMed session (a fresh one per run) driven by
the real :class:`~openadapt_flow.runtime.replayer.Replayer`. Its buttons write
through the same HTTP API the MockMed app uses under ``?fault=ok``:

* ``p1`` opens behind an allergy alert. Until someone presses Accept, nothing
  under it can be clicked.
* ``p2``'s chart is already signed, so its Sign chart button does nothing.
* ``p3`` is a plain chart. Sign chart writes a Signature record.

All data is synthetic.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

from openadapt_flow.compiler.induction import induce_program
from openadapt_flow.compiler.probing import (
    ProbeRefused,
    ProbeTrace,
    RunSignal,
    probe_and_induce,
    probe_target_refusals,
    require_test_copy,
    runtime_signal_of,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.ir import ActionKind, Anchor, StateKind, Step, Workflow
from openadapt_flow.mockmed.fault_server import serve as serve_fault
from openadapt_flow.reward.seed import write_bundle
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

NOTE = "Synthetic triage note for probe tests"

#: Button label -> (template colour, click point). Each template is a distinct
#: PNG, which is how the scripted vision tells the buttons apart.
BUTTONS: dict[str, tuple[tuple[int, int, int], tuple[int, int]]] = {
    "Accept": ((200, 40, 40), (150, 60)),
    "Sign chart": ((40, 200, 40), (60, 150)),
    "Save Encounter": ((40, 40, 200), (150, 150)),
    "Print summary": ((200, 200, 40), (240, 150)),
}
ALLERGY_ALERT = {"p1"}
ALREADY_SIGNED = {"p2"}


def _template(label: str) -> bytes:
    return make_png((40, 16), BUTTONS[label][0])


# ---------------------------------------------------------------------------
# The scripted MockMed session (screen side of the test copy)
# ---------------------------------------------------------------------------


class MockMedSession:
    """One fresh MockMed session. Writes go to the test copy over HTTP."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.patient: str | None = None
        self.alert = False
        self.note = ""

    def visible(self) -> set[str]:
        if self.patient is None:
            return set()
        if self.alert:
            return {"Accept", "Allergy alert"}
        return {"Sign chart", "Save Encounter", "Print summary"}

    def type(self, text: str) -> bool:
        if self.patient is None:
            self.patient = text
            self.alert = text in ALLERGY_ALERT
            return True
        if self.alert:
            return False  # the modal swallows the keystrokes
        self.note += text
        return True

    def click(self, label: str) -> None:
        if label not in self.visible():
            return
        if label == "Accept":
            self.alert = False
        elif label == "Save Encounter":
            self._write("Triage", self.note)
        elif label == "Sign chart" and self.patient not in ALREADY_SIGNED:
            self._write("Signature", "")

    def _write(self, kind: str, note: str) -> None:
        body = json.dumps(
            {"patient_id": self.patient, "type": kind, "note": note}
        ).encode()
        request = urllib.request.Request(
            self.base_url + "api/encounter?fault=ok",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5).close()


class MockMedBackend(FakeBackend):
    def __init__(self, session: MockMedSession) -> None:
        super().__init__()
        self.session = session

    def type_text(self, text):
        landed = self.session.type(text)
        self._type_accept_results = [landed]
        super().type_text(text)

    def click(self, x, y, *, double=False):
        super().click(x, y, double=double)
        for label, (_colour, point) in BUTTONS.items():
            if abs(point[0] - x) <= 12 and abs(point[1] - y) <= 12:
                self.session.click(label)
                return


class MockMedVision(FakeVision):
    def __init__(self, session: MockMedSession) -> None:
        super().__init__()
        self.session = session
        self._by_png = {_template(label): label for label in BUTTONS}

    def _match(self, label: str):
        if label not in self.session.visible():
            return None
        point = BUTTONS[label][1] if label in BUTTONS else (10, 10)
        return Match(point, (point[0] - 20, point[1] - 8, 40, 16))

    def find_template(self, screen_png, template_png, **_kw):
        label = self._by_png.get(template_png)
        return self._match(label) if label else None

    def find_text(self, screen_png, text, **_kw):
        return self._match(text)

    def text_present(self, screen_png, text, **_kw):
        return text in self.session.visible()


class MockMedTestCopy:
    """A ``ProbeTarget`` over the MockMed transactional server."""

    def __init__(self, base_url: str, *, reset: bool = True) -> None:
        self.name = "mockmed-test-copy"
        self.base_url = base_url
        self.resets = 0
        self.sessions = 0
        self._reset = reset

    def reset(self) -> None:
        self.resets += 1
        if not self._reset:
            return
        request = urllib.request.Request(
            self.base_url + "api/reset", data=b"{}", method="POST"
        )
        urllib.request.urlopen(request, timeout=5).close()

    def run(self, workflow, *, bundle_dir, run_dir, params) -> RunSignal:
        self.sessions += 1
        session = MockMedSession(self.base_url)  # a fresh session every run
        report = Replayer(
            MockMedBackend(session),
            vision=MockMedVision(session),
            poll_interval_s=0.01,
        ).run(workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params)
        return RunSignal(runtime_signal_of(report), model_calls=report.model_calls)


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------


def _type(step_id: str, field: str, value: str) -> Step:
    return Step(id=step_id, intent=f"type {field}", action=ActionKind.TYPE, text=value)


def _click(step_id: str, label: str, *, intent: str | None = None) -> Step:
    point = BUTTONS[label][1]
    return Step(
        id=step_id,
        intent=intent or f"click {label}",
        action=ActionKind.CLICK,
        risk="irreversible",
        anchor=Anchor(
            template=f"templates/{label.replace(' ', '_').lower()}.png",
            region=(point[0] - 20, point[1] - 8, 40, 16),
            click_point=point,
            ocr_text=label,
        ),
    )


def _recording(tmp: Path, label: str, patient: str, extra: list[Step], at: int):
    steps = [
        _type("s_patient", "patient", patient),
        _type("s_note", "note", NOTE),
        _click("s_save", "Save Encounter"),
    ]
    steps[at:at] = extra
    workflow = Workflow(name="chart-note", steps=steps, params={"patient_id": patient})
    bundle = tmp / label
    (bundle / "templates").mkdir(parents=True)
    for step in steps:
        if step.anchor is not None:
            (bundle / step.anchor.template).write_bytes(_template(step.anchor.ocr_text))
    return ProbeTrace(workflow=workflow, bundle_dir=bundle, label=label)


def incidental_traces(tmp: Path) -> list[ProbeTrace]:
    """A clicked Sign chart on p2, where it was already signed (a no-op)."""
    return [
        _recording(tmp, "rec-a", "p2", [_click("s_sign", "Sign chart")], at=2),
        _recording(tmp, "rec-b", "p3", [], at=2),
    ]


def branch_traces(tmp: Path) -> list[ProbeTrace]:
    """A accepted p1's allergy alert; B's patient had none."""
    accept = _click("s_accept", "Accept", intent="accept the allergy alert")
    return [
        _recording(tmp, "rec-a", "p1", [accept], at=1),
        _recording(tmp, "rec-b", "p3", [], at=1),
    ]


def undecidable_traces(tmp: Path) -> list[ProbeTrace]:
    """A printed a summary. Printing writes nothing, so both readings hold."""
    return [
        _recording(tmp, "rec-a", "p2", [_click("s_print", "Print summary")], at=2),
        _recording(tmp, "rec-b", "p3", [], at=2),
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mockmed() -> Iterator[tuple[str, object]]:
    url, db, stop = serve_fault(port=0)
    yield url, db
    stop()


def _contract(directory: Path, base_url: str) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    write_bundle(
        directory,
        contract_id="reward_contract_mockmed_probe",
        oracle={"kind": "rest", "base_url": base_url.rstrip("/")},
        channel="api",
        key=Ed25519PrivateKey.generate(),
        issuer_key_id="self_signed:test",
        certify=False,
        required_effects=[
            {
                "kind": "record_written",
                "match": {
                    "patient_id": {"param": "patient_id"},
                    "type": "Triage",
                    "note": NOTE,
                },
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
        forbidden_effects=[
            {
                "kind": "record_written",
                "match": {"patient_id": {"param": "patient_id"}, "type": "Signature"},
                "expected_count": 1,
            }
        ],
    )
    return directory


@pytest.fixture()
def contract(tmp_path: Path, mockmed) -> Path:
    return _contract(tmp_path / "contract", mockmed[0])


def _probe(traces, mockmed, contract, tmp_path, **kw):
    target = MockMedTestCopy(mockmed[0])
    result = probe_and_induce(
        traces,
        target=target,
        reward_contract=contract,
        work_dir=tmp_path / "probe",
        **kw,
    )
    return result, target


# ---------------------------------------------------------------------------
# Without probing, each disagreement is refused (the baseline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make, kind",
    [
        (incidental_traces, "unconfirmed_optional"),
        (branch_traces, "unconfirmed_branch"),
        (undecidable_traces, "unconfirmed_optional"),
    ],
)
def test_plain_induction_refuses_each_disagreement(tmp_path, make, kind):
    result = induce_program([t.workflow for t in make(tmp_path)])
    assert result.certified is False
    assert [u.kind for u in result.uncertainties] == [kind]
    assert result.probe_target is None and result.probe_runs == []


# ---------------------------------------------------------------------------
# Done criteria
# ---------------------------------------------------------------------------


def test_incidental_click_is_resolved(tmp_path, mockmed, contract):
    result, target = _probe(incidental_traces(tmp_path), mockmed, contract, tmp_path)

    assert result.certified is True
    [verdict] = result.probe_verdicts
    assert verdict.certified == "incidental"
    assert verdict.interpretations == {"incidental": "passed", "conditional": "failed"}
    # The program has no Sign chart step at all.
    steps = [
        s.step.id for s in result.program.states.values() if s.kind is StateKind.ACTION
    ]
    assert "s_sign" not in steps
    assert any(d.kind == "incidental" for d in result.column_decisions)
    # The conditional reading signed p3's chart: a forbidden effect.
    signed = [
        r
        for r in result.probe_runs
        if r.interpretation == "conditional" and r.context == "rec-b"
    ]
    assert [r.outcome for r in signed] == ["wrong_effect"]


def test_real_branch_is_resolved(tmp_path, mockmed, contract):
    result, _target = _probe(branch_traces(tmp_path), mockmed, contract, tmp_path)

    assert result.certified is True
    [verdict] = result.probe_verdicts
    assert verdict.certified == "conditional"
    assert verdict.interpretations == {"incidental": "failed", "conditional": "passed"}
    branches = [s for s in result.program.states.values() if s.kind is StateKind.BRANCH]
    assert len(branches) == 1
    [guard] = [t.guard for t in branches[0].transitions if t.guard is not None]
    assert guard.text == "Accept"
    # Dropping Accept left p1's save behind the alert: the run halted.
    missed = [
        r
        for r in result.probe_runs
        if r.interpretation == "incidental" and r.context == "rec-a"
    ]
    assert [r.outcome for r in missed] == ["halted_before_effect"]


def test_probe_that_cannot_decide_keeps_the_point_refused(tmp_path, mockmed, contract):
    result, _target = _probe(undecidable_traces(tmp_path), mockmed, contract, tmp_path)

    assert result.certified is False
    assert result.program is None
    [verdict] = result.probe_verdicts
    assert verdict.certified is None
    assert verdict.interpretations == {"incidental": "passed", "conditional": "passed"}
    assert "stays refused" in verdict.detail
    # The record-next item is still there, as without probing.
    assert [i.code for i in result.record_next] == ["unconfirmed_optional"]


def test_inconclusive_read_back_keeps_the_point_refused(tmp_path, mockmed):
    """A store the probe cannot read decides nothing, even when one reading
    would otherwise have won."""
    contract = _contract(tmp_path / "contract", "http://127.0.0.1:9")
    result, _target = _probe(incidental_traces(tmp_path), mockmed, contract, tmp_path)
    assert result.certified is False
    [verdict] = result.probe_verdicts
    assert verdict.certified is None
    assert set(verdict.interpretations.values()) == {"inconclusive"}
    assert all(not r.conclusive for r in result.probe_runs)


def test_probing_a_non_test_target_is_refused(tmp_path, mockmed, contract):
    url, db = mockmed
    # A record the probe's reset would wipe: proof nothing was touched.
    db.add("p3", "Triage", "pre-existing")
    traces = incidental_traces(tmp_path)
    deployment = tmp_path / "production.yaml"
    deployment.write_text(f"name: clinic\nbackend:\n  url: {url}\n")

    from openadapt_flow.__main__ import main

    rc = main(
        [
            "induce",
            *[str(_save(t)) for t in traces],
            "--out",
            str(tmp_path / "out"),
            "--probe-on",
            str(deployment),
        ]
    )
    assert rc == 3
    assert len(db.snapshot()["records"]) == 1
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "section, profile, needle",
    [
        (None, None, "does not declare"),
        (
            {"declared": False, "reset_url": "x", "reward_contract": "c"},
            None,
            "does not declare",
        ),
        ({"declared": True, "reward_contract": "c"}, None, "no reset"),
        ({"declared": True, "reset_url": "x"}, None, "no reward_contract"),
        (
            {"declared": True, "reset_url": "x", "reward_contract": "c"},
            "regulated",
            "production profile",
        ),
        (
            {"declared": True, "reset_url": "x", "reward_contract": "c"},
            "standard",
            "production profile",
        ),
    ],
)
def test_test_copy_gate(section, profile, needle):
    data = {"runtime": {"profile": profile}}
    if section is not None:
        data["test_copy"] = section
    cfg = DeploymentConfig.model_validate(data)
    assert any(needle in r for r in probe_target_refusals(cfg))
    with pytest.raises(ProbeRefused):
        require_test_copy(cfg)


def test_declared_test_copy_passes_the_gate():
    cfg = DeploymentConfig.model_validate(
        {
            "runtime": {"profile": "demo"},
            "test_copy": {"declared": True, "reset_url": "x", "reward_contract": "c"},
        }
    )
    assert probe_target_refusals(cfg) == []


def test_attached_browser_is_refused():
    cfg = DeploymentConfig.model_validate(
        {
            "backend": {"browser_cdp_endpoint": "http://127.0.0.1:9222"},
            "test_copy": {"declared": True, "reset_url": "x", "reward_contract": "c"},
        }
    )
    assert any("fresh session" in r for r in probe_target_refusals(cfg))


# ---------------------------------------------------------------------------
# Logging, fresh sessions, no model calls, advisory proposer
# ---------------------------------------------------------------------------


def test_every_run_is_logged_reset_first_and_in_a_fresh_session(
    tmp_path, mockmed, contract
):
    result, target = _probe(branch_traces(tmp_path), mockmed, contract, tmp_path)
    # incidental fails on the first context and stops; conditional runs both.
    assert len(result.probe_runs) == target.sessions == 3
    assert target.resets == target.sessions + 1  # one before each run, one after
    rendered = result.render()
    assert rendered.count("[probe]") == len(result.probe_runs)
    assert "[verdict]   branch_" in rendered
    dumped = json.loads(result.model_dump_json())
    assert len(dumped["probe_runs"]) == 3


def test_probing_makes_no_model_calls(tmp_path, mockmed, contract):
    result, _target = _probe(incidental_traces(tmp_path), mockmed, contract, tmp_path)
    assert result.probe_runs
    assert sum(r.model_calls for r in result.probe_runs) == 0


class _WrongProposer:
    """Always argues that the step is noise. Zero model calls."""

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, target, kind, context):
        self.calls += 1
        return "drop this step: the demonstrator clicked it by habit"


def test_proposer_stays_advisory(tmp_path, mockmed, contract):
    proposer = _WrongProposer()
    result, _target = _probe(
        branch_traces(tmp_path), mockmed, contract, tmp_path, propose=proposer
    )
    # The read-back decides, not the proposal.
    assert result.probe_verdicts[0].certified == "conditional"
    assert result.certified is True
    assert proposer.calls >= 1
    assert result.proposed and all(p.trusted is False for p in result.proposed)


def test_missing_identity_is_not_probed(tmp_path, mockmed, contract):
    traces = incidental_traces(tmp_path)
    traces[1].workflow = traces[1].workflow.model_copy(update={"params": {}})
    result, target = _probe(traces, mockmed, contract, tmp_path)
    assert target.sessions == 0
    assert result.certified is False
    assert "not probed" in result.probe_verdicts[0].detail


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _save(trace: ProbeTrace) -> Path:
    trace.workflow.save(trace.bundle_dir)
    return trace.bundle_dir


def test_cli_probe_on_declared_test_copy_certifies_and_logs(
    tmp_path, mockmed, contract, monkeypatch, capsys
):
    url, _db = mockmed
    import openadapt_flow.compiler.probing as probing

    made: list[MockMedTestCopy] = []

    def fake_target(cfg):
        probing.require_test_copy(cfg)
        made.append(MockMedTestCopy(url))
        return made[-1]

    monkeypatch.setattr(probing, "DeploymentProbeTarget", fake_target)
    deployment = tmp_path / "test-copy.yaml"
    deployment.write_text(
        "name: mockmed-copy\n"
        f"backend:\n  url: {url}\n"
        "test_copy:\n"
        "  declared: true\n"
        f"  reset_url: {url}api/reset\n"
        "  reward_contract: contract\n"
    )
    traces = incidental_traces(tmp_path)
    out = tmp_path / "out"
    report = tmp_path / "report.json"

    from openadapt_flow.__main__ import main

    rc = main(
        [
            "induce",
            *[str(_save(t)) for t in traces],
            "--out",
            str(out),
            "--probe-on",
            str(deployment),
            "--report",
            str(report),
        ]
    )
    printed = capsys.readouterr().out
    assert rc == 0, printed
    assert (out / "workflow.json").is_file()
    assert "[probe]" in printed and "certified incidental" in printed
    saved = json.loads(report.read_text())
    assert len(saved["probe_runs"]) == made[0].sessions > 0
