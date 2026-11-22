"""A sealed bundle's BYOC job finishes cleanly: the connector's own plaintext
inputs never land in the run directory, the run's sealed report is read with
the key the child got, and a stop signal never misses a job being submitted.
Synthetic data only; no browser."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

from openadapt_flow import crypto, sealed_set
from openadapt_flow.connector import daemon as daemon_mod
from openadapt_flow.connector import executor as executor_mod
from openadapt_flow.connector.executor import (
    RunOutcome,
    _subprocess_runner,
    execute_job,
)
from openadapt_flow.connector.protocol import parse_job
from openadapt_flow.connector.storage import InMemoryCustomerStorage
from tests import test_connector_pilot as pilot

KEY = "synthetic-sealed-byoc-key-0123456789"

# Exactly what the governed child does for a sealed bundle
# (runtime/durable/first_local_runtime.py): open the run directory as a sealed
# writer, write the report through it, then finalize with adopt_external=True,
# which refuses any plaintext file found in the run directory.
SEALED_CHILD = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path
    from openadapt_flow import sealed_set

    run = Path(sys.argv[1])
    key = os.environ["OPENADAPT_BUNDLE_KEY"]
    writer = sealed_set.SealedSetWriter(
        run, "run", key, resume=sealed_set.is_sealed(run)
    )
    sealed_set.register_writer(writer)
    try:
        sealed_set.write_bytes(run / "report.json", json.dumps({
            "execution_target_kind": "web",
            "execution_outcome": "VERIFIED",
            "success": True,
            "terminal_outcome": "success",
            "results": [],
        }).encode("utf-8"))
    finally:
        sealed_set.unregister_writer(writer)
    writer.finalize(adopt_external=True)
    """
)


# The pilot module's signed program case and offline read-back, reused as is
# (pytest registers fixtures found in a test module's namespace).
from tests.test_connector_pilot import (  # noqa: E402,F401
    _signed_transport_program,
    _transport_has_no_external_readback,
)


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_a_sealed_job_finishes_cleanly_with_its_key_only_in_secrets_ref(
    tmp_path, monkeypatch
):
    # The daemon itself holds no bundle key: only the job's secrets_ref does.
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    monkeypatch.delenv(sealed_set.RECORDING_ENV_KEY, raising=False)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secret_file = secrets_dir / "sealed-run.env"
    secret_file.write_text(f"{crypto.ENV_KEY}={KEY}\n", encoding="utf-8")
    os.chmod(secret_file, 0o600)
    job = parse_job(
        pilot._payload(secrets_ref="sealed-run"), lease_job_id="bjob_sealed"
    )
    seen: dict = {}

    def runner(argv, run_dir: Path, child_env) -> RunOutcome:
        staged = [
            Path(p)
            for flag in ("--params-file", "--managed-dispatch-file")
            for p in _flag_values(argv, flag)
        ]
        seen.update(
            run_dir=run_dir,
            staged=staged,
            before=sorted(p.name for p in run_dir.iterdir()),
            staging_mode=stat.S_IMODE(staged[0].parent.stat().st_mode),
        )
        # The real _subprocess_runner and a real child process that seals the
        # run exactly as the governed child does.
        outcome = _subprocess_runner(
            [sys.executable, "-c", SEALED_CHILD, str(run_dir)], run_dir, child_env
        )
        seen["after"] = sorted(p.name for p in run_dir.iterdir())
        seen["sealed"] = sealed_set.is_sealed(run_dir)
        return outcome

    result = execute_job(
        job,
        pilot._configured_settings(secrets_dir=str(secrets_dir)),
        InMemoryCustomerStorage(pilot.PLAIN_BUNDLE),
        runner=runner,
    )

    assert result.status == "success", (result.status, result.error_code)
    assert result.error_code is None and result.halt is None
    # Nothing the connector wrote was in the run directory, before or after.
    assert seen["before"] == []
    assert seen["sealed"] is True
    assert "report.json" not in seen["after"] or seen["sealed"]
    assert seen["staged"], "the params and dispatch files were staged"
    for path in seen["staged"]:
        assert seen["run_dir"] not in path.parents
    assert seen["staging_mode"] == 0o700
    # The whole per-job directory, staging included, is gone afterwards.
    assert not seen["run_dir"].exists()
    for path in seen["staged"]:
        assert not path.exists() and not path.parent.exists()


def test_a_sealed_report_without_the_childs_key_is_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    monkeypatch.delenv(sealed_set.RECORDING_ENV_KEY, raising=False)
    run = tmp_path / "run"
    run.mkdir()
    env = {**os.environ, crypto.ENV_KEY: KEY}
    outcome = _subprocess_runner(
        [sys.executable, "-c", SEALED_CHILD, str(run)], run, env
    )
    assert outcome.returncode == 0
    assert outcome.report["execution_outcome"] == "VERIFIED"
    # Read with no key, or the wrong one: never a report, so never a verdict.
    report = run / "report.json"
    assert executor_mod._read_report(report, key="not the key") == {}
    assert executor_mod._read_report(report, key=None) == {}


def test_plaintext_in_a_sealed_run_directory_is_refused(tmp_path):
    """Why staging moved: the sealed run refuses a plaintext file beside it."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "params.json").write_text("{}", encoding="utf-8")
    env = {**os.environ, crypto.ENV_KEY: KEY}
    outcome = _subprocess_runner(
        [sys.executable, "-c", SEALED_CHILD, str(run)], run, env
    )
    assert outcome.returncode != 0


def test_a_stop_between_submit_and_register_still_cancels_the_job(monkeypatch):
    plane = pilot.RecordingPlane()
    plane.jobs.append(
        {"id": "bjob_gap", "status": "queued", "payload": pilot._payload()}
    )
    client = pilot._client(plane)
    cancelled: list[str] = []
    monkeypatch.setattr(
        daemon_mod, "cancel_job", lambda job_id: cancelled.append(job_id) or True
    )
    real_submit = daemon_mod.ThreadPoolExecutor.submit

    def submit_then_stop(self, *args, **kwargs):
        real_submit(self, *args, **kwargs)
        raise KeyboardInterrupt  # the stop lands before the future is listed

    monkeypatch.setattr(daemon_mod.ThreadPoolExecutor, "submit", submit_then_stop)
    settings = pilot._configured_settings(
        control_plane_url="https://cloud.test",
        token=pilot.TOKEN,
        poll_wait_s=0,
    )
    daemon_mod.run_loop(
        client,
        settings,
        runner=lambda argv, run_dir, env: RunOutcome(returncode=0, report={}),
        storage_factory=lambda job: InMemoryCustomerStorage(pilot.PLAIN_BUNDLE),
        log=lambda _m: None,
    )
    assert "bjob_gap" in cancelled
