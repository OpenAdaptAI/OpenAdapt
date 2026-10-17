"""The vocabulary registry and the stdlib lint that other repositories run."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openadapt_types.vocabulary import REGISTRY_DIR, load_registry
from openadapt_types.vocabulary import lint
from openadapt_types.vocabulary.lint import (
    RegistryError,
    check_claims,
    check_coverage,
    check_terms,
    main,
    validate_outcome_table,
    validate_registry_pair,
)

ROOT = Path(__file__).resolve().parents[1]
VOCABULARY, TABLE = load_registry()


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _rules(findings: list[lint.Finding]) -> list[tuple[int, str, str]]:
    return [(f.line, f.rule, f.severity) for f in findings]


# ------------------------------------------------------- the registry itself

# The business vocabulary from the 2026-10-08 brief, term by term.
BRIEF_TERMS = {
    "workflow": "workflow",
    "recording": "showing the task",
    "automation": "the automation",
    "run": "run",
    "done_and_checked": "Done and checked",
    "stopped_before_saving": "Stopped before saving",
    "check_the_record": "Check the record",
    "finished_not_checked": "Finished, not checked",
    "did_not_finish": "Didn't finish",
    "receipt": "receipt",
    "readiness_test": "readiness test",
    "approved_fix": "approved fix",
    "screen_changes": "screen changes",
    "right_record_check": "right-record check",
    "record_check": "record check",
    "person_steps_in": "a person steps in",
    "computer_that_runs_it": "the computer that runs it",
}


def test_every_business_term_in_the_brief_is_registered() -> None:
    terms = {term["id"]: term for term in VOCABULARY["terms"]}
    for term_id, business in BRIEF_TERMS.items():
        assert terms[term_id]["business_term"] == business


# The brief's list of words that must never reach a business-facing first
# screen. Each one must be an error at the business level.
NEVER_ON_FIRST_SCREENS = [
    "Your Seal is ready.",
    "Admission passed.",
    "We admit the workflow.",
    "Choose a qualification profile.",
    "It runs under the Standard profile.",
    "A governed run.",
    "The oracle confirmed it.",
    "The effect contract held.",
    "Pick a substrate.",
    "Load the fixture.",
    "We check REST+SQL parity.",
    "No non-target delta.",
    "Its sha256 matches.",
    "Status: RECONCILIATION_REQUIRED",
    "Status: VERIFIED",
    "Status: HALTED",
    "Open the Program State Console.",
    "Connect over MCP.",
    "Pick a runner.",
]


def test_every_word_the_brief_bans_from_first_screens_is_an_error(tmp_path: Path) -> None:
    page = _write(tmp_path, "page.md", "\n".join(NEVER_ON_FIRST_SCREENS) + "\n")
    findings = check_terms([page], "business", VOCABULARY)
    errors = {f.line for f in findings if f.severity == "error"}
    missing = [
        line for number, line in enumerate(NEVER_ON_FIRST_SCREENS, 1) if number not in errors
    ]
    assert missing == []


def test_plain_business_copy_passes(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        "\n".join(
            [
                "# Hand off the last step",
                "",
                "Show the task one or more times. OpenAdapt builds the automation and runs it.",
                "Each run ends Done and checked, Stopped before saving, or Check the record.",
                "A record check reads the saved entry back. A right-record check comes first.",
                "Before it goes live, a readiness test uses your cases.",
                "When the screen changes, a person approves the fix.",
                "It runs on the computer that runs it: one you control, or one we host.",
                "The receipt shows what was entered and how it was verified.",
            ]
        ),
    )
    assert check_terms([page], "business", VOCABULARY) == []


def _registry_sentences() -> list[str]:
    sentences: list[str] = []
    for result in TABLE["results"].values():
        sentences += [result["label"], result["meaning"]]
    for proof in TABLE["proof"].values():
        sentences += [proof["label"], proof["meaning"]]
    for _, row in lint._rows(TABLE):
        sentences.append(row["next_action"])
    # Other sources' in-progress values point at these Execute states.
    for state in TABLE["in_progress"]["execute_state"].values():
        sentences += [state["label"], state["next_action"]]
    for term in VOCABULARY["terms"]:
        sentences.append(term["meaning"])
        if term.get("business_term"):
            sentences.append(term["business_term"])
    return sentences


def test_the_registry_plain_text_passes_its_own_business_check(tmp_path: Path) -> None:
    # Meanings of internal-only terms describe the internal word, so skip
    # those; every result, proof, and next step is shown to business readers.
    internal = {
        term["meaning"] for term in VOCABULARY["terms"] if term.get("business_term") is None
    }
    lines = [s for s in _registry_sentences() if s not in internal]
    page = _write(tmp_path, "registry.md", "\n".join(dict.fromkeys(lines)) + "\n")
    assert check_terms([page], "business", VOCABULARY) == []


PUFFERY = re.compile(
    r"\b(seamless|robust|pivotal|unlock|empower|elevate|cutting-edge|transformative|"
    r"game-changer|landscape|notably|ultimately|in summary|it's important to note)\b",
    re.IGNORECASE,
)


def test_the_registry_plain_text_follows_the_writing_rules() -> None:
    for sentence in _registry_sentences():
        assert "\u2014" not in sentence and "\u2013" not in sentence, sentence
        assert not PUFFERY.search(sentence), sentence
        assert "!" not in sentence, sentence


def _broken(mutate: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    vocabulary, table = copy.deepcopy(VOCABULARY), copy.deepcopy(TABLE)
    mutate(vocabulary, table)
    return vocabulary, table


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda v, t: t["sources"]["execution_outcome"]["values"]["HALTED"].update(
                result="stopped_before_change", may_have_changed=False, safe_to_retry=True
            ),
            "doesn't prove no effect",
        ),
        (
            lambda v, t: t["sources"]["transaction_outcome"]["values"]["RECONCILIATION_REQUIRED"].update(
                safe_to_retry=True
            ),
            "safe_to_retry needs may_have_changed=false",
        ),
        (
            lambda v, t: t["sources"]["transaction_outcome"]["values"]["HALTED_BEFORE_EFFECT"].update(
                may_have_changed=True, safe_to_retry=False
            ),
            "claims nothing changed",
        ),
        (
            lambda v, t: t["fallbacks"]["unrecognized"].update(
                result="did_not_finish", may_have_changed=False, safe_to_retry=True
            ),
            "fail closed",
        ),
        (
            lambda v, t: t["sources"]["transaction_outcome"]["values"]["VERIFIED"].update(
                execution_outcomes=["SOMETHING"]
            ),
            "execution_outcomes is invalid",
        ),
        (lambda v, t: t["tiers"].reverse(), "precedence order"),
        (lambda v, t: t["emitters"].append({"source": "nope"}), "unknown source"),
    ],
)
def test_the_outcome_table_refuses_unsafe_rows(mutate: Any, message: str) -> None:
    _, table = _broken(mutate)
    with pytest.raises(RegistryError, match=re.escape(message)):
        validate_outcome_table(table)


def test_the_pair_check_refuses_disagreeing_labels() -> None:
    vocabulary, table = _broken(
        lambda v, t: t["results"]["check_the_record"].update(label="Needs review")
    )
    with pytest.raises(RegistryError, match="check_the_record"):
        validate_registry_pair(vocabulary, table)


def test_the_pair_check_refuses_a_no_change_claim_on_a_row_that_may_have_changed() -> None:
    vocabulary, table = _broken(
        lambda v, t: t["sources"]["agent_status"]["values"]["halt"].update(
            next_action="It stopped safely. Tell the user the record did not change."
        )
    )
    with pytest.raises(RegistryError, match="sources.agent_status.halt"):
        validate_registry_pair(vocabulary, table)


def _move_halted_under_stopped_before_saving(vocabulary: dict[str, Any], _: dict[str, Any]) -> None:
    terms = {term["id"]: term for term in vocabulary["terms"]}
    halted = terms["coarse_halt"]["technical"].pop()
    terms["stopped_before_saving"]["technical"].append(halted)


def test_the_pair_check_refuses_an_outcome_term_under_a_result_it_doesnt_prove() -> None:
    # A bare HALTED reads "Check the record". Filing it under "Stopped before
    # saving" would make the lint suggest a no-change claim for it.
    vocabulary, table = _broken(_move_halted_under_stopped_before_saving)
    with pytest.raises(RegistryError, match="HALTED on its own reads as"):
        validate_registry_pair(vocabulary, table)


def test_the_lint_never_suggests_a_no_change_label_for_a_bare_halt(tmp_path: Path) -> None:
    page = _write(tmp_path, "status.md", "Status: HALTED\n")
    (finding,) = check_terms([page], "business", VOCABULARY)
    assert "Say: Check the record." in finding.message
    assert "Say: Stopped before saving" not in finding.message


# ------------------------------------------------------------------- terms


def test_detail_level_allows_detail_terms_but_not_engineering_terms(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "detail.md",
        "The runner sends VERIFIED over MCP.\nThe oracle checks the effect contract.\n",
    )
    findings = check_terms([page], "detail", VOCABULARY)
    assert {f.line for f in findings} == {2}
    assert {f.rule for f in findings} == {"term:record_check"}


def test_overlapping_terms_report_once(tmp_path: Path) -> None:
    page = _write(tmp_path, "page.md", "Each fix goes through governed repair.\n")
    findings = check_terms([page], "business", VOCABULARY)
    assert _rules(findings) == [(1, "term:approved_fix", "error")]


def test_advanced_sections_and_reasoned_pragmas_are_skipped(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "page.md",
        "\n".join(
            [
                "Plain words up here.",
                "<!-- vocab: advanced -->",
                "Technical details: transaction_outcome=RECONCILIATION_REQUIRED on the runner.",
                "<!-- vocab: end -->",
                '<details data-level="advanced"><summary>Technical details</summary>',
                "The Seal records the oracle tier.",
                "</details>",
                "<!-- vocab-allow: qualification -- the name of the paid service -->",
                "Workflow qualification is a paid service.",
                "<!-- vocab-allow: runner -->",
                "Pick a runner.",
            ]
        ),
    )
    findings = check_terms([page], "business", VOCABULARY)
    assert _rules(findings) == [
        (10, "pragma", "error"),
        (11, "term:computer_that_runs_it", "error"),
    ]


def test_script_files_check_visible_text_not_identifiers(tmp_path: Path) -> None:
    component = _write(
        tmp_path,
        "Card.tsx",
        "\n".join(
            [
                "import { runner } from './mcp';",
                "export function Card() {",
                '  const cls = "runner-card";',
                '  return <div className="runner-card" title="Seal">',
                "    <p>Your run is on the runner.</p>",
                "  </div>;",
                "}",
            ]
        ),
    )
    findings = check_terms([component], "business", VOCABULARY)
    assert sorted(_rules(findings)) == [
        (4, "term:receipt", "error"),
        (5, "term:computer_that_runs_it", "error"),
    ]


def test_python_files_check_strings_but_not_docstrings(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "cli.py",
        '"""The runner module."""\n\ndef help():\n    """Docs for the runner."""\n'
        '    return "Start the runner now."\n',
    )
    findings = check_terms([module], "business", VOCABULARY)
    assert _rules(findings) == [(5, "term:computer_that_runs_it", "error")]


def test_json_copy_files_report_the_right_line(tmp_path: Path) -> None:
    copy_file = _write(
        tmp_path,
        "copy.json",
        json.dumps({"hero": "Hand off the last step.", "cta": "Talk to the runner team."}, indent=2),
    )
    findings = check_terms([copy_file], "business", VOCABULARY)
    assert _rules(findings) == [(3, "term:computer_that_runs_it", "error")]


# ------------------------------------------------------------------ claims

# Sentences that shipped on real surfaces and told a person nothing changed
# after a halt that didn't prove it (concept model, finding F1).
UNPROVEN_CLAIMS = [
    "HALTED. The independent check did not confirm the write. Tell the user the record did not change.",
    "Needs attention: halted before writing",
    "Halted. This run stopped safely.",
    "Outcome HALTED. The record did not change.",
    "It failed. It's safe to re-run.",
    "Timed out. Nothing was saved.",
]


@pytest.mark.parametrize("sentence", UNPROVEN_CLAIMS)
def test_a_no_change_claim_after_a_bare_halt_is_an_error(tmp_path: Path, sentence: str) -> None:
    page = _write(tmp_path, "status.md", f"Intro.\n\n\n\n{sentence}\n")
    findings = check_claims([page], VOCABULARY)
    assert _rules(findings) == [(5, "no-change-claim", "error")]


def test_a_claim_inside_joined_python_literals_is_found_on_its_own_line(tmp_path: Path) -> None:
    # Adjacent literals become one string that starts far from the claim.
    module = _write(
        tmp_path,
        "server.py",
        "\n".join(
            [
                "INSTRUCTIONS = (",
                '    "Local bridge exposing workflows as tools. "',
                '    "Each tool returns a structured outcome. "',
                '    "Only status success means the workflow completed. "',
                '    "Read the outcome before you answer. "',
                '    "Never report a refused run as a success. "',
                '    "If execution_outcome is HALTED, tell the user the record did not change. "',
                '    "Keep evidence local."',
                ")",
            ]
        ),
    )
    findings = check_claims([module], VOCABULARY)
    assert _rules(findings) == [(7, "no-change-claim", "error")]


def test_a_no_change_claim_next_to_a_proving_outcome_passes(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "status.ts",
        "\n".join(
            [
                "if (outcome.transaction_outcome === 'HALTED_BEFORE_EFFECT') {",
                "  return 'Stopped before saving. Nothing was written.';",
                "}",
                "",
                "",
                "",
                "",
                "if (!result.may_have_changed) show('Stopped. Nothing was written.');",
            ]
        ),
    )
    assert check_claims([module], VOCABULARY) == []


def test_a_claim_far_from_any_halt_is_left_alone(tmp_path: Path) -> None:
    page = _write(tmp_path, "privacy.md", "Nothing was written to disk during setup.\n")
    assert check_claims([page], VOCABULARY) == []


def test_the_claim_rule_has_a_reasoned_escape_hatch(tmp_path: Path) -> None:
    page = _write(
        tmp_path,
        "doc.md",
        "<!-- vocab-allow: no-change-claim -- quoting the old wording to explain the bug -->\n"
        "The old screen said: Halted. The record did not change.\n",
    )
    assert check_claims([page], VOCABULARY) == []


# ---------------------------------------------------------------- coverage


def test_coverage_reports_unmapped_and_stale_values() -> None:
    findings = check_coverage(
        TABLE, values={"agent_status": ["success", "halt", "refused", "timeout", "partial"]}
    )
    assert [(f.severity, "partial" in f.message, "'error'" in f.message) for f in findings] == [
        ("error", True, False),
        ("warn", False, True),
    ]


def test_partial_coverage_skips_stale_warnings() -> None:
    findings = check_coverage(TABLE, values={"execution_outcome": ["VERIFIED", "HALTED"]}, partial=True)
    assert findings == []
    assert main(["coverage", "--partial", "--strict", "--values", "execution_outcome=VERIFIED"]) == 0
    assert main(["coverage", "--strict", "--values", "execution_outcome=VERIFIED"]) == 1


def test_coverage_reads_literals_and_pydantic_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(
        tmp_path,
        "fake_engine.py",
        "\n".join(
            [
                "from typing import Literal, Optional",
                "from pydantic import BaseModel",
                'Coarse = Literal["VERIFIED", "COMPLETED_UNVERIFIED", "HALTED", "FAILED", "ROLLED_BACK"]',
                "class Report(BaseModel):",
                '    transaction_outcome: Optional[Literal["VERIFIED", "PARTIALLY_SAVED"]] = None',
            ]
        ),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert check_coverage(TABLE, enums=["fake_engine:Coarse=execution_outcome"]) == []
    findings = check_coverage(
        TABLE, enums=["fake_engine:Report.transaction_outcome=transaction_outcome"]
    )
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 1 and "PARTIALLY_SAVED" in errors[0].message


# --------------------------------------------------------------------- CLI


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clean = _write(tmp_path, "clean.md", "Each run ends Done and checked.\n")
    dirty = _write(tmp_path, "dirty.md", "Pick a runner.\n")
    warn = _write(tmp_path, "warn.md", "Watch the replay.\n")
    assert main(["terms", "--level", "business", str(clean)]) == 0
    assert main(["terms", "--level", "business", str(dirty)]) == 1
    assert main(["terms", "--level", "business", str(warn)]) == 0
    assert main(["terms", "--level", "business", "--strict", str(warn)]) == 1
    assert main(["coverage", "--values", "nope=x"]) == 2
    assert main(["coverage", "--enum", "not a spec"]) == 2
    capsys.readouterr()
    assert main(["terms", "--level", "business", "--format", "json", str(dirty)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["rule"] == "term:computer_that_runs_it"


def test_check_runs_a_vocab_config(tmp_path: Path) -> None:
    _write(tmp_path, "home.md", "Pick a runner.\n")
    (tmp_path / "docs").mkdir()
    _write(tmp_path / "docs", "guide.md", "The runner has a substrate.\n")
    _write(tmp_path, "status.md", "Halted. Nothing was written.\n")
    config = _write(
        tmp_path,
        ".vocab.json",
        json.dumps(
            {
                "levels": {"business": ["home.md"], "detail": ["docs/*.md"]},
                "claims": ["status.md"],
                "coverage": {"values": {"agent_status": ["success", "halt", "refused", "timeout", "error"]}},
            }
        ),
    )
    vocabulary, table = load_registry()
    findings = lint.check_config(config, vocabulary, table)
    assert sorted((Path(f.path).name, f.rule) for f in findings) == [
        ("guide.md", "term:substrate"),
        ("home.md", "term:computer_that_runs_it"),
        ("status.md", "no-change-claim"),
    ]
    bad = _write(tmp_path, "bad.json", json.dumps({"levels": {"engineering": ["home.md"]}}))
    assert main(["check", "--config", str(bad)]) == 2
    missing = _write(tmp_path, "missing.json", json.dumps({"claims": ["nowhere/*.md"]}))
    assert main(["check", "--config", str(missing)]) == 2


def test_a_copied_vocabulary_directory_runs_without_this_package(tmp_path: Path) -> None:
    copied = tmp_path / "vendor" / "vocabulary"
    shutil.copytree(REGISTRY_DIR, copied, ignore=shutil.ignore_patterns("__pycache__"))
    page = _write(tmp_path, "page.md", "Pick a runner.\n")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(copied / "lint.py"), "terms", "--level", "business", str(page)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "term:computer_that_runs_it" in result.stdout


def test_a_vendored_copy_passes_the_claims_check_it_ships() -> None:
    # Repositories copy this directory into their tree and then run
    # `claims src`. The copy itself must not fail that run.
    assert check_claims([REGISTRY_DIR], VOCABULARY) == []


def test_text_that_only_mentions_the_pragma_is_not_a_pragma(tmp_path: Path) -> None:
    page = _write(tmp_path, "notes.py", 'PATTERN = r"vocab-allow:\\s*(?P<names>.*)"\n')
    assert check_claims([page], VOCABULARY) == []


def test_the_module_runs_with_python_dash_m(tmp_path: Path) -> None:
    page = _write(tmp_path, "page.md", "Each run ends Done and checked.\n")
    result = subprocess.run(
        [sys.executable, "-m", "openadapt_types.vocabulary", "terms", "--level", "business", str(page)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_the_console_script_points_at_main() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'openadapt-vocab-lint = "openadapt_types.vocabulary.lint:main"' in pyproject


def test_the_registry_ships_inside_the_package() -> None:
    for name in ("vocabulary.json", "outcomes.json", "outcome-vectors.json", "lint.py"):
        assert (REGISTRY_DIR / name).is_file()
    assert REGISTRY_DIR == (ROOT.parent / "flow" / "openadapt_flow" / "contracts") / "vocabulary"


def test_the_vocabulary_doc_matches_the_registry() -> None:
    doc = (ROOT / "docs" / "VOCABULARY.md").read_text(encoding="utf-8")
    for term in VOCABULARY["terms"]:
        if term.get("business_term"):
            assert term["business_term"] in doc, term["id"]
        for variant in term.get("technical", []):
            assert variant["term"] in doc, variant["term"]
    for result, spec in TABLE["results"].items():
        assert f"| `{result}` | {spec['label']} |" in doc
    cases = len(json.loads((REGISTRY_DIR / "outcome-vectors.json").read_text())["cases"])
    assert f"{cases} hand-written cases" in doc
    words = {11: "eleven"}
    assert f"{words[len(lint.NO_EFFECT_PROOFS)]} rows allowed to say so" in doc
