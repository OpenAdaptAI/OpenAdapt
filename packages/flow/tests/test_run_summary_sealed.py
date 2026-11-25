"""The run summary inside an encrypted run directory joins the sealed set.

``write_receipt`` writes ``run-summary.{json,md,png}`` beside the run after
it finished. Under the encryption-at-rest default the run directory is a
sealed set, and a file written there outside the set makes every later read
of the run refuse ("holds run-summary.json in plaintext, outside its sealed
index"): ``openadapt quickstart`` then failed to seal its first run. The
summary is the allow-listed receipt (outcomes, counts and digests, no record
text), so it stays plaintext on disk, like the receipt files beside it, but
it is now written through the set and named in its index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import at_rest, crypto, sealed_set
from openadapt_flow.receipt import build_receipt, read_run_summary, write_receipt
from tests.test_run_receipt import _report


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


def test_the_summary_in_a_sealed_run_is_indexed_and_the_run_still_opens(
    sealed_run: Path,
) -> None:
    receipt = build_receipt(_report())
    paths = write_receipt(receipt, sealed_run)

    # The exact read seal_run makes first: it refused before the fix.
    opened = sealed_set.open_set(sealed_run)
    opened.verify_all()
    for name in ("run-summary.json", "run-summary.md", "run-summary.png"):
        assert opened.is_file(name), name
    # Nothing on disk is outside the index.
    indexed = {
        sealed_set._physical(rel, entry["codec"])
        for rel, entry in opened._files.items()
    }
    assert set(sealed_set._disk_files(sealed_run)) <= indexed

    # Allow-listed plaintext: readable without the key, at the printed paths.
    assert paths["png"].read_bytes().startswith(b"\x89PNG")
    summary = read_run_summary(sealed_run)
    assert summary is not None
    assert summary["receipt_digest"] == receipt.receipt_digest
    assert sealed_set.read_bytes(paths["json"]) == paths["json"].read_bytes()


def test_a_later_write_to_the_run_still_works(sealed_run: Path) -> None:
    write_receipt(build_receipt(_report()), sealed_run)
    sealed_set.write_text(sealed_run / "report.json", '{"ok": true}')
    assert sealed_set.read_text(sealed_run / "report.json") == '{"ok": true}'


def test_any_other_plaintext_file_still_makes_the_run_refuse(
    sealed_run: Path,
) -> None:
    """Fail-closed is unchanged: only the named summary files are allow-listed."""
    (sealed_run / "run-summary.txt").write_text("not on the allow-list")
    with pytest.raises(sealed_set.SealedSetError, match="in plaintext"):
        sealed_set.open_set(sealed_run)
