"""The candidate's model traffic during a readiness run: scrubbed, blocked or
(opted in) unscrubbed, and metered either way.

Every task's candidate process (``--agent-cmd`` or ``--program``) runs under
one :class:`EgressSession`, built on the flight recorder's
:class:`~openadapt_flow.flight.egress.ScrubbedEgress` (the J7 API):

``scrubbed`` (default)
    The candidate's model calls go through the flight proxy on loopback,
    which scrubs every text and image before forwarding and meters the cost.
    The process runs under the OS egress block (macOS ``sandbox-exec``) and
    may reach only the guarded browser's DevTools port and the proxy. Refuses
    to start when the scrubber or the block is unavailable.
``blocked``
    The candidate may make no model calls at all (a scripted or local
    agent). The same OS block, with the DevTools port the only address it
    may reach; the model base URLs point nowhere. The cost is 0, known
    because nothing could pass. Refuses to start without the block.
``unscrubbed``
    The proxy and the block as in ``scrubbed``, without the scrubber. An
    explicit opt-in for synthetic data only; the scorecard says so.

Cost is metered by the proxy (``model_cost_usd.source: metered``). The
agent's own ``usage.json`` is only a cross-check: when it reports more model
calls than the proxy saw (some went another way), or a direct connection was
trapped, the task is refused (``EGRESS_REFUSED``; its case is
``NOT_CHECKED``, reason ``egress_refused``, counted in ``failed_scope``).

Each task's run folder gets ``egress.json`` (the J7 summary:
``mode, enforced, model_calls, cost_usd, cost_source, refused_requests``)
and the proxy's scrubbed record in ``egress/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "EGRESS_MODES",
    "EgressRefused",
    "EgressSession",
    "metered_cost",
    "preflight",
]

EGRESS_MODES = ("scrubbed", "blocked", "unscrubbed")
#: Where the model base URLs point in ``blocked`` mode: a loopback port the
#: block doesn't allow, so a call fails at once.
_NOWHERE = "http://127.0.0.1:9/blocked"


class EgressRefused(Exception):
    """The candidate can't be run with its model traffic under control."""


def _refused(exc: Exception) -> EgressRefused:
    return EgressRefused(str(exc))


def preflight(mode: str, *, scrubber: Any = None) -> Any:
    """Refuse the whole run before any task when ``mode`` can't be had here.

    Returns the checked scrubber for ``scrubbed`` (``scrubber`` when one is
    given), so each task doesn't load it again; None otherwise."""

    from openadapt_flow.flight.egress import egress_block_available

    if mode not in EGRESS_MODES:
        raise EgressRefused(f"egress must be one of {', '.join(EGRESS_MODES)}")
    ok, why = egress_block_available()
    if not ok:
        raise EgressRefused(
            f"the OS egress block is not available ({why}); the candidate's model "
            "traffic can't be held to the proxy, so nothing was run"
        )
    if mode != "scrubbed":
        return None
    if scrubber is not None:
        return scrubber
    from openadapt_flow.flight.scrub import ScrubberUnavailable, require_scrubber

    try:
        return require_scrubber()
    except ScrubberUnavailable as exc:
        raise EgressRefused(
            f"the scrubber can't run ({exc}); nothing was run"
        ) from None


def metered_cost(summary: Mapping[str, Any]) -> tuple[Optional[float], Optional[str]]:
    """A task's model cost and its source from an egress summary (J7):
    ``(cost_usd, "metered")``, or ``(None, None)`` when the proxy couldn't
    price a call or the session was refused before it ran."""

    cost = summary.get("cost_usd")
    if (
        summary.get("cost_source") == "metered"
        and isinstance(cost, (int, float))
        and not isinstance(cost, bool)
    ):
        return round(float(cost), 6), "metered"
    return None, None


def _reported_calls(usage_path: Path) -> Optional[int]:
    try:
        data = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    calls = data.get("calls") if isinstance(data, dict) else None
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        return None
    return calls


