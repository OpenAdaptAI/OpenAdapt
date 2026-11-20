"""Identity of an outbound transformation, independent of its approval.

Provider configuration must describe only semantic, non-secret settings.
Credential values and reviewer redaction literals never belong in this identity.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.sanitization-transformation/v1"
POLICY_REVISION = "outbound-phi-v2"


def identity_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _implementation_identity(provider: Any) -> dict[str, Any]:
    modules = {
        "openadapt_flow.sanitization_policy",
        "openadapt_flow.sanitized_artifact",
        "openadapt_flow.privacy",
        "openadapt_flow.evidence_files",
        "openadapt_flow.report_redaction",
    }
    modules.update(
        base.__module__ for base in type(provider).__mro__ if base is not object
    )
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
    return {"modules": sources, "methods": methods}


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
