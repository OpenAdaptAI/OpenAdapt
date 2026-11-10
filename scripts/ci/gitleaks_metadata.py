"""Print a strict projection of Gitleaks' four-field metadata report."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import TextIO

_RULE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class InvalidReport(ValueError):
    """Raised when report metadata cannot be safely projected."""


def _decode_field(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise InvalidReport from exc


def _safe_path(value: str) -> str:
    if not value or len(value) > 512:
        raise InvalidReport
    if "\\" in value or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    ):
        raise InvalidReport
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvalidReport
    return value


def project_report(report: str) -> list[dict[str, str | int]]:
    """Decode four-field TSV containing only Gitleaks metadata fields."""
    if not isinstance(report, str):
        raise InvalidReport
    if not report:
        return []

    projected: list[dict[str, str | int]] = []
    for record in report.splitlines():
        fields = record.split("\t")
        if len(fields) != 4:
            raise InvalidReport
        rule_id, file_path, line_text, commit = (
            _decode_field(fields[0]),
            _decode_field(fields[1]),
            fields[2],
            _decode_field(fields[3]),
        )
        if not _RULE_ID.fullmatch(rule_id):
            raise InvalidReport
        file_path = _safe_path(file_path)
        if not line_text.isascii() or not line_text.isdecimal():
            raise InvalidReport
        line = int(line_text)
        if line < 1:
            raise InvalidReport
        if not _COMMIT.fullmatch(commit):
            raise InvalidReport
        projected.append(
            {
                "RuleID": rule_id,
                "File": file_path,
                "StartLine": line,
                "Commit": commit,
            }
        )
    return projected


def read_projection(report_path: Path) -> list[dict[str, str | int]]:
    try:
        return project_report(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, InvalidReport) as exc:
        raise InvalidReport from exc


def report_scan_result(
    report_path: Path, scanner_exit_code: int, output: TextIO = sys.stdout
) -> int:
    """Emit safe metadata and preserve scanner failure semantics."""
    try:
        findings = read_projection(report_path)
    except InvalidReport:
        print(
            "Gitleaks metadata report missing or invalid; scan remains blocked.",
            file=output,
        )
        return scanner_exit_code or 2

    print(f"Gitleaks findings: {len(findings)}", file=output)
    for finding in findings:
        # JSON escaping prevents repository-controlled metadata from becoming
        # GitHub Actions workflow commands or additional log lines.
        print(
            "Gitleaks metadata: "
            + json.dumps(finding, ensure_ascii=True, separators=(",", ":")),
            file=output,
        )

    if findings and scanner_exit_code == 0:
        return 1
    return scanner_exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("report_path", type=Path)
    parser.add_argument("scanner_exit_code", type=int)
    args = parser.parse_args()
    if args.scanner_exit_code < 0 or args.scanner_exit_code > 255:
        parser.error("scanner exit code must be between 0 and 255")
    return report_scan_result(args.report_path, args.scanner_exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
