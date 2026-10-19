"""Everything that can hold patient data is ciphertext at rest.

Contract (docs/phi_at_rest.md):

- ``seal`` encrypts every bundle file the inventory marks as patient data,
  binds the whole file set to one sealed index, and refuses a file it does
  not know.
- With a key configured, ``record`` writes an encrypted recording from the
  first frame; ``compile`` turns it into a sealed bundle; a run of a sealed
  bundle writes a sealed run directory. None of them writes plaintext to
  disk, temporary files included.
- Without the key, each of them refuses with a message that names the key.

The no-plaintext check uses a process audit hook: every file this Python
process opens for writing, renames or deletes during the watched block is
checked for plaintext markers, including files that are gone by the end.
Everything here is synthetic.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from PIL import Image

from openadapt_flow import crypto, sealed_set
from openadapt_flow.__main__ import main
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.recorder import Recorder

KEY = "synthetic-at-rest-key"
NOTE = "Synthetic follow-up in two weeks"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _DiskWatch:
    """Remember every file written, renamed or removed while armed."""

    def __init__(self) -> None:
        self.armed = False
        self.written: set[str] = set()
        self.transient: list[tuple[str, bytes]] = []
        self._busy = False

    def _snapshot(self, raw: object) -> None:
        if self._busy or isinstance(raw, int):
            return
        self._busy = True
        try:
            path = os.fsdecode(raw)  # type: ignore[arg-type]
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    self.transient.append((path, fh.read()))
        finally:
            self._busy = False

    def __call__(self, event: str, args: tuple) -> None:
        if not self.armed or self._busy:
            return
        if event == "open":
            path, mode, flags = args
            if path is None or isinstance(path, int):
                return
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
            )
            if writes:
                self.written.add(os.path.abspath(os.fsdecode(path)))
        elif event == "os.remove":
            self._snapshot(args[0])
        elif event == "os.rename":
            self._snapshot(args[0])
            self.written.add(os.path.abspath(os.fsdecode(args[1])))

    def leaks(self, markers: list[bytes]) -> list[str]:
        found: list[str] = []
        for path in sorted(self.written):
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    data = fh.read()
                found += [f"{path}: {m!r}" for m in markers if m in data]
        for path, data in self.transient:
            found += [f"{path} (transient): {m!r}" for m in markers if m in data]
        return found


_WATCH = _DiskWatch()
# Audit hooks cannot be removed; this one does nothing unless a test arms it.
sys.addaudithook(_WATCH)


@contextmanager
def watching_disk() -> Iterator[_DiskWatch]:
    _WATCH.written.clear()
    _WATCH.transient.clear()
    _WATCH.armed = True
    try:
        yield _WATCH
    finally:
        _WATCH.armed = False


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _assert_ciphertext_only(root: Path) -> None:
    """Each file is a header, a structural file, or a sealed container."""
    kind = sealed_set.header_kind(root)
    for rel, data in _files(root).items():
        if Path(rel).name == sealed_set.HEADER:
            continue
        entry = sealed_set.classify(kind, rel)
        if entry is not None and entry.classification == "structural":
            continue
        assert crypto.is_encrypted(data), f"{root.name}/{rel} is not ciphertext"


def _png(size: tuple[int, int] = (320, 200), shade: int = 240) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (shade, shade, shade)).save(buf, format="PNG")
    return buf.getvalue()


# -- sealing a compiled bundle ----------------------------------------------


def _plain_bundle(root: Path) -> Path:
    bundle = root / "plain"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "open.png").write_bytes(_png(shade=10))
    (bundle / "templates" / "save.png").write_bytes(_png(shade=20))
    Workflow(
        name="synthetic-intake",
        steps=[
            Step(
                id=f"step_{i}",
                intent=f"Click {name}",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template=f"templates/{name}.png",
                    region=(10, 20, 30, 40),
                    click_point=(25, 40),
                    ocr_text=f"{name} Testpatient Zed",
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text=f"{name} done"
                    )
                ],
            )
            for i, name in enumerate(("open", "save"))
        ],
    ).save(bundle)
    (bundle / "workflow.py").write_text("# Testpatient Zed\n")
    (bundle / "param_proposals.json").write_text(
        json.dumps({"proposals": [{"field_label": "Testpatient Zed"}]})
    )
    return bundle


PLAIN_MARKERS = [b"Testpatient", PNG_SIGNATURE]


def test_seal_encrypts_every_file_and_binds_the_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _plain_bundle(tmp_path)
    sealed = tmp_path / "sealed"
    monkeypatch.setenv(crypto.ENV_KEY, KEY)

    with watching_disk() as watch:
        assert main(["seal", str(source), "--out", str(sealed)]) == 0
    written_under_sealed = [p for p in watch.written if p.startswith(str(sealed.parent))]
    assert written_under_sealed
    assert [
        leak for leak in watch.leaks(PLAIN_MARKERS) if str(source) not in leak
    ] == []

    _assert_ciphertext_only(sealed)
    on_disk = _files(sealed)
    assert set(on_disk) == {
        sealed_set.HEADER,
        "manifest.json",
        "workflow.json.enc",
        "workflow.py.enc",
        "param_proposals.json.enc",
        "templates/open.png.enc",
        "templates/save.png.enc",
    }
    opened = sealed_set.SealedSet.open(sealed, KEY)
    assert f"Sealed file set: sha256:{opened.set_digest}" in capsys.readouterr().out
    assert sealed_set.read_text(sealed / "workflow.py") == "# Testpatient Zed\n"

    loaded = Workflow.load(sealed, key=KEY)
    assert loaded.encrypted
    assert loaded.decrypted_template("templates/open.png") == _png(shade=10)


def test_seal_refuses_a_file_it_does_not_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _plain_bundle(tmp_path)
    (source / "notes.txt").write_text("Testpatient Zed called back")
    monkeypatch.setenv(crypto.ENV_KEY, KEY)

    assert main(["seal", str(source), "--out", str(tmp_path / "sealed")]) == 2
    out = capsys.readouterr().out
    assert "notes.txt" in out and "does not know" in out
    assert not (tmp_path / "sealed").exists()


@pytest.fixture
def sealed_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = _plain_bundle(tmp_path)
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    first, second = tmp_path / "first", tmp_path / "second"
    assert main(["seal", str(source), "--out", str(first)]) == 0
    assert main(["seal", str(source), "--out", str(second)]) == 0
    return first, second


def test_sealed_set_refuses_an_added_file(sealed_pair: tuple[Path, Path]) -> None:
    sealed, _ = sealed_pair
    (sealed / "templates" / "extra.png.enc").write_bytes(
        (sealed / "templates" / "open.png.enc").read_bytes()
    )
    with pytest.raises(sealed_set.SealedSetError, match="not in the sealed index"):
        Workflow.load(sealed, key=KEY)


def test_sealed_set_refuses_a_dropped_file(sealed_pair: tuple[Path, Path]) -> None:
    sealed, _ = sealed_pair
    (sealed / "param_proposals.json.enc").unlink()
    with pytest.raises(sealed_set.SealedSetError, match="missing"):
        Workflow.load(sealed, key=KEY)


def test_sealed_set_refuses_swapped_files(sealed_pair: tuple[Path, Path]) -> None:
    sealed, _ = sealed_pair
    first = sealed / "templates" / "open.png.enc"
    second = sealed / "templates" / "save.png.enc"
    a, b = first.read_bytes(), second.read_bytes()
    first.write_bytes(b)
    second.write_bytes(a)
    with pytest.raises(sealed_set.SealedSetError, match="changed after sealing"):
        Workflow.load(sealed, key=KEY)


def test_sealed_set_refuses_a_ciphertext_from_another_seal(
    sealed_pair: tuple[Path, Path],
) -> None:
    sealed, other = sealed_pair
    (sealed / "workflow.json.enc").write_bytes(
        (other / "workflow.json.enc").read_bytes()
    )
    with pytest.raises(sealed_set.SealedSetError, match="changed after sealing"):
        Workflow.load(sealed, key=KEY)


def test_sealed_set_refuses_an_edited_index(sealed_pair: tuple[Path, Path]) -> None:
    sealed, other = sealed_pair
    header = json.loads((sealed / sealed_set.HEADER).read_text())
    header["index"] = json.loads((other / sealed_set.HEADER).read_text())["index"]
    (sealed / sealed_set.HEADER).write_text(json.dumps(header))
    with pytest.raises(crypto.DecryptionError):
        Workflow.load(sealed, key=KEY)


def test_sealed_bundle_fails_closed_without_or_with_a_wrong_key(
    sealed_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sealed, _ = sealed_pair
    with pytest.raises(crypto.DecryptionError):
        Workflow.load(sealed, key="not-the-key")
    monkeypatch.delenv(crypto.ENV_KEY)
    with pytest.raises(sealed_set.SealedSetKeyError, match=crypto.ENV_KEY):
        Workflow.load(sealed)
    assert main(["certify", str(sealed), "--policy", "permissive"]) == 2
    assert crypto.ENV_KEY in capsys.readouterr().err


def test_command_that_cannot_read_sealed_input_refuses(
    sealed_pair: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sealed, _ = sealed_pair
    before = _files(sealed)
    assert main(["visualize", str(sealed)]) == 2
    err = capsys.readouterr().err
    assert "does not read encrypted input" in err
    assert _files(sealed) == before


def test_certify_and_resave_keep_a_sealed_bundle_sealed(
    sealed_pair: tuple[Path, Path],
) -> None:
    sealed, _ = sealed_pair
    with watching_disk() as watch:
        assert main(["certify", str(sealed), "--policy", "permissive"]) == 0
        # Many commands re-save a bundle with a bare save(); a sealed bundle
        # must stay sealed and its index must follow the change.
        workflow = Workflow.load(sealed, key=KEY)
        workflow.stamp_certification("permissive", passed=True)
        workflow.save(sealed)
    assert watch.leaks(PLAIN_MARKERS) == []
    _assert_ciphertext_only(sealed)
    reloaded = Workflow.load(sealed, key=KEY)
    assert reloaded.manifest is not None and reloaded.manifest.provenance.certified
    assert reloaded.decrypted_template("templates/save.png") == _png(shade=20)


# -- encrypted recording ------------------------------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self._png = _png((640, 400))

    @property
    def viewport(self) -> tuple[int, int]:
        return (640, 400)

    def screenshot(self) -> bytes:
        return self._png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        pass

    def type_text(self, text: str) -> None:
        pass

    def press(self, key: str) -> None:
        pass

    def scroll(self, dx: int, dy: int) -> None:
        pass


def test_encrypted_recording_writes_no_plaintext(tmp_path: Path) -> None:
    rec_dir = tmp_path / "rec"
    with watching_disk() as watch:
        rec = Recorder(
            _FakeBackend(), rec_dir, app_url="http://app.test/", encryption_key=KEY
        )
        rec.click(10, 20)
        rec.type_text("Testpatient Zed", param="patient")
        rec.press("Enter")
        rec.finish()
    assert watch.leaks([b"Testpatient", PNG_SIGNATURE, b'"viewport_before"']) == []
    _assert_ciphertext_only(rec_dir)
    opened = sealed_set.SealedSet.open(rec_dir, KEY)
    assert opened.files() == [
        "events.jsonl",
        "frames/0000_after.png",
        "frames/0000_before.png",
        "frames/0001_after.png",
        "frames/0001_before.png",
        "frames/0002_after.png",
        "frames/0002_before.png",
        "meta.json",
    ]
    meta = json.loads(opened.read_bytes("meta.json"))
    assert meta["params"] == {"patient": "Testpatient Zed"}
    events = [json.loads(x) for x in opened.read_bytes("events.jsonl").splitlines()]
    assert [e["kind"] for e in events] == ["click", "type", "key"]


def test_unfinished_encrypted_recording_is_refused(tmp_path: Path) -> None:
    rec_dir = tmp_path / "rec"
    rec = Recorder(_FakeBackend(), rec_dir, encryption_key=KEY)
    rec.click(10, 20)
    rec.abandon_encryption()  # the recorder stopped before finish()
    with pytest.raises(sealed_set.SealedSetError, match="never finalized"):
        sealed_set.SealedSet.open(rec_dir, KEY)


def test_plaintext_written_beside_an_encrypted_recording_is_refused(
    tmp_path: Path,
) -> None:
    rec_dir = tmp_path / "rec"
    rec = Recorder(
        _FakeBackend(), rec_dir, encryption_key=KEY, finalize_on_finish=False
    )
    rec.click(10, 20)
    rec.finish()
    (rec_dir / "frames" / "0000_before.png").write_bytes(_png())
    with pytest.raises(sealed_set.SealedSetError, match="outside the sealed writer"):
        rec.finalize_encryption()


# -- record, compile and run, end to end --------------------------------------


@pytest.fixture
def fault_app() -> Iterator[str]:
    from openadapt_flow.mockmed.fault_server import serve

    base_url, _db, stop = serve()
    try:
        yield base_url
    finally:
        stop()


E2E_MARKERS = [
    NOTE.encode(),
    PNG_SIGNATURE,
    b'"viewport_before"',  # events.jsonl
    b'"app_url"',  # meta.json
    b'"anchor"',  # workflow.json
    b'"results"',  # report.json
]


def test_record_compile_and_governed_run_write_no_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_app: str
) -> None:
    from openadapt_flow import tutorial
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.report import render_run_report

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    import tempfile

    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))

    rec_dir, bundle, run_dir = tmp_path / "rec", tmp_path / "bundle", tmp_path / "run"
    with watching_disk() as watch:
        tutorial.record_tutorial(fault_app, rec_dir, encryption_key=KEY)
        compile_recording(rec_dir, bundle, name="sealed-tutorial", mine_effects=True)
        workflow = Workflow.load(bundle)
        tutorial.certify_tutorial(workflow)
        report = tutorial.run_tutorial_workflow(
            base_url=fault_app, workflow=workflow, bundle_dir=bundle, run_dir=run_dir
        )
        render_run_report(run_dir)

    assert report.execution_outcome == "VERIFIED"
    assert watch.leaks(E2E_MARKERS) == []
    assert list(system_tmp.iterdir()) == []
    for root in (rec_dir, bundle, run_dir):
        _assert_ciphertext_only(root)
        sealed_set.SealedSet.open(root, KEY).verify_all()
    stored = json.loads(sealed_set.read_text(run_dir / "report.json"))
    assert stored["execution_outcome"] == "VERIFIED"


def test_encrypted_recording_and_bundle_refuse_without_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rec_dir = tmp_path / "rec"
    rec = Recorder(_FakeBackend(), rec_dir, encryption_key=KEY)
    rec.click(10, 20)
    rec.finish()
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    monkeypatch.delenv(sealed_set.RECORDING_ENV_KEY, raising=False)

    code = main(["compile", str(rec_dir), "--out", str(tmp_path / "b"), "--name", "x"])
    assert code == 2
    err = capsys.readouterr().err
    assert sealed_set.RECORDING_ENV_KEY in err and "Nothing was read" in err
    assert not (tmp_path / "b").exists()

    monkeypatch.setenv(sealed_set.RECORDING_ENV_KEY, "wrong key")
    with pytest.raises(crypto.DecryptionError):
        sealed_set.SealedSet.open(rec_dir)


# -- the commands that read sealed input --------------------------------------


def _sealed_bundle_of(workflow: Workflow, bundle: Path) -> Path:
    with sealed_set.writing(bundle, "bundle", KEY):
        workflow.save(bundle)
    return bundle


def test_halted_sealed_run_is_undone_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.after_stop import (
        approve_undo,
        offer_undos,
        read_undo_result,
        run_undo,
    )
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.runtime.effects import RestRecordVerifier
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_after_stop_mockmed import (
        MockMedBackend,
        _undo_replayer,
        _vision,
        _workflow,
    )

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    url, _db, stop = serve()
    url = url.rstrip("/")
    try:
        bundle = _sealed_bundle_of(_workflow(), tmp_path / "bundle")
        workflow = Workflow.load(bundle)
        run_dir = tmp_path / "run"
        with watching_disk() as watch:
            report = Replayer(
                MockMedBackend(url),
                vision=_vision(),
                effect_verifier=RestRecordVerifier(url),
                poll_interval_s=0.01,
            ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
            assert report.execution_outcome == "HALTED"
            assert [o.undo_id for o in offer_undos(run_dir, workflow)] == [
                "undo-triage"
            ]
            approve_undo(run_dir, workflow, "undo-triage", approver="nurse-a")
            replayer, backend = _undo_replayer(url)
            result = run_undo(
                run_dir, workflow, "undo-triage", replayer=replayer, bundle_dir=bundle
            )
        assert result.status == "verified", result.reason
        assert backend.deletes == 1
        assert read_undo_result(run_dir, "undo-triage") == result
    finally:
        stop()
    assert watch.leaks([b"nurse-a", b"Triage", b'"results"', PNG_SIGNATURE]) == []
    _assert_ciphertext_only(run_dir)
    undo_run = run_dir / "undo" / "undo-triage" / "run"
    assert sealed_set.is_sealed(undo_run)
    _assert_ciphertext_only(undo_run)
    sealed_set.SealedSet.open(run_dir, KEY).verify_all()


def _recording(rec_dir: Path, *, key: str | None) -> Path:
    rec = Recorder(_FakeBackend(), rec_dir, app_url="http://app.test/", encryption_key=key)
    rec.click(40, 40)
    rec.type_text("Testpatient Zed", param="patient")
    rec.click(80, 80)
    return rec.finish()


def test_compile_and_induce_from_encrypted_recordings_stay_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    first = _recording(tmp_path / "rec1", key=KEY)
    second = _recording(tmp_path / "rec2", key=KEY)
    with watching_disk() as watch:
        assert (
            main(["compile", str(first), "--out", str(tmp_path / "b"), "--name", "x"])
            == 0
        )
        induced = main(
            ["induce", str(first), str(second), "--out", str(tmp_path / "induced")]
        )
    assert watch.leaks([b"Testpatient", PNG_SIGNATURE, b'"anchor"']) == []
    _assert_ciphertext_only(tmp_path / "b")
    assert Workflow.load(tmp_path / "b").encrypted
    assert induced == 0
    _assert_ciphertext_only(tmp_path / "induced")
    assert Workflow.load(tmp_path / "induced").encrypted


def test_scaffold_verifier_keeps_its_draft_inside_the_sealed_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    rec = tmp_path / "rec"
    with sealed_set.writing(rec, "recording", KEY) as writer:
        writer.write("meta.json", json.dumps({"params": {"p": "Testpatient Zed"}}).encode())
        writer.write(
            "events.jsonl",
            json.dumps(
                {"i": 0, "kind": "click", "x": 5, "y": 5, "field_label": "Save encounter"}
            ).encode()
            + b"\n",
        )
    with watching_disk() as watch:
        assert main(["scaffold-verifier", str(rec)]) == 0
        with pytest.raises(SystemExit, match="Drop --out"):
            main(["scaffold-verifier", str(rec), "--out", str(tmp_path / "o")])
    assert watch.leaks([b"Testpatient", PNG_SIGNATURE]) == []
    assert not (tmp_path / "o").exists()
    _assert_ciphertext_only(rec)
    opened = sealed_set.SealedSet.open(rec, KEY)
    assert "effect_contract.yaml" in opened.files()
    opened.verify_all()


def test_qualify_and_admit_keep_a_sealed_bundle_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rec = _recording(tmp_path / "rec", key=KEY)
    target = tmp_path / "admission"
    with watching_disk() as watch:
        code = main(
            [
                "admit",
                "run",
                str(rec),
                "--out",
                str(target),
                "--name",
                "sealed-admission",
            ]
        )
    assert watch.leaks([b"Testpatient", PNG_SIGNATURE, b'"anchor"']) == [], code
    # The synthetic demo has no identity or effect evidence, so the admission
    # stops for a person; what matters here is that it stays sealed.
    bundle = target / "bundle"
    assert sealed_set.is_sealed(bundle)
    _assert_ciphertext_only(bundle)
    assert "qualification-proposal.draft.json" in sealed_set.open_set(bundle).files()
    assert not (target / "qualification-proposal.draft.json").exists()
    for compiled in (target / "compiled").iterdir():
        _assert_ciphertext_only(compiled)
    with watching_disk() as watch:
        main(["qualify", "propose", str(bundle), "--recording", str(rec)])
    assert watch.leaks([b"Testpatient", PNG_SIGNATURE]) == []
    _assert_ciphertext_only(bundle)


def test_sealed_run_pauses_and_resumes_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.runtime.durable import (
        CheckpointStore,
        bundle_version,
        issue_resume_approval,
        resume,
    )
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_durable_runtime import (
        FakeSoRVerifier,
        _three_step_workflow,
        _vision_ok,
    )
    from tests.test_replayer import FakeBackend

    monkeypatch.setenv(crypto.ENV_KEY, KEY)
    bundle = _sealed_bundle_of(
        _three_step_workflow(with_effects=True), tmp_path / "bundle"
    )
    run_dir = tmp_path / "run"
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", "s2"),))
    with watching_disk() as watch:
        report = Replayer(
            FakeBackend(),
            vision=_vision_ok(),
            effect_verifier=verifier,
            durable=True,
            poll_interval_s=0.01,
        ).run(
            Workflow.load(bundle),
            params={"who": "Testpatient Zed"},
            bundle_dir=bundle,
            run_dir=run_dir,
        )
        assert report.success is False
        store = CheckpointStore(run_dir, key=KEY)
        manifest, pending = store.read_manifest(), store.read_pending()
        assert manifest is not None and pending is not None
        approval = issue_resume_approval(
            pending,
            approver="operator@example.com",
            resolution="verified the system of record; approve resume",
            bundle_version=bundle_version(bundle),
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            run_dir=run_dir,
        )
        verifier.refute.clear()
        resumed_backend = FakeBackend()
        resumed = resume(
            run_dir,
            Replayer(
                resumed_backend,
                vision=_vision_ok(),
                effect_verifier=verifier,
                poll_interval_s=0.01,
            ),
            approval=approval,
            key=KEY,
        )
    assert resumed.success is True
    assert ("press", "A") not in resumed_backend.actions
    assert ("press", "C") in resumed_backend.actions
    assert watch.leaks([b"Testpatient", b"operator@example.com", b'"results"']) == []
    _assert_ciphertext_only(run_dir)
    sealed_set.SealedSet.open(run_dir, KEY).verify_all()
