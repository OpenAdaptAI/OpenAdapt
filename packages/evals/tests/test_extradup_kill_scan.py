"""ExtraDup kill-scan: frozen gold-FAIL corpus, SIS vs honest-write, no seals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_evals.extradup.kill_scan import (
    EXECUTE_SEAL,
    PRODUCTION_SEAL,
    TRAINING_MIX,
    corpus_digest,
    frozen_rollouts,
)
from openadapt_evals.extradup.mutations import MUTANTS, OPERATORS
from openadapt_evals.extradup.suite import main


def _verdicts(
    paid_mutants: bool, paid_control: bool, envs: tuple[str, ...] = ("mockmed",)
) -> dict:
    paid = {}
    for env in envs:
        for operator in OPERATORS:
            paid[f"{env}:{operator}"] = (
                paid_control if operator == "control" else paid_mutants
            )
    return {"name": "acme-agent", "paid": paid}


def test_corpus_digest_is_frozen() -> None:
    digest = corpus_digest()
    assert digest == (
        "sha256:d8043c67d37ee69f05600c29a96afd488f14764066899b8e3f90229861558994"
    )
    assert EXECUTE_SEAL is False
    assert PRODUCTION_SEAL is False
    assert TRAINING_MIX is False


def test_frozen_rollouts_use_public_gold_fail() -> None:
    rollouts = frozen_rollouts()
    ids = {item["id"] for item in rollouts}
    assert ids == {
        f"{env}:{operator}" for env in ("mockmed", "openemr") for operator in OPERATORS
    }
    by_id = {item["id"]: item for item in rollouts}
    assert by_id["mockmed:control"]["gold"] == "PASS"
    for operator in MUTANTS:
        assert by_id[f"mockmed:{operator}"]["gold"] == "FAIL"
        assert by_id[f"openemr:{operator}"]["gold"] == "FAIL"


def test_dump_corpus_keeps_seals_off(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    assert main(["kill-scan", "--dump-corpus", str(path)]) == 0
    payload = json.loads(path.read_text())
    assert payload["execute_seal"] is False
    assert payload["production_seal"] is False
    assert payload["training_mix"] is False
    assert payload["corpus_digest"] == corpus_digest()
    assert len(payload["cells"]) == 12


def test_verdicts_refusing_mutants_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps(_verdicts(paid_mutants=False, paid_control=True)))
    summary = tmp_path / "summary.json"
    assert main(["kill-scan", "--verdicts", str(path), "--json", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "execute_seal: false" in out
    assert "production_seal: false" in out
    assert "training_mix: false" in out
    assert "gold-FAIL silent-incorrect-success: 0/5" in out
    assert "control honest-write: 1/1" in out
    payload = json.loads(summary.read_text())
    assert payload["execute_seal"] is False
    assert payload["production_seal"] is False
    assert payload["training_mix"] is False
    target = payload["targets"][0]
    assert target["silent_incorrect_success_rate"] == 0.0
    assert target["honest_write_rate"] == 1.0


def test_verdicts_paying_mutants_is_silent_incorrect_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps(_verdicts(paid_mutants=True, paid_control=True)))
    assert main(["kill-scan", "--verdicts", str(path)]) == 1
    out = capsys.readouterr().out
    assert "gold-FAIL silent-incorrect-success: 5/5" in out
    assert "control honest-write: 1/1" in out
    assert "paid a gold-FAIL mutant" in out


def test_builtin_kill_scan_matches_committed_proof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.json"
    assert main(["kill-scan", "--json", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "execute_seal: false" in out
    assert "production_seal: false" in out
    assert "training_mix: false" in out
    assert "visual_only:" in out
    assert "gold-FAIL silent-incorrect-success: 15/15" in out
    assert "certified_sor:" in out
    assert "gold-FAIL silent-incorrect-success: 0/15" in out
    assert "control honest-write: 3/3" in out
    payload = json.loads(summary.read_text())
    by_name = {row["name"]: row for row in payload["targets"]}
    assert by_name["visual_only"]["gold_fail_trials"] == 15
    assert by_name["visual_only"]["gold_fail_paid"] == 15
    assert by_name["certified_sor"]["gold_fail_trials"] == 15
    assert by_name["certified_sor"]["gold_fail_paid"] == 0
    assert by_name["certified_sor"]["control_paid"] == 3
    assert "identity_swap" not in out
    assert payload["execute_seal"] is False
    assert payload["production_seal"] is False
    assert payload["training_mix"] is False
