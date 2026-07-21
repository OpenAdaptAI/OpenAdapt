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
from typing import Mapping, Optional

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
