"""scaffold-verifier + explain operator tooling (scaffold_verifier.py).

Fixture run dirs / recordings are built in-test, mirroring
tests/test_report.py's synthetic builders. The load-bearing contracts:

* scaffold-verifier drafts, and never approves: every draft carries TODO
  markers plus the loud requires-human-edit header;
* a demonstration with no consequential step is REFUSED (nonzero), never
  scaffolded anyway;
* explain is pure read-only and names the check that fired for a HALT.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openadapt_flow import crypto, sealed_set
from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.ir import (
    ActionKind,
    Effect,
    HaltObservation,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.record_check_identity import IDENTITY_PROPOSED_NAME
from openadapt_flow.record_check_synthesis import PROOF_NAME, PROPOSED_NAME
from openadapt_flow.scaffold_verifier import (
    CONTRACT_FILENAME,
    candidates_from_bundle,
    candidates_from_recording,
    classify_target,
    generation_filenames,
    write_draft,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _recording_dir(tmp_path: Path, *, consequential: bool = True) -> Path:
    """A minimal recording dir; one save click carrying a SoR delta."""
    rec = tmp_path / "recording"
    rec.mkdir()
    events: list[dict] = [
        {
            "i": 0,
            "kind": "click",
            "x": 100,
            "y": 200,
            "t": 0.1,
            "structural": {"role": "button", "name": "New encounter"},
        }
    ]
    if consequential:
        events.append(
            {
                "i": 1,
                "kind": "click",
                "x": 300,
                "y": 400,
                "t": 0.4,
                "structural": {"role": "button", "name": "Save encounter"},
                "sor_before": [{"id": 1, "patient_id": "p1", "type": "Intake"}],
                "sor_after": [
                    {"id": 1, "patient_id": "p1", "type": "Intake"},
                    {
                        "id": 2,
                        "patient_id": "p2",
                        "type": "Triage",
                        "note": "Synthetic follow-up in two weeks",
                        "key": "demo-key-1",
                    },
                ],
            }
        )
    (rec / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (rec / "meta.json").write_text(
        json.dumps({"params": {"note": "Synthetic follow-up in two weeks"}}),
        encoding="utf-8",
    )
    return rec


def _bundle_dir(tmp_path: Path) -> Path:
    """A compiled bundle with a typed record_written effect on its last step."""
    from openadapt_flow.runtime.effects.effect import ValueExpr

    steps = [
        Step(id="step_000", intent="click 'Open'", action=ActionKind.CLICK),
        Step(
            id="step_001",
            intent="type note",
            action=ActionKind.TYPE,
            param="note",
            field_label="Note",
            text="ignored-at-draft-time",
        ),
        Step(
            id="step_002",
            intent="click 'Save encounter'",
            action=ActionKind.CLICK,
            risk="irreversible",
            effects=[
                Effect(
                    kind="record_written",
                    match={
                        "patient_id": ValueExpr(literal="p2"),
                        "type": ValueExpr(literal="Triage"),
                    },
                )
            ],
        ),
    ]
    bundle = tmp_path / "bundle"
    Workflow(name="triage demo", steps=steps).save(bundle)
    return bundle


def _run_report(outcome: str, *, halted: bool = False) -> RunReport:
    results = [
        StepResult(step_id="step_000", intent="open", ok=True, elapsed_ms=120.0),
        StepResult(
            step_id="step_004",
            intent="click 'Save encounter'",
            ok=not halted,
            safety_halt=halted,
            effect_verified=False if halted else None,
            postconditions_ok=False if halted else None,
            elapsed_ms=480.0,
        ),
    ]
    return RunReport(
        workflow_name="local-quickstart",
        started_at="2026-08-20T12:00:00+00:00",
        execution_outcome=outcome,
        execution_profile="standard" if outcome != "COMPLETED_UNVERIFIED" else "demo",
        results=results,
        success=outcome == "VERIFIED",
        halt=(
            HaltObservation(
                state_id="step_004",
                intent="click 'Save encounter'",
                reason="record_written refuted against the system of record",
            )
            if halted
            else None
        ),
        model_calls=0,
        total_ms=600.0,
    )


# ---------------------------------------------------------------------------
# scaffold-verifier
# ---------------------------------------------------------------------------


class TestScaffoldVerifier:
    def test_classify_bundle_and_recording(self, tmp_path: Path) -> None:
        assert classify_target(_bundle_dir(tmp_path)) == "bundle"
        assert classify_target(_recording_dir(tmp_path)) == "recording"
        with pytest.raises(SystemExit, match="path not found"):
            classify_target(tmp_path / "missing")
        empty = tmp_path / "empty-dir"
        empty.mkdir()
        with pytest.raises(SystemExit, match="neither a workflow bundle"):
            classify_target(empty)

    def test_recording_candidates_split_identity_and_payload(
        self, tmp_path: Path
    ) -> None:
        rec = _recording_dir(tmp_path)
        # The demo app keeps its at-most-once key in "key".
        candidates = candidates_from_recording(rec, idempotency_key_field="key")
        assert [candidate.step_id for candidate in candidates] == ["step_001"]
        candidate = candidates[0]
        assert candidate.observed_delta
        assert candidate.match == {"patient_id": "p2", "type": "Triage"}
        assert candidate.payload == {"note": "Synthetic follow-up in two weeks"}
        assert candidate.idempotency == ("key", "demo-key-1")
        # None: no field is taken as the key, so it stays an identity field
        # for a person to review.
        (plain,) = candidates_from_recording(rec, idempotency_key_field=None)
        assert plain.idempotency is None
        assert plain.match["key"] == "demo-key-1"

    def test_write_draft_from_recording_is_todo_marked(self, tmp_path: Path) -> None:
        rec = _recording_dir(tmp_path)
        out_dir = tmp_path / "drafted"
        out, count = write_draft(rec, out_dir)
        assert out == out_dir / CONTRACT_FILENAME and count == 1
        text = out.read_text(encoding="utf-8")
        assert "REQUIRES HUMAN EDIT BEFORE USE" in text
        assert "requires-human-edit" in text
        assert "TODO" in text
        assert "patient_id: p2" in text
        assert "kind: rest" in text
        # Valid YAML despite all the comments.
        import yaml

        parsed = yaml.safe_load(text)
        assert parsed["source_kind"] == "recording"
        assert parsed["effects"][0]["kind"] == "record_written"

    def test_write_draft_from_bundle(self, tmp_path: Path) -> None:
        bundle = _bundle_dir(tmp_path)
        candidates = candidates_from_bundle(bundle)
        assert [candidate.step_id for candidate in candidates] == ["step_002"]
        assert candidates[0].match == {"patient_id": "p2", "type": "Triage"}
        out, count = write_draft(bundle)
        assert count == 1
        text = out.read_text(encoding="utf-8")
        assert "compiled effect contract" in text
        assert "TODO" in text

    def test_no_consequential_step_refused(self, tmp_path: Path) -> None:
        rec = _recording_dir(tmp_path, consequential=False)
        with pytest.raises(SystemExit, match="REFUSED.*no consequential"):
            write_draft(rec)

    def test_cli_scaffold_verifier(self, tmp_path: Path, capsys) -> None:
        rec = _recording_dir(tmp_path)
        out_dir = tmp_path / "cli-out"
        code = main(["scaffold-verifier", str(rec), "-o", str(out_dir)])
        assert code == 0
        out = capsys.readouterr().out
        assert "DRAFT oracle" in out
        assert "Next commands:" in out
        assert (out_dir / CONTRACT_FILENAME).is_file()

    def test_parser_registers_both_new_commands(self) -> None:
        parser = build_parser()
        helptext = parser.format_help()
        assert "scaffold-verifier" in helptext
        assert "explain" in helptext


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_write_draft_refuses_an_occupied_generation(tmp_path: Path) -> None:
    rec = _recording_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    prior = b"prior-candidate: true\n"
    (out / PROPOSED_NAME).write_bytes(prior)
    with pytest.raises(SystemExit, match="already holds a verifier generation"):
        write_draft(rec, out)
    assert (out / PROPOSED_NAME).read_bytes() == prior
    assert not (out / CONTRACT_FILENAME).exists()
    assert _tree(out) == {PROPOSED_NAME: prior}


def _entry_cases() -> list[tuple[str | None, str]]:
    occupied = [
        (name, shape)
        for name in generation_filenames()
        for shape in ("file", "directory", "dangling-link")
    ]
    return [*occupied, ("notes.txt", "unrelated"), (None, "fresh")]


@pytest.mark.parametrize(("name", "shape"), _entry_cases())
def test_generation_entries_are_refused_without_following(
    tmp_path: Path, name: str | None, shape: str
) -> None:
    """Reserved names occupy the directory as a file, directory or link.

    A dangling link to a missing path outside ``--out`` is not followed.
    An unrelated file and a fresh directory still publish one draft.
    """
    rec = _recording_dir(tmp_path)
    out = tmp_path / "out"
    outside = tmp_path / "outside-target"
    if shape != "fresh":
        out.mkdir()
    if shape == "file":
        assert name is not None
        (out / name).write_bytes(b"prior-bytes\n")
    elif shape == "directory":
        assert name is not None
        (out / name).mkdir()
        (out / name / "kept").write_bytes(b"inside\n")
    elif shape == "dangling-link":
        assert name is not None
        (out / name).symlink_to(Path("..") / outside.name)
    elif shape == "unrelated":
        assert name is not None
        (out / name).write_bytes(b"keep\n")

    if shape in ("file", "directory", "dangling-link"):
        assert name is not None
        with pytest.raises(
            SystemExit, match="already holds a verifier generation"
        ) as raised:
            main(["scaffold-verifier", str(rec), "--out", str(out)])
        assert name in str(raised.value)
        assert [path.name for path in out.iterdir()] == [name]
        entry = out / name
        if shape == "file":
            assert entry.read_bytes() == b"prior-bytes\n"
            assert not entry.is_symlink()
        elif shape == "directory":
            assert entry.is_dir() and not entry.is_symlink()
            assert (entry / "kept").read_bytes() == b"inside\n"
            assert [path.name for path in entry.iterdir()] == ["kept"]
        else:
            assert entry.is_symlink()
            assert os.readlink(entry) == os.path.join("..", outside.name)
            assert not outside.exists()
        return

    assert main(["scaffold-verifier", str(rec), "--out", str(out)]) == 0
    draft = out / CONTRACT_FILENAME
    assert draft.is_file() and not draft.is_symlink()
    if shape == "unrelated":
        assert name is not None
        assert (out / name).read_bytes() == b"keep\n"
        assert not (out / name).is_symlink()


def _sealed_recording(rec: Path, key: str, *, generation: dict[str, bytes]) -> None:
    events = (
        json.dumps(
            {
                "i": 0,
                "kind": "click",
                "x": 5,
                "y": 5,
                "field_label": "Save encounter",
            }
        ).encode()
        + b"\n"
    )
    with sealed_set.writing(rec, "recording", key) as writer:
        writer.write(
            "meta.json",
            json.dumps({"params": {"note": "Synthetic note"}}).encode(),
        )
        writer.write("events.jsonl", events)
        for name, payload in generation.items():
            writer.write(name, payload)


def test_sealed_draft_reuse_and_plaintext_out_leave_the_set_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "synthetic-generation-key"
    monkeypatch.setenv(crypto.ENV_KEY, key)
    rec = tmp_path / "rec"
    _sealed_recording(rec, key, generation={})
    assert main(["scaffold-verifier", str(rec)]) == 0
    opened = sealed_set.SealedSet.open(rec, key)
    assert CONTRACT_FILENAME in opened.files()
    draft = opened.read_bytes(CONTRACT_FILENAME)
    before = _tree(rec)
    with pytest.raises(SystemExit, match="Drop --out"):
        main(["scaffold-verifier", str(rec), "--out", str(tmp_path / "plain")])
    assert not (tmp_path / "plain").exists()
    assert _tree(rec) == before
    with pytest.raises(SystemExit, match="already holds a verifier generation"):
        main(["scaffold-verifier", str(rec)])
    assert _tree(rec) == before
    again = sealed_set.SealedSet.open(rec, key)
    assert again.read_bytes(CONTRACT_FILENAME) == draft
    outside = b"".join(
        path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and rec not in path.parents and path != rec
    )
    assert draft not in outside


def test_sealed_candidate_reuse_keeps_the_prior_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "synthetic-generation-key"
    monkeypatch.setenv(crypto.ENV_KEY, key)
    rec = tmp_path / "rec"
    proposed = b"schema_version: prior\nneeds_operator_confirmation: true\n"
    proof = b'{"approved": false, "status": "draft"}\n'
    _sealed_recording(
        rec,
        key,
        generation={PROPOSED_NAME: proposed, PROOF_NAME: proof},
    )
    before = _tree(rec)
    with pytest.raises(SystemExit, match="already holds a verifier generation"):
        main(["scaffold-verifier", str(rec)])
    assert _tree(rec) == before
    opened = sealed_set.SealedSet.open(rec, key)
    assert opened.read_bytes(PROPOSED_NAME) == proposed
    assert opened.read_bytes(PROOF_NAME) == proof
    assert CONTRACT_FILENAME not in opened.files()


# These four are already recording inventory. read_discovery.json and
# check_coverage.json are not, so planting them would add a sealed filename.
_SEALED_GENERATION_NAMES = (
    PROPOSED_NAME,
    PROOF_NAME,
    IDENTITY_PROPOSED_NAME,
    CONTRACT_FILENAME,
)


@pytest.mark.parametrize("name", _SEALED_GENERATION_NAMES)
def test_sealed_logical_file_occupies_without_a_plaintext_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    key = "synthetic-generation-key"
    monkeypatch.setenv(crypto.ENV_KEY, key)
    rec = tmp_path / "rec"
    payload = b"sealed-prior\n"
    _sealed_recording(rec, key, generation={name: payload})
    before = _tree(rec)
    with pytest.raises(SystemExit, match="already holds a verifier generation"):
        main(["scaffold-verifier", str(rec)])
    assert _tree(rec) == before
    opened = sealed_set.SealedSet.open(rec, key)
    assert opened.read_bytes(name) == payload
    if name != CONTRACT_FILENAME:
        assert CONTRACT_FILENAME not in opened.files()


def test_sealed_directory_on_a_logical_name_is_not_a_new_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "synthetic-generation-key"
    monkeypatch.setenv(crypto.ENV_KEY, key)
    rec = tmp_path / "rec"
    _sealed_recording(rec, key, generation={})
    (rec / CONTRACT_FILENAME).mkdir()
    before = _tree(rec)
    with pytest.raises(SystemExit, match="already holds a verifier generation"):
        main(["scaffold-verifier", str(rec)])
    assert (rec / CONTRACT_FILENAME).is_dir()
    assert not (rec / CONTRACT_FILENAME).is_symlink()
    assert _tree(rec) == before
    assert not (rec / f"{CONTRACT_FILENAME}{sealed_set.SEALED_SUFFIX}").exists()


def test_sealed_symlink_is_still_rejected_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "synthetic-generation-key"
    monkeypatch.setenv(crypto.ENV_KEY, key)
    rec = tmp_path / "rec"
    outside = tmp_path / "outside-target"
    _sealed_recording(rec, key, generation={})
    (rec / CONTRACT_FILENAME).symlink_to(Path("..") / outside.name)
    before = _tree(rec)
    assert main(["scaffold-verifier", str(rec)]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert (rec / CONTRACT_FILENAME).is_symlink()
    assert os.readlink(rec / CONTRACT_FILENAME) == os.path.join("..", outside.name)
    assert not outside.exists()
    assert _tree(rec) == before


def test_sealed_plaintext_out_refuses_before_following_a_dangling_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "synthetic-generation-key"
    monkeypatch.setenv(crypto.ENV_KEY, key)
    rec = tmp_path / "rec"
    plain = tmp_path / "plain"
    outside = tmp_path / "outside-target"
    _sealed_recording(rec, key, generation={})
    plain.mkdir()
    (plain / CONTRACT_FILENAME).symlink_to(Path("..") / outside.name)
    before = _tree(rec)
    with pytest.raises(SystemExit, match="Drop --out"):
        main(["scaffold-verifier", str(rec), "--out", str(plain)])
    assert (plain / CONTRACT_FILENAME).is_symlink()
    assert os.readlink(plain / CONTRACT_FILENAME) == os.path.join("..", outside.name)
    assert not outside.exists()
    assert [path.name for path in plain.iterdir()] == [CONTRACT_FILENAME]
    assert _tree(rec) == before


# ---------------------------------------------------------------------------
# flow explain
# ---------------------------------------------------------------------------


class TestExplainRun:
    def _run_dir(self, tmp_path: Path, outcome: str) -> Path:
        run_dir = tmp_path / "run"
        _run_report(outcome, halted=outcome == "HALTED").save(run_dir)
        return run_dir

    def test_explains_a_halt_and_names_the_fired_check(
        self, tmp_path: Path, capsys
    ) -> None:
        run_dir = self._run_dir(tmp_path, "HALTED")
        assert main(["explain", str(run_dir)]) == 0
        out = capsys.readouterr().out
        assert "finished HALTED" in out
        assert "independent effect check on step `step_004`" in out
        assert "Why this is safe" in out
        assert f"read {run_dir}/REPORT.md" in out

    def test_unverified_points_at_scaffold_verifier(
        self, tmp_path: Path, capsys
    ) -> None:
        run_dir = self._run_dir(tmp_path, "COMPLETED_UNVERIFIED")
        assert main(["explain", str(run_dir)]) == 0
        out = capsys.readouterr().out
        assert "must never be reported as success" in out
        assert "scaffold-verifier" in out

    def test_missing_report_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="holds no report.json"):
            main(["explain", str(tmp_path)])

    def test_explain_is_read_only(self, tmp_path: Path) -> None:
        import hashlib

        run_dir = self._run_dir(tmp_path, "HALTED")

        def snapshot(root: Path) -> dict[str, bytes]:
            return {
                str(path.relative_to(root)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        before = snapshot(run_dir)
        from openadapt_flow.scaffold_verifier import explain_run

        assert "HALTED" in explain_run(run_dir)
        assert snapshot(run_dir) == before

    def test_halt_line_falls_back_to_halt_reason(self, tmp_path: Path) -> None:
        from openadapt_flow.scaffold_verifier import _halt_check_line

        report = RunReport(
            workflow_name="w",
            started_at="2026-08-20T12:00:00+00:00",
            execution_outcome="HALTED",
            halt=HaltObservation(state_id="s", reason="unhandled screen state"),
        )
        assert "unhandled screen state" in _halt_check_line(report)


# ---------------------------------------------------------------------------
# social_card.py smoke + determinism
# ---------------------------------------------------------------------------


class TestSocialCard:
    def _stats(self, tmp_path: Path) -> dict[str, object]:
        run_dir = tmp_path / "run"
        _run_report("VERIFIED").save(run_dir)
        from scripts.social_card import _card_stats

        return _card_stats(
            run_dir,
            RunReport.model_validate_json((run_dir / "report.json").read_text()),
        )

    def test_renders_nonempty_png_into_tmp(self, tmp_path: Path) -> None:
        from scripts.social_card import render_card

        stats = self._stats(tmp_path)
        out = tmp_path / "card.png"
        image = render_card(stats)
        image.save(out, format="PNG")
        assert out.is_file() and out.stat().st_size > 0

    @pytest.mark.parametrize("outcome", ["VERIFIED", "HALTED", "COMPLETED_UNVERIFIED"])
    def test_smoke_all_outcomes_render(self, tmp_path: Path, outcome: str) -> None:
        from scripts.social_card import render_card

        stats = self._stats(tmp_path) | {"outcome": outcome}
        assert render_card(stats).size[0] > 0

    def test_deterministic_pixels(self, tmp_path: Path) -> None:
        from scripts.social_card import render_card

        stats = self._stats(tmp_path)
        first = render_card(stats).tobytes()
        second = render_card(stats).tobytes()
        assert first == second
