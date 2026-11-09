"""Act 2's worker pool: parallel runs, and what happens when a worker can't.

The workers here are stand-ins (tests/quickstart_fake_workers.py) in real
spawned processes, so the pool's scheduling, stall handling and fallbacks run
for real without a browser.
"""

from __future__ import annotations

import quickstart_fake_workers as fake

from openadapt.quickstart import workers
from openadapt.quickstart.workers import WorkerPool, worker_count

JOBS = [(index, f"note {index}", f"run-{index}") for index in range(1, 6)]


def _run(tmp_path, target, size=5):
    pool = WorkerPool(size, visible=False, target=target)
    finished = []
    try:
        back = pool.run(
            JOBS, bundle_dir=tmp_path, root=tmp_path, on_finish=finished.append
        )
    finally:
        pool.close()
    return finished, back


def test_worker_count_is_one_per_two_cpus_and_at_most_one_per_run():
    assert [worker_count(cpus) for cpus in (1, 2, 4, 8, 12, 64)] == [1, 1, 2, 4, 5, 5]


def test_every_run_finishes_in_the_workers(tmp_path):
    finished, back = _run(tmp_path, fake.good)
    assert back == []
    assert sorted(run.index for run in finished) == [1, 2, 3, 4, 5]
    assert all(run.checked for run in finished)
    assert finished[0].run_dir.parent == tmp_path


def test_fewer_workers_than_runs_take_turns(tmp_path):
    finished, back = _run(tmp_path, fake.good, size=2)
    assert back == []
    assert sorted(run.index for run in finished) == [1, 2, 3, 4, 5]


def test_a_stalled_run_comes_back_in_a_new_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "STALL_S", 3.0)
    finished, back = _run(tmp_path, fake.stalls_on_run_2)
    assert back == [(2, "note 2", "run-2-again")]
    assert 2 not in {run.index for run in finished}


def test_a_run_that_raised_is_reported_and_not_repeated(tmp_path):
    finished, back = _run(tmp_path, fake.fails_run_3)
    assert back == []
    (failed,) = [run for run in finished if run.index == 3]
    assert not failed.checked
    assert failed.plain_label.startswith("Didn't finish: TutorialError")


def test_workers_that_cannot_start_hand_every_run_back(tmp_path):
    finished, back = _run(tmp_path, fake.never_ready)
    assert finished == []
    assert back == JOBS


def test_usable_counts_only_workers_that_did_not_give_up(tmp_path):
    import time

    pool = WorkerPool(3, visible=False, target=fake.never_ready)
    try:
        deadline = time.monotonic() + 30
        while pool.usable() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert pool.usable() == 0
    finally:
        pool.close()
    good = WorkerPool(2, visible=False, target=fake.good)
    try:
        assert good.usable() == 2
    finally:
        good.close()
