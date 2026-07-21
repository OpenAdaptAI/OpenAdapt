"""Actuators: PERFORM a step's write through a non-GUI channel.

The runtime's default actuator is the GUI: it resolves the recorded target on
the live screen (:mod:`openadapt_flow.runtime.resolver`) and clicks / types
through the :class:`~openadapt_flow.backend.Backend`. That is the FLOOR -- it
works on any pixel surface (RDP/Citrix/canvas) -- but it is also the *weakest*
and most expensive way to effect a change: where the target app exposes a real
API, driving its GUI to make the same write is the wrong tool.

This package adds the TOP of the capability ladder (RFC
``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4, the ``api`` implementation of
a ``TransitionContract``): when a step carries an
:class:`~openadapt_flow.ir.ApiBinding`, perform the write by CALLING the API
deterministically -- $0, zero model calls -- and confirm it with the same
:class:`~openadapt_flow.runtime.effects.EffectVerifier` that gates a GUI write.
The GUI resolution ladder is then SKIPPED for that step. A step with no binding
(or with no actuator configured) behaves EXACTLY as before -- the API tier is
additive and falls through to the structural -> visual ladder.

Public surface:

- :class:`ApiActuator` -- the REST/JSON actuator (and the shape a FHIR/MCP/tool
  actuator slots into).
- :class:`ApiActuationResult`, :class:`ActuationStatus` -- the fail-safe
  outcome of an actuation attempt (the no-double-write contract).
"""

from openadapt_flow.runtime.actuators.api import (  # noqa: F401
    ActuationStatus,
    ApiActuationResult,
    ApiActuator,
)

__all__ = [
    "ApiActuator",
    "ApiActuationResult",
    "ActuationStatus",
]
