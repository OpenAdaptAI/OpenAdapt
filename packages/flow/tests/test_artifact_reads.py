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
