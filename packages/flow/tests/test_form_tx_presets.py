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
from openadapt_flow.form_tx import types as form_types
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
    "property_quote": (
        pack_compiler.build_property_quote_domain_schema,
        "2f506d6c463aef8098fc8fcc2fd5bc46e55c1829",
        "build_property_quote_domain_schema",
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
    # The old builder named form_tx types that pack_compiler may no longer import.
    namespace = {**vars(form_types), **vars(pack_compiler)}
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


# -- word lists ------------------------------------------------------------------


def _old_constant(sha: str, source: str, name: str) -> object:
    """A module constant's value at ``sha``: a literal, or frozenset(literal)."""

    import ast

    got = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{sha}:{source}"],
        capture_output=True,
        text=True,
    )
    if got.returncode != 0:
        pytest.skip("no git history for the commit before the move")
    for node in ast.parse(got.stdout).body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else getattr(node, "targets", [])
        )
        if any(getattr(t, "id", None) == name for t in targets):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and getattr(value.func, "id", "") == "frozenset"
            ):
                return frozenset(ast.literal_eval(value.args[0]))
            return ast.literal_eval(value)  # type: ignore[arg-type]
    raise AssertionError(f"{name} not found in {source} at {sha}")


def _primary_entity_kinds() -> object:
    from openadapt_flow.form_tx import reconciler

    return reconciler._PRIMARY_ENTITY_KINDS


def _next_button_pattern() -> object:
    import inspect

    return (
        inspect.signature(pack_compiler.induce_form_pack)
        .parameters["next_button_pattern"]
        .default
    )


#: word list: (current value, the commit before the move, file, constant)
MOVED_WORDS = {
    "primary_entity_kinds": (
        _primary_entity_kinds,
        "2f506d6c463aef8098fc8fcc2fd5bc46e55c1829",
        "packages/flow/openadapt_flow/form_tx/reconciler.py",
        "_PRIMARY_ENTITY_KINDS",
    ),
}


def test_the_next_button_default_is_unchanged() -> None:
    assert _next_button_pattern() == r"next|continue"
    assert pack_compiler.NEXT_BUTTON_PATTERN == r"next|continue"


@pytest.mark.parametrize("words", sorted(MOVED_WORDS))
def test_the_constant_equals_the_old_one(words: str) -> None:
    current, sha, source, name = MOVED_WORDS[words]
    old = _old_constant(sha, source, name)
    now = current()
    assert type(now) is type(old)
    assert now == old


@pytest.mark.parametrize("words", sorted(MOVED_WORDS))
def test_each_word_list_ships_as_json(words: str) -> None:
    from openadapt_flow.form_tx.presets import load_words

    assert load_words(words)
    assert words not in preset_names()


def test_an_unknown_word_list_is_refused() -> None:
    from openadapt_flow.form_tx.presets import load_words

    with pytest.raises(ValueError):
        load_words("no_such_list")


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '"words"',
        '{"schema": "other/v1", "name": "bad", "words": []}',
        '{"schema": "openadapt.form-word-list/v1", "name": "other", "words": []}',
        '{"schema": "openadapt.form-word-list/v1", "name": "bad", "words": [1]}',
        '{"schema": "openadapt.form-word-list/v1", "name": "bad", "words": "a"}',
    ],
)
def test_a_malformed_word_list_is_refused(
    content: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.form_tx import presets

    (tmp_path / "bad.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(presets, "_WORDS", tmp_path)
    presets.load_words.cache_clear()
    try:
        with pytest.raises(ValueError):
            presets.load_words("bad")
    finally:
        presets.load_words.cache_clear()
