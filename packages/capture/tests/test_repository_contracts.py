"""Static contracts for repository integration workflows."""

import re
from pathlib import Path


def test_docs_dispatch_targets_canonical_repo_with_pinned_action() -> None:
    """Avoid redirecting a signed dispatch body and pin the secret-using action."""
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "notify-docs.yml"
    ).read_text(encoding="utf-8")

    assert "repository: OpenAdaptAI/openadapt-ops" in workflow
    assert "OpenAdaptAI/openadapt-maintenance" not in workflow
    assert re.search(
        r"uses: peter-evans/repository-dispatch@[0-9a-f]{40}(?:\s+#.*)?$",
        workflow,
        flags=re.MULTILINE,
    )
