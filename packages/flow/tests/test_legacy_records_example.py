"""The legacy-records example: intake, exceptions, decisions, grades, exchanges.

The desktop part needs an X display and runs only with
OPENADAPT_LEGACY_RECORDS_DEMO=1 (see examples/legacy_records/README.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "legacy_records"
sys.path.insert(0, str(EXAMPLE))

import evidence  # noqa: E402
import make_inbox  # noqa: E402
import pipeline  # noqa: E402
import referral_triage  # noqa: E402
from x11 import Frame, OcrLine  # noqa: E402

from openadapt_flow.interchange import check_file, emit_exchange  # noqa: E402


@pytest.fixture()
def inbox(tmp_path: Path) -> Path:
    make_inbox.main(tmp_path)
    return tmp_path


def _people() -> pipeline.People:
    return pipeline.People(
        "intake_staff:t-staff",
        "physician:t-doc",
        {
            "corrections": {"referral-0007.pdf": {"subject_id": "TEST-0000-0007"}},
            "decisions": {"referral-0007.pdf": "urgent"},
        },
    )


def test_intake_flags_the_look_alike_id_and_staff_correct_it(inbox: Path) -> None:
    logs: list[str] = []
    run = inbox / "run"
    ex = pipeline.intake(
        inbox / "fax_inbox" / "referral-0007.pdf",
        referral_triage.SPECS,
        run,
        workflow="t",
        artifact={
            "source_type": "fax_folder",
            "file_number": "x",
            "date": "2026-10-29",
            "doctype": "referral",
        },
        people=_people(),
        staff_role="intake_staff",
        log=logs.append,
    )
    sid = next(f for f in ex["fields"] if f["name"] == "subject_id")
    assert sid["value"] == "TEST-0000-0007"
    assert sid["status"] == "corrected"
    assert sid["confirmed_by"]["id"] == "intake_staff:t-staff"
    assert sid["reads"][0]["value"] == "TEST-OOOO-0007"
    assert any("exception: subject_id" in m for m in logs)

    pipeline.decide(
        ex,
        _people(),
        question="Priority?",
        options=["urgent", "routine"],
        role="physician",
        output_field="priority",
        log=logs.append,
    )
    path = evidence.write_exchange(evidence.seal(ex, run), run)
    assert check_file(path) == []
    refs = emit_exchange(path, inbox / "l1")
    assert refs[0].path.name == "x_2026-10-29_referral.pdf"


def test_one_person_cannot_confirm_and_approve(tmp_path: Path) -> None:
    answers = tmp_path / "a.json"
    answers.write_text('{"staff": "p:same", "approver": "p:same"}')
    with pytest.raises(SystemExit, match="cannot also approve"):
        pipeline.People.load(answers, "intake_staff", "physician")


def _frame() -> Frame:
    return Frame(b"\x89PNG not decoded here", [])


def test_grades_follow_the_reading_rule() -> None:
    line = OcrLine("REC-1", (1, 1, 5, 5), 0.97)
    kw = dict(
        expected="REC-1",
        clipboard="REC-1",
        ocr_line=None,
        frame=_frame(),
        artifact_id="a",
        screen="s",
    )
    single = evidence.read_field("r", "code", **kw, session="reopened")
    assert single["grade"] == "unconfirmed"

    frame = _frame()
    frame.crop_sha256 = lambda bbox: "0" * 64  # type: ignore[method-assign]
    kw.update(ocr_line=line, frame=frame)
    assert (
        evidence.read_field("r", "code", **kw, session="acting")["grade"]
        == "screen_readback"
    )
    assert (
        evidence.read_field("r", "code", **kw, session="reopened")["grade"]
        == "committed_record_readback"
    )
    assert (
        evidence.read_field("r", "code", **kw, session="reopened", export="REC-1")[
            "grade"
        ]
        == "verified"
    )


def test_a_lost_write_is_reported_as_lost_not_as_disagreement() -> None:
    f = evidence.read_field(
        "p",
        "enum",
        expected="urgent",
        clipboard=None,
        ocr_line=None,
        frame=_frame(),
        session="reopened",
        artifact_id="a",
        screen="s",
    )
    assert f["needs_review"] and "wrote 'urgent'" in f["review_reason"]


@pytest.mark.skipif(
    os.environ.get("OPENADAPT_LEGACY_RECORDS_DEMO") != "1",
    reason="needs Xvfb, xdotool, xclip and ImageMagick",
)
def test_demo_end_to_end(tmp_path: Path) -> None:
    for tool in ("Xvfb", "xdotool", "xclip", "import"):
        assert shutil.which(tool), tool
    env = dict(
        os.environ,
        FLOW_PYTHON=sys.executable,
        APP_PYTHON=os.environ.get("APP_PYTHON", "python3"),
    )
    out = subprocess.run(
        [str(EXAMPLE / "demo.sh"), str(tmp_path / "demo")],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout
    assert "referral-0009.pdf: halted" in out.stdout
    assert "referral-0008.pdf: halted" in out.stdout  # the dropped save
    assert "invoice-INV-20419.pdf: halted" in out.stdout
