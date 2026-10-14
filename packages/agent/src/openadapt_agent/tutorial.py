"""Generate and host the synthetic MockMed workflow the sandbox runs.

The compiled bundle is not vendored. Record, compile, and certify go through
Flow's public tutorial APIs. MockMed stays up for the life of the MCP server
so every sandbox run hits a live system of record. Synthetic data only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = [
    "TutorialError",
    "TutorialSession",
    "bind_param_effects",
    "prepare_tutorial_session",
]


class TutorialError(RuntimeError):
    """The synthetic tutorial could not be materialized."""


@dataclass
class TutorialSession:
    bundle_dir: Path
    url: str
    deployment_config: Path
    work_dir: Path
    close: Callable[[], None]
    #: MockMed's origin, without the tutorial entry query.
    base_url: str = ""


def _deployment_yaml(base_url: str, entry_url: str) -> str:
    origin = base_url.rstrip("/")
    return (
        "name: synthetic-tutorial\n"
        "backend:\n"
        "  kind: web\n"
        f'  url: "{entry_url}"\n'
        "  headed: false\n"
        "effects:\n"
        "  kind: rest\n"
        f'  base_url: "{origin}"\n'
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
        "runtime:\n"
        "  profile: standard\n"
        "  durable: true\n"
        "  allow_model_grounding: false\n"
        "policy:\n"
        "  policy: clinical-write\n"
    )


def _bundle_is_loadable(bundle_dir: Path) -> bool:
    if not (bundle_dir / "workflow.json").is_file():
        return False
    try:
        from openadapt_flow.ir import Workflow

        Workflow.load(bundle_dir)
    except Exception:
        return False
    return True


def bind_param_effects(bundle_dir: Path) -> object:
    """Bind mined read-back values to the parameters that typed them.

    Flow mines each ``field_equals`` check with the demonstrated value as a
    literal and leaves binding it to the run's parameter to the operator's
    review. This is that review for the synthetic workflow: a check whose
    literal equals a parameter's recorded value reads that parameter instead,
    so the record check confirms the caller's value, not the recorded one.
    Saving re-seals the bundle manifest. Returns the loaded workflow.
    """
    from openadapt_flow.ir import Workflow

    workflow = Workflow.load(bundle_dir)
    try:
        from openadapt_flow.runtime.effects.effect import ValueExpr
    except ImportError:
        return workflow
    by_value = {str(value): name for name, value in (workflow.params or {}).items()}
    changed = False
    for step in workflow.steps:
        for effect in getattr(step, "effects", None) or []:
            kind = getattr(effect.kind, "value", effect.kind)
            value = getattr(effect, "value", None)
            literal = getattr(value, "literal", None)
            if kind == "field_equals" and literal is not None and literal in by_value:
                effect.value = ValueExpr(param=by_value[literal])
                changed = True
    if changed:
        workflow.save(bundle_dir)
        workflow = Workflow.load(bundle_dir)
    return workflow


def prepare_tutorial_session(
    work_dir: Path,
    *,
    headed: bool = False,
    reuse_bundle: bool = True,
) -> TutorialSession:
    """Record (if needed), compile, certify, and keep MockMed running.

    Admission is Flow's. This function does not pass
    ``--approve-unverified-writes`` or otherwise weaken the run gate.
    """

    try:
        from openadapt_flow.compiler import compile_recording
        from openadapt_flow.mockmed.fault_server import serve
        from openadapt_flow.tutorial import (
            TUTORIAL_ENTRY_QUERY,
            TUTORIAL_WORKFLOW_NAME,
            certify_tutorial,
            record_tutorial,
        )
    except ImportError as exc:
        raise TutorialError(
            "serve --tutorial needs the browser extra. Install with "
            "pip install 'openadapt-agent[tutorial]' or "
            "uvx --from 'openadapt-agent[tutorial]' openadapt-agent "
            "serve --allow-run"
        ) from exc

    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    recording_dir = root / "recording"
    bundle_dir = root / "bundle"
    config_path = root / "deployment.yaml"

    base_url, _db, stop = serve()
    try:
        if not (reuse_bundle and _bundle_is_loadable(bundle_dir)):
            record_tutorial(base_url, recording_dir, headed=headed)
            compile_recording(
                recording_dir,
                bundle_dir,
                name=TUTORIAL_WORKFLOW_NAME,
                mine_effects=True,
            )
        certify_tutorial(bind_param_effects(bundle_dir))
        entry_url = f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}"
        config_path.write_text(_deployment_yaml(base_url, entry_url), encoding="utf-8")
    except TutorialError:
        stop()
        raise
    except Exception as exc:
        stop()
        raise TutorialError(f"synthetic tutorial failed: {exc}") from exc

    return TutorialSession(
        bundle_dir=bundle_dir,
        url=entry_url,
        deployment_config=config_path,
        work_dir=root,
        close=stop,
        base_url=base_url,
    )
