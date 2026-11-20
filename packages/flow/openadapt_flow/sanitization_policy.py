"""Identity of an outbound transformation, independent of its approval.

Provider configuration must describe only semantic, non-secret settings.
Credential values and reviewer redaction literals never belong in this identity.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.sanitization-transformation/v1"
POLICY_REVISION = "outbound-phi-v2"


def identity_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _engine_source_root() -> Path:
    return Path(__file__).resolve().parent


def _source_inventory(root: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    rows = []
    for path in sorted(root.rglob("*.py")):
        stat = path.stat()
        rows.append(
            (
                path.relative_to(root).as_posix(),
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
                stat.st_ino,
            )
        )
    if not rows:
        raise ValueError("engine implementation source is unavailable")
    return tuple(rows)


@lru_cache(maxsize=16)
def _source_snapshot_sha256(
    root: str,
    inventory: tuple[tuple[str, int, int, int, int], ...],
) -> str:
    # Metadata only selects a cached content digest. It never becomes exported
    # identity material. Paths inside the package, and every source byte, bind
    # the digest equally across checkout locations and operating systems.
    return identity_sha256(
        [
            (rel, hashlib.sha256((Path(root) / rel).read_bytes()).hexdigest())
            for rel, *_metadata in inventory
        ]
    )


def _engine_source_sha256() -> str:
    root = _engine_source_root()
    inventory = _source_inventory(root)
    digest = _source_snapshot_sha256(str(root), inventory)
    if inventory != _source_inventory(root):
        raise ValueError("engine implementation changed during identity verification")
    return digest


def _implementation_identity(provider: Any) -> dict[str, Any]:
    # The complete engine source closure covers imported compiler, schema,
    # capture and privacy helpers, including helpers added in future changes.
    # A hand-maintained list could silently leave a new dependency unbound.
    modules = {base.__module__ for base in type(provider).__mro__ if base is not object}
    sources = {}
    for name in sorted(modules):
        module = importlib.import_module(name)
        path = inspect.getsourcefile(module)
        if path is None:
            raise ValueError("sanitizer implementation source is unavailable")
        sources[name] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    methods = {}
    for name in (
        "scrub_text",
        "scrub_image",
        "sanitization_config",
        "sanitization_operation",
    ):
        method = getattr(provider, name, None)
        if method is not None:
            methods[name] = hashlib.sha256(
                inspect.getsource(method).encode()
            ).hexdigest()
    return {
        "engine_source_sha256": _engine_source_sha256(),
        "provider_modules": sources,
        "provider_methods": methods,
    }


def transformation_identity(
    *,
    source_sha256: str,
    kind: str,
    policy_revision: str,
    provider: Any,
    scrubber_version: str,
    manual_overrides: bool = False,
) -> dict[str, Any]:
    """Bind all reuse inputs without exposing private provider state.

    All providers, including stateless injected providers, must implement
    ``sanitization_config()`` returning JSON with their complete
    semantic, non-secret configuration. Runtime caches and credentials must be
    excluded. A missing or unreadable identity refuses outbound publication.
    """
    config_hook = getattr(provider, "sanitization_config", None)
    if not callable(config_hook):
        raise ValueError("scrubbers must declare non-secret sanitization_config()")
    config = config_hook()
    if not isinstance(config, dict):
        raise ValueError("sanitization_config() must return a JSON object")
    return {
        "schema_version": SCHEMA,
        "source_tree_sha256": source_sha256,
        "kind": kind,
        "policy_revision": policy_revision,
        "implementation_sha256": identity_sha256(_implementation_identity(provider)),
        "scrubber": {
            "class": f"{type(provider).__module__}.{type(provider).__qualname__}",
            "version": scrubber_version,
            "config_sha256": identity_sha256(config),
        },
        # Overrides are not reusable. Their private literals are never hashed
        # into a new exported identity. The reviewed output is bound separately.
        "manual_overrides": manual_overrides,
    }
