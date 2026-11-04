"""Commands that read sealed input read every artifact file through
:mod:`openadapt_flow.artifacts`.

Two parts:

- One test per command added to ``__main__._SEALED_READY_COMMANDS`` here:
  it runs on a sealed bundle, run directory or recording, succeeds, and leaves
  the sealed set ciphertext only (docs/phi_at_rest.md).
- A lint over the functions those commands use to read artifacts. Inside
  them, a call to ``.read_text()``, ``.read_bytes()``, ``.is_file()``,
  ``.exists()``, ``.open()`` or the builtin ``open()`` must go through
  ``artifacts`` (or the ``sealed_set`` helpers it wraps). ``pathlib`` reads a
  sealed file as ciphertext, or misses it, so a direct read is a bug the day
  the input is sealed. A read of a file that is not part of an artifact (a
  config or CSV the person passes in) goes on :data:`ALLOWED`, with the reason.

Everything here is synthetic and runs without a browser.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from openadapt_flow import crypto, sealed_set
from openadapt_flow.__main__ import _SEALED_READY_COMMANDS, main
from openadapt_flow.ir import ActionKind, Step, Workflow
from tests.test_sealed_at_rest import (
    KEY,
    _assert_ciphertext_only,
    _files,
    _plain_bundle,
)

_PKG = Path(__file__).resolve().parent.parent / "openadapt_flow"

#: The functions that read artifacts for the converted commands, by module.
CONVERTED: dict[str, tuple[str, ...]] = {
    "__main__.py": (
        "_cmd_visualize",
        "_cmd_explain",
        "_cmd_emit_skill",
        "_cmd_for_each",
        "_cmd_record",
    ),
    "admitted_composition.py": ("is_process_contract_artifact",),
    "composition.py": ("is_composition_artifact",),
    "scaffold_verifier.py": ("explain_run", "_pending_decision"),
    "emit/skill.py": ("emit_skill",),
    "skills.py": ("read_lock", "read_binding"),
    "sections.py": ("read_file", "load"),
    "narration.py": ("read_events", "load_narration"),
    "cli_sections.py": ("cmd_sections",),
    "cli_check.py": ("cmd_check_mark", "guided_check"),
    # compose, process, variants, route and visualize of a process contract.
    "cli_compose.py": ("cmd_compose",),
    "compiler/compose_authoring.py": ("author_composition",),
    "cli_process.py": ("cmd_process",),
    "cli_variants.py": ("cmd_variants", "cmd_route", "first_screen_probe"),
    "variants.py": (
        "load",
        "is_variant_set_artifact",
        "_admitted_spec",
        "_repair_candidate",
        "_check_lineage",
        "author_variant_set",
    ),
    "runtime/variant_router.py": ("route_variant", "_admitted_variant"),
    "visualize/admitted_composition.py": ("build_process_graph",),
}
CONVERTED["__main__.py"] += (
    "_cmd_visualize_process",
    "_cmd_compose",
    "_cmd_process",
    "_cmd_variants",
    "_cmd_route",
)
CONVERTED["composition.py"] += ("load",)
# teach: the halted run, the fix recording, the base bundle, the library.
CONVERTED["__main__.py"] += ("_cmd_teach",)
CONVERTED["learning/teach.py"] = (
    "load_halt_report",
    "load_base_program",
    "_texts_seen_before_halt",
    "_spec_from_recording",
    "_real_target_canary",
    "_copy_templates",
    "_copy_carried",
    "teach",
)
CONVERTED["learning/library.py"] = ("_load",)
# push, and the review and approval of its sanitized copy.
CONVERTED["hosted.py"] = ("push", "_is_bundle_dir")
CONVERTED["sanitized_artifact.py"] = (
    "_source_files",
    "_scrub_text_file",
    "_scrub_image_file",
    "_tree_rows",
    "_reseal_sanitized_bundle",
    "sanitize_artifact",
    "load_and_verify_derivative",
    "_rescan_derivative",
    "_write_deterministic_archive",
    "approve_derivative",
    "load_valid_approval",
    "_update_entry",
    "_authorized_review_path",
    "add_manual_text_redaction",
    "add_manual_image_redaction",
    "render_review_html",
)
CONVERTED["repair/registration.py"] = (
    "_evidence_from_run_dir",
    "load_detached_candidate",
)
CONVERTED["admitted_composition.py"] += (
    "load",
    "load_child_envelope",
    "_load_admitted_bundle",
    "author_process_contract",
)

#: Receivers whose reads are sealed-aware.
_READERS = frozenset({"artifacts", "_artifacts", "sealed_set", "_sealed"})
_READ_CALLS = frozenset({"read_text", "read_bytes", "is_file", "exists", "open"})

#: (module, function, source of the call) -> why a direct read is right.
ALLOWED: dict[tuple[str, str, str], str] = {
    (
        "learning/library.py",
        "_load",
        "self._writer.is_file(_LIBRARY_FILE)",
    ): "the sealed library's own writer, opened and hash-checked with its key",
    (
        "learning/library.py",
        "_load",
        "self._writer.read_bytes(_LIBRARY_FILE)",
    ): "the sealed library's own writer, opened and hash-checked with its key",
    (
        "__main__.py",
        "_cmd_record",
        '(Path(args.out) / "events.jsonl").is_file()',
    ): "gates the look-up proposal, which runs on plaintext recordings only",
    (
        "compiler/compose_authoring.py",
        "author_composition",
        "out_dir.exists()",
    ): "the new composition directory, not an artifact",
    (
        "admitted_composition.py",
        "author_process_contract",
        "out_dir.exists()",
    ): "the new process-contract directory, not an artifact",
    (
        "variants.py",
        "author_variant_set",
        "out_dir.exists()",
    ): "the new variant-set directory, not an artifact",
    (
        "cli_process.py",
        "cmd_process",
        'Path(spec_path).read_text(encoding="utf-8")',
    ): "the v1 contract spec a person passes in",
    ("cli_process.py", "cmd_process", "output.exists()"): "the new output directory",
    (
        "cli_variants.py",
        "cmd_variants",
        'spec_path.read_text(encoding="utf-8")',
    ): "the variant spec a person passes in",
    (
        "cli_variants.py",
        "first_screen_probe",
        "Path(explicit).read_bytes()",
    ): "the --first-screen PNG a person passes in",
    ("hosted.py", "push", "derivative.exists()"): "the review copy's directory",
    (
        "sanitized_artifact.py",
        "_source_files",
        "path.is_file()",
    ): "walks a plain source; a sealed one is listed from its index above",
    (
        "sanitized_artifact.py",
        "sanitize_artifact",
        "destination.exists()",
    ): "the new derivative directory, not an artifact",
    (
        "sanitized_artifact.py",
        "load_valid_approval",
        "archive.is_file()",
    ): "the archive file beside the derivative, read by read_approved_archive",
}


def _direct_reads() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for module, names in CONVERTED.items():
        source = (_PKG / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in names:
            assert name in functions, f"{module}:{name} is gone; update CONVERTED"
            for node in ast.walk(functions[name]):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    pass
                elif isinstance(func, ast.Attribute) and func.attr in _READ_CALLS:
                    receiver = func.value
                    if isinstance(receiver, ast.Name) and receiver.id in _READERS:
                        continue
                else:
                    continue
                call = ast.get_source_segment(source, node) or ""
                found.append((module, name, call))
    return found


def test_converted_commands_read_artifacts_through_the_reader() -> None:
    direct = [read for read in _direct_reads() if read not in ALLOWED]
    assert direct == [], (
        "Read these artifact files through openadapt_flow.artifacts, or add "
        f"them to ALLOWED with the reason: {direct}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    assert set(ALLOWED) <= set(_direct_reads())


# -- each converted command on sealed input -----------------------------------


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    out = tmp_path / "sealed"
    assert main(["seal", str(_plain_bundle(tmp_path)), "--out", str(out)]) == 0
    return out


def _ready(command: str) -> None:
    assert command in _SEALED_READY_COMMANDS


def test_visualize_reads_a_sealed_bundle(sealed: Path, tmp_path: Path) -> None:
    _ready("openadapt_flow.__main__:_cmd_visualize")
    before = _files(sealed)
    out = tmp_path / "graph.json"
    assert main(["visualize", str(sealed), "--format", "json", "--out", str(out)]) == 0
    assert "synthetic-intake" in out.read_text()
    assert _files(sealed) == before


def test_emit_skill_copies_a_sealed_bundle_sealed(sealed: Path, tmp_path: Path) -> None:
    _ready("openadapt_flow.__main__:_cmd_emit_skill")
    assert main(["emit-skill", str(sealed), "--out", str(tmp_path / "skills")]) == 0
    copy = tmp_path / "skills" / "synthetic-intake" / "bundle"
    assert sealed_set.is_sealed(copy)
    _assert_ciphertext_only(copy)
    assert Workflow.load(copy, key=KEY).name == "synthetic-intake"


def test_for_each_writes_a_sealed_loop_from_a_sealed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready("openadapt_flow.__main__:_cmd_for_each")
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    plain = _plain_bundle(tmp_path)
    body = Workflow.load(plain)
    body.steps.append(
        Step(id="step_2", intent="type note", action=ActionKind.TYPE, param="note")
    )
    body.params = {"note": "x"}
    body.save(plain)
    src = tmp_path / "sealed"
    assert main(["seal", str(plain), "--out", str(src)]) == 0
    records = tmp_path / "records.csv"
    records.write_text("note\nfirst\nsecond\n")
    out = tmp_path / "loop"
    args = ["for-each", str(src), "--records", str(records), "--out", str(out)]
    assert main(args) == 0
    assert sealed_set.is_sealed(out)
    _assert_ciphertext_only(out)
    assert sealed_set.is_file(out / "templates" / "open.png")
    assert Workflow.load(out, key=KEY).data_sources


def test_sections_reads_and_edits_a_sealed_bundle(
    sealed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ready("openadapt_flow.cli_sections:cmd_sections")
    assert main(["sections", str(sealed), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)["sections"][0]["id"]
    assert main(["sections", str(sealed), "--rename", first, "Intake"]) == 0
    assert "Intake" in capsys.readouterr().out
    _assert_ciphertext_only(sealed)


def test_explain_reads_a_sealed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.test_explain_transaction import _halted_run

    _ready("openadapt_flow.__main__:_cmd_explain")
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    with sealed_set.writing(tmp_path / "run", "run", KEY):
        run_dir = _halted_run(tmp_path, "HALTED_BEFORE_EFFECT", pending_status=None)
    _assert_ciphertext_only(run_dir)
    assert main(["explain", str(run_dir)]) == 0
    assert "local-quickstart" in capsys.readouterr().out


def _sealed_recording(root: Path) -> Path:
    events = [{"kind": "click", "x": 5, "y": 5}, {"kind": "type", "text": "x"}]
    with sealed_set.writing(root, "recording", KEY):
        sealed_set.write_text(
            root / "events.jsonl", "".join(json.dumps(e) + "\n" for e in events)
        )
        sealed_set.write_text(root / "meta.json", json.dumps({"surface": "web"}))
    return root


def test_check_mark_writes_into_a_sealed_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.compiler.readback_authoring import load_check

    _ready("openadapt_flow.cli_check:cmd_check_mark")
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    rec = _sealed_recording(tmp_path / "rec")
    mark = ["check", "mark", str(rec), "--start-event", "1", "--field", "note=1,2,3,4"]
    assert main(mark) == 0
    _assert_ciphertext_only(rec)
    marks = load_check(rec)
    assert marks is not None and marks.start_event == 1


def test_record_with_check_marks_a_sealed_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.cli_check import guided_check
    from openadapt_flow.compiler.readback_authoring import load_check

    _ready("openadapt_flow.__main__:_cmd_record")
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    rec = _sealed_recording(tmp_path / "rec")
    shown: list[str] = []
    answers = iter(["1", "note=1,2,3,4", "", ""])
    assert guided_check(rec, ask=lambda _q: next(answers), echo=shown.append)
    assert any("type" in line for line in shown)
    _assert_ciphertext_only(rec)
    assert load_check(rec) is not None


# -- compose, process, variants and route over sealed children ----------------


def _sealed_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, same_surface: bool = False
) -> dict[str, tuple[Path, Path]]:
    """Two sealed child bundles, each with its admission envelope beside it."""
    from openadapt_flow.admitted_composition import live_bundle_content_digest
    from openadapt_flow.qualification_admission import sign_qualification_admission
    from tests.test_admitted_composition_authoring import (
        INTAKE_ADMISSION,
        INTAKE_VERSION,
        POSTING_ADMISSION,
        POSTING_VERSION,
        _reader,
        _save,
        _writer,
    )
    from tests.test_qualification_admission import _payload, _private_key

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    children: dict[str, tuple[Path, Path]] = {}
    for name, workflow, admission, version in (
        ("intake", _writer(), INTAKE_ADMISSION, INTAKE_VERSION),
        (
            "posting",
            _writer("posting") if same_surface else _reader(),
            POSTING_ADMISSION,
            POSTING_VERSION,
        ),
    ):
        plain = _save(tmp_path, workflow, f"{name}-plain")
        bundle = tmp_path / name
        assert main(["seal", str(plain), "--out", str(bundle)]) == 0
        digest = live_bundle_content_digest(Workflow.load(bundle), bundle)
        envelope = sign_qualification_admission(
            _payload(
                admission_id=admission,
                workflow_version_id=version,
                bundle_content_digest=digest,
            ),
            _private_key(),
        )
        path = tmp_path / f"{name}.admission.json"
        path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
        children[name] = (bundle, path)
    return children


def test_compose_copies_sealed_children_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.composition import Composition, child_bundle_path

    _ready("openadapt_flow.__main__:_cmd_compose")
    children = _sealed_admitted(tmp_path, monkeypatch)
    before = {name: _files(bundle) for name, (bundle, _) in children.items()}
    out = tmp_path / "composed"
    args = ["compose", "--out", str(out)]
    for name, (bundle, _) in children.items():
        args += ["--child", f"{name}={bundle}"]
    args += ["--handoff", "intake.patient_id=posting.patient_id"]
    assert main(args) == 0
    composition = Composition.load(out)
    for child in composition.children:
        copy = child_bundle_path(out, child)
        assert sealed_set.is_sealed(copy)
        _assert_ciphertext_only(copy)
        assert Workflow.load(copy, key=KEY).name == child.name
    assert {name: _files(b) for name, (b, _) in children.items()} == before


def test_process_and_its_visualization_read_sealed_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.admitted_composition import ProcessContract

    _ready("openadapt_flow.__main__:_cmd_process")
    _ready("openadapt_flow.__main__:_cmd_visualize_process")
    children = _sealed_admitted(tmp_path, monkeypatch)
    out = tmp_path / "process"
    args = ["process", "--out", str(out), "--name", "two-step"]
    for name, (bundle, envelope) in children.items():
        args += ["--child", f"{name}={bundle}", "--admission", f"{name}={envelope}"]
    args += ["--handoff", "intake.patient_id=posting.patient_id"]
    assert main(args) == 0
    assert [c.name for c in ProcessContract.load(out).children] == [
        "intake",
        "posting",
    ]
    graph = tmp_path / "process.json"
    assert main(["visualize", str(out), "--format", "json", "--out", str(graph)]) == 0
    assert "two-step" in graph.read_text()
    for bundle, _ in children.values():
        _assert_ciphertext_only(bundle)


def test_variants_and_route_read_sealed_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ready("openadapt_flow.__main__:_cmd_variants")
    _ready("openadapt_flow.__main__:_cmd_route")
    children = _sealed_admitted(tmp_path, monkeypatch, same_surface=True)
    payers = {"intake": "commercial", "posting": "medicare"}
    spec = tmp_path / "variants.json"
    spec.write_text(
        json.dumps(
            {
                "name": "by-payer",
                "variants": [
                    {
                        "name": name,
                        "bundle": str(bundle),
                        "admission": str(envelope),
                        "when": {"inputs": {"payer": [payers[name]]}},
                    }
                    for name, (bundle, envelope) in children.items()
                ],
            }
        )
    )
    out = tmp_path / "set"
    assert main(["variants", "author", "--spec", str(spec), "--out", str(out)]) == 0
    capsys.readouterr()
    assert main(["route", str(out), "--param", "payer=medicare"]) == 0
    assert json.loads(capsys.readouterr().out)["variant"] == "posting"
    for bundle, _ in children.values():
        _assert_ciphertext_only(bundle)


def test_name_path_arguments_go_through_the_missing_key_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    children = _sealed_admitted(tmp_path, monkeypatch)
    monkeypatch.delenv(crypto.ENV_KEY)
    monkeypatch.delenv("OPENADAPT_FLOW_PLAINTEXT_AT_REST", raising=False)
    args = ["compose", "--out", str(tmp_path / "composed")]
    for name, (bundle, _) in children.items():
        args += ["--child", f"{name}={bundle}"]
    args += ["--handoff", "intake.patient_id=posting.patient_id"]
    assert main(args) == 2
    err = capsys.readouterr().err
    assert "OPENADAPT_BUNDLE_KEY" in err and "Nothing was read" in err
    assert not (tmp_path / "composed").exists()


# -- teach on sealed input ----------------------------------------------------


def _seal_dir(plain: Path, root: Path, kind: str) -> Path:
    """Copy a plaintext recording or run directory into a sealed set."""
    with sealed_set.writing(root, kind, KEY):  # type: ignore[arg-type]
        for path in sorted(plain.rglob("*")):
            if path.is_file():
                rel = path.relative_to(plain).as_posix()
                sealed_set.write_bytes(root / rel, path.read_bytes())
    return root


def _teach_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A halted run, a fix recording and the bundle that halted, all sealed."""
    from tests.test_teach_cli import _base_bundle, _dismiss_recording, _halt_run

    plain = tmp_path / "plain"
    plain.mkdir()
    base = _base_bundle(plain)
    _halt_run(base, plain / "run")
    _dismiss_recording(plain / "fix")
    bundle = tmp_path / "bundle"
    assert main(["seal", str(base), "--out", str(bundle)]) == 0
    run = _seal_dir(plain / "run", tmp_path / "run", "run")
    fix = _seal_dir(plain / "fix", tmp_path / "fix", "recording")
    return run, fix, bundle


