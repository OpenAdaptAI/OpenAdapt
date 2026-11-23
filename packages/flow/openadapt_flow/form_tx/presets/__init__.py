"""Domain presets for ``form_tx`` pack induction, kept as data.

A preset is a canonical domain schema (the fields a kind of form asks for,
with label synonyms) used by ``induce_form_pack`` and the ``--domain`` flag.
It is vocabulary for one kind of form, so it lives here as JSON
(``openadapt.form-domain-preset/v1``), not in the engine's code. The
``build_*_domain_schema`` functions in ``pack_compiler`` load these files and
return the same objects they used to build.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openadapt_flow.form_tx.pack_compiler import CanonicalDomainSchema

__all__ = ["PRESET_SCHEMA", "load_domain_schema", "preset_names"]

PRESET_SCHEMA = "openadapt.form-domain-preset/v1"
_DIR = Path(__file__).resolve().parent


def preset_names() -> tuple[str, ...]:
    """The names of the presets shipped with this package."""

    return tuple(sorted(p.stem for p in _DIR.glob("*.json")))


@lru_cache(maxsize=None)
def _raw(name: str) -> dict[str, Any]:
    if name not in preset_names():
        raise ValueError(f"unknown form_tx domain preset {name!r}")
    data = json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != PRESET_SCHEMA:
        raise ValueError(f"preset {name!r} is not {PRESET_SCHEMA}")
    preset = data.get("preset")
    if not isinstance(preset, dict):
        raise ValueError(f"preset {name!r} has no preset object")
    return preset


def load_domain_schema(name: str) -> "CanonicalDomainSchema":
    """The canonical domain schema stored as preset ``name``."""

    from openadapt_flow.form_tx.pack_compiler import CanonicalDomainSchema

    return CanonicalDomainSchema.model_validate(_raw(name))
