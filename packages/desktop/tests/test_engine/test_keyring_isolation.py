"""Engine tests must never reach the developer's OS keychain.

The autouse ``fake_keyring`` fixture patches ``openadapt_desktop.auth.store._keyring``. A
module that binds ``_keyring`` or a ``_kr_*`` helper by name keeps whatever
function it saw at import time: the real keychain when it is imported before a
fixture runs, or one test's stale fake when it is first imported inside a test.
"""

from __future__ import annotations

import ast
from base64 import b64encode
from pathlib import Path

import openadapt_desktop

# Imported at collection time, before any fixture runs: the case that used to
# bind the real keychain.
from openadapt_desktop import bundle_keys, qualification_keys
from openadapt_desktop.auth.store import SERVICE_NAME

_KEYRING_HELPERS = {"_keyring", "_kr_get", "_kr_set", "_kr_delete"}
_ENGINE_ROOT = Path(openadapt_desktop.__file__).resolve().parent


def _store_helper_bindings(tree: ast.AST, package: tuple[str, ...]) -> list[tuple[int, str]]:
    """Return ``(line, name)`` for each keyring helper imported by name from the store."""
    bindings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package[: len(package) - node.level + 1]
            module = ".".join(base + ((node.module,) if node.module else ()))
        else:
            module = node.module or ""
        if module == "openadapt_desktop.auth.store":
            bindings.extend(
                (node.lineno, alias.name) for alias in node.names if alias.name in _KEYRING_HELPERS
            )
    return bindings


def test_guard_detects_by_name_binding():
    absolute = ast.parse(
        "from openadapt_desktop.auth.store import DEFAULT_HOST, _keyring, _kr_get\n"
    )
    relative = ast.parse("from .store import (\n    _kr_set,\n)\n")
    attribute = ast.parse("from openadapt_desktop.auth import store as _store\n")

    assert _store_helper_bindings(absolute, ("openadapt_desktop",)) == [
        (1, "_keyring"),
        (1, "_kr_get"),
    ]
    assert _store_helper_bindings(relative, ("openadapt_desktop", "auth")) == [(1, "_kr_set")]
    assert _store_helper_bindings(attribute, ("openadapt_desktop",)) == []


def test_no_engine_module_binds_keyring_helpers_by_name():
    paths = sorted(_ENGINE_ROOT.rglob("*.py"))
    assert _ENGINE_ROOT / "bundle_keys.py" in paths

    offenders = []
    for path in paths:
        relative = path.relative_to(_ENGINE_ROOT.parent)
        package = relative.with_suffix("").parts[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{relative}:{line} {name}" for line, name in _store_helper_bindings(tree, package)
        )

    assert not offenders, (
        "These imports bypass the test keyring fake. Use "
        "`from openadapt_desktop.auth import store as _store` and call `_store._keyring()`:\n"
        + "\n".join(offenders)
    )


def test_bundle_keys_round_trip_through_fake_keyring(fake_keyring):
    key = bundle_keys.generate_bundle_key("wf-1")

    assert fake_keyring.get_password(SERVICE_NAME, "bundle-key|wf-1") == key
    assert bundle_keys.load_bundle_key("wf-1") == key
    assert bundle_keys.bundle_key_environment("wf-1") == {"OPENADAPT_BUNDLE_KEY": key}

    bundle_keys.copy_bundle_key("wf-1", "wf-1-v2")
    assert fake_keyring.get_password(SERVICE_NAME, "bundle-key|wf-1-v2") == key

    bundle_keys.delete_bundle_key("wf-1-v2")
    assert bundle_keys.load_bundle_key("wf-1-v2") is None
    assert fake_keyring.get_password(SERVICE_NAME, "bundle-key|wf-1-v2") is None
    assert bundle_keys.load_bundle_key("wf-1") == key


def test_qualification_signer_persists_in_fake_keyring(fake_keyring):
    private_raw, public_key = qualification_keys.qualification_signer()

    stored = fake_keyring.get_password(SERVICE_NAME, "qualification-runner-ed25519-v1")
    assert stored == b64encode(private_raw).decode("ascii")
    assert qualification_keys.qualification_signer() == (private_raw, public_key)