def _assert_taught_sealed(out: Path, fix: Path) -> None:
    for root in (out, out.parent / f"{out.name}.skills", fix / "_teach_compiled"):
        assert sealed_set.is_sealed(root), root
        _assert_ciphertext_only(root)
    assert sealed_set.header_kind(out.parent / f"{out.name}.skills") == "library"
    if sealed_set.is_sealed(fix):
        # The fix recording still opens: its compiled copy is a separate set.
        sealed_set.SealedSet.open(fix, KEY).verify_all()
    assert sealed_set.is_file(out / "repair" / "candidate.json")
    learned = Workflow.load(out, key=KEY)
    clicks = [
        state.step.anchor.click_point
        for state in learned.program.states.values()  # type: ignore[union-attr]
        if state.step is not None and state.step.anchor is not None
    ]
    assert (140, 108) in clicks  # the recorded dismiss click was spliced in


def test_teach_reads_sealed_inputs_and_writes_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from openadapt_flow.learning.library import SkillLibrary

    _ready("openadapt_flow.__main__:_cmd_teach")
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    run, fix, bundle = _teach_inputs(tmp_path)
    before = {p: _files(p) for p in (run, bundle)}
    out = tmp_path / "taught"
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(bundle)]
    name = Workflow.load(bundle).name
    assert name not in str(tmp_path)
    capsys.readouterr()
    assert main([*args, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    # The summary names no skill id (the workflow name) read from sealed input.
    assert "LEARNED" in printed and name not in printed and "withheld" in printed
    _assert_taught_sealed(out, fix)
    assert {p: _files(p) for p in (run, bundle)} == before
    library = SkillLibrary(tmp_path / "taught.skills")
    assert library.sealed and library.skill_ids()
    # A second teach reads the sealed library back: the fix is learned already.
    assert main([*args, "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "no_change" in printed and name not in printed
    _assert_taught_sealed(out, fix)


def test_teach_in_strict_mode_seals_plain_inputs_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_teach_cli import _base_bundle, _dismiss_recording, _halt_run

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    base = _base_bundle(tmp_path)
    _halt_run(base, tmp_path / "run")
    fix = _dismiss_recording(tmp_path / "fix")
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    out = tmp_path / "taught"
    args = ["teach", str(tmp_path / "run"), "--fix", str(fix)]
    assert main([*args, "--bundle", str(base), "--out", str(out)]) == 0
    _assert_taught_sealed(out, fix)


def test_teach_refuses_a_plaintext_library_for_sealed_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    run, fix, bundle = _teach_inputs(tmp_path)
    library = tmp_path / "plain-library"
    library.mkdir()
    (library / "skills.json").write_text('{"skills": {}}')
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(bundle)]
    out = tmp_path / "taught"
    assert main([*args, "--out", str(out), "--library", str(library)]) == 2
    assert "unencrypted skill library" in capsys.readouterr().out
    assert not out.exists()


_MARKER = "PRIVATE-MARKER-4f1c"


def _marker_absent(root: Path, *printed: str) -> None:
    """The marker is in no printed text, log record or file under ``root``."""
    for text in printed:
        assert _MARKER not in text
    for path in root.rglob("*"):
        if path.is_file():
            assert _MARKER.encode() not in path.read_bytes(), path


@pytest.mark.parametrize("stale", ["state", "workflow"])
def test_teach_refusal_on_a_sealed_run_never_prints_its_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog, stale: str
) -> None:
    """A provenance refusal names only the paths on the command line, never a
    state id, step intent or workflow name read from the encrypted report."""
    from tests.test_teach_cli import _base_bundle, _dismiss_recording, _halt_run

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    plain = tmp_path / "plain"
    plain.mkdir()
    base = _base_bundle(plain)
    _halt_run(base, plain / "run")
    fix = _dismiss_recording(plain / "fix")
    report = json.loads((plain / "run" / "report.json").read_text())
    if stale == "state":
        report["halt"]["state_id"] = "s::gone"
        report["halt"]["intent"] = _MARKER
    else:
        report["workflow_name"] = _MARKER
    (plain / "run" / "report.json").write_text(json.dumps(report))
    run = _seal_dir(plain / "run", tmp_path / "run", "run")
    for path in (plain / "run").rglob("*"):
        if path.is_file():
            path.unlink()
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    out = tmp_path / "taught"
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(base)]
    capfd.readouterr()
    caplog.set_level("DEBUG")
    assert main([*args, "--out", str(out)]) == 2
    printed = capfd.readouterr()
    _marker_absent(tmp_path, printed.out, printed.err, caplog.text)
    assert "teach cannot run" in printed.out
    assert "did not stop in a program --bundle" in printed.out
    assert repr(report["halt"]["state_id"]) not in printed.out
    assert not out.exists()


@pytest.mark.parametrize("alias", ["same path", "symlink", "inside"])
def test_teach_never_writes_over_the_sealed_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, alias: str
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    run, fix, bundle = _teach_inputs(tmp_path)
    if alias == "same path":
        out = bundle
    elif alias == "symlink":
        out = tmp_path / "alias"
        out.symlink_to(bundle, target_is_directory=True)
    else:
        out = bundle / "taught"
    before = {p: _files(p) for p in (run, fix, bundle)}
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(bundle)]
    capsys.readouterr()
    assert main([*args, "--out", str(out)]) == 2
    printed = capsys.readouterr().out
    assert "teach cannot run" in printed and "overlaps the base bundle" in printed
    assert {p: _files(p) for p in (run, fix, bundle)} == before
    assert not (out.parent / f"{out.name}.skills").exists()
    assert not (fix / "_teach_compiled").exists()
    assert Workflow.load(bundle, key=KEY).name


