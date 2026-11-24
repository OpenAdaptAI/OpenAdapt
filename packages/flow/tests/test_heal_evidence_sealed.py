"""Heal evidence frames inside an encrypted run directory stay sealed.

Self-repair writes the frame each new bridge step was captured from under
``run_dir/self-repair/attempt-N/evidence/heals/<step>/screen.png``. Under the
encryption-at-rest default the run directory is a sealed set, so that
screenshot (which can show a person's record) must be written through the
set: a plaintext file there is a leak, and the next write to the run (the
report) refuses the whole set.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow import at_rest, crypto, sealed_set
from openadapt_flow.bundle_persistence import copy_bundle_files
from openadapt_flow.ir import Workflow
from openadapt_flow.repair.self_repair import _before_frame
from openadapt_flow.runtime.heal import (
    has_heal_frame,
    heal_frame_path,
    read_heal_frame,
    write_heal_frame,
)
from tests.test_sealed_at_rest import (
    KEY,
    PNG_SIGNATURE,
    _assert_ciphertext_only,
    _png,
    sealed_pair,  # noqa: F401 - the shared sealed-bundle fixture
)


@pytest.fixture
def sealed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A finalized sealed run directory under the default at-rest mode."""
    for name in (
        at_rest.OPT_OUT_ENV,
        at_rest.MODE_ENV,
        at_rest.ENCRYPT_ENV,
        crypto.ENV_KEY,
        sealed_set.RECORDING_ENV_KEY,
        "OPENADAPT_POLICY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(at_rest.HOME_ENV, str(tmp_path / "at-rest-home"))
    at_rest._reset_for_tests()
    run_dir = tmp_path / "run"
    with sealed_set.writing(run_dir, "run", crypto.resolve_key(None)):
        sealed_set.write_text(run_dir / "report.json", "{}")
    assert sealed_set.is_sealed(run_dir)
    yield run_dir
    at_rest._reset_for_tests()


def _plaintext_pngs(root: Path) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and p.read_bytes().startswith(PNG_SIGNATURE)
    ]


def test_a_bridge_frame_in_a_sealed_run_is_sealed_and_readable(
    sealed_run: Path,
) -> None:
    evidence = sealed_run / "self-repair" / "attempt-1" / "evidence"
    frame = _png()
    write_heal_frame(evidence, "step_000_bridge_1", frame)

    # The set still opens, and a later write to it (the run report, as
    # self-repair's finish does) is accepted rather than refused.
    sealed_set.open_set(sealed_run)
    sealed_set.write_text(sealed_run / "report.json", '{"ok": true}')
    sealed_set.open_set(sealed_run)

    assert not heal_frame_path(evidence, "step_000_bridge_1").exists()
    assert _plaintext_pngs(sealed_run) == []
    _assert_ciphertext_only(sealed_run)
    assert has_heal_frame(evidence, "step_000_bridge_1")
    assert read_heal_frame(evidence, "step_000_bridge_1") == frame


def test_a_plain_run_keeps_its_frame_where_reviewers_look(tmp_path: Path) -> None:
    frame = _png()
    path = write_heal_frame(tmp_path / "run", "step_000_bridge_1", frame)
    assert path == tmp_path / "run" / "heals" / "step_000_bridge_1" / "screen.png"
    assert path.read_bytes() == frame
    assert read_heal_frame(tmp_path / "run", "step_000_bridge_1") == frame


def test_the_halted_frame_is_read_through_the_sealed_run(sealed_run: Path) -> None:
    frame = _png(shade=200)
    sealed_set.write_bytes(sealed_run / "steps" / "step_000_before.png", frame)
    halted = SimpleNamespace(before_png="steps/step_000_before.png")
    assert _before_frame(sealed_run, halted) == frame
    missing = SimpleNamespace(before_png="steps/step_009_before.png")
    assert _before_frame(sealed_run, missing) is None


def test_a_patched_copy_of_a_sealed_bundle_is_a_sealed_set_that_opens(
    sealed_pair: tuple[Path, Path],  # noqa: F811 - the shared fixture
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-repair copies the bundle without its templates and manifest and
    then saves the patch into the copy. A file copy of a sealed set that
    leaves files out no longer matches its index, and the save refused it."""
    source, _ = sealed_pair
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    workflow = Workflow.load(source)
    out = tmp_path / "patched"
    copy_bundle_files(source, out, ignore=("repair", "manifest.json", "templates"))
    copied = sealed_set.open_set(out).files()
    assert copied
    assert not [r for r in copied if r == "manifest.json" or r.startswith("templates/")]
    _assert_ciphertext_only(out)

    patched = workflow.model_copy(deep=True)
    patched.name = "patched-copy"
    patched.save(out, encrypt=True)
    sealed_set.open_set(out).verify_all()
    _assert_ciphertext_only(out)
    assert _plaintext_pngs(out) == []
    assert Workflow.load(out).name == "patched-copy"


def test_a_plain_bundle_copy_is_a_plain_file_copy(tmp_path: Path) -> None:
    source = tmp_path / "plain"
    (source / "templates").mkdir(parents=True)
    (source / "workflow.json").write_text("{}")
    (source / "manifest.json").write_text("{}")
    (source / "templates" / "a.png").write_bytes(_png())
    out = tmp_path / "copy"
    copy_bundle_files(source, out, ignore=("manifest.json", "templates"))
    assert sorted(p.name for p in out.rglob("*")) == ["workflow.json"]
