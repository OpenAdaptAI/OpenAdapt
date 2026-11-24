"""The receipt rule for model calls, split by where the frame went.

A signed record's ``model_calls`` counts only calls that may have sent a frame
off the box: drift-oracle calls, off-box identity-VLM calls and off-box
grounder calls (:func:`openadapt_flow.runtime.model_egress.model_calls_off_box`).
Calls that ran in process are counted on the run report as
``local_model_calls`` and never enter ``model_calls``.

The two placements carry different rules:

* **Off the box.** Any off-box call is model evidence, and the record must say
  ``external_network_calls == "observed"``. A record that counts off-box calls
  and says ``"none"`` is refused, so off-box identity crops can never be
  reported as a run with no external calls.
* **In process.** An in-process call sends nothing over the network, so it
  never requires ``"observed"`` and never makes a record with no off-box calls
  claim model evidence.

The receipt (:class:`openadapt_flow.receipt.RunReceipt`) applies
:func:`model_call_evidence_error`; the terminal verification payload
checks the same two rules in its own validator.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional


def model_call_evidence_error(
    off_box_calls: int, evidence_classes: Iterable[str], network: str
) -> Optional[str]:
    """Why a record's off-box model-call count contradicts it, or None."""

    if (off_box_calls > 0) != ("model" in set(evidence_classes)):
        return "model-call count and evidence class disagree"
    if off_box_calls > 0 and network != "observed":
        return "model calls require observed external network calls"
    return None
