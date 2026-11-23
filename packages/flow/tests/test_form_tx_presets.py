"""form_tx domain presets are data, and each shim returns what the old code built.

Each preset moved out of ``pack_compiler.py`` into ``form_tx/presets/*.json``.
The differential test rebuilds the old builder from git history (the commit
before the move) and compares it with the shim. CI's shallow checkout has no
history, so it skips there; the round-trip tests below still run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from openadapt_flow.form_tx import pack_compiler
from openadapt_flow.form_tx.presets import (
    PRESET_SCHEMA,
    load_domain_schema,
    preset_names,
)

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "packages/flow/openadapt_flow/form_tx/pack_compiler.py"

#: preset name: (shim, the commit before the move, the old function's name)
MOVED = {
    "patient_registration": (
        pack_compiler.build_patient_registration_domain_schema,
        "2f506d6c463aef8098fc8fcc2fd5bc46e55c1829",
        "build_patient_registration_domain_schema",
    ),
}


def _old_builder(sha: str, name: str):  # type: ignore[no-untyped-def]
    got = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{sha}:{SOURCE}"],
        capture_output=True,
        text=True,
    )
    if got.returncode != 0:
        pytest.skip("no git history for the commit before the move")
    text = got.stdout
    start = text.index(f"def {name}(")
    end = text.index("\ndef ", start + 1)
    namespace = dict(vars(pack_compiler))
    exec(compile(text[start:end], f"{sha}:{SOURCE}", "exec"), namespace)  # noqa: S102
    return namespace[name]


@pytest.mark.parametrize("preset", sorted(MOVED))
def test_the_shim_returns_what_the_old_builder_built(preset: str) -> None:
    shim, sha, name = MOVED[preset]
    old = _old_builder(sha, name)()
    assert shim() == old
    assert shim().model_dump(mode="json") == old.model_dump(mode="json")


@pytest.mark.parametrize("preset", sorted(MOVED))
def test_each_moved_preset_ships_as_json(preset: str) -> None:
    assert preset in preset_names()
    path = Path(pack_compiler.__file__).with_name("presets") / f"{preset}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == PRESET_SCHEMA
    assert load_domain_schema(preset).model_dump(mode="json") == data["preset"]


def test_an_unknown_preset_is_refused() -> None:
    with pytest.raises(ValueError):
        load_domain_schema("no_such_preset")
    with pytest.raises(ValueError):
        load_domain_schema("../pack_compiler")
