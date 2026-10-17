"""Secret-isolated auth references for effect-verifier configs.

A deployment YAML (``docs/deployment.example.yaml``) is reviewed, versioned,
and often committed -- so it must NEVER carry a credential literal. An
:class:`AuthRef` names WHERE the secret lives (an environment variable staged
by the operator / secret manager) and this module resolves it at
verifier-construction time, failing LOUD when the variable is absent: a
verifier must never be wired silently unauthenticated, because its every read
would then be a 401 -> INDETERMINATE -> HALT with a misleading reason.

The resolved secret goes straight into request headers held in process memory;
it is never echoed into configs, reports, effect contract hashes (which are
one-way digests of the contract, not the transport), or logs.
"""

from __future__ import annotations

import base64
import os
from typing import Iterator, Mapping, Optional

from pydantic import BaseModel, model_validator


class AuthRef(BaseModel):
    """A reference to an HTTP credential held OUTSIDE the config.

    Exactly one auth style may be set:

    - ``bearer_env`` -- name of an env var holding a bearer token; resolves to
      ``Authorization: Bearer <token>``.
    - ``header`` + ``value_env`` -- an arbitrary auth header (e.g.
      ``X-API-Key``) whose VALUE comes from the named env var. Frappe-style
      ``Authorization: token <key>:<secret>`` fits here too (put the whole
      value in the env var).
    - ``basic_env`` -- name of an env var holding ``user:password``; resolves
      to ``Authorization: Basic <base64>``.

    No field ever holds the secret itself -- only the env var NAME.
    """

    bearer_env: Optional[str] = None
    header: Optional[str] = None
    value_env: Optional[str] = None
    basic_env: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_style(self) -> "AuthRef":
        styles = [
            self.bearer_env is not None,
            self.header is not None or self.value_env is not None,
            self.basic_env is not None,
        ]
        if sum(styles) != 1:
            raise ValueError(
                "auth must use exactly one style: bearer_env | "
                "(header + value_env) | basic_env"
            )
        if (self.header is None) != (self.value_env is None):
            raise ValueError("auth header and value_env must be set together")
        return self

    def resolve_headers(
        self, env: Optional[Mapping[str, str]] = None
    ) -> dict[str, str]:
        """Resolve this reference into request headers.

        Raises:
            ValueError: When the referenced environment variable is absent or
                empty -- fail loud at construction, never wire a verifier that
                would silently read 401s.
        """
        source = os.environ if env is None else env

        def _require(name: str) -> str:
            value = source.get(name, "")
            if not value:
                raise ValueError(
                    f"auth references environment variable {name!r}, which is "
                    "not set (or empty) -- refusing to wire an "
                    "unauthenticated effect verifier"
                )
            return value

        if self.bearer_env is not None:
            return {"Authorization": f"Bearer {_require(self.bearer_env)}"}
        if self.basic_env is not None:
            creds = _require(self.basic_env)
            encoded = base64.b64encode(creds.encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        assert self.header is not None and self.value_env is not None
        return {self.header: _require(self.value_env)}


class ResolvedSecrets(Mapping[str, str]):
    """Credential values resolved from environment-variable references.

    The plugin half of the kit's secret convention: a deployment names, per
    logical slot (``username``, ``password``, ``client_id``), the environment
    variable that holds the value, and a plugin factory resolves them here at
    construction. Missing or empty variables fail LOUD with the variable NAME
    (never a value). ``repr`` / ``str`` show slot names only, so logging the
    mapping, or an object holding it, cannot leak a credential.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    @classmethod
    def from_env(
        cls,
        slots: Mapping[str, str],
        *,
        env: Optional[Mapping[str, str]] = None,
        what: str = "secret_env",
    ) -> "ResolvedSecrets":
        """Resolve ``slot -> ENV_VAR_NAME`` references from ``env``.

        Raises:
            ValueError: When any referenced variable is absent or empty. Every
                missing variable is named, so an operator fixes them in one go.
        """
        source = os.environ if env is None else env
        values: dict[str, str] = {}
        missing: list[str] = []
        for slot, name in slots.items():
            value = source.get(name, "")
            if not value:
                missing.append(f"{slot} ({name})")
            else:
                values[slot] = value
        if missing:
            raise ValueError(
                f"{what} references environment variable(s) that are not set "
                "(or empty): " + ", ".join(missing) + " -- refusing to wire a "
                "record check that cannot authenticate"
            )
        return cls(values)

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"ResolvedSecrets(slots={sorted(self._values)!r})"

    __str__ = __repr__

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("ResolvedSecrets cannot be pickled")