@pytest.mark.parametrize("which", ["run", "fix"])
def test_teach_refuses_an_out_that_is_another_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, which: str
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    run, fix, bundle = _teach_inputs(tmp_path)
    out = {"run": run, "fix": fix}[which]
    before = {p: _files(p) for p in (run, fix, bundle)}
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(bundle)]
    assert main([*args, "--out", str(out)]) == 2
    assert "overlaps the" in capsys.readouterr().out
    assert {p: _files(p) for p in (run, fix, bundle)} == before


# -- push of a sealed source --------------------------------------------------


@pytest.fixture
def push_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import base64

    from openadapt_flow import hosted, privacy
    from tests.test_hosted import _FakeScrubber

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(hosted.TOKEN_ENV, raising=False)
    monkeypatch.setenv(hosted.DESTINATION_KIND_ENV, "customer-managed")
    monkeypatch.setenv(hosted.TRUSTED_HOSTS_ENV, "https://h.test")
    monkeypatch.setenv("OPENADAPT_SANITIZATION_POLICY_KEY_ID", "test-policy-key")
    monkeypatch.setenv(
        "OPENADAPT_SANITIZATION_POLICY_KEY",
        base64.b64encode(b"test-policy-secret-material-32-bytes!!").decode(),
    )
    monkeypatch.setattr(hosted, "_keyring_token", lambda host: None)
    privacy.set_text_scrubber(_FakeScrubber())
    yield
    privacy.reset_scrubbers()


