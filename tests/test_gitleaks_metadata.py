"""Synthetic controls for Gitleaks' metadata-only report projection."""

from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/secret-scan.yml"
PARSER = Path(__file__).resolve().parents[1] / "scripts/ci/gitleaks_metadata.py"
PARSER_SPEC = importlib.util.spec_from_file_location("gitleaks_metadata", PARSER)
if PARSER_SPEC is None or PARSER_SPEC.loader is None:
    raise RuntimeError("could not load the Gitleaks metadata parser")
PARSER_MODULE = importlib.util.module_from_spec(PARSER_SPEC)
PARSER_SPEC.loader.exec_module(PARSER_MODULE)

InvalidReport = PARSER_MODULE.InvalidReport
project_report = PARSER_MODULE.project_report
report_scan_result = PARSER_MODULE.report_scan_result


def _encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _record(
    *,
    rule_id: str = "synthetic-api-key",
    file_path: str = "tests/fixtures/history.py",
    line: int = 17,
    commit: str = "a" * 40,
) -> str:
    return f"{_encode(rule_id)}\t{_encode(file_path)}\t{line}\t{_encode(commit)}\n"


def test_projection_contains_only_the_allowlisted_metadata() -> None:
    result = project_report(_record())

    assert result == [
        {
            "RuleID": "synthetic-api-key",
            "File": "tests/fixtures/history.py",
            "StartLine": 17,
            "Commit": "a" * 40,
        }
    ]


def test_report_output_never_emits_non_allowlisted_fields(tmp_path: Path) -> None:
    report_path = tmp_path / "synthetic-report.tsv"
    report_path.write_text(_record(), encoding="utf-8")
    output = io.StringIO()

    result = report_scan_result(report_path, 1, output)

    assert result == 1
    assert "synthetic-api-key" in output.getvalue()
    assert "tests/fixtures/history.py" in output.getvalue()
    assert "a" * 40 in output.getvalue()
    assert "SYNTHETIC_MATCH_SENTINEL_NOT_A_SECRET" not in output.getvalue()
    assert "SYNTHETIC_SECRET_SENTINEL_NOT_A_SECRET" not in output.getvalue()
    assert "synthetic@example.invalid" not in output.getvalue()
    assert "synthetic commit message" not in output.getvalue()
    assert "synthetic-fingerprint" not in output.getvalue()


@pytest.mark.parametrize(
    "record",
    [
        _record(rule_id="bad\n::error::synthetic"),
        _record(file_path="../outside.txt"),
        _record(file_path="/absolute/path.txt"),
        _record(file_path="."),
        _record(file_path="dir\\file.txt"),
        _record(file_path="line\nbreak.txt"),
        _record(line=0),
        _record(commit="not-a-commit"),
        "%%%\t%%%\t17\t%%%\n",
        "one\ttwo\tthree\n",
        "one\ttwo\t3\tfour\textra\n",
        _record() + "not-a-record\n",
    ],
)
def test_unsafe_or_ambiguous_metadata_fails_closed(record: str) -> None:
    with pytest.raises(InvalidReport):
        project_report(record)


def test_missing_or_invalid_report_stays_failed_and_does_not_echo_values(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.tsv"
    output = io.StringIO()

    result = report_scan_result(missing_path, 1, output)

    assert result == 1
    assert "remains blocked" in output.getvalue()
    assert str(missing_path) not in output.getvalue()


def test_malformed_report_error_is_generic(tmp_path: Path) -> None:
    report_path = tmp_path / "synthetic-report.tsv"
    report_path.write_text("not-a-record", encoding="utf-8")
    output = io.StringIO()

    result = report_scan_result(report_path, 0, output)

    assert result == 2
    assert output.getvalue() == (
        "Gitleaks metadata report missing or invalid; scan remains blocked.\n"
    )


def test_findings_with_zero_scanner_status_are_still_failed(tmp_path: Path) -> None:
    report_path = tmp_path / "synthetic-report.tsv"
    report_path.write_text(_record(), encoding="utf-8")

    assert report_scan_result(report_path, 0, io.StringIO()) == 1


def test_empty_report_preserves_success(tmp_path: Path) -> None:
    report_path = tmp_path / "synthetic-report.tsv"
    report_path.write_text("", encoding="utf-8")

    assert report_scan_result(report_path, 0, io.StringIO()) == 0


def test_scanner_failure_is_preserved_without_findings(tmp_path: Path) -> None:
    report_path = tmp_path / "synthetic-report.tsv"
    report_path.write_text("", encoding="utf-8")

    assert report_scan_result(report_path, 2, io.StringIO()) == 2


def test_workflow_retains_full_history_scan_and_uses_only_metadata_template() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "fetch-depth: 0" in workflow
    assert "gitleaks git ." in workflow
    assert "--config .gitleaks.toml" in workflow
    assert "--log-opts=HEAD" in workflow
    assert "--redact=100" in workflow
    assert "--report-format=template" in workflow
    assert '--report-template "$template_path"' in workflow
    assert "{{end -}}" in workflow
    assert "{{.RuleID | b64enc}}" not in workflow
    assert "{{.File | b64enc}}" not in workflow
    assert (
        '{{printf "%s\\t%s\\t%d\\t%s\\n" (.RuleID | b64enc) '
        "(.File | b64enc) .StartLine (.Commit | b64enc)}}" in workflow
    )
    assert "--report-format=json" not in workflow
    assert '--report-path "$report_path"' in workflow
    assert ">/dev/null 2>&1" in workflow
    assert "python3 scripts/ci/gitleaks_metadata.py" in workflow
    assert "actions/upload-artifact" not in workflow
