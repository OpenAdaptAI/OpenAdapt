"""Required Flow claims consume complete current-run evidence and keep failures."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "packages" / "flow"
SPEC = importlib.util.spec_from_file_location(
    "merge_flow_junit", ROOT / "scripts" / "ci" / "merge_flow_junit.py"
)
assert SPEC and SPEC.loader
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)
validator = merge._validator()


def _report(artifacts: Path, index: int, shards: int, cases: str) -> Path:
    directory = artifacts / f"flow-unit-junit-{index}-of-{shards}"
    directory.mkdir(parents=True)
    path = directory / f"flow-unit-shard-{index}-of-{shards}.xml"
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    return path


def _case(module: str, detail: str = "", name: str = "example") -> str:
    return f'<testcase classname="tests.{module}" name="{name}">{detail}</testcase>'


def _registry(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "claims": [
                    {
                        "id": "transport-proof",
                        "claim": "synthetic transport control",
                        "tier": "supported",
                        "evidence": [
                            {"path": "tests/test_replayer.py", "proves": "control"},
                            {
                                "path": "tests/test_effect_verifier.py",
                                "proves": "control",
                            },
                        ],
                    }
                ]
            }
        )
    )
    return path


def _check(out: Path, registry: Path) -> int:
    return validator.main(
        [
            "--check",
            "--ci-job",
            "test",
            "--registry",
            str(registry),
            "--junit",
            str(out),
        ]
    )


@pytest.mark.parametrize("shards", [2, 3])
def test_complete_partial_shards_pass_the_unchanged_full_scope_validator(
    tmp_path: Path, shards: int
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    for index in range(1, shards + 1):
        module = "test_replayer" if index == 1 else "test_effect_verifier"
        _report(artifacts, index, shards, _case(module))
    merge.merge_reports(artifacts, shards, out)
    assert _check(out, _registry(tmp_path / "claims.yaml")) == 0


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_case("test_replayer"), _case("test_replayer")),  # required file absent
        (_case("test_replayer"), _case("test_effect_verifier", "<skipped/>")),
        (
            _case("test_replayer", "<failure/>") + _case("test_effect_verifier"),
            _case("test_replayer"),
        ),
        (
            _case("test_replayer"),
            _case("test_replayer", "<error/>") + _case("test_effect_verifier"),
        ),
    ],
)
def test_aggregation_never_hides_absent_skipped_failed_or_errored_evidence(
    tmp_path: Path, first: str, second: str
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    _report(artifacts, 1, 2, first)
    _report(artifacts, 2, 2, second)
    merge.merge_reports(artifacts, 2, out)
    assert _check(out, _registry(tmp_path / "claims.yaml")) == 1


def test_optional_skip_does_not_overwrite_the_existing_file_pass_rule(
    tmp_path: Path,
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    _report(artifacts, 1, 2, _case("test_replayer", "<skipped/>", "optional"))
    _report(artifacts, 2, 2, _case("test_replayer") + _case("test_effect_verifier"))
    merge.merge_reports(artifacts, 2, out)
    assert _check(out, _registry(tmp_path / "claims.yaml")) == 0


@pytest.mark.parametrize("fault", ["missing", "extra", "wrong-count", "duplicate"])
def test_artifact_set_must_match_every_expected_shard(
    tmp_path: Path, fault: str
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    _report(artifacts, 1, 2, _case("test_replayer"))
    if fault != "missing":
        _report(artifacts, 2, 2, _case("test_effect_verifier"))
    if fault == "extra":
        _report(artifacts, 3, 3, _case("test_replayer"))
    if fault == "duplicate":
        directory = artifacts / "flow-unit-junit-1-of-2"
        (directory / "duplicate.xml").write_text("<testsuite/>")
    with pytest.raises(ValueError):
        merge.merge_reports(artifacts, 3 if fault == "wrong-count" else 2, out)
    assert not out.exists()


@pytest.mark.parametrize(
    "content",
    [
        "<broken",
        "<testsuite/>",
        '<testsuite><testcase classname="foreign"/></testsuite>',
    ],
)
def test_bad_report_cannot_be_covered_by_another_shards_good_report(
    tmp_path: Path, content: str
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    _report(artifacts, 1, 2, _case("test_replayer") + _case("test_effect_verifier"))
    report = _report(artifacts, 2, 2, _case("test_replayer"))
    report.write_text(content)
    assert (
        merge.main(
            ["--artifacts-dir", str(artifacts), "--shards", "2", "--out", str(out)]
        )
        == 1
    )
    assert not out.exists()


def test_missing_download_or_stale_output_is_never_a_fallback(tmp_path: Path) -> None:
    artifacts, out = tmp_path / "missing", tmp_path / "merged.xml"
    with pytest.raises(ValueError):
        merge.merge_reports(artifacts, 2, out)
    for index in (1, 2):
        _report(artifacts, index, 2, _case("test_replayer"))
    out.write_text("old report")
    with pytest.raises(ValueError, match="already exists"):
        merge.merge_reports(artifacts, 2, out)
    assert out.read_text() == "old report"


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/packages.yml").read_text())


def test_producer_and_consumer_share_the_shard_count_and_current_run_artifacts() -> (
    None
):
    jobs = _workflow()["jobs"]
    classify = jobs["scope"]["steps"][-1]["run"]
    assert classify.index('echo "flow_unit_shards=') < classify.index("all() {")
    assert (
        'if [ "$EVENT_NAME" = pull_request ]; then unit_shards=3; else unit_shards=2; fi'
        in classify
    )
    unit = jobs["flow-unit"]
    assert (
        "needs.scope.outputs.flow_unit_shards" in unit["strategy"]["matrix"]["include"]
    )
    test = next(s for s in unit["steps"] if s.get("name") == "Test")
    assert (
        "--junitxml=runs/flow-unit-shard-${{ matrix.shard }}-of-${{ matrix.of }}.xml"
        in test["run"]
    )
    upload = unit["steps"][-1]
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    assert (
        upload["with"]["name"]
        == "flow-unit-junit-${{ matrix.shard }}-of-${{ matrix.of }}"
    )
    gate = jobs["packages-gate"]
    download = next(
        s for s in gate["steps"] if "download-artifact@" in s.get("uses", "")
    )
    assert download["with"] == {
        "pattern": "flow-unit-junit-*",
        "path": "runs/flow-unit-junit",
        "merge-multiple": False,
    }
    # No run-id, repository, or token override: the action reads this run only.
    for step in gate["steps"][2:]:
        assert (
            step["if"]
            == "needs.scope.outputs.flow == 'true' && github.event_name != 'merge_group'"
        )
        assert not step.get("continue-on-error")
    consumer = gate["steps"][-1]["run"]
    assert consumer.startswith("set -euo pipefail\n")
    assert "--shards '${{ needs.scope.outputs.flow_unit_shards }}'" in consumer
    assert "--check --ci-job test --junit ../../runs/flow-unit-junit.xml" in consumer
    assert "--evidence-path" not in consumer and "--structure-only" not in consumer
    assert "merge_group" in unit["if"]


def test_existing_required_status_and_critical_checks_still_precede_claims() -> None:
    jobs = _workflow()["jobs"]
    gate = jobs["packages-gate"]
    status = gate["steps"][1]
    assert "RESULTS" in status["env"]
    assert 'if result not in ("success", "skipped"):' in status["run"]
    assert 'bad[name] = "skipped although selected"' in status["run"]
    assert 'not_in_queue = {"flow-unit"}' in status["run"]
    critical = jobs["flow-critical"]["steps"]
    assert "skipped" in critical[-2]["run"]
    assert "tests == 0 or skipped" in critical[-2]["run"]
    assert "--check --ci-job e2e-browser" in critical[-1]["run"]
    assert "--junit runs/critical-e2e-junit.xml" in critical[-1]["run"]
    assert "--evidence-path" not in critical[-1]["run"]


@pytest.mark.parametrize(
    ("event", "flow_selected", "producer_result", "expected"),
    [
        ("pull_request", True, "success", 0),
        ("pull_request", True, "failure", 1),
        ("pull_request", True, "cancelled", 1),
        ("pull_request", True, "skipped", 1),
        ("merge_group", True, "skipped", 0),
        ("push", True, "skipped", 1),
        ("pull_request", False, "skipped", 0),
    ],
)
def test_the_actual_required_status_gate_rejects_failed_or_missing_producers(
    event: str, flow_selected: bool, producer_result: str, expected: int
) -> None:
    gate = _workflow()["jobs"]["packages-gate"]["steps"][1]["run"]
    script = "\n".join(gate.splitlines()[1:-1])
    needs = {
        "scope": {"result": "success", "outputs": {"flow": str(flow_selected).lower()}},
        "flow-unit": {"result": producer_result},
        "flow-critical": {"result": "success"},
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RESULTS": json.dumps(needs), "EVENT_NAME": event},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr


def test_supported_backing_files_cannot_be_wholly_deselected_in_the_fast_lane() -> None:
    excluded = {
        line.split("#", 1)[0].strip().removeprefix("live ")
        for line in (FLOW / "tests/nightly_only.txt").read_text().splitlines()
    }
    for claim in validator.load_registry()["claims"]:
        if claim["tier"] != "supported":
            continue
        for evidence in claim["evidence"]:
            if evidence["path"].startswith("tests/"):
                assert evidence["path"] not in excluded


def test_workflow_uses_only_the_small_locked_yaml_group() -> None:
    groups = (ROOT / ".github/ci-tools/pyproject.toml").read_text()
    assert 'claims = ["pyyaml>=6.0"]' in groups
    steps = _workflow()["jobs"]["packages-gate"]["steps"]
    install = next(
        s
        for s in steps
        if s.get("name") == "Install the locked claims validator dependency"
    )
    assert (
        install["run"]
        == "python scripts/ci/locked_install.py --lock .github/ci-tools:claims"
    )


@pytest.mark.parametrize("flag", ["--artifacts-dir", "--shards", "--out", "--help"])
def test_junit_cli_rejects_every_undeclared_option_prefix_before_writing(
    tmp_path: Path, flag: str
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    _report(artifacts, 1, 2, _case("test_replayer"))
    _report(artifacts, 2, 2, _case("test_effect_verifier"))
    canonical = [
        "--artifacts-dir",
        str(artifacts),
        "--shards",
        "2",
        "--out",
        str(out),
    ]
    before = {path: path.read_bytes() for path in artifacts.rglob("*.xml")}
    for length in range(3, len(flag)):
        prefix = flag[:length]
        if flag == "--help":
            candidates = [canonical + [prefix]]
        else:
            index = canonical.index(flag)
            value = canonical[index + 1]
            candidates = [
                canonical[:index] + [prefix, value] + canonical[index + 2 :],
                canonical[:index] + [prefix + "=" + value] + canonical[index + 2 :],
            ]
        for argv in candidates:
            with pytest.raises(SystemExit) as refused:
                merge.main(argv)
            assert refused.value.code == 2
            assert not out.exists()
    assert {path: path.read_bytes() for path in artifacts.rglob("*.xml")} == before


def test_junit_cli_keeps_full_option_spellings_and_complete_validation(
    tmp_path: Path,
) -> None:
    artifacts, out = tmp_path / "artifacts", tmp_path / "merged.xml"
    _report(artifacts, 1, 2, _case("test_replayer"))
    _report(artifacts, 2, 2, _case("test_effect_verifier"))
    assert (
        merge.main(
            ["--artifacts-dir", str(artifacts), "--shards", "2", "--out", str(out)]
        )
        == 0
    )
    assert out.is_file()
    assert _check(out, _registry(tmp_path / "claims.yaml")) == 0


def _serial_seed_script() -> tuple[str, str]:
    run = next(
        step["run"]
        for step in _workflow()["jobs"]["flow-unit"]["steps"]
        if step.get("name") == "Test"
    )
    guard = run.split("<<'PYTHON'\n", 1)[1].split("\nPYTHON", 1)[0]
    return run, guard


def test_seed_timing_case_has_one_required_serial_owner() -> None:
    run, _guard = _serial_seed_script()
    case = "tests/test_tx_acid_servers.py::test_50k_seed_speed_and_8_worker_background_concurrency"
    assert f"serial_case={case}" in run
    assert 'if [ "$file" = "${serial_case%%::*}" ]; then run_serial=true; fi' in run
    assert '--deselect="$serial_case"' in run
    assert run.count('"$serial_case"') == 2  # deselect and exact serial invocation
    assert "pytest -n 0 -v --basetemp=runs/ci-serial" in run
    assert '--junitxml=runs/flow-serial.xml "$serial_case" || serial_rc=$?' in run
    assert "serial_coverage=(--cov-append)" in run
    assert 'if [ "$serial_rc" -ne 0 ]; then exit "$serial_rc"; fi' in run
    assert 'exit "$unit_rc"' in run
    # Check the actual shell structure, without executing pytest or any command.
    rendered = run.replace("${{ matrix.shard }}", "1").replace("${{ matrix.of }}", "3")
    result = subprocess.run(
        ["bash", "-n"], input=rendered, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "missing",
        "duplicate",
        "skipped",
        "failure",
        "error",
        "name",
        "class",
        "parallel-duplicate",
    ],
)
def test_serial_seed_census_preserves_cases_and_refuses_missing_or_failed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None
) -> None:
    import xml.etree.ElementTree as ET

    _run, guard = _serial_seed_script()
    name = "test_50k_seed_speed_and_8_worker_background_concurrency"
    case = _case("test_tx_acid_servers", name=name)
    parallel = tmp_path / "unit.xml"
    (tmp_path / "runs").mkdir()
    serial = tmp_path / "runs" / "flow-serial.xml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["census", str(parallel)])

    def write_reports(
        serial_cases: str, parallel_cases: str = _case("test_replayer")
    ) -> None:
        parallel.write_text(
            f"<testsuites><testsuite>{parallel_cases}</testsuite></testsuites>"
        )
        serial.write_text(
            f"<testsuites><testsuite>{serial_cases}</testsuite></testsuites>"
        )

    # Every negative is paired with the same genuine one-case positive first.
    write_reports(case)
    exec(compile(guard, "workflow-serial-census", "exec"), {})
    merged = list(ET.parse(parallel).getroot().iter("testcase"))
    assert [(c.get("classname"), c.get("name")) for c in merged] == [
        ("tests.test_replayer", "example"),
        ("tests.test_tx_acid_servers", name),
    ]
    if defect is None:
        return
    if defect == "missing":
        write_reports("")
    elif defect == "duplicate":
        write_reports(case + case)
    elif defect in ("skipped", "failure", "error"):
        write_reports(_case("test_tx_acid_servers", f"<{defect}/>", name=name))
    elif defect == "name":
        write_reports(_case("test_tx_acid_servers", name="different_case"))
    elif defect == "class":
        write_reports(_case("different_module", name=name))
    else:
        write_reports(case, case)
    with pytest.raises(AssertionError):
        exec(compile(guard, "workflow-serial-census", "exec"), {})
