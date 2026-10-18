"""The OpenAdapt vocabulary registry: plain words, exact terms, and outcomes.

Two JSON files are the source of truth, so code in any language can read them:

``vocabulary.json``
    The plain business term for each concept, its one-line meaning, and the
    technical terms that may appear only one level deeper.
``outcomes.json``
    How every run outcome OpenAdapt emits maps to one plain result, whether
    anything may have changed, and whether a retry is safe.

:mod:`openadapt_types.plain_results` applies the outcome table in Python.
:mod:`openadapt_types.vocabulary.lint` checks user-facing text against the
registry and needs only the standard library.
"""

from openadapt_types.vocabulary.lint import (
    LEVELS,
    REGISTRY_DIR,
    RESULTS,
    RegistryError,
    known_values,
    load_outcome_table,
    load_outcome_vectors,
    load_registry,
    load_vocabulary,
)

__all__ = [
    "LEVELS",
    "REGISTRY_DIR",
    "RESULTS",
    "RegistryError",
    "known_values",
    "load_outcome_table",
    "load_outcome_vectors",
    "load_registry",
    "load_vocabulary",
]
