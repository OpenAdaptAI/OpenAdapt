"""Supply a program's secret parameters to one local run, from memory.

A secret parameter (``ir.Step.secret``, such as a sign-in password) is typed
at replay from ``OPENADAPT_FLOW_SECRET_<NAME>``; the bundle, its parameters
and every record keep only the name. :func:`secret_environment` sets those
variables from values the caller holds in memory for the length of one run,
then restores what was there, so a local canary or cached run can sign in
without the value being written anywhere. Hosted runners set the same
variables from the organization's vault instead.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

__all__ = ["public_values", "secret_environment"]


class CredentialObservationUnsupported(ValueError):
    """Credential withholding needs an observable field classification seam."""

    code = "CAPTURE_CREDENTIAL_OBSERVATION_UNSUPPORTED"

    def __init__(self) -> None:
        super().__init__(
            f"{self.code}: this backend cannot observe credential fields; "
            "credential-withholding capture cannot start"
        )


def credential_withholding_requested(value: bool) -> bool:
    """A trusted caller option cannot be a truthy task-data value."""
    if type(value) is not bool:
        raise TypeError("withhold_credentials must be a bool")
    return value


def require_credential_observation(backend: Any) -> None:
    """Refuse missing capabilities before credential-withholding capture."""
    try:
        supported = callable(getattr(backend, "focused_field_credential", None))
    except Exception:
        supported = False
    if not supported:
        raise CredentialObservationUnsupported()


# A secret TYPE step's value is never stored in the bundle; it is read at
# replay from this environment variable (the param name upper-cased, with
# non-alphanumeric characters mapped to '_'). See ir.Step.secret.
SECRET_ENV_PREFIX = "OPENADAPT_FLOW_SECRET_"


def secret_env_var(param: str) -> str:
    """Environment variable a secret parameter's value is read from."""
    key = "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in param).upper()
    return f"{SECRET_ENV_PREFIX}{key}"


def secret_env_vars(params: Sequence[str]) -> dict[str, str]:
    """Require an injective mapping from secret names to environment keys.

    Case and punctuation normalization is lossy. Distinct parameters must
    never silently share a credential slot, even when values happen to match.
    """
    keys: dict[str, str] = {}
    owners: dict[str, str] = {}
    for name in sorted(set(params)):
        key = secret_env_var(name)
        for alias in (name, key):
            if alias in owners and owners[alias] != name:
                raise ValueError("secret parameter names collide in the environment")
            owners[alias] = name
        keys[name] = key
    return keys


def require_secret_sources(
    params: Mapping[str, Any], secret_params: Sequence[str]
) -> None:
    """Reject aliases and any ordinary-input source for a declared secret."""
    secret_env_vars(secret_params)
    if set(params) & set(secret_params):
        raise ValueError("secret parameters must come from their secret source")


#: One run at a time may change this process's secret variables.
_LOCK = threading.RLock()


def public_values(values: Mapping[str, Any], secret_params: Any) -> dict[str, Any]:
    """``values`` without the secret parameters."""

    secret = set(secret_params or ())
    return {name: value for name, value in values.items() if name not in secret}


@contextmanager
def secret_environment(values: Mapping[str, Any], secret_params: Any) -> Iterator[None]:
    """Expose each named secret's in-memory value for one run, then restore.

    A secret with no non-empty value in ``values`` is left as the
    environment has it (an operator may export it instead). The values are
    never logged or returned.
    """

    slots = secret_env_vars(list(secret_params or ()))
    supplied = {
        key: str(values[name])
        for name, key in slots.items()
        if values.get(name) not in (None, "")
    }
    if not supplied:
        yield
        return
    with _LOCK:
        previous = {key: os.environ.get(key) for key in supplied}
        os.environ.update(supplied)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