class EgressSession:
    """One task's egress (see the module docstring)."""

    def __init__(
        self,
        mode: str,
        run_dir: Path,
        *,
        cdp_port: int,
        spend_cap_usd: Optional[float] = None,
        upstreams: Optional[Mapping[str, str]] = None,
        scrubber: Any = None,
    ) -> None:
        """``scrubber``: an already checked scrubber (tests); None loads the
        real one, and refuses when it can't run."""
        if mode not in EGRESS_MODES:
            raise EgressRefused(f"egress must be one of {', '.join(EGRESS_MODES)}")
        self.mode = mode
        self.run_dir = Path(run_dir)
        self.cdp_port = int(cdp_port)
        self.spend_cap_usd = spend_cap_usd
        self.upstreams = dict(upstreams or {})
        self.scrubber = scrubber
        self._egress: Any = None
        self._enforced = False
        self._summary: Optional[dict[str, Any]] = None
        self.refusal: Optional[str] = None

    # -- start --------------------------------------------------------------

    def start(self) -> "EgressSession":
        from openadapt_flow.flight import egress as flight

        if self.mode == "blocked":
            ok, why = flight.egress_block_available()
            if not ok:
                raise EgressRefused(f"the OS egress block is not available ({why})")
            self._enforced = True
            return self
        egress = flight.ScrubbedEgress(
            self.run_dir,
            spend_cap_usd=self.spend_cap_usd,
            allow_unenforced=False,
            upstreams=self.upstreams or None,
            scrub=self.mode == "scrubbed",
            scrubber=self.scrubber if self.mode == "scrubbed" else None,
            loopback_ports=[self.cdp_port],
        )
        try:
            egress.__enter__()
        except flight.EgressRefused as exc:
            raise _refused(exc) from None
        self._egress = egress
        self._enforced = bool(egress.enforced)
        return self

    # -- what the candidate gets -------------------------------------------------

    @property
    def env(self) -> dict[str, str]:
        if self._egress is not None:
            return dict(self._egress.env)
        return {
            "ANTHROPIC_BASE_URL": _NOWHERE,
            "OPENAI_BASE_URL": _NOWHERE,
            "HTTPS_PROXY": _NOWHERE,
            "HTTP_PROXY": _NOWHERE,
            "ALL_PROXY": _NOWHERE,
            "https_proxy": _NOWHERE,
            "http_proxy": _NOWHERE,
            "all_proxy": _NOWHERE,
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }

    def wrap(self, argv: list[str]) -> list[str]:
        from openadapt_flow.flight import egress as flight

        if self._egress is not None:
            return self._egress.wrap(list(argv))
        return [
            flight.SANDBOX_EXEC,
            "-p",
            flight.sandbox_profile([self.cdp_port]),
            *argv,
        ]

    # -- the result -----------------------------------------------------------------

    def finish(self) -> dict[str, Any]:
        """Stop the proxy, cross-check ``usage.json``, write ``egress.json``.

        Returns ``{mode, enforced, model_calls, cost_usd, cost_source,
        refused_requests, refused, reason}``: ``mode`` is this session's mode
        (``egress.json`` keeps the flight recorder's own summary)."""

        if self._summary is not None:
            return self._summary
        if self._egress is not None:
            summary = dict(self._egress.finish())
            refusal = self._egress.refusal
        else:
            summary = {
                "mode": "blocked",
                "enforced": self._enforced,
                "model_calls": 0,
                "cost_usd": 0.0,
                "cost_source": "metered",
                "refused_requests": 0,
            }
            reported = _reported_calls(self.run_dir / "usage.json")
            refusal = (
                f"the agent reports {reported} model calls, but under the egress "
                "block none could be made"
                if reported
                else None
            )
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "egress.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        self.refusal = refusal
        self._summary = {
            **summary,
            "mode": self.mode,
            "enforced": self._enforced,
            "refused": refusal is not None,
            "reason": refusal,
        }
        return self._summary