def _sealed_phi_recording(root: Path) -> Path:
    with sealed_set.writing(root, "recording", KEY):
        sealed_set.write_text(
            root / "events.jsonl",
            json.dumps({"kind": "type", "text": "Jane Doe 12345"}) + "\n",
        )
        sealed_set.write_text(root / "meta.json", json.dumps({"task": "Jane Doe"}))
    return root


def _uploaded(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    import httpx

    from tests.test_hosted import _accepted_recording_post

    sent: dict[str, bytes] = {}
    accept = _accepted_recording_post()

    def post(url, **kw):
        sent["body"] = kw["files"]["file"][1].read()
        return accept(url, **kw)

    monkeypatch.setattr(httpx, "post", post)
    return sent


def _no_plaintext_under(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"Jane Doe" not in data and b"<PERSON>" not in data, path


def test_push_of_a_sealed_recording_keeps_the_review_copy_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, push_env, capsys
) -> None:
    import io
    import zipfile

    from openadapt_flow import hosted
    from openadapt_flow.sanitized_artifact import (
        add_manual_text_redaction,
        approved_archive_path,
        render_review_html,
    )

    for name in ("push", "review_sanitized", "approve_sanitized"):
        _ready(f"openadapt_flow.__main__:_cmd_{name}")
    monkeypatch.setenv(hosted.AUTO_APPROVE_ENV, "false")
    rec = _sealed_phi_recording(tmp_path / "rec")
    before = _files(rec)
    push = ["push", str(rec), "--host", "https://h.test", "--token", "tok"]
    assert main(push) == 0
    out = capsys.readouterr().out
    derivative = Path(out.split("Sanitized derivative: ")[1].splitlines()[0])
    assert sealed_set.header_kind(derivative) == "sanitized"
    _assert_ciphertext_only(derivative)
    # Review decrypts in memory; a reviewer's redaction stays encrypted.
    page = render_review_html(rec, derivative, "csrf")
    assert "Jane Doe" in page and "&lt;PERSON&gt;" in page
    add_manual_text_redaction(derivative, "meta.json", "task", "field")
    _assert_ciphertext_only(derivative)
    approve = ["approve-sanitized", str(derivative), "--original", str(rec)]
    assert main([*approve, "--reviewer", "reviewer@example.test"]) == 0
    assert crypto.is_encrypted(approved_archive_path(derivative).read_bytes())
    sent = _uploaded(monkeypatch)
    upload = ["push", str(derivative), "--host", "https://h.test", "--token", "tok"]
    assert main(upload) == 0
    with zipfile.ZipFile(io.BytesIO(sent["body"])) as zf:
        uploaded = {name: zf.read(name) for name in zf.namelist()}
    assert b"<PERSON>" in uploaded["events.jsonl"]
    assert all(b"Jane Doe" not in data for data in uploaded.values())
    assert b"field" in uploaded["meta.json"]
    assert _files(rec) == before
    _no_plaintext_under(tmp_path)


def test_push_in_strict_mode_seals_the_review_copy_of_a_plain_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, push_env
) -> None:
    from openadapt_flow import hosted

    monkeypatch.setenv(hosted.AUTO_APPROVE_ENV, "true")
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "events.jsonl").write_text(json.dumps({"text": "Jane Doe"}) + "\n")
    (rec / "meta.json").write_text("{}")
    sent = _uploaded(monkeypatch)
    out = tmp_path / "derivative"
    push = ["push", str(rec), "--host", "https://h.test", "--token", "tok"]
    assert main([*push, "--sanitized-out", str(out)]) == 0
    assert sealed_set.header_kind(out) == "sanitized"
    _assert_ciphertext_only(out)
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(sent["body"])) as zf:
        assert b"<PERSON>" in zf.read("events.jsonl")
    (rec / "events.jsonl").unlink()
    _no_plaintext_under(tmp_path)


