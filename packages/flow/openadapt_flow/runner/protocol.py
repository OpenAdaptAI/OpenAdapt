"""Typed, STRICT models of the ``/api/runners/*`` dispatch wire contract.

Field-for-field against ``openadapt-cloud`` ``src/lib/runners.ts``
(``RunnerDispatchPayload`` / ``RunnerDispatch``). The models are strict
(``extra="forbid"``): a payload that carries fields this client does not
understand is CONTRACT DRIFT and must be refused and reported, never
best-effort executed — the whole point of the local verification step is that
the machine only acts on shapes it can fully judge.

``authorization`` reuses the canonical pydantic
:class:`~openadapt_flow.runtime.authorization.GovernedRunAuthorization`
(flow PR #129) — the cloud mints it field-for-field, and this client
re-validates it locally before any GUI is touched.
"""

from __future__ import annotations

from typing import Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.runtime.authorization import GovernedRunAuthorization

#: The only job kind the v1 client executes.
JOB_KIND_GOVERNED_RUN = "governed_run"

#: Cloud lease TTL ceiling (runners.ts DISPATCH_LEASE_TTL_S). There is NO lease
#: renewal endpoint in the merged contract: a run longer than this lands
#: ``dispatch_uncertain`` server-side and this client reports honestly late.
DISPATCH_LEASE_TTL_S = 900

#: Cloud long-poll ceiling (runners.ts POLL_MAX_WAIT_S).
POLL_MAX_WAIT_S = 25


class DispatchParseError(ValueError):
    """A dispatch payload could not be strictly parsed (contract drift)."""


class DispatchBundle(BaseModel):
    """The sealed-bundle reference inside a dispatch.

    ``url`` is parsed for contract fidelity but NEVER dereferenced: v1 has no
    remote code delivery. Execution requires the digest to name a bundle the
    operator already installed and trusted locally.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version_id: str | None = None
    content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    url: str | None = None


class DispatchParamsValues(BaseModel):
    """Cloud-lane runtime params: complete effective NON-PHI scalar values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: dict[str, str]


class DispatchParamsRef(BaseModel):
    """Regulated-lane params-by-reference. Parsed, but refused in v1: the
    local reference resolver does not exist yet, and guessing values would
    break the runtime-inputs digest binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    expected_digest: str = Field(pattern="^[a-f0-9]{64}$")


class RunnerDispatchPayload(BaseModel):
    """The PHI-free dispatch descriptor a runner leases (runners.ts)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_kind: str
    run_id: str
    workflow_id: str
    bundle: DispatchBundle
    deployment_profile_id: str
    authorization: GovernedRunAuthorization
    params: Union[DispatchParamsValues, DispatchParamsRef]
    expires_at: str


class LeasedDispatch(BaseModel):
    """One leased dispatch row as returned by ``POST /api/runners/poll``.

    ``payload`` stays a raw mapping here; :func:`parse_dispatch` upgrades it
    strictly so a malformed payload is a REPORTABLE refusal (we still know the
    dispatch id) rather than a poll-decoding crash.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    org_id: str | None = None
    runner_id: str | None = None
    run_id: str | None = None
    payload: dict[str, object]
    status: str | None = None
    leased_at: str | None = None
    lease_expires_at: str | None = None


def parse_dispatch(payload: dict[str, object]) -> RunnerDispatchPayload:
    """Strictly parse a leased dispatch payload.

    Raises:
        DispatchParseError: on any shape the client cannot fully judge —
            unknown fields, missing fields, malformed digests. The caller
            reports the refusal; it never executes a partially understood
            dispatch.
    """
    try:
        return RunnerDispatchPayload.model_validate(payload)
    except ValidationError as exc:
        raise DispatchParseError(
            f"dispatch payload does not match the runner contract: {exc}"
        ) from exc
