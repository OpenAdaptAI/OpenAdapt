"""The JSON schema of every model in the modules the refactor moves stays as it is.

The monorepo refactor (openadapt-internal ``docs/design/monorepo-refactor-
2026-11/PLAN.md``) splits ``ir.py`` and the qualification modules into
packages and leaves re-exports at the old paths. Workflow bundles, run reports,
qualification projects and admission envelopes are files people keep, so a
move must not change what a model accepts or writes.

For each module below, this test pins every Pydantic model defined in it when
the snapshot was written, by the name it is imported under from that module:
its validation schema and its serialization schema from ``model_json_schema``,
with property order kept (field order is part of what a model writes). After a
move the name still resolves through the old module, and its schemas must be
equal.

Schemas are compared only on the Pydantic version the snapshot was written
with, because Pydantic's schema output changes between versions; on another
version the test checks only that every pinned model name still resolves.

Snapshots are under ``fixtures/schema_surface/``, one file per module, so a
pull request that changes models in one module touches one file. Regenerate
after a deliberate model change and say why in the pull request:

    OPENADAPT_WRITE_SCHEMA_SURFACE=1 pytest tests/test_schema_surface.py
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

SNAPSHOTS = Path(__file__).parent / "fixtures" / "schema_surface"
# The modules the plan moves first. ``qualify/`` is left out while open pull
# requests change it; receipt and Seal formats have their own vector tests.
MODULES = [
    "openadapt_flow.approval_mode.model",
    "openadapt_flow.ir",
    "openadapt_flow.production_qualification",
    "openadapt_flow.qualification",
    "openadapt_flow.qualification_admission",
    "openadapt_flow.qualification_admission_v2",
    "openadapt_flow.qualification_admission_v4",
    "openadapt_flow.qualification_campaign_authority",
    "openadapt_flow.qualification_campaign_permit",
    "openadapt_flow.qualification_dev_signer",
    "openadapt_flow.qualification_environment",
    "openadapt_flow.qualification_faults",
    "openadapt_flow.qualification_oracle_gate",
    "openadapt_flow.qualification_proposal",
    "openadapt_flow.records.schema",
    "openadapt_flow.runtime.effects.effect",
]


def _pydantic() -> str:
    return importlib.metadata.version("pydantic")


def _schemas(model: type[BaseModel]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in ("validation", "serialization"):
        try:
            out[mode] = model.model_json_schema(mode=mode)
        except Exception as error:  # noqa: BLE001 - pin the failure as well
            out[mode] = {"error": type(error).__name__}
    return out


def _defined_models(module_name: str) -> dict[str, type[BaseModel]]:
    module = importlib.import_module(module_name)
    found = {}
    for name, value in vars(module).items():
        if (
            isinstance(value, type)
            and issubclass(value, BaseModel)
            and value.__module__ == module_name
        ):
            found[name] = value
    return dict(sorted(found.items()))


def _path(module_name: str) -> Path:
    return SNAPSHOTS / f"{module_name}.json"


def test_snapshots_are_current_when_regenerating() -> None:
    if os.environ.get("OPENADAPT_WRITE_SCHEMA_SURFACE") != "1":
        pytest.skip("set OPENADAPT_WRITE_SCHEMA_SURFACE=1 to regenerate")
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    for module_name in MODULES:
        models = {
            name: _schemas(model)
            for name, model in _defined_models(module_name).items()
        }
        payload = {"pydantic": _pydantic(), "models": models}
        _path(module_name).write_text(
            json.dumps(payload, indent=1) + "\n", encoding="utf-8"
        )


@pytest.mark.parametrize("module_name", MODULES)
def test_every_pinned_model_keeps_its_schema(module_name: str) -> None:
    pinned = json.loads(_path(module_name).read_text(encoding="utf-8"))
    module = importlib.import_module(module_name)
    missing = [name for name in pinned["models"] if not hasattr(module, name)]
    assert missing == [], (
        f"{module_name} no longer provides models {missing}; a move must "
        "re-export what it moves"
    )
    if pinned["pydantic"] != _pydantic():
        pytest.skip(
            f"schemas pinned on Pydantic {pinned['pydantic']}, not {_pydantic()}"
        )
    changed = []
    for name, schemas in pinned["models"].items():
        model = getattr(module, name)
        current = json.loads(json.dumps(_schemas(model)))
        if current != schemas or list(current) != list(schemas):
            changed.append(name)
        else:
            for mode, schema in schemas.items():
                # Equal dicts can still differ in property order.
                if json.dumps(current[mode]) != json.dumps(schema):
                    changed.append(name)
                    break
    assert changed == [], (
        f"{module_name}: the JSON schema of {changed} changed. Regenerate only "
        "for a deliberate model change (see this test's docstring)."
    )


def test_every_listed_module_has_a_snapshot() -> None:
    assert sorted(p.stem for p in SNAPSHOTS.glob("*.json")) == sorted(MODULES)