def _bundle_with_a_bad_action(source: Path, out: Path, *, key=None) -> Path:
    """A copy of ``source`` whose first action is the private marker."""
    opened = sealed_set.open_set(source, KEY)
    files = {rel: opened.read_bytes(rel) for rel in opened.files("")}
    doc = json.loads(files["workflow.json"])
    doc["steps"][0]["action"] = _MARKER
    files["workflow.json"] = json.dumps(doc).encode()
    if key is None:
        for rel, data in files.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_bytes(data)
        return out
    with sealed_set.writing(out, "bundle", key):
        for rel, data in files.items():
            sealed_set.write_bytes(out / rel, data)
    return out


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "sealed input"])
def test_push_refusal_on_a_sealed_bundle_never_prints_its_contents(
    sealed: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    push_env,
    capfd,
    caplog,
    strict: bool,
) -> None:
    from openadapt_flow import hosted

    monkeypatch.setenv(hosted.AUTO_APPROVE_ENV, "false")
    if strict:
        monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    bad = _bundle_with_a_bad_action(sealed, tmp_path / "bad", key=KEY)
    assert sealed_set.is_sealed(bad)
    push = ["push", str(bad), "--kind", "bundle", "--host", "https://h.test"]
    capfd.readouterr()
    caplog.set_level("DEBUG")
    assert main([*push, "--token", "tok"]) == 1
    captured = capfd.readouterr()
    _marker_absent(tmp_path, captured.out, captured.err, caplog.text)
    assert "push failed: the input was refused (ValidationError)" in captured.out
    assert "withheld" in captured.out


