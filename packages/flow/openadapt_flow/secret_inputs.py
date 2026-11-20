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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

__all__ = ["public_values", "secret_environment"]

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

    from openadapt_flow.runtime.replayer import secret_env_vars

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
