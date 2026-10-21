"""``openadapt-flow run`` repairs a stop by default once a repair agent is set.

The deployment file names the repair agent; no ``--self-repair`` flag. The
redesigned save control halts the run before any effect, the agent proposes
the new control, one phone tap approves it, and the run resumes and verifies.
``--no-auto-repair`` leaves the same stop for a person, and a run that can't
be repaired runs plainly instead of being refused. Live MockMed, no model.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from openadapt_flow.__main__ import main
from openadapt_flow.ir import RunReport
from openadapt_flow.repair.auto_repair import RepairLedger, summarize
from tests import test_self_repair_cli as cli
from tests.test_self_repair import (  # noqa: F401 - the shared MockMed fixture
    QUERY,
    _browser,
    _center_of,
    _records,
    _reset,
    mockmed,
)

pytest.importorskip("playwright.sync_api")

# The phone portal records the person's tap signed with its registered
# approver key (OPENADAPT_PORTAL_APPROVER_KEY); without one it records nothing.
pytestmark = pytest.mark.usefixtures("approver")


def _config(tmp_path: Path, base_url: str, *, auto: bool = True) -> Path:
    store = tmp_path / "repair-store"
    config = tmp_path / "deploy.yaml"
    config.write_text(
        f"effects:\n  kind: rest\n  base_url: {base_url}\n"
        "policy:\n  policy: clinical-write\n"
        "repair:\n"
        "  agent: tests.test_self_repair_cli:CliAgent\n"
        "  screen_access_by: ops-lead\n"
        "  screen_share_mode: allow_unscrubbed\n"
        f"  store: {store}\n"
        f"  auto: {'true' if auto else 'false'}\n"
        "  approval_wait_s: 120\n"
        "  fault_urls:\n"
        f"    - {base_url}/{QUERY}&drift=redesign,missing#tasks\n"
    )
    return config


def _run(bundle: Path, config: Path, url: str, run_dir: Path, *extra: str) -> int:
    """``main`` on a worker thread, for a run that ends on the plain replay
    path: its Playwright sync API refuses the test thread, where the fixture
    left an event loop set. (The self-repair path runs on the test thread.)"""

    out: list = []

    def target() -> None:
        try:
            out.append(_main(bundle, config, url, run_dir, *extra))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            out.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=600)
    if isinstance(out[0], BaseException):
        raise out[0]
    return out[0]


def _main(bundle: Path, config: Path, url: str, run_dir: Path, *extra: str) -> int:
    return main(
        [
            "run",
            str(bundle),
            "--config",
            str(config),
            "--backend",
            "web",
            "--url",
            url,
            "--run-dir",
            str(run_dir),
            "--param",
            "note=Synthetic follow-up in two weeks",
            "--approval-mode",
            "not_gated",
            "--allow-unencrypted",
            *extra,
        ]
    )


def test_a_configured_agent_repairs_a_stop_without_being_asked(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path,
    capsys,
) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    with _browser():
        cli.TARGET = _center_of(base_url, "#submit-encounter")
    run_dir = tmp_path / "run"
    stop = threading.Event()
    answered: list[str] = []
    phone = threading.Thread(
        target=cli._answer_on_phone, args=(run_dir, stop, answered), daemon=True
    )
    phone.start()
    try:
        _main(
            bundle,
            _config(tmp_path, base_url),
            f"{base_url}/{QUERY}&drift=redesign#tasks",
            run_dir,
        )
    finally:
        stop.set()
        phone.join(timeout=5)
        cli.TARGET = None
    out = capsys.readouterr().out
    assert answered, out
    assert "Self-repair: resumed" in out
    resumed = RunReport.model_validate_json(
        (run_dir / "self-repair" / "resume" / "report.json").read_text()
    )
    assert resumed.execution_outcome == "VERIFIED" or resumed.success
    assert len(_records(base_url)) == 1
    rows = RepairLedger(tmp_path / "repair-store").rows()
    assert len(rows) == 1
    assert rows[0]["route"] == "repair"
    assert rows[0]["approved_via"] == "person"
    assert rows[0]["person_decisions"] == 1
    assert rows[0]["person_wait_s"] is not None
    summary = summarize(rows)
    assert summary["resumed_items"] == 1 and summary["repair_success_rate"] == 1.0


def test_no_auto_repair_leaves_the_stop_for_a_person(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path,
    capsys,
) -> None:
    base_url, bundle = mockmed
    _reset(base_url)
    run_dir = tmp_path / "run"
    code = _run(
        bundle,
        _config(tmp_path, base_url),
        f"{base_url}/{QUERY}&drift=redesign#tasks",
        run_dir,
        "--no-auto-repair",
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "Self-repair" not in out
    halted = RunReport.model_validate_json((run_dir / "report.json").read_text())
    assert halted.halt is not None and halted.self_repair is None
    assert not (run_dir / "self-repair").exists()
    assert _records(base_url) == []
    assert RepairLedger(tmp_path / "repair-store").rows() == []


def test_a_run_that_cannot_be_repaired_runs_plainly(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path,
    capsys,
) -> None:
    from openadapt_flow.ir import Workflow

    base_url, bundle = mockmed
    _reset(base_url)
    digest = Workflow.load(bundle).manifest.content_digest
    run_dir = tmp_path / "run"
    code = _run(
        bundle,
        _config(tmp_path, base_url),
        f"{base_url}/{QUERY}#tasks",
        run_dir,
        "--pin-digest",
        digest,
    )
    out = capsys.readouterr().out
    assert "Auto-repair is off for this run: --pin-digest" in out
    assert code == 0, out
    assert (run_dir / "report.json").is_file()
    assert len(_records(base_url)) == 1


def test_the_cli_repairs_without_a_shared_browser_from_its_caller(
    mockmed,  # noqa: F811 - the shared fixture
    tmp_path,
    capsys,
) -> None:
    """The halted session stays open while the probe opens another. Outside
    this module's fixture nothing shares one browser, and a second Playwright
    driver on the same thread fails, so ``run`` must share one itself. Runs
    on a fresh thread with the fixture's browser set aside, as a real
    ``openadapt-flow run`` process would."""

    from openadapt_flow.backends import _browser_process

    base_url, bundle = mockmed
    _reset(base_url)
    with _browser():
        cli.TARGET = _center_of(base_url, "#submit-encounter")
    run_dir = tmp_path / "run"
    stop = threading.Event()
    answered: list[str] = []
    phone = threading.Thread(
        target=cli._answer_on_phone, args=(run_dir, stop, answered), daemon=True
    )
    phone.start()
    saved = dict(_browser_process._SHARED_BROWSER)
    _browser_process._SHARED_BROWSER.clear()
    try:
        _run(
            bundle,
            _config(tmp_path, base_url),
            f"{base_url}/{QUERY}&drift=redesign#tasks",
            run_dir,
        )
    finally:
        _browser_process._SHARED_BROWSER.update(saved)
        stop.set()
        phone.join(timeout=5)
        cli.TARGET = None
    out = capsys.readouterr().out
    assert "Self-repair: resumed" in out, out
    assert len(_records(base_url)) == 1