def test_push_refusal_on_a_plain_bundle_keeps_its_details(
    sealed: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    push_env,
    capsys,
) -> None:
    """Outside strict mode a plaintext input's refusal says what is wrong."""
    from openadapt_flow import hosted

    monkeypatch.setenv(hosted.AUTO_APPROVE_ENV, "false")
    bad = _bundle_with_a_bad_action(sealed, tmp_path / "bad")
    assert not sealed_set.is_sealed(bad)
    push = ["push", str(bad), "--kind", "bundle", "--host", "https://h.test"]
    assert main([*push, "--token", "tok"]) == 1
    out = capsys.readouterr().out
    assert "Cannot validate generated workflow.py" in out
    assert "type=enum" in out and _MARKER not in out  # no quoted input value


def test_refusal_text_never_quotes_a_validation_input() -> None:
    from pydantic import ValidationError

    from openadapt_flow import safe_errors

    try:
        Step.model_validate({"id": "s", "intent": "x", "action": _MARKER})
    except ValidationError as exc:
        error = exc
    assert _MARKER in str(error)
    wrapped = RuntimeError(f"Cannot validate: {error}")
    for text in (safe_errors.message(error), safe_errors.message(wrapped)):
        assert _MARKER not in text and "input_type=str" in text
    assert "input_value withheld" in safe_errors.message(f"input_value={_MARKER}")


def test_an_unexpected_failure_on_sealed_input_prints_no_traceback(
    sealed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog
) -> None:
    """An error no handler expects would print a traceback quoting what the
    command read; with sealed input the CLI names its kind only."""
    import openadapt_flow.__main__ as cli

    def _cmd_visualize(args) -> int:
        raise RuntimeError(f"read {_MARKER}")

    _cmd_visualize.__module__ = "openadapt_flow.__main__"
    monkeypatch.setattr(cli, "_cmd_visualize", _cmd_visualize)
    capfd.readouterr()
    caplog.set_level("DEBUG")
    out = tmp_path / "graph.json"
    assert main(["visualize", str(sealed), "--format", "json", "--out", str(out)]) == 1
    printed = capfd.readouterr()
    _marker_absent(tmp_path, printed.out, printed.err, caplog.text)
    assert "FAILED: the input was refused (RuntimeError)" in printed.err
    # Plain input still raises, so its traceback stays available.
    with pytest.raises(RuntimeError, match=_MARKER):
        main(["visualize", str(_plain_bundle(tmp_path / "p")), "--out", str(out)])


def test_a_sealed_library_reads_back_with_its_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.ir import lift_to_program
    from openadapt_flow.learning.library import SkillLibrary

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    own_key = "library-only-test-passphrase-7d2e"
    root = tmp_path / "own.skills"
    graph = lift_to_program(Workflow.load(_plain_bundle(tmp_path)))
    SkillLibrary(root, seal=True, key=own_key).create_skill("own", graph)
    _assert_ciphertext_only(root)
    assert SkillLibrary(root, key=own_key).skill_ids() == ["own"]
    with pytest.raises((sealed_set.SealedSetError, crypto.CryptoError)):
        SkillLibrary(root)  # the global key does not open it


