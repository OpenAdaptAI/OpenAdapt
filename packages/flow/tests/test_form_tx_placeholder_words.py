"""The placeholder texts are data now, and equal to the old constants.

Both ``reconciler._DEFAULT_PLACEHOLDERS`` and the default of
``FieldBindingSpec.placeholder_values`` load
``form_tx/presets/words/placeholders.json``. The test reads the old literals
from 1ddd69712 (the commit before the move) and compares; it skips without
history.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.form_tx import reconciler
from openadapt_flow.form_tx.presets import load_words
from openadapt_flow.form_tx.types import (
    FieldAnchorSpec,
    FieldBindingSpec,
    FormPack,
)

ROOT = Path(__file__).resolve().parents[3]
BEFORE = "1ddd69712"


def _old_source(path: str) -> str:
    got = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{BEFORE}:packages/flow/{path}"],
        capture_output=True,
        text=True,
    )
    if got.returncode != 0:
        pytest.skip("no git history for the commit before the move")
    return got.stdout


def _old_reconciler_default() -> tuple[str, ...]:
    tree = ast.parse(_old_source("openadapt_flow/form_tx/reconciler.py"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "_DEFAULT_PLACEHOLDERS"
        ):
            assert node.value is not None
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("_DEFAULT_PLACEHOLDERS not found")


def _old_binding_default() -> tuple[str, ...]:
    tree = ast.parse(_old_source("openadapt_flow/form_tx/types.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "FieldBindingSpec":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and getattr(
                    item.target, "id", ""
                ) == ("placeholder_values"):
                    assert item.value is not None
                    return tuple(ast.literal_eval(item.value))
    raise AssertionError("FieldBindingSpec.placeholder_values not found")


def test_the_reconciler_default_is_unchanged() -> None:
    assert reconciler._DEFAULT_PLACEHOLDERS == _old_reconciler_default()
    assert isinstance(reconciler._DEFAULT_PLACEHOLDERS, tuple)


def test_the_binding_default_is_unchanged() -> None:
    binding = FieldBindingSpec(
        binding_id="b", dossier_key="k", anchor=FieldAnchorSpec(label_patterns=("x",))
    )
    assert binding.placeholder_values == _old_binding_default()


def test_both_defaults_come_from_one_word_list() -> None:
    words = load_words("placeholders")
    assert reconciler._DEFAULT_PLACEHOLDERS == words
    assert FieldBindingSpec.model_fields["placeholder_values"].default == words


def _pack(**kw: Any) -> FormPack:
    return FormPack(pack_id="p", domain_schema="d", ui_profile_id="u", **kw)


def test_a_pack_can_override_the_placeholders_for_unbound_controls() -> None:
    from openadapt_flow.form_tx.reconciler import _is_control_empty, _pack_placeholders
    from openadapt_flow.form_tx.types import ObservedControl

    ctrl = ObservedControl.model_construct(
        input_type="text",
        role="textbox",
        tag="input",
        current_value="Bitte wählen",
        checked=None,
        options=(),
    )
    default = _pack()
    assert _pack_placeholders(default) == load_words("placeholders")
    assert not _is_control_empty(ctrl, _pack_placeholders(default))
    german = _pack(placeholder_values=("", "Bitte wählen"))
    assert _pack_placeholders(german) == ("", "Bitte wählen")
    assert _is_control_empty(ctrl, _pack_placeholders(german))


def test_an_unset_placeholder_override_keeps_the_pack_digest() -> None:
    from openadapt_flow.form_tx.types import canonical_sha256

    pack = _pack()
    old_style = pack.model_dump(mode="json")
    old_style.pop("placeholder_values")
    assert pack.digest() == canonical_sha256(old_style)
    assert _pack(placeholder_values=("",)).digest() != pack.digest()


def test_the_option_resolver_default_is_unchanged() -> None:
    import inspect

    from openadapt_flow.form_tx.codecs import resolve_unique_option

    tree = ast.parse(_old_source("openadapt_flow/form_tx/codecs.py"))
    old = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_unique_option":
            names = [a.arg for a in node.args.args]
            defaults = node.args.defaults
            offset = len(names) - len(defaults)
            old = tuple(
                ast.literal_eval(defaults[names.index("placeholder_values") - offset])
            )
    assert old is not None
    now = inspect.signature(resolve_unique_option).parameters["placeholder_values"]
    assert now.default == old == load_words("placeholders")
