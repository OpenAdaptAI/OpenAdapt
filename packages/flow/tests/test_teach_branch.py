"""Teach a branch from several resolved halts (MockMed prior-auth scenario).

The scenario is the MockMed ``modal-once`` save, driven through the same
FakeBackend and FakeVision as ``test_halt_learn_loop`` (no browser, no model
calls). After Save, a "Prior Auth Required" notice covers the confirmation
banner. Its wording carries the payer and a reference number, so each run
shows a different line. The fix is the same each time: dismiss the notice.

These tests show what one example can't: a branch learned from two halts keys
on the text they share and handles a third payer it never saw, while a branch
learned from one halt keys on that halt's whole line and stops on the third.
They also cover narrowing by payer type, the refusals, and that the result is a
repair candidate that is not active until the governed lifecycle approves it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from openadapt_flow.__main__ import main
from openadapt_flow.ir import ParamKind, ParamSpec, RunReport, Workflow
from openadapt_flow.learning.branch_teach import (
    BranchExample,
    find_signature,
    teach_branch,
)
from openadapt_flow.learning.teach import teach
from openadapt_flow.repair.registration import load_detached_candidate
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import OcrLine
from tests.test_halt_learn_loop import (
    DISMISS_POINT,
    INTENT_DISMISS,
    SKILL_ID,
    ModalOnceVision,
    modal_once_base_program,
)
from tests.test_replayer import FakeBackend, Match, make_png

AETNA = "Prior Auth Required - Aetna ref 4471"
CIGNA = "Prior Auth Required - Cigna ref 9032"
HUMANA = "Prior Auth Required - Humana ref 1200"
SHARED = "Prior Auth Required"


class PortalVision(ModalOnceVision):
    """``ModalOnceVision`` with a tolerant presence check, like the real one
    (a guard phrase matches inside a longer OCR line), and a notice that can
    be informational (shown, but not covering the banner)."""

    def __init__(self, backend, *, modal_text=None, blocking=True):
        super().__init__(backend, modal_text=modal_text)
        self.blocking = blocking

    def _modal_up(self) -> bool:
        return self.blocking and super()._modal_up()

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8, **kw):
        self.text_calls.append(text)
        shown = self.modal_text is not None and (
            not self.blocking or super()._modal_up()
        )
        if shown and text.lower() in self.modal_text.lower():
            return Match(point=(50, 10), region=(30, 5, 160, 16), confidence=0.9)
        return None

    def ocr(self, screen_png, *, region=None):
        shown = self.modal_text is not None and (
            not self.blocking or super()._modal_up()
        )
        if shown:
            return [
                OcrLine(text=self.modal_text, region=(30, 5, 160, 16), confidence=0.9)
            ]
        return []


def _workflow(with_payer: bool = False) -> Workflow:
    wf = Workflow(name=SKILL_ID, program=modal_once_base_program())
    if with_payer:
        wf = wf.model_copy(
            update={
                "param_specs": {
                    "payer_type": ParamSpec(
                        name="payer_type",
                        type=ParamKind.ENUM,
                        choices=["medicaid", "commercial"],
                        example="commercial",
                    )
                }
            }
        )
    return wf


def _bundle(root: Path, wf: Workflow) -> Path:
    bundle = root / "base_bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "verify.png").write_bytes(make_png((50, 20)))
    (bundle / "templates" / "x.png").write_bytes(make_png((20, 20)))
    wf.save(bundle)
    return bundle


def _replay(
    wf: Workflow,
    bundle: Path,
    run_dir: Path,
    *,
    text: Optional[str],
    blocking: bool = True,
    params: Optional[dict] = None,
) -> tuple[RunReport, FakeBackend]:
    backend = FakeBackend()
    vision = PortalVision(backend, modal_text=text, blocking=blocking)
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        wf, params=params, bundle_dir=bundle, run_dir=run_dir
    )
    return report, backend


def _fix(root: Path, intent: str = INTENT_DISMISS) -> Path:
    path = root / f"fix-{abs(hash(intent))}.json"
    path.write_text(
        json.dumps({"resolution_steps": [{"intent": intent, "action": "click"}]})
    )
    return path


def _halt(wf, bundle, run_dir, text, params=None) -> Path:
    report, _ = _replay(wf, bundle, run_dir, text=text, params=params)
    assert report.terminal_outcome == "halt"
    return run_dir


def _dismissed(backend: FakeBackend) -> bool:
    return ("click", *DISMISS_POINT, False) in backend.actions


# -- the signature ---------------------------------------------------------------


def test_signature_is_the_phrase_every_example_shares():
    assert find_signature([[AETNA], [CIGNA]]) == [SHARED]


def test_signature_prefers_a_shared_whole_line():
    assert find_signature([["Session expired"], ["Session expired"]]) == [
        "Session expired"
    ]


def test_no_signature_when_an_example_shares_nothing():
    assert find_signature([[AETNA], ["Session Timeout"]]) == []


def test_a_single_shared_word_is_not_a_signature():
    assert find_signature([["Required field"], ["Login required"]]) == []


# -- two examples generalize where one does not ----------------------------------


def test_branch_from_two_halts_handles_an_unseen_payer(tmp_path):
    wf = _workflow()
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    examples = [
        BranchExample(run_dir=_halt(wf, bundle, tmp_path / "r1", AETNA), fix=fix),
        BranchExample(run_dir=_halt(wf, bundle, tmp_path / "r2", CIGNA), fix=fix),
    ]
    out = tmp_path / "out"

    result = teach_branch(examples, out, bundle=bundle)

    assert result.promoted, result.summary()
    assert result.signature is not None and result.signature.text == SHARED
    learned = Workflow.load(out)

    report, backend = _replay(learned, out, tmp_path / "humana", text=HUMANA)
    assert report.success and _dismissed(backend)

    clean, backend = _replay(learned, out, tmp_path / "clean", text=None)
    assert clean.success and not _dismissed(backend)

    other, backend = _replay(learned, out, tmp_path / "other", text="Session Timeout")
    assert other.terminal_outcome == "halt" and not _dismissed(backend)


def test_branch_from_one_halt_keys_on_that_halts_whole_line(tmp_path):
    wf = _workflow()
    bundle = _bundle(tmp_path, wf)
    run = _halt(wf, bundle, tmp_path / "r1", AETNA)
    out = tmp_path / "out"

    assert teach(run, _fix(tmp_path), out, bundle=bundle).promoted
    learned = Workflow.load(out)

    report, _ = _replay(learned, out, tmp_path / "humana", text=HUMANA)
    assert report.terminal_outcome == "halt"


# -- the result is a candidate, not an active bundle ------------------------------


def test_the_branch_is_a_repair_candidate_with_its_evidence(tmp_path):
    wf = _workflow()
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    examples = [
        BranchExample(run_dir=_halt(wf, bundle, tmp_path / f"r{i}", text), fix=fix)
        for i, text in enumerate([AETNA, CIGNA, HUMANA])
    ]
    out = tmp_path / "out"

    assert teach_branch(examples, out, bundle=bundle).promoted

    candidate = load_detached_candidate(out)
    assert candidate.source == "teach"
    assert candidate.state == "candidate"
    assert candidate.approval is None
    evidence = json.loads((out / "repair" / "branch_induction.json").read_text())
    assert evidence["examples"] == 3
    assert evidence["runtime_model_calls"] == 0
    # The record carries a digest of the condition text, not the text.
    assert SHARED not in json.dumps(evidence)


# -- narrowing by payer type ---------------------------------------------------


def _normal_texts(texts_by_run: dict[Path, list[str]]):
    return lambda run_dir, report: texts_by_run.get(Path(run_dir), [])


def test_banner_on_every_payer_narrows_the_branch_to_the_payer(tmp_path):
    wf = _workflow(with_payer=True)
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    medicaid = {"payer_type": "medicaid"}
    commercial = {"payer_type": "commercial"}
    examples = [
        BranchExample(
            run_dir=_halt(wf, bundle, tmp_path / "r1", AETNA, medicaid), fix=fix
        ),
        BranchExample(
            run_dir=_halt(wf, bundle, tmp_path / "r2", CIGNA, medicaid), fix=fix
        ),
    ]
    # For a commercial payer the same notice is informational: the run went
    # the normal way with it on screen.
    normal_dir = tmp_path / "normal"
    normal, _ = _replay(
        wf, bundle, normal_dir, text=AETNA, blocking=False, params=commercial
    )
    assert normal.success
    out = tmp_path / "out"

    result = teach_branch(
        examples,
        out,
        bundle=bundle,
        negatives=[normal_dir],
        screen_texts=_normal_texts({normal_dir: [AETNA]}),
    )

    assert result.promoted, result.summary()
    assert result.signature is not None
    assert (result.signature.param, result.signature.value) == (
        "payer_type",
        "medicaid",
    )
    learned = Workflow.load(out)
    report, backend = _replay(
        learned, out, tmp_path / "m", text=HUMANA, params=medicaid
    )
    assert report.success and _dismissed(backend)
    report, backend = _replay(
        learned, out, tmp_path / "c", text=HUMANA, blocking=False, params=commercial
    )
    assert report.success and not _dismissed(backend)


def test_refuses_when_nothing_separates_the_examples_from_a_normal_run(tmp_path):
    wf = _workflow()  # no declared payer parameter
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    examples = [
        BranchExample(run_dir=_halt(wf, bundle, tmp_path / "r1", AETNA), fix=fix),
        BranchExample(run_dir=_halt(wf, bundle, tmp_path / "r2", CIGNA), fix=fix),
    ]
    normal_dir = tmp_path / "normal"
    assert _replay(wf, bundle, normal_dir, text=AETNA, blocking=False)[0].success
    out = tmp_path / "out"

    result = teach_branch(
        examples,
        out,
        bundle=bundle,
        negatives=[normal_dir],
        screen_texts=_normal_texts({normal_dir: [AETNA]}),
    )

    assert not result.promoted
    assert "nothing observed tells the cases apart" in result.reason
    assert not out.exists()


# -- refusals --------------------------------------------------------------------


def test_refuses_examples_resolved_differently(tmp_path):
    wf = _workflow()
    bundle = _bundle(tmp_path, wf)
    examples = [
        BranchExample(
            run_dir=_halt(wf, bundle, tmp_path / "r1", AETNA), fix=_fix(tmp_path)
        ),
        BranchExample(
            run_dir=_halt(wf, bundle, tmp_path / "r2", CIGNA),
            fix=_fix(tmp_path, "Click 'Request auth'"),
        ),
    ]
    result = teach_branch(examples, tmp_path / "out", bundle=bundle)
    assert not result.promoted and "resolved differently" in result.reason
    assert not (tmp_path / "out").exists()


def test_refuses_examples_with_no_shared_screen_text(tmp_path):
    wf = _workflow()
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    examples = [
        BranchExample(run_dir=_halt(wf, bundle, tmp_path / "r1", AETNA), fix=fix),
        BranchExample(
            run_dir=_halt(wf, bundle, tmp_path / "r2", "Session Timeout"), fix=fix
        ),
    ]
    result = teach_branch(examples, tmp_path / "out", bundle=bundle)
    assert not result.promoted and "shared signature" in result.reason


# -- CLI -------------------------------------------------------------------------


def test_cli_teach_branch(tmp_path):
    wf = _workflow()
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    r1 = _halt(wf, bundle, tmp_path / "r1", AETNA)
    r2 = _halt(wf, bundle, tmp_path / "r2", CIGNA)
    out = tmp_path / "out"
    rc = main(
        [
            "teach-branch",
            "--example",
            str(r1),
            str(fix),
            "--example",
            str(r2),
            str(fix),
            "--bundle",
            str(bundle),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert (out / "repair" / "candidate.json").is_file()