def test_a_plaintext_copy_of_a_sealed_bundle_never_loads_as_encrypted(
    sealed: Path, tmp_path: Path
) -> None:
    """A decrypted copy (an upload, an export) keeps encrypted: true in its
    JSON, but the flag holds only with an encrypted envelope on disk."""
    opened = sealed_set.open_set(sealed, KEY)
    copy = tmp_path / "plain-copy"
    for rel in opened.files(""):
        target = copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(opened.read_bytes(rel))
    assert json.loads((copy / "workflow.json").read_text())["encrypted"] is True
    loaded = Workflow.load(copy)
    assert loaded.encrypted is False
    assert loaded.manifest is not None and loaded.manifest.encrypted is False


def _named_teach_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Plain teach inputs whose workflow name (the default skill id) is the
    private marker."""
    import tests.test_teach_cli as cli

    original = cli._base_workflow

    def named() -> Workflow:
        workflow = original()
        workflow.name = _MARKER
        return workflow

    monkeypatch.setattr(cli, "_base_workflow", named)
    base = cli._base_bundle(tmp_path / "in")
    cli._halt_run(base, tmp_path / "in" / "run")
    fix = cli._dismiss_recording(tmp_path / "in" / "fix")
    assert Workflow.load(base).name == _MARKER
    return tmp_path / "in" / "run", fix, base


def test_strict_teach_success_never_prints_a_name_from_its_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    run, fix, base = _named_teach_inputs(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    out = tmp_path / "taught"
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(base)]
    capfd.readouterr()
    caplog.set_level("DEBUG")
    assert main([*args, "--out", str(out)]) == 0
    learned = capfd.readouterr()
    # A second teach reads the library back and refuses with no_change.
    assert main([*args, "--out", str(out)]) == 1
    again = capfd.readouterr()
    for text in (learned.out, learned.err, again.out, again.err, caplog.text):
        assert _MARKER not in text
    assert "LEARNED" in learned.out and "no_change" in again.out
    assert "(skill withheld, sha256 " in learned.out
    for root in (out, out.parent / f"{out.name}.skills", fix / "_teach_compiled"):
        _marker_absent(root)


def test_teach_success_on_plain_input_names_its_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The control: outside strict mode a plaintext run's summary names it."""
    run, fix, base = _named_teach_inputs(tmp_path, monkeypatch)
    out = tmp_path / "taught"
    capsys.readouterr()
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(base)]
    assert main([*args, "--out", str(out)]) == 0
    assert f"(skill {_MARKER!r})" in capsys.readouterr().out


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "plain"])
def test_push_success_names_the_workflow_only_for_plain_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog, strict: bool
) -> None:
    from openadapt_flow import hosted

    if strict:
        monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    pushed = {
        "workflow_id": "wf_1",
        "workflow_name": _MARKER,
        "kind": "bundle",
        "status": "active",
    }
    monkeypatch.setattr(hosted, "push", lambda *a, **kw: pushed)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    capfd.readouterr()
    caplog.set_level("DEBUG")
    push = ["push", str(bundle), "--kind", "bundle", "--host", "https://h.test"]
    assert main([*push, "--token", "tok"]) == 0
    captured = capfd.readouterr()
    assert "Pushed. workflow_id=wf_1" in captured.out
    if strict:
        for text in (captured.out, captured.err, caplog.text):
            assert _MARKER not in text
        assert "name=withheld, sha256 " in captured.out
    else:
        assert f"name={_MARKER!r}" in captured.out


def test_strict_library_mismatch_refusal_never_prints_the_skill_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog
) -> None:
    """A skill library that already holds another program for the skill is
    refused without naming the skill id teach read from the input."""
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    run, fix, base = _named_teach_inputs(tmp_path, monkeypatch)
    from openadapt_flow.learning.library import SkillLibrary

    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    args = ["teach", str(run), "--fix", str(fix), "--bundle", str(base)]
    first = tmp_path / "first"
    assert main([*args, "--out", str(first)]) == 0
    # A sealed library that holds the taught program, not the base one, for
    # the same skill id.
    library = tmp_path / "library"
    taught = Workflow.load(first, key=KEY)
    SkillLibrary(library, seal=True).create_skill(_MARKER, taught.program)
    capfd.readouterr()
    caplog.set_level("DEBUG")
    second = tmp_path / "second"
    assert main([*args, "--library", str(library), "--out", str(second)]) == 2
    printed = capfd.readouterr()
    for text in (printed.out, printed.err, caplog.text):
        assert _MARKER not in text
    assert "teach cannot run: the input was refused (TeachError)" in printed.out
    assert not second.exists()


@pytest.mark.parametrize("public", [False, True], ids=["read", "command line"])
def test_strict_teach_refusal_prints_only_command_line_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog, public: bool
) -> None:
    """Strict teach prints a refusal's text only when the error says it was
    built from the command line. (teach-branch is refused in strict mode and
    for sealed input before it runs.)"""
    from openadapt_flow.learning import teach
    from openadapt_flow.learning.teach import TeachError

    def refuse(*_args, **_kwargs):
        raise TeachError(f"refused {_MARKER!r}", public=public)

    monkeypatch.setattr(teach, "teach", refuse)
    monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")
    for name in ("run", "fix", "bundle"):
        (tmp_path / name).mkdir()
    run, fix, bundle = (str(tmp_path / n) for n in ("run", "fix", "bundle"))
    capfd.readouterr()
    caplog.set_level("DEBUG")
    out = str(tmp_path / "out")
    args = ["teach", run, "--fix", fix, "--bundle", bundle, "--out", out]
    assert main(args) == 2
    printed = capfd.readouterr()
    assert "teach cannot run" in printed.out
    if public:
        assert f"refused {_MARKER!r}" in printed.out
    else:
        for text in (printed.out, printed.err, caplog.text):
            assert _MARKER not in text
        assert "the input was refused (TeachError)" in printed.out


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "plain"])
def test_push_failure_of_its_own_is_withheld_only_for_private_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog, strict: bool
) -> None:
    """A push error with no cause can still name a path inside the artifact,
    so strict mode withholds it too."""
    from openadapt_flow import hosted

    if strict:
        monkeypatch.setenv("OPENADAPT_FLOW_AT_REST", "strict")

    def fail(*_args, **_kwargs):
        raise hosted.HostedError(f"cannot read {_MARKER}/steps.json")

    monkeypatch.setattr(hosted, "push", fail)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    capfd.readouterr()
    caplog.set_level("DEBUG")
    push = ["push", str(bundle), "--kind", "bundle", "--host", "https://h.test"]
    assert main([*push, "--token", "tok"]) == 1
    captured = capfd.readouterr()
    if strict:
        for text in (captured.out, captured.err, caplog.text):
            assert _MARKER not in text
        assert "push failed: the input was refused (HostedError)" in captured.out
    else:
        assert f"push failed: cannot read {_MARKER}/steps.json" in captured.out


