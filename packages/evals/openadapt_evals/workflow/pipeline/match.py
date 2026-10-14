"""Pass 3: Cosine similarity matching of workflows to canonical workflows.

See docs/design/workflow_extraction_pipeline.md Section 4.4.2 for the
design specification.

The matching algorithm is intentionally simple: embed each workflow as a
text string, compute cosine similarity against all canonical workflow
embeddings, and match if similarity > threshold.
"""

from __future__ import annotations

import numpy as np

from openadapt_evals.workflow.models import (
    CanonicalWorkflow,
    Workflow,
    WorkflowInstance,
    WorkflowLibrary,
)

SIMILARITY_THRESHOLD = 0.85


def match_workflow_to_canonical(
    new_workflow: Workflow,
    library: WorkflowLibrary,
    threshold: float = SIMILARITY_THRESHOLD,
) -> str | None:
    """Find the canonical workflow that matches, or return None.

    Returns canonical_id if similarity > threshold, else None.

    Args:
        new_workflow: Workflow with a populated ``embedding`` field.
        library: WorkflowLibrary containing canonical workflows to
            compare against.
        threshold: Minimum cosine similarity to consider a match.
            Defaults to 0.85.

    Returns:
        The ``canonical_id`` of the best-matching canonical workflow,
        or None if no match exceeds the threshold.
    """
    if not library.canonical_workflows:
        return None

    if new_workflow.embedding is None:
        return None

    new_emb = np.array(new_workflow.embedding)

    best_score = -1.0
    best_canonical_id = None

    for canonical in library.canonical_workflows:
        if canonical.embedding is None:
            continue
        canon_emb = np.array(canonical.embedding)

        # Cosine similarity
        norm_product = np.linalg.norm(new_emb) * np.linalg.norm(canon_emb)
        if norm_product == 0:
            continue
        similarity = float(
            np.dot(new_emb, canon_emb) / norm_product
        )

        if similarity > best_score:
            best_score = similarity
            best_canonical_id = canonical.canonical_id

    if best_score >= threshold:
        return best_canonical_id

    return None


def create_canonical_from_workflow(
    workflow: Workflow,
    library: WorkflowLibrary,
) -> CanonicalWorkflow:
    """Create a new singleton CanonicalWorkflow from a Workflow.

    Adds the canonical workflow to the library and returns it.

    Args:
        workflow: Source workflow to promote to canonical.
        library: Library to add the new canonical to.

    Returns:
        The newly created CanonicalWorkflow.
    """
    instance = WorkflowInstance(
        workflow_id=workflow.workflow_id,
        session_id=workflow.session_id,
        similarity_score=1.0,  # Perfect match to itself
        step_count=workflow.step_count,
        duration_seconds=workflow.total_duration_seconds,
    )

    canonical = CanonicalWorkflow(
        name=workflow.name,
        description=workflow.description,
        goal=workflow.goal,
        app_names=workflow.app_names,
        domain=workflow.domain,
        complexity=workflow.complexity,
        tags=list(workflow.tags),
        steps=list(workflow.steps),
        instance_count=1,
        instances=[instance],
        embedding=list(workflow.embedding) if workflow.embedding else None,
        embedding_model=workflow.embedding_model,
        embedding_dim=workflow.embedding_dim,
        avg_similarity=1.0,
        min_similarity=1.0,
        confidence=0.3,  # Low confidence for singleton
    )

    # Link workflow back to canonical
    workflow.canonical_workflow_id = canonical.canonical_id

    library.canonical_workflows.append(canonical)
    return canonical


def add_instance_to_canonical(
    workflow: Workflow,
    canonical_id: str,
    library: WorkflowLibrary,
) -> CanonicalWorkflow | None:
    """Add a workflow as a new instance of an existing canonical workflow.

    Updates the canonical's instance list, count, embedding centroid,
    and quality metrics.

    Args:
        workflow: Workflow to add as an instance.
        canonical_id: ID of the canonical workflow to add to.
        library: Library containing the canonical workflow.

    Returns:
        The updated CanonicalWorkflow, or None if canonical_id not found.
    """
    # Find the canonical workflow
    canonical = None
    for cw in library.canonical_workflows:
        if cw.canonical_id == canonical_id:
            canonical = cw
            break

    if canonical is None:
        return None

    # Compute similarity score for the new instance
    similarity = 1.0
    if (
        workflow.embedding is not None
        and canonical.embedding is not None
    ):
        new_emb = np.array(workflow.embedding)
        canon_emb = np.array(canonical.embedding)
        norm_product = np.linalg.norm(new_emb) * np.linalg.norm(canon_emb)
        if norm_product > 0:
            similarity = float(
                np.dot(new_emb, canon_emb) / norm_product
            )

    # Create instance reference
    instance = WorkflowInstance(
        workflow_id=workflow.workflow_id,
        session_id=workflow.session_id,
        similarity_score=similarity,
        step_count=workflow.step_count,
        duration_seconds=workflow.total_duration_seconds,
    )

    canonical.instances.append(instance)
    canonical.instance_count = len(canonical.instances)

    # Update embedding centroid (average of all instance embeddings)
    if workflow.embedding is not None and canonical.embedding is not None:
        # Weighted average: existing centroid * (n-1)/n + new embedding * 1/n
        n = canonical.instance_count
        canon_emb = np.array(canonical.embedding)
        new_emb = np.array(workflow.embedding)
        updated_emb = (canon_emb * (n - 1) + new_emb) / n
        canonical.embedding = updated_emb.tolist()

    # Update quality metrics
    similarities = [inst.similarity_score for inst in canonical.instances]
    canonical.avg_similarity = float(np.mean(similarities))
    canonical.min_similarity = float(np.min(similarities))
    # Confidence grows with more instances, caps at ~0.95
    canonical.confidence = min(
        0.95, 0.3 + 0.15 * (canonical.instance_count - 1)
    )

    # Link workflow back to canonical
    workflow.canonical_workflow_id = canonical_id

    # Bump version
    canonical.version += 1

    return canonical
