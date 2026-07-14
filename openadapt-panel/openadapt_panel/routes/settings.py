"""Settings route — view effective config, edit a curated set, persist to .env.

The panel edits a curated subset of ``openadapt.config.OpenAdaptSettings`` (API
keys, key directories, device, server) rather than all ~40 fields. API keys use
their bare env names (``ANTHROPIC_API_KEY`` — what the SDKs and ``doctor`` read);
other settings use the ``OPENADAPT_`` prefix. Changes are written to ``.env`` in
the working directory (the same file config.py loads).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Body

router = APIRouter(tags=["settings"])

ENV_PATH = Path(".env")

# key = env var name; secret values are never returned, only their set-state.
SCHEMA = [
    {
        "key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API key",
        "group": "API keys",
        "type": "string",
        "secret": True,
    },
    {
        "key": "OPENAI_API_KEY",
        "label": "OpenAI API key",
        "group": "API keys",
        "type": "string",
        "secret": True,
    },
    {
        "key": "GOOGLE_API_KEY",
        "label": "Google API key",
        "group": "API keys",
        "type": "string",
        "secret": True,
    },
    {
        "key": "LAMBDA_API_KEY",
        "label": "Lambda API key",
        "group": "API keys",
        "type": "string",
        "secret": True,
    },
    {
        "key": "OPENADAPT_CAPTURE_DIR",
        "label": "Capture directory",
        "group": "Directories",
        "type": "path",
        "secret": False,
    },
    {
        "key": "OPENADAPT_TRAINING_OUTPUT_DIR",
        "label": "Training output directory",
        "group": "Directories",
        "type": "path",
        "secret": False,
    },
    {
        "key": "OPENADAPT_MODEL_CACHE_DIR",
        "label": "Model cache directory",
        "group": "Directories",
        "type": "path",
        "secret": False,
    },
    {
        "key": "OPENADAPT_BENCHMARK_RESULTS_DIR",
        "label": "Benchmark results directory",
        "group": "Directories",
        "type": "path",
        "secret": False,
    },
    {
        "key": "OPENADAPT_DEFAULT_MODEL",
        "label": "Default model",
        "group": "ML",
        "type": "string",
        "secret": False,
    },
    {
        "key": "OPENADAPT_DEFAULT_DEVICE",
        "label": "Device (auto/cuda/mps/cpu)",
        "group": "ML",
        "type": "string",
        "secret": False,
    },
    {
        "key": "OPENADAPT_SERVER_PORT",
        "label": "Server port",
        "group": "Server",
        "type": "int",
        "secret": False,
    },
    {
        "key": "OPENADAPT_SERVER_HOST",
        "label": "Server host",
        "group": "Server",
        "type": "string",
        "secret": False,
    },
]

_KEYS = {entry["key"] for entry in SCHEMA}


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Update/append keys in .env, preserving any lines the panel doesn't own."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else None
        if key and key in remaining:
            val = remaining.pop(key)
            out.append(f"{key}={_quote(val)}")
        else:
            out.append(raw)
    for key, val in remaining.items():
        out.append(f"{key}={_quote(val)}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _quote(val: str) -> str:
    return f'"{val}"' if (" " in val or val == "") else val


def load_env_file() -> None:
    """Load .env into os.environ without overriding already-set vars.

    API clients (e.g. openadapt_evals.ApiAgent) read keys via os.getenv, so a
    key saved to .env via this page only takes effect once it's in the
    environment. Called at panel startup; the PUT handler also updates
    os.environ directly so a freshly saved key works without a restart.
    """
    for key, val in _read_env(ENV_PATH).items():
        os.environ.setdefault(key, val)


@router.get("/settings")
def get_settings() -> dict:
    env = _read_env(ENV_PATH)
    fields = []
    for entry in SCHEMA:
        key = entry["key"]
        current = os.environ.get(key, env.get(key))
        field = {**entry}
        if entry["secret"]:
            field["value"] = None
            field["is_set"] = bool(current)
        else:
            field["value"] = current
            field["is_set"] = current is not None
        fields.append(field)
    return {"env_path": str(ENV_PATH.resolve()), "fields": fields}


@router.put("/settings")
def update_settings(values: dict = Body(..., embed=True)) -> dict:
    updates: dict[str, str] = {}
    schema_by_key = {e["key"]: e for e in SCHEMA}
    for key, val in values.items():
        if key not in _KEYS:
            continue  # ignore unknown keys
        # Blank secret means "leave unchanged" — don't wipe a stored key.
        if schema_by_key[key]["secret"] and (val is None or val == ""):
            continue
        if val is None:
            continue
        updates[key] = str(val)
    if updates:
        _write_env(ENV_PATH, updates)
        # Take effect immediately in the running panel process (so eval/agents
        # that read os.getenv see the new value without a restart).
        for key, val in updates.items():
            os.environ[key] = val
    return {"updated": sorted(updates), "env_path": str(ENV_PATH.resolve())}
