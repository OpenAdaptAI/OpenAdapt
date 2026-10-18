"""The customer's own model key for the recording run (BYOK).

The recording run is the only part of the JIT path that calls a model, and
the customer's agent loop makes those calls. The key it uses must be the
customer's. :func:`load_customer_model_key` reads it from the customer's
environment or from a config file the customer names, and nowhere else. There
is no OpenAdapt-held key to fall back to: when the customer's key is missing,
loading fails and the recording does not start.

The key itself is never written to a candidate, a bundle, or a log. The
candidate records where the key came from and a fingerprint of it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

__all__ = [
    "PROVIDER_ENV",
    "CustomerModelKey",
    "MissingModelKey",
    "load_customer_model_key",
]

#: The environment variable each provider's own SDK reads.
PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class MissingModelKey(RuntimeError):
    """The customer's model key was not found. Nothing falls back."""


@dataclass(frozen=True)
class CustomerModelKey:
    """A model key the customer supplied, and where it came from.

    ``source`` is ``env:<NAME>`` or ``config:<path>``. ``repr`` and
    :meth:`provenance` never include the key.
    """

    provider: str
    source: str
    _secret: str = field(repr=False)

    def secret(self) -> str:
        """The key, for the agent loop's model client."""

        return self._secret

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self._secret.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:16]}"

    def provenance(self) -> dict[str, str]:
        """What the candidate records: provider, source, fingerprint."""

        return {
            "provider": self.provider,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "held_by": "customer",
        }


def load_customer_model_key(
    provider: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path | str] = None,
) -> CustomerModelKey:
    """Load the customer's key for ``provider``.

    Looks in two places, in order:

    1. The provider's own environment variable (see :data:`PROVIDER_ENV`).
    2. ``config_path``, when given: a JSON file the customer controls, shaped
       ``{"anthropic": {"api_key": "..."}}``.

    Args:
        provider: ``anthropic`` or ``openai``.
        env: The environment to read; defaults to ``os.environ``.
        config_path: The customer's key file. Not read unless given.

    Raises:
        MissingModelKey: the key is in neither place. No other source is
            tried, and no OpenAdapt-held key exists to try.
        ValueError: an unknown provider, or a config file that isn't the
            expected shape.
    """

    if provider not in PROVIDER_ENV:
        raise ValueError(
            f"unknown provider {provider!r} (expected one of {sorted(PROVIDER_ENV)})"
        )
    environ = os.environ if env is None else env
    name = PROVIDER_ENV[provider]
    value = (environ.get(name) or "").strip()
    if value:
        return CustomerModelKey(provider, f"env:{name}", value)
    if config_path is not None:
        path = Path(config_path).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} is not JSON: {exc}") from exc
            entry = data.get(provider) if isinstance(data, dict) else None
            if entry is not None and not isinstance(entry, dict):
                raise ValueError(f"{path}: {provider!r} must be an object")
            value = str((entry or {}).get("api_key") or "").strip()
            if value:
                return CustomerModelKey(provider, f"config:{path}", value)
    where = f"set {name}"
    if config_path is not None:
        where += f" or add {provider}.api_key to {config_path}"
    raise MissingModelKey(
        f"no {provider} key for the recording run: {where}. The recording run "
        "uses your own model key; OpenAdapt does not supply one."
    )
