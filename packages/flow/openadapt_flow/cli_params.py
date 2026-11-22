"""Read CLI runtime parameters from flags or a scalar-only JSON file."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from openadapt_flow.runtime.authorization import RuntimeParamScalar


def _parse_params(pairs: Sequence[str] | None) -> dict[str, str]:
    """Parse repeated ``--param k=v`` flags into a dict."""
    params: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects k=v, got {pair!r}")
        key, value = pair.split("=", 1)
        params[key] = value
    return params


def _replay_params(
    pairs: Sequence[str] | None,
    params_file: str | None = None,
) -> dict[str, "RuntimeParamScalar"]:
    """Load replay bindings without requiring sensitive values in argv.

    ``--params-file`` is intended for managed runners: the file can be staged
    inside the per-run boundary while process listings contain only its path.
    Explicit ``--param`` flags remain supported and override file values.
    """
    import json

    from openadapt_flow.runtime.authorization import is_runtime_param_scalar

    params: dict[str, RuntimeParamScalar] = {}
    if params_file:
        path = Path(params_file)
        try:
            from openadapt_flow import sealed_set

            raw = json.loads(sealed_set.read_text(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--params-file could not be read as JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit("--params-file must contain one JSON object")
        if len(raw) > 100:
            raise SystemExit("--params-file may contain at most 100 parameters")
        for key, value in raw.items():
            if not isinstance(key, str) or not key:
                raise SystemExit("--params-file keys must be non-empty strings")
            if not is_runtime_param_scalar(value):
                raise SystemExit(f"--params-file value for {key!r} must be a scalar")
            params[key] = value
    params.update(_parse_params(pairs))
    return params
