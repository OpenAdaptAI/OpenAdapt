"""Authenticated loopback operator console over engine artifacts.

The browser receives explicit redacted projections of compiled bundles, run
reports, durable pause/approval state, and versioned skill libraries. It does
not receive raw workflow/report models, protected labels, parameter values, or
local paths. The console invents no new engine semantics:

- Every number it shows is computed by the SAME callables the CLI uses
  (``policy.evaluate_policy`` / ``policy.lint_workflow`` / the identity- and
  effect-coverage helpers in ``openadapt_flow.policy``).
- Every governance ACTION it offers is one of the existing verbs (``teach`` /
  ``approve`` / ``certify`` and the skill library's ``promote`` /
  ``quarantine``), using the same CLI or library entry point. Actions requiring
  deployment-bound inputs remain copy-only command templates.
- The server binds 127.0.0.1 ONLY and starts READ-ONLY: mutating endpoints
  refuse unless the operator opted in with ``--allow-actions``. APIs and
  artifacts require an unguessable fragment-delivered bearer capability;
  mutations additionally require same-origin JSON and a session CSRF token.
- ``console --attend`` opens the redacted Needs Attention queue first. It
  remains read-only by default; explicit action enablement still requires an
  exact engine-issued pause capability and a deployment-bound action service before
  an attended decision can cross a delivery boundary.

Serve it with ``openadapt-flow console`` (requires the ``console`` extra:
``pip install 'openadapt-flow[console]'``).
"""

from __future__ import annotations

__all__ = ["create_app", "serve"]


def __getattr__(name: str):  # PEP 562: keep fastapi/uvicorn imports lazy
    if name == "create_app":
        from openadapt_flow.console.app import create_app

        return create_app
    if name == "serve":
        from openadapt_flow.console.server import serve

        return serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
