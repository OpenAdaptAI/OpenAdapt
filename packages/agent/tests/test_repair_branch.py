"""A second demonstration on one step also proposes a branch candidate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from openadapt_agent.repair import RepairAccessPolicy, RepairDesk


class _Store:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add_candidate(self, candidate):
        self.added.append(candidate)
        return SimpleNamespace(candidate_id=f"cand-{len(self.added)}")


class _AgentTeach:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[tuple[str, str]]] = []
        self.fail = fail

    def teach_branch_from_calling_agent(self, examples, out, *, bundle, provenance):
        self.calls.append(list(examples))
        if self.fail:
            raise RuntimeError("no shared screen text")
        return SimpleNamespace(candidate="branch-candidate")


def _demo(tmp: Path, name: str) -> tuple[dict, Path]:
    run_dir = tmp / "runs" / name
    recording = tmp / "teach" / name / "recording"
    run_dir.mkdir(parents=True)
    recording.mkdir(parents=True)
    record = {"workflow": "wf", "step_id": "s_save", "run_dir": str(run_dir), "bundle_dir": "b"}
    return record, recording


def test_first_demo_is_remembered_second_proposes_a_branch(tmp_path: Path) -> None:
    desk = RepairDesk(tmp_path / ".repair", RepairAccessPolicy(None))
    store, teach = _Store(), _AgentTeach()

    record, rec = _demo(tmp_path, "r1")
    assert desk._teach_branch(record, rec, tmp_path / "w1", store, teach, None) is None
    assert teach.calls == []

    record, rec = _demo(tmp_path, "r2")
    assert desk._teach_branch(record, rec, tmp_path / "w2", store, teach, None) == "cand-1"
    assert [Path(run).name for run, _ in teach.calls[0]] == ["r1", "r2"]
    assert store.added == ["branch-candidate"]


def test_other_step_and_refusal_add_no_branch(tmp_path: Path) -> None:
    desk = RepairDesk(tmp_path / ".repair", RepairAccessPolicy(None))
    store, teach = _Store(), _AgentTeach(fail=True)
    record, rec = _demo(tmp_path, "r1")
    desk._teach_branch(record, rec, tmp_path / "w1", store, teach, None)
    other, rec2 = _demo(tmp_path, "r2")
    other["step_id"] = "s_other"
    assert desk._teach_branch(other, rec2, tmp_path / "w2", store, teach, None) is None
    assert teach.calls == []
    record3, rec3 = _demo(tmp_path, "r3")
    assert desk._teach_branch(record3, rec3, tmp_path / "w3", store, teach, None) is None
    assert store.added == []


def test_older_flow_without_branch_support_is_skipped(tmp_path: Path) -> None:
    desk = RepairDesk(tmp_path / ".repair", RepairAccessPolicy(None))
    for name in ("r1", "r2"):
        record, rec = _demo(tmp_path, name)
        assert desk._teach_branch(record, rec, tmp_path / name, _Store(), object(), None) is None
