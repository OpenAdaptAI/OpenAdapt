"""Scrubbed, metered model egress for an agent process, with an OS egress block.

Use it in-process around any agent you start::

    from openadapt_flow.flight.egress import EgressRefused, ScrubbedEgress

    with ScrubbedEgress(run_dir, spend_cap_usd=5.0) as egress:
        proc = subprocess.run(egress.wrap(argv), env={**os.environ, **egress.env})
    egress.summary()  # also in <run_dir>/egress.json

Importing this module starts nothing; the proxy and the checks run in
``__enter__``.

API (the J7 contract):

``ScrubbedEgress(run_dir, *, spend_cap_usd, allow_unenforced=False, upstreams=None)``
    ``run_dir``: the task's run folder. ``egress.json`` is written there,
    and the proxy's timeline and scrubbed frames go to ``run_dir/egress/``
    (so a folder the caller already uses is not touched otherwise).
    ``spend_cap_usd``: model calls are answered 429 once this much is spent,
    or at once for a model with no price (the cap can't be enforced); None
    for no cap. ``upstreams``: ``{"anthropic": url, "openai": url}``, the
    providers the agent targets (default: the providers' public APIs).

``__enter__`` raises :class:`EgressRefused`, and starts nothing, when

* the scrubber can't run: openadapt-privacy, its spaCy model
  (``en_core_web_sm``), pytesseract or the tesseract binary is missing, or
  a probe scrub of one text and one image fails
  (:func:`openadapt_flow.flight.scrub.require_scrubber`);
* the OS egress block is not available and ``allow_unenforced`` is False.
  The block is macOS ``sandbox-exec`` with a profile that denies every
  outbound connection except to loopback (and DNS lookups); it is checked
  by running a sandboxed connect to a TEST-NET address, which must fail
  with "operation not permitted". There is no block on other systems yet.

``env``
    The variables the agent gets: ``ANTHROPIC_BASE_URL`` and
    ``OPENAI_BASE_URL`` point at the loopback proxy, and ``HTTPS_PROXY`` /
    ``HTTP_PROXY`` / ``ALL_PROXY`` at a trap on the same port (loopback is
    exempt through ``NO_PROXY``). A client that ignores the base URLs but
    honours the proxy variables is refused there, and each attempt is
    counted as a blocked call. Merge it over the agent's own environment.

``wrap(argv)``
    ``argv`` under the egress block (``sandbox-exec -p <profile> ...``), or
    ``argv`` unchanged when the block is not enforced. Everything the agent
    starts inherits the block: a browser it launches reaches loopback only.

``summary()``
    ``{"mode", "enforced", "model_calls", "cost_usd", "cost_source",
    "refused_requests"}``:

    * ``mode``: ``"scrubbed"``; ``"blocked"`` when the run is refused for
      egress outside the proxy (see below);
    * ``enforced``: whether the OS egress block was on;
    * ``model_calls``: model calls that passed the proxy;
    * ``cost_usd`` and ``cost_source``: ``"metered"`` (priced from
      :data:`openadapt_flow.compile_agent.agents.PRICES_PER_MTOK`; 0.0 when
      there were no calls) or ``"unknown"`` (a model has no price; cost
      null);
    * ``refused_requests``: requests the proxy answered without sending
      (could not be scrubbed, over the spend cap) plus trapped direct calls.

``__exit__`` stops the proxy and writes ``egress.json``. It then refuses
the run, by setting ``mode`` to ``"blocked"`` and raising
:class:`EgressRefused` (unless an exception is already on its way), when

* the agent's ``usage.json`` (``openadapt.agent-usage/v1`` in ``run_dir``,
  as the readiness runner reads it) reports more model calls than the proxy
  saw, or
* a direct connection was trapped (a call the block refused).

A connection the kernel block refuses without going through the trap can't
be counted without privileges (macOS does not log these denials for an
unprivileged caller), so it is refused but not counted. Without
``usage.json`` the agent's own count can't be compared; under the block a
bypass still can't reach the network. :attr:`ScrubbedEgress.refusal` holds
the reason, and :attr:`ScrubbedEgress.checks` the numbers behind it.

The scrubber also hides personal data from the agent: the model can't read
a patient name it was asked to check. Task success with scrubbing on has not
been measured.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "SANDBOX_EXEC",
    "EgressRefused",
    "ScrubbedEgress",
    "egress_block_available",
    "sandbox_profile",
]

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_PROBE_ADDRESS = ("192.0.2.1", 9)  # TEST-NET-1 (RFC 5737): never routed
_PROBE = (
    "import socket,sys\n"
    "s=socket.socket();s.settimeout(3)\n"
    "try:\n"
    f"    s.connect({_PROBE_ADDRESS!r})\n"
    "except PermissionError:\n"
    "    sys.exit(0)\n"
    "except OSError:\n"
    "    sys.exit(3)\n"
    "sys.exit(4)\n"
)


class EgressRefused(Exception):
    """The run can't start, or is refused, because egress isn't controlled."""


def sandbox_profile(loopback_ports: Optional[Sequence[int]] = None) -> str:
    """The ``sandbox-exec`` profile: no outbound network except loopback.

    DNS lookups (the mDNSResponder socket) are denied too, so a host name
    can't carry data out; ``localhost`` still resolves from ``/etc/hosts``.
    ``loopback_ports`` narrows loopback to those ports.
    """
    if loopback_ports:
        allowed = "".join(
            f'(allow network-outbound (remote ip "localhost:{int(p)}"))'
            for p in loopback_ports
        )
    else:
        allowed = '(allow network-outbound (remote ip "localhost:*"))'
    return (
        "(version 1)"
        "(allow default)"
        "(deny network-outbound)"
        + allowed
        + "(allow network-outbound (remote unix-socket))"
        '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
    )


def egress_block_available() -> tuple[bool, str]:
    """(True, "") when the OS egress block works here, else (False, why)."""
    if sys.platform != "darwin":
        return False, f"no OS egress block on {sys.platform} yet"
    if not os.path.exists(SANDBOX_EXEC):
        return False, f"{SANDBOX_EXEC} is missing"
    try:
        result = subprocess.run(
            [SANDBOX_EXEC, "-p", sandbox_profile(), sys.executable, "-I", "-c", _PROBE],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"sandbox-exec could not run ({type(exc).__name__})"
    if result.returncode != 0:
        return False, (
            "sandbox-exec did not block a test connection "
            f"(probe exit {result.returncode})"
        )
    return True, ""


def _read_usage_calls(path: Path) -> Optional[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    calls = data.get("calls") if isinstance(data, Mapping) else None
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        return None
    return calls


class ScrubbedEgress:
    """Scrubbed, metered model egress for one agent run (J7)."""

    def __init__(
        self,
        run_dir: Path,
        *,
        spend_cap_usd: Optional[float],
        allow_unenforced: bool = False,
        upstreams: Optional[Mapping[str, str]] = None,
        gate: Any = None,
        timeline: Any = None,
        scrub: bool = True,
        scrubber: Any = None,
        loopback_ports: Optional[Sequence[int]] = None,
        usage_path: Optional[Path] = None,
    ) -> None:
        """The J7 arguments, plus keyword-only extras for ``openadapt-flow
        flight``: ``gate`` (the pre-commit gate the proxy answers for
        :class:`~openadapt_flow.flight.computer.GatedComputer`), ``timeline``
        (record into this Timeline instead of ``run_dir/egress/``),
        ``scrub=False`` with a ``scrubber`` of None (the CLI's recorded
        ``--no-scrub`` opt-out; mode ``"unscrubbed"``), ``loopback_ports``
        and ``usage_path`` (default ``run_dir/usage.json``)."""
        if spend_cap_usd is not None and spend_cap_usd < 0:
            raise ValueError("spend_cap_usd must be zero or more")
        self.run_dir = Path(run_dir)
        self.spend_cap_usd = spend_cap_usd
        self.allow_unenforced = allow_unenforced
        self.upstreams = dict(upstreams or {})
        self.gate = gate
        self.scrub = scrub
        self.loopback_ports = list(loopback_ports or [])
        self.usage_path = (
            Path(usage_path) if usage_path else self.run_dir / "usage.json"
        )
        self._timeline = timeline
        self._scrubber = scrubber
        self._proxy: Any = None
        self.enforced = False
        self.unenforced_reason = ""
        self.refusal: Optional[str] = None
        self.checks: dict[str, Any] = {}
        self._finished: Optional[dict[str, Any]] = None

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "ScrubbedEgress":
        from openadapt_flow.flight.proxy import FlightProxy
        from openadapt_flow.flight.scrub import ScrubberUnavailable, require_scrubber
        from openadapt_flow.flight.timeline import Timeline

        scrubber = self._scrubber
        if self.scrub and scrubber is None:
            try:
                scrubber = require_scrubber()
            except ScrubberUnavailable as exc:
                raise EgressRefused(
                    f"nothing was started: {exc}. Screenshots and text can't be "
                    "scrubbed before they are sent."
                ) from None
        ok, why = egress_block_available()
        if not ok and not self.allow_unenforced:
            raise EgressRefused(
                f"nothing was started: the OS egress block is not available ({why}), "
                "so a call that skips the proxy would leave unscrubbed. Pass "
                "allow_unenforced=True to run without it."
            )
        self.enforced = ok
        self.unenforced_reason = why
        timeline = self._timeline
        if timeline is None:
            from openadapt_flow.privacy import scrub_outbound_text

            def local_text(text: str) -> str:
                return scrub_outbound_text(text, scrubber) or ""

            timeline = Timeline(
                self.run_dir / "egress",
                task_id=self.run_dir.name,
                executor_label="agent",
                scrub_image=None,
                scrub_text=local_text if scrubber is not None else None,
                scrub_record={"mode": "on" if self.scrub else "off"},
            )
            self._timeline = timeline
        try:
            self._proxy = FlightProxy(
                timeline,
                upstreams=self.upstreams,
                scrubber=scrubber,
                scrub=self.scrub,
                max_cost_usd=self.spend_cap_usd,
                gate=self.gate,
            ).start()
        except ValueError as exc:
            raise EgressRefused(f"nothing was started: {exc}") from None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.finish()
        if self.refusal and exc_type is None:
            raise EgressRefused(self.refusal)

    # -- what the agent gets ----------------------------------------------
    @property
    def proxy(self) -> Any:
        if self._proxy is None:
            raise RuntimeError("enter the ScrubbedEgress context first")
        return self._proxy

    @property
    def timeline(self) -> Any:
        return self._timeline

    @property
    def env(self) -> dict[str, str]:
        proxy = self.proxy
        env = proxy.model_env()
        if self.gate is not None:
            from openadapt_flow.flight.computer import GATE_URL_ENV

            env[GATE_URL_ENV] = proxy.gate_url
        return env

    def wrap(self, argv: list[str]) -> list[str]:
        if not self.enforced:
            return list(argv)
        ports = self.loopback_ports
        if ports and self._proxy is not None:
            ports = [*ports, self._proxy.port]
        return [SANDBOX_EXEC, "-p", sandbox_profile(ports or None), *argv]

    # -- the result -------------------------------------------------------
    def finish(self) -> dict[str, Any]:
        """Stop the proxy, run the bypass checks, write ``egress.json``."""
        if self._finished is not None:
            return self._finished
        proxy = self._proxy
        if proxy is not None:
            proxy.stop()
        trapped = list(proxy.trapped) if proxy is not None else []
        calls = self._timeline.calls if self._timeline is not None else 0
        reported = _read_usage_calls(self.usage_path)
        self.checks = {
            "proxy_calls": calls,
            "agent_reported_calls": reported,
            "trapped_direct_calls": len(trapped),
            "trapped_hosts": sorted(set(trapped)),
            "kernel_blocked_calls": None,  # not observable unprivileged
            "enforced": self.enforced,
            "unenforced_reason": self.unenforced_reason or None,
        }
        reasons = []
        if reported is not None and reported > calls:
            reasons.append(
                f"the agent reports {reported} model calls but {calls} passed the "
                "proxy, so some went another way"
            )
        if trapped:
            reasons.append(
                f"{len(trapped)} direct connection(s) outside the proxy were "
                f"refused ({', '.join(sorted(set(trapped)))})"
            )
        self.refusal = "; ".join(reasons) or None
        self._finished = self._summary()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "egress.json").write_text(
            json.dumps(self._finished, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self._finished

    def _summary(self) -> dict[str, Any]:
        timeline = self._timeline
        calls = timeline.calls if timeline is not None else 0
        if timeline is not None and timeline.unpriced_models:
            cost, source = None, "unknown"
        else:
            cost = round(timeline.priced_cost(), 6) if timeline is not None else 0.0
            source = "metered"
        refused = len(timeline.refused_requests) if timeline is not None else 0
        if self.refusal:
            mode = "blocked"
        elif self.scrub:
            mode = "scrubbed"
        else:
            mode = "unscrubbed"
        return {
            "mode": mode,
            "enforced": self.enforced,
            "model_calls": calls,
            "cost_usd": cost,
            "cost_source": source,
            "refused_requests": refused,
        }

    def summary(self) -> dict[str, Any]:
        if self._finished is not None:
            return dict(self._finished)
        return self._summary()
