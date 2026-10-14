"""ExtraDup kill-scan: frozen mutants, public gold-FAIL, one command.

Default target is the two MockMed rewards from ``openadapt_evals.reward.proof``
(visual_only vs certified_sor). ``--verdicts`` scores someone else's checker
on the same frozen cells. Mutants stay off the training reward.
``execute_seal`` and ``production_seal`` are always false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_evals.extradup.gold import GOLD_SPECS
from openadapt_evals.extradup.mutations import MUTANTS, OPERATORS, apply
from openadapt_evals.extradup.store import store_for
from openadapt_evals.extradup.suite import _gold_for, cell_id, cells

SUITE = "ExtraDup"
GOLD_RULE = "FAIL unless operator is control"
EXECUTE_SEAL = False
PRODUCTION_SEAL = False
TRAINING_MIX = False
BUILTIN_TARGETS = ("visual_only", "certified_sor")


def corpus_payload() -> dict[str, Any]:
    """Public gold definition plus the frozen operator list."""

    return {
        "suite": SUITE,
        "gold_rule": GOLD_RULE,
        "operators": list(OPERATORS),
        "mutants": list(MUTANTS),
        "specs": [
            {
                "env": spec.env,
                "collection": spec.collection,
                "fields": dict(spec.fields),
                "expected_new": spec.expected_new,
                "allowed_fields": sorted(spec.allowed_fields),
                "extra_field": spec.extra_field,
                "extra_value": spec.extra_value,
                "omit_field": spec.omit_field,
            }
            for spec in GOLD_SPECS
        ],
    }


def corpus_digest() -> str:
    blob = json.dumps(corpus_payload(), sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def frozen_rollouts() -> list[dict[str, Any]]:
    """One deterministic rollout per frozen cell. No banner-wording trials."""

    rollouts: list[dict[str, Any]] = []
    for cell, (spec, operator) in cells().items():
        store = store_for(spec.env)
        before, after, screen = apply(store, spec, operator)
        rollouts.append(
            {
                "id": cell,
                "env": spec.env,
                "operator": operator,
                "gold": _gold_for(operator),
                "before": before,
                "after": after,
                "screen": {
                    "banner_saved": screen.banner_saved,
                    "form_looks_complete": screen.form_looks_complete,
                    "claimed_success": screen.claimed_success,
                },
                "spec": {
                    "collection": spec.collection,
                    "fields": dict(spec.fields),
                    "expected_new": spec.expected_new,
                    "allowed_fields": sorted(spec.allowed_fields),
                },
            }
        )
    return rollouts


@dataclass(frozen=True)
class TargetScore:
    name: str
    fail_trials: int
    fail_paid: int
    control_trials: int
    control_paid: int

    @property
    def silent_incorrect_success_rate(self) -> float | None:
        if self.fail_trials == 0:
            return None
        return round(self.fail_paid / self.fail_trials, 4)

    @property
    def honest_write_rate(self) -> float | None:
        if self.control_trials == 0:
            return None
        return round(self.control_paid / self.control_trials, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gold_fail_trials": self.fail_trials,
            "gold_fail_paid": self.fail_paid,
            "silent_incorrect_success": (
                f"{self.fail_paid}/{self.fail_trials}" if self.fail_trials else "n/a"
            ),
            "silent_incorrect_success_rate": self.silent_incorrect_success_rate,
            "control_trials": self.control_trials,
            "control_paid": self.control_paid,
            "honest_write": (
                f"{self.control_paid}/{self.control_trials}"
                if self.control_trials
                else "n/a"
            ),
            "honest_write_rate": self.honest_write_rate,
        }

    def gates(self) -> bool:
        """A Seal-shaped target must refuse every mutant and pay the control."""

        if self.name == "visual_only":
            return True
        return (
            self.fail_trials > 0
            and self.fail_paid == 0
            and self.control_trials > 0
            and self.control_paid == self.control_trials
        )


def _score_rows(name: str, rows: Sequence[Mapping[str, Any]]) -> TargetScore:
    fail_trials = fail_paid = control_trials = control_paid = 0
    for row in rows:
        gold = row["gold"]
        paid = bool(row["paid"])
        if gold == "FAIL":
            fail_trials += 1
            fail_paid += int(paid)
        elif gold == "PASS":
            control_trials += 1
            control_paid += int(paid)
    return TargetScore(name, fail_trials, fail_paid, control_trials, control_paid)


def score_proof(target: str = "both") -> tuple[list[TargetScore], dict[str, Any]]:
    from openadapt_evals.reward import proof

    # ExtraDup operators only. identity_swap lives on the 09-02 proof, not this corpus.
    run = proof.run_proof(conditions=proof.PROOF_2026_09_01_CONDITIONS)
    wanted = BUILTIN_TARGETS if target == "both" else (target,)
    scores: list[TargetScore] = []
    for name in wanted:
        rows = []
        for item in run.rollouts:
            if item.condition not in OPERATORS:
                continue
            scored = run.scored[(item.condition, item.trial, name)]
            paid = scored.scalar is not None and scored.scalar > 0
            rows.append({"gold": item.gold, "paid": paid})
        scores.append(_score_rows(name, rows))
    return scores, proof.to_json(run)


def load_verdicts(path: Path) -> tuple[str, dict[str, bool]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"verdicts file is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit("verdicts file must be a JSON object")
    name = str(document.get("name") or "agent")
    paid_map = document.get("paid")
    if paid_map is None and "cells" in document:
        paid_map = {
            str(row["id"]): bool(row["paid"])
            for row in document["cells"]
            if isinstance(row, dict) and "id" in row
        }
    if not isinstance(paid_map, dict) or not paid_map:
        raise SystemExit("verdicts file needs a non-empty 'paid' object or 'cells' list")
    paid = {str(key): bool(value) for key, value in paid_map.items()}
    catalog = cells()
    unknown = sorted(key for key in paid if key not in catalog)
    if unknown:
        raise SystemExit(f"unknown ExtraDup cell ids: {unknown}")
    envs = {key.split(":", 1)[0] for key in paid}
    required: list[str] = []
    for spec in GOLD_SPECS:
        env_key = "mockmed" if spec.env == "mockmed" else "openemr"
        if env_key in envs:
            for operator in OPERATORS:
                required.append(cell_id(spec, operator))
    missing = [cell for cell in required if cell not in paid]
    if missing:
        raise SystemExit(
            "verdicts file is missing cells for the envs it started: "
            + ", ".join(missing)
        )
    return name, paid


def score_verdicts(name: str, paid: Mapping[str, bool]) -> TargetScore:
    catalog = cells()
    rows = [
        {"gold": _gold_for(catalog[cell][1]), "paid": paid[cell]}
        for cell in paid
    ]
    return _score_rows(name, rows)


def banner() -> list[str]:
    return [
        f"{SUITE} kill-scan",
        f"corpus_digest: {corpus_digest()}",
        f"gold: {GOLD_RULE}",
        f"execute_seal: {str(EXECUTE_SEAL).lower()}",
        f"production_seal: {str(PRODUCTION_SEAL).lower()}",
        f"training_mix: {str(TRAINING_MIX).lower()}",
    ]


def render_score(score: TargetScore) -> list[str]:
    sis = score.as_dict()["silent_incorrect_success"]
    honest = score.as_dict()["honest_write"]
    return [
        f"{score.name}:",
        f"  gold-FAIL silent-incorrect-success: {sis}",
        f"  control honest-write: {honest}",
    ]


def summary_payload(scores: Sequence[TargetScore]) -> dict[str, Any]:
    return {
        "suite": SUITE,
        "command": "kill-scan",
        "corpus_digest": corpus_digest(),
        "gold": GOLD_RULE,
        "execute_seal": EXECUTE_SEAL,
        "production_seal": PRODUCTION_SEAL,
        "training_mix": TRAINING_MIX,
        "targets": [score.as_dict() for score in scores],
    }


def dump_corpus(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "suite": SUITE,
                "corpus_digest": corpus_digest(),
                "gold": GOLD_RULE,
                "execute_seal": EXECUTE_SEAL,
                "production_seal": PRODUCTION_SEAL,
                "training_mix": TRAINING_MIX,
                "cells": frozen_rollouts(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def cmd_kill_scan(args: argparse.Namespace) -> int:
    if args.dump_corpus is not None:
        dump_corpus(args.dump_corpus)
        print(f"wrote frozen ExtraDup corpus to {args.dump_corpus}")
        if args.verdicts is None and args.json is None:
            return 0

    scores: list[TargetScore]
    extra_table: str | None = None
    if args.verdicts is not None:
        name, paid = load_verdicts(args.verdicts)
        scores = [score_verdicts(name, paid)]
    else:
        scores, proof_json = score_proof(args.target)
        table = proof_json.get("table")
        if table:
            from openadapt_evals.reward.proof import to_markdown_table

            extra_table = to_markdown_table(table)

    lines = banner()
    lines.append("")
    for score in scores:
        lines.extend(render_score(score))
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    print(text, end="")
    if extra_table and args.verdicts is None:
        print()
        print(extra_table)

    payload = summary_payload(scores)
    if args.json is not None:
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    if all(score.gates() for score in scores):
        return 0
    print("FAIL: a Seal-shaped target paid a gold-FAIL mutant or refused the control")
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m openadapt_evals.extradup kill-scan")
    # Standalone entry so tests can call kill_scan.main without the suite parser.
    parser.add_argument("--target", choices=("both", *BUILTIN_TARGETS), default="both")
    parser.add_argument("--verdicts", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--dump-corpus", type=Path)
    args = parser.parse_args(argv)
    return cmd_kill_scan(args)
