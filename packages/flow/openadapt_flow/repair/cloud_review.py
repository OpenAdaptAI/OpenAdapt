"""Send repair candidates to Cloud for a person's review, and take decisions back.

This connects :mod:`openadapt_flow.repair` to Cloud's governed repair review
(``/dashboard/repairs``). It never activates anything:

* :func:`ingest_projection` turns a :class:`RepairCandidate` into the
  digest-only ``openadapt.repair-candidate-ingest/v1`` projection. No path,
  free text, or raw value leaves the machine. ``consequential`` is fail
  closed: unless every changed step is provably reversible, it is true.
* :func:`propose_to_cloud` posts it with the ingest token.
* :func:`apply_cloud_decision` maps a Cloud decision onto the local
  lifecycle. A Cloud **approval is the human diff review**
  (``candidate -> reviewed``), bound to the exact digests the reviewer saw;
  the replay campaign, fault campaign, final human approval, staging, and
  canary all still run locally and fail closed. A Cloud **rejection** rejects
  the candidate. A digest mismatch is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.repair.candidate import Actor, RepairCandidate
from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore

INGEST_SCHEMA = "openadapt.repair-candidate-ingest/v1"
DECISION_SCHEMA = "openadapt.repair-decision/v1"


def _changed_steps_reversible(candidate: RepairCandidate) -> bool:
    """True only when every changed step is loadable and marked reversible."""
    if not candidate.proposed_bundle_path:
        return False
    try:
        from openadapt_flow.ir import Workflow

        workflow = Workflow.load(Path(candidate.proposed_bundle_path))
    except Exception:
        return False
    steps = {str(getattr(step, "id", "")): step for step in workflow.steps}
    for change in candidate.binding_changes:
        step = steps.get(change.step_id)
        if step is None or getattr(step, "risk", None) != "reversible":
            return False
    return True


def ingest_projection(
    candidate: RepairCandidate,
    *,
    workflow_id: str,
    run_id: Optional[str] = None,
    consequential: Optional[bool] = None,
) -> dict[str, Any]:
    """The privacy-safe projection Cloud accepts (closed schema)."""
    if consequential is None:
        consequential = not _changed_steps_reversible(candidate)
    return {
        "schema": INGEST_SCHEMA,
        "candidate_id": candidate.candidate_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "source": candidate.source,
        "prior_content_digest": candidate.prior_content_digest,
        "proposed_content_digest": candidate.proposed_content_digest,
        "consequential": bool(consequential),
        "binding_changes": [
            {
                "step_id": change.step_id,
                "field": change.field,
                "identity": change.identity,
                "old_sha256": change.old_sha256,
                "new_sha256": change.new_sha256,
            }
            for change in candidate.binding_changes
        ],
        "failure_fingerprints": [
            {
                "step_id": fp.step_id,
                "failure_class": fp.failure_class,
                "fingerprint": fp.fingerprint,
            }
            for fp in candidate.failure_fingerprints
        ],
    }


def propose_to_cloud(
    candidate: RepairCandidate,
    *,
    workflow_id: str,
    run_id: Optional[str] = None,
    host: Optional[str] = None,
    token: Optional[str] = None,
    client: Any = None,
) -> dict[str, Any]:
    """POST the projection to ``/api/repairs/ingest``. Returns the response."""
    from openadapt_flow.hosted import HostedError, resolve_host, resolve_token

    resolved_host = resolve_host(host)
    resolved_token = resolve_token(token, host=resolved_host)
    payload = ingest_projection(candidate, workflow_id=workflow_id, run_id=run_id)
    owned = client is None
    if owned:
        import httpx

        client = httpx.Client(timeout=15.0)
    try:
        resp = client.post(
            f"{resolved_host}/api/repairs/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {resolved_token}"},
        )
    finally:
        if owned:
            client.close()
    if resp.status_code not in (200, 201):
        raise HostedError(f"repair ingest returned {resp.status_code}")
    body = resp.json()
    if isinstance(body.get("review_url"), str) and body["review_url"].startswith("/"):
        body["review_url"] = f"{resolved_host}{body['review_url']}"
    return body


def apply_cloud_decision(
    store: RepairStore, decision: Mapping[str, Any]
) -> RepairCandidate:
    """Apply one Cloud decision to the local lifecycle. Fails closed."""
    if decision.get("schema") != DECISION_SCHEMA:
        raise RepairLifecycleError("refused: unsupported Cloud repair decision schema")
    candidate = store.load_candidate(str(decision.get("candidate_id", "")))
    if (
        decision.get("prior_content_digest") != candidate.prior_content_digest
        or decision.get("proposed_content_digest") != candidate.proposed_content_digest
    ):
        raise RepairLifecycleError(
            "refused: the Cloud decision was made for different bundle bytes"
        )
    decided_by = str(decision.get("decided_by") or "").strip()
    if not decided_by:
        raise RepairLifecycleError(
            "refused: a Cloud decision must name the person who decided"
        )
    actor = Actor(kind="human", id=f"cloud:{decided_by}"[:256])
    if decision.get("decision") == "reject":
        if candidate.state == "rejected":
            return candidate
        return store.reject(candidate.candidate_id, actor, "rejected in Cloud review")
    if decision.get("decision") != "approve":
        raise RepairLifecycleError("refused: decision must be approve or reject")
    if candidate.state != "candidate":
        # Already reviewed or further along; a Cloud approval never skips gates.
        return candidate
    return store.review(candidate.candidate_id, actor)


__all__ = ["ingest_projection", "propose_to_cloud", "apply_cloud_decision"]
