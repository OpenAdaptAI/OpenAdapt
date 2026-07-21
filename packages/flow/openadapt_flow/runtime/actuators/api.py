"""REST/JSON :class:`ApiActuator` -- perform a step's write via its API binding.

This is the ``api`` implementation tier of the RFC's transition contract
(``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4: "call the app's API / DB
write; effect probed against the system of record"). Given a step's
:class:`~openadapt_flow.ir.ApiBinding` and the run's typed params, it
substitutes the params into the endpoint / query / body templates and issues a
single deterministic HTTP request -- no pixel matching, no model call, ``$0``.

The hard requirement is **never double-write the same effect**. A naive
"try the API, and on any failure fall back to GUI-clicking the same save"
would, on a request that the server actually PROCESSED (a read-timeout after
commit, a 5xx after a partial write), perform the write TWICE. So the actuator
classifies every attempt into exactly one of three fail-safe outcomes
(:class:`ActuationStatus`), keyed on *whether the request could have reached
the server*:

- :attr:`ActuationStatus.UNAVAILABLE` -- the request was **never sent** (the
  TCP connection was never established: connection refused, DNS failure,
  connect-timeout) or the binding could not even be built (a param the URL/body
  needs was not supplied). Nothing was written, so it is SAFE for the caller to
  fall through to the GUI ladder for this step. This is the "reachable
  ApiBinding" gate: an unreachable endpoint simply is not actuated.
- :attr:`ActuationStatus.ACTUATED` -- the request was sent and the server
  returned success (2xx, or an explicitly-allowed status). The write was
  performed; the caller MUST now confirm it with the EffectVerifier and MUST
  skip the GUI (never re-do the write).
- :attr:`ActuationStatus.HALT` -- the request WAS sent but its outcome is
  unknown or a rejection (read-timeout after the bytes went out, a non-2xx
  response, any post-send transport error). The write MAY have landed, so the
  caller must NEITHER accept it as success NOR GUI-write it again -- it HALTs
  (the same refuse-rather-than-guess posture as the EffectVerifier's
  INDETERMINATE verdict).

The connect-phase / read-phase split is exact in ``requests``:
``ConnectTimeout`` subclasses ``ConnectionError`` (nothing sent -> UNAVAILABLE)
while ``ReadTimeout`` does not (bytes sent -> HALT), so catching
``ConnectionError`` before ``Timeout`` gives the right classification.

Import-light: ``requests`` is imported lazily so importing this module (and the
runtime package) stays cheap and model-free.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel

from openadapt_flow.ir import ApiBinding


class ActuationStatus(str, Enum):
    """The fail-safe outcome of an API actuation attempt (no-double-write)."""

    #: The write was performed and the server acknowledged success -> the
    #: caller confirms it with the EffectVerifier and SKIPS the GUI.
    ACTUATED = "actuated"
    #: The request was never sent (endpoint unreachable, or the binding could
    #: not be built) -> nothing was written; SAFE to fall through to the GUI
    #: ladder for this step.
    UNAVAILABLE = "unavailable"
    #: The request WAS sent but its outcome is unknown or a rejection -> the
    #: write may have landed; HALT (never accept, never GUI-write it again).
    HALT = "halt"


class ApiActuationResult(BaseModel):
    """Outcome of one :meth:`ApiActuator.actuate` call."""

    status: ActuationStatus
    substrate: str = "rest"
    #: Human-readable reason (audit trail / error surface). Contains the
    #: UNSUBSTITUTED method + endpoint template only -- never the substituted
    #: values -- so it is safe to log without leaking PHI-bearing params.
    reason: str = ""
    #: HTTP status code when a response was received; None otherwise.
    http_status: Optional[int] = None
    #: ``"METHOD url_template"`` (unsubstituted) for the audit line.
    request_summary: str = ""

    @property
    def actuated(self) -> bool:
        return self.status is ActuationStatus.ACTUATED

    @property
    def should_fall_through(self) -> bool:
        """True when the caller may safely fall through to the GUI ladder
        (the request was never sent, so nothing was written)."""
        return self.status is ActuationStatus.UNAVAILABLE

    @property
    def should_halt(self) -> bool:
        return self.status is ActuationStatus.HALT


class _MissingParam(KeyError):
    """A template referenced a param the run did not supply."""


class _StrictMap(dict):
    def __missing__(self, key: str) -> Any:  # noqa: D401
        raise _MissingParam(key)


def _fill(template: str, params: dict[str, str]) -> str:
    """Substitute ``{param}`` placeholders in ``template`` from ``params``.

    Raises :class:`_MissingParam` when the template references a key that is
    not in ``params`` -- the binding cannot be built, so the actuator reports
    UNAVAILABLE (a before-send problem: nothing is written, GUI fallback is
    safe) rather than sending a half-formed request.
    """
    return template.format_map(_StrictMap(params))


def _fill_body(node: Any, params: dict[str, str]) -> Any:
    """Recursively substitute ``{param}`` in every string leaf of a JSON body."""
    if isinstance(node, str):
        return _fill(node, params)
    if isinstance(node, dict):
        return {k: _fill_body(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_body(v, params) for v in node]
    return node


class ApiActuator:
    """Perform a step's write via its :class:`~openadapt_flow.ir.ApiBinding`.

    Bound to a deployment's API base URL (and an optional injected session for
    auth headers / tests), mirroring
    :class:`~openadapt_flow.runtime.effects.rest.RestRecordVerifier`. The
    binding's ``url_template`` may be absolute (``http...``) or relative to
    ``base_url``; params from the run substitute into the URL, query, and body
    templates. A single request is issued and classified into the fail-safe
    :class:`ActuationStatus` outcome. Makes ZERO model calls.

    Args:
        base_url: Deployment API base URL (used for relative ``url_template``s;
            trailing slash optional). May be empty when every binding is
            absolute.
        session: Optional ``requests``-style session (auth headers / test
            injection); a module-level default is created lazily when omitted.
        timeout_s: Default per-request timeout when the binding sets none.
    """

    substrate = "rest"

    def __init__(
        self,
        base_url: str = "",
        *,
        session: Any = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout_s = timeout_s
        self._session = session

    def _get_session(self) -> Any:
        if self._session is None:
            import requests  # lazy: keep module import light and model-free

            self._session = requests.Session()
        return self._session

    def _resolve_url(self, url_template: str) -> str:
        if url_template.startswith(("http://", "https://")):
            return url_template
        return f"{self.base_url}{url_template}"

    def actuate(
        self, binding: ApiBinding, params: dict[str, str]
    ) -> ApiActuationResult:
        """Perform ``binding``'s write, substituting ``params``; classify safely.

        Returns an :class:`ApiActuationResult` whose :attr:`status` tells the
        caller exactly one safe next move: confirm-and-skip-GUI (ACTUATED),
        fall-through-to-GUI (UNAVAILABLE, nothing was written), or HALT
        (attempted, outcome unknown -- never double-write). Never raises.
        """
        summary = f"{binding.method} {binding.url_template}"

        # -- build the request (a before-send problem is UNAVAILABLE) ---------
        try:
            url = self._resolve_url(_fill(binding.url_template, params))
            query = {k: _fill(v, params) for k, v in binding.query.items()}
            body = _fill_body(binding.body_template, params)
            headers = {k: _fill(v, params) for k, v in binding.headers.items()}
        except _MissingParam as exc:
            return ApiActuationResult(
                status=ActuationStatus.UNAVAILABLE,
                substrate=self.substrate,
                reason=(
                    f"binding for {summary} references param {exc} not supplied "
                    "by the run -- API tier unavailable, falling through to GUI"
                ),
                request_summary=summary,
            )

        import requests  # lazy; hierarchy: ConnectTimeout is a ConnectionError

        timeout = binding.timeout_s or self.default_timeout_s
        try:
            resp = self._get_session().request(
                binding.method.upper(),
                url,
                params=query or None,
                json=body if body else None,
                headers=headers or None,
                timeout=timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            # Connection never established (refused / DNS / connect-timeout):
            # the request was NEVER sent, so nothing was written -> it is safe
            # to fall through to the GUI ladder for this step.
            return ApiActuationResult(
                status=ActuationStatus.UNAVAILABLE,
                substrate=self.substrate,
                reason=(
                    f"endpoint unreachable ({type(exc).__name__}) -- request "
                    "not sent, API tier unavailable, falling through to GUI"
                ),
                request_summary=summary,
            )
        except requests.exceptions.Timeout as exc:
            # Read-timeout: the bytes WENT OUT and the server may have processed
            # the write. Outcome unknown -> HALT (never GUI-write it again).
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=self.substrate,
                reason=(
                    f"request sent but timed out awaiting the response "
                    f"({type(exc).__name__}) -- the write may have landed; HALT "
                    "(never double-write via the GUI)"
                ),
                request_summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            # Any other transport error after the request left the client is of
            # unknown effect on the server -> HALT rather than risk a duplicate.
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=self.substrate,
                reason=(
                    f"request failed after being sent ({type(exc).__name__}) -- "
                    "outcome unknown; HALT (never double-write via the GUI)"
                ),
                request_summary=summary,
            )

        allowed = binding.expected_status or list(range(200, 300))
        ok = resp.status_code in allowed or (
            not binding.expected_status and resp.status_code // 100 == 2
        )
        if ok:
            return ApiActuationResult(
                status=ActuationStatus.ACTUATED,
                substrate=self.substrate,
                reason=f"{summary} -> {resp.status_code}",
                http_status=resp.status_code,
                request_summary=summary,
            )
        # Non-success response: the request was PROCESSED by the server. Even a
        # clean rejection is ambiguous about what (if anything) persisted, and
        # re-driving the same write through the GUI risks a duplicate -> HALT.
        return ApiActuationResult(
            status=ActuationStatus.HALT,
            substrate=self.substrate,
            reason=(
                f"{summary} returned {resp.status_code} (not success) -- the "
                "write was attempted; HALT (never double-write via the GUI)"
            ),
            http_status=resp.status_code,
            request_summary=summary,
        )
