"""Short Python entry points for the record -> compile -> replay path.

These call the same code as the ``openadapt-flow`` CLI verbs, so behavior,
refusals, and outputs match the commands in the README::

    from openadapt_flow import api

    url = "http://localhost:8080"
    rec = api.record(url, out="rec")
    bundle = api.compile(rec, out="bundle", name="my-task")
    report = api.replay(bundle, url, params={"note": "Follow-up in 2 weeks"})
    print(report.transaction_outcome)

``replay`` is a rehearsal and ends ``COMPLETED_UNVERIFIED`` (finished, not
checked). A production run that reads the record back is ``openadapt-flow run``
with a deployment config; see docs/PARAMETERS.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from openadapt_flow.ir import RunReport

__all__ = ["FlowCommandFailed", "compile", "record", "replay"]


class FlowCommandFailed(RuntimeError):
    """A verb exited nonzero. ``exit_code`` is the CLI exit code."""

    def __init__(self, verb: str, exit_code: int) -> None:
        super().__init__(f"openadapt-flow {verb} exited {exit_code}")
        self.exit_code = exit_code


def _call(argv: Sequence[str]) -> None:
    from openadapt_flow.__main__ import main

    code = main(list(argv))
    if code:
        raise FlowCommandFailed(argv[0], code)


def record(url: str, *, out: str | Path = "rec", headless: bool = False) -> Path:
    """Open ``url`` in a browser and record what you do until you close it."""
    argv = ["record", "--backend", "web", "--url", url, "--out", str(out)]
    if headless:
        argv.append("--headless")
    _call(argv)
    return Path(out)


def compile(  # noqa: A001 - mirrors the CLI verb
    recording: str | Path,
    *,
    out: str | Path = "bundle",
    name: str = "my-task",
    accept_params: Optional[Sequence[str]] = None,
) -> Path:
    """Compile a recording into a bundle.

    Proposed parameters stay recorded constants unless named in
    ``accept_params``; this call never prompts.
    """
    argv = ["compile", str(recording), "--out", str(out), "--name", name]
    if accept_params:
        argv += ["--accept-params", ",".join(accept_params)]
    else:
        argv.append("--no-confirm-params")
    _call(argv)
    return Path(out)


def replay(
    bundle: str | Path,
    url: Optional[str] = None,
    *,
    params: Optional[Mapping[str, str]] = None,
    run_dir: str | Path = "run",
) -> RunReport:
    """Rehearse ``bundle`` against ``url`` and return the run report.

    Without ``url`` it serves the bundled MockMed demo app, as the CLI does.

    A halted run is not an exception: check ``report.transaction_outcome``.
    """
    argv = ["replay", str(bundle), "--run-dir", str(run_dir)]
    if url:
        argv += ["--url", url]
    for key, value in (params or {}).items():
        argv += ["--param", f"{key}={value}"]
    from openadapt_flow.__main__ import main

    main(argv)
    report_path = Path(run_dir) / "report.json"
    if not report_path.exists():
        raise FlowCommandFailed("replay", 1)
    return RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
