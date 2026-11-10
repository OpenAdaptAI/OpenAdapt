"""Which record checks may run on a side thread while the next item runs."""

from __future__ import annotations

from typing import Any


def _pipeline_safe_verifier(verifier: Any) -> bool:
    """Whether a read-back may run on a side thread while the next item runs.

    Only verifiers that read the system of record over the network and never
    touch the screen: REST, FHIR, GraphQL and SQL. Anything that binds to a
    backend (on-screen read-backs, and the redacting and candidate wrappers,
    which forward ``bind_backend``) or any other kind verifies inline.
    """

    if (
        verifier is None
        or hasattr(verifier, "bind_backend")
        or getattr(verifier, "backend", None) is not None
        or getattr(verifier, "_backend", None) is not None
    ):
        return False
    safe: list[type] = []
    for load, name in (
        (
            lambda: importlib.import_module("openadapt_flow.runtime.effects.rest"),
            "RestRecordVerifier",
        ),
        (
            lambda: importlib.import_module("openadapt_flow.runtime.effects.fhir"),
            "FhirEffectVerifier",
        ),
        (
            lambda: importlib.import_module("openadapt_flow.runtime.effects.graphql"),
            "GraphQLRecordVerifier",
        ),
        (
            lambda: importlib.import_module("openadapt_flow.runtime.effects.sql"),
            "SqlRecordVerifier",
        ),
    ):
        try:
            import importlib

            safe.append(getattr(load(), name))
        except Exception:  # noqa: BLE001 - an unavailable kind is not safe
            continue
    return isinstance(verifier, tuple(safe))