def test_teach_branch_with_a_sealed_example_is_refused_before_it_runs(
    sealed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd, caplog
) -> None:
    """--example RUN FIX arrives as a list of pairs; a sealed RUN inside it
    is still found, so teach-branch is refused before it reads anything."""
    from openadapt_flow.learning import branch_teach

    calls: list[object] = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise branch_teach.TeachError(f"workflow {_MARKER!r} did not stop here")

    monkeypatch.setattr(branch_teach, "teach_branch", refuse)
    base = _plain_bundle(tmp_path / "base")
    fix = tmp_path / "fix"
    fix.mkdir()
    capfd.readouterr()
    caplog.set_level("DEBUG")
    examples = ["--example", str(sealed), str(fix)] * 2
    out = str(tmp_path / "out")
    args = ["teach-branch", *examples, "--bundle", str(base), "--out", out]
    assert main(args) == 2
    printed = capfd.readouterr()
    for text in (printed.out, printed.err, caplog.text):
        assert _MARKER not in text
    assert "REFUSED" in printed.err and "does not read encrypted input" in printed.err
    assert not calls


def test_sealed_paths_are_found_at_any_depth_and_unknown_values_are_private(
    sealed: Path, tmp_path: Path
) -> None:
    import argparse

    from openadapt_flow.__main__ import _private_input, _sealed_paths_in

    plain = str(tmp_path / "plain")
    nested = argparse.Namespace(
        example=[[plain, plain], (plain, {"fix": [str(sealed)]})], out=plain
    )
    assert _sealed_paths_in(nested) == [sealed]
    assert _private_input(nested)
    assert not _private_input(argparse.Namespace(example=[[plain, plain]]))
    # A value that is neither a path, a scalar nor a container: fail closed.
    assert _private_input(argparse.Namespace(example=[[plain, object()]]))
    # Objects the code sets on the namespace do not make the run private.
    assert not _private_input(argparse.Namespace(_handler=object(), out=plain))


def _odd_spelling(sealed: Path, tmp_path: Path, spelling: str) -> str:
    """Move ``sealed`` under a directory whose path is spelled oddly."""
    import shutil

    if spelling == "newline":
        parent = tmp_path / "line\nbreak"
        parent.mkdir()
        shutil.move(str(sealed), str(parent / "sealed"))
        return str(parent / "sealed")
    parent = tmp_path / "url:"
    parent.mkdir()
    shutil.move(str(sealed), str(parent / "sealed"))
    # "url://sealed" is the same local directory with a redundant slash.
    return f"{tmp_path}/url://sealed"


@pytest.mark.parametrize("spelling", ["newline", "url-slash"])
def test_a_sealed_example_under_any_path_spelling_is_refused(
    sealed: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd,
    caplog,
    spelling: str,
) -> None:
    """A newline or "://" in a path is still a local path: the sealed example
    is found and teach-branch is refused before it reads anything."""
    from openadapt_flow.learning import branch_teach

    calls: list[object] = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise branch_teach.TeachError(f"workflow {_MARKER!r} did not stop here")

    monkeypatch.setattr(branch_teach, "teach_branch", refuse)
    example = _odd_spelling(sealed, tmp_path, spelling)
    base = _plain_bundle(tmp_path / "base")
    fix = tmp_path / "fix"
    fix.mkdir()
    capfd.readouterr()
    caplog.set_level("DEBUG")
    out = tmp_path / "out"
    args = ["teach-branch", "--example", example, str(fix), "--bundle", str(base)]
    assert main([*args, "--out", str(out)]) == 2
    printed = capfd.readouterr()
    for text in (printed.out, printed.err, caplog.text):
        assert _MARKER not in text
    assert "REFUSED" in printed.err and "does not read encrypted input" in printed.err
    assert not calls
    assert not out.exists()


def test_a_path_spelled_with_a_newline_or_url_slash_is_private(
    sealed: Path, tmp_path: Path
) -> None:
    import argparse

    from openadapt_flow.__main__ import _private_input, _sealed_paths_in

    example = _odd_spelling(sealed, tmp_path, "url-slash")
    assert _sealed_paths_in(argparse.Namespace(example=[[example, "x"]]))
    # Text that is no local path stays public, whatever it looks like.
    public = argparse.Namespace(
        url="https://example.invalid/a", note="two\nlines", long="x" * 5000
    )
    assert not _private_input(public)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any dir"
)
def test_a_path_whose_header_cannot_be_checked_is_private(tmp_path: Path) -> None:
    import argparse

    from openadapt_flow.__main__ import _private_input

    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert _private_input(argparse.Namespace(example=[str(locked / "inner")]))
    finally:
        locked.chmod(0o700)
