"""flow-unit's shards: every Flow unit test file runs once, the shards balanced.

scripts/ci/flow_shards.py packs files by their measured run time
(scripts/ci/flow_test_durations.json). Stale or missing durations may
unbalance the shards but must never drop or repeat a file.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "packages" / "flow"

_spec = importlib.util.spec_from_file_location(
    "flow_shards", ROOT / "scripts" / "ci" / "flow_shards.py"
)
assert _spec and _spec.loader
flow_shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flow_shards)


def _unit_files() -> list[str]:
    return sorted(
        p.relative_to(FLOW).as_posix()
        for p in (FLOW / "tests").rglob("test_*.py")
        if "e2e" not in p.relative_to(FLOW / "tests").parts
    )


@pytest.mark.parametrize("shards", [1, 2, 3, 4])
def test_every_file_lands_in_exactly_one_shard(shards: int) -> None:
    files = _unit_files()
    plan = flow_shards.plan(files, shards, flow_shards.load())
    flat = [f for shard in plan for f in shard]
    assert sorted(flat) == files
    assert len(flat) == len(set(flat))


def test_the_plan_does_not_depend_on_input_order() -> None:
    files = _unit_files()
    seconds = flow_shards.load()
    assert flow_shards.plan(files, 3, seconds) == flow_shards.plan(
        list(reversed(files)), 3, seconds
    )


def test_shards_are_balanced_and_list_the_longest_file_first() -> None:
    seconds = {"a": 100.0, "b": 60.0, "c": 50.0, "d": 40.0, "e": 10.0}
    plan = flow_shards.plan(list(seconds), 2, seconds)
    loads = [sum(seconds[f] for f in shard) for shard in plan]
    assert loads == [140.0, 120.0]
    for shard in plan:
        assert shard == sorted(shard, key=lambda f: -seconds[f])


def test_a_file_without_a_duration_counts_as_the_median() -> None:
    seconds = {"a": 10.0, "b": 20.0, "c": 30.0}
    plan = flow_shards.plan(["a", "b", "c", "new"], 2, seconds)
    # new counts 20 s (the median), so it pairs with b, and a with c.
    assert plan == [["c", "a"], ["b", "new"]]


def test_the_durations_file_covers_most_unit_files() -> None:
    known = set(flow_shards.load())
    files = _unit_files()
    assert sum(f in known for f in files) >= 0.8 * len(files)


def test_durations_come_from_job_logs(tmp_path: Path) -> None:
    log = tmp_path / "shard.log"
    log.write_text(
        "2026-11-03T22:00:00.0000000Z [gw0] [  0%] PASSED tests/test_a.py::t1\n"
        "2026-11-03T22:00:00.0000000Z [gw1] [  0%] PASSED tests/test_c.py::t1\n"
        "2026-11-03T22:00:04.0000000Z [gw0] [ 50%] PASSED tests/test_a.py::t2\n"
        "2026-11-03T22:00:10.0000000Z [gw0] [ 75%] FAILED tests/test_b.py::t1\n"
        "2026-11-03T22:00:30.0000000Z [gw1] [100%] PASSED tests/test_c.py::t2\n"
        "noise line\n",
        encoding="utf-8",
    )
    assert flow_shards.from_logs([log]) == {
        "tests/test_a.py": 4.0,
        "tests/test_b.py": 6.0,
        "tests/test_c.py": 30.0,
    }


def test_gh_run_view_lines_and_colour_codes_parse(tmp_path: Path) -> None:
    log = tmp_path / "view.log"
    log.write_text(
        "unit\tTest\t2026-11-03T22:00:00.0000000Z [gw0] [  0%] "
        "\x1b[32mPASSED\x1b[0m tests/test_a.py::t1\n"
        "unit\tTest\t2026-11-03T22:00:05.0000000Z [gw0] [ 50%] "
        "\x1b[32mPASSED\x1b[0m tests/test_b.py::t1\n",
        encoding="utf-8",
    )
    assert flow_shards.from_logs([log]) == {
        "tests/test_a.py": 0.0,
        "tests/test_b.py": 5.0,
    }


def test_a_log_with_no_results_leaves_the_durations_alone(tmp_path: Path) -> None:
    durations = tmp_path / "d.json"
    durations.write_text('{"seconds": {"tests/test_a.py": 3.0}}', encoding="utf-8")
    log = tmp_path / "empty.log"
    log.write_text("nothing here\n", encoding="utf-8")
    code = flow_shards.main(["--from-logs", str(log), "--durations", str(durations)])
    assert code == 1
    assert flow_shards.load(durations) == {"tests/test_a.py": 3.0}


def test_an_empty_shard_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("tests/test_a.py\n"))
    assert flow_shards.main(["--shard", "2", "--of", "3"]) == 0
    assert capsys.readouterr().out == ""


def test_the_workflow_shards_with_this_script() -> None:
    text = (ROOT / ".github" / "workflows" / "packages.yml").read_text(encoding="utf-8")
    assert "scripts/ci/flow_shards.py --shard" in text
    assert "NR % n" not in text
    data = json.loads(flow_shards.DURATIONS.read_text(encoding="utf-8"))
    assert all(v >= 0 for v in data["seconds"].values())
