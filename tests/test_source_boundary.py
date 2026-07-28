"""The source-availability guard must fail closed and must never get weaker.

The guard reads its rules from `source-policy.public.json`, which is rendered
from the canonical private manifest. Two properties matter and are pinned here:

1. Fail closed. A missing, unparseable, incomplete, or unknown-schema policy
   stops the build with exit code 2. A guard that passes with no rules is worse
   than a hardcoded list, because everyone believes it ran.
2. Never weaker. `LEGACY_DENYLIST` is the hand-copied denylist this guard
   carried before the rules moved into the manifest. It is a one-way ratchet: a
   rule may be added, but a rule that once blocked something must keep blocking
   it. Failing this test means the change removed protection.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_source_boundary as guard  # noqa: E402

# The exact denylist this script carried on 2026-07-28, before it derived its
# rules from the manifest. Ratchet only: never delete an entry to make a test
# pass.
LEGACY_DENYLIST = (
    "powerchart",
    "cerner",
    "millennium_recipe",
    "epic_hyperspace",
    "meditech",
    "customer_fixture",
    "customer_recipe",
    "client_fixture",
    "deployment_corpus",
    "deployment_thresholds",
    "real_emr",
    "grown_corpus",
    "tuned_adversary",
    "adversary_corpus",
    "identity_roc",
    "held_out_corpus",
    "oracle_recipe",
    "effect_oracle_recipe",
    "pixel_verify_cert",
    "enterprise_productionized",
    "paid_agent_evidence",
    "openadapt-corpus",
    "openadapt-cloud",
    "openadapt-internal",
)

LEGACY_CONTENT_PATTERNS = (
    "powerchart",
    "cerner millennium",
    "epic hyperspace",
    "openadapt-corpus/",
    "deployment-derived threshold =",
    "oracle-recipe-id: 7",
)


@pytest.fixture()
def policy() -> guard.SourcePolicy:
    return guard.load_policy()


def _repo(tmp_path: Path, relative: str, content: str = "placeholder\n") -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", relative], cwd=tmp_path, check=True)
    return tmp_path


def _write_policy(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The repository this guard defends is clean, and the guard says so.
# --------------------------------------------------------------------------


def test_this_repository_passes() -> None:
    assert guard.main.__module__  # import smoke
    completed = subprocess.run(
        [sys.executable, "scripts/check_source_boundary.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


# --------------------------------------------------------------------------
# Not weaker: every legacy rule still blocks.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token", LEGACY_DENYLIST)
def test_legacy_path_token_still_blocked(
    tmp_path: Path, policy: guard.SourcePolicy, token: str
) -> None:
    root = _repo(tmp_path, f"src/{token}_fixture.json", "{}\n")
    violations = guard.scan(root, policy)
    assert violations, f"{token} no longer fails the path scan"


@pytest.mark.parametrize("phrase", LEGACY_CONTENT_PATTERNS)
def test_legacy_content_pattern_still_blocked(
    tmp_path: Path, policy: guard.SourcePolicy, phrase: str
) -> None:
    root = _repo(tmp_path, "docs/notes.md", f"a line mentioning {phrase} here\n")
    violations = guard.scan(root, policy)
    assert violations, f"content {phrase!r} no longer fails the content scan"


@pytest.mark.parametrize("token", LEGACY_DENYLIST)
def test_legacy_tokens_are_present_in_the_rendered_policy(
    policy: guard.SourcePolicy, token: str
) -> None:
    assert token in policy.path_tokens


def test_every_policy_token_is_actually_enforced(
    tmp_path: Path, policy: guard.SourcePolicy
) -> None:
    for index, token in enumerate(policy.path_tokens):
        root = tmp_path / f"case{index}"
        root.mkdir()
        _repo(root, f"data/{token}/payload.txt")
        assert guard.scan(root, policy), f"policy token {token!r} is not enforced"


def test_private_path_segment_is_blocked(
    tmp_path: Path, policy: guard.SourcePolicy
) -> None:
    root = _repo(tmp_path, "private/notes.txt")
    assert guard.scan(root, policy)


def test_private_artifact_banner_is_blocked(
    tmp_path: Path, policy: guard.SourcePolicy
) -> None:
    banner = policy.content_signatures[0]
    root = _repo(tmp_path, "docs/benign_name.md", f"header\n{banner}\nbody\n")
    violations = guard.scan(root, policy)
    assert violations and "banner" in violations[0]


def test_a_clean_tree_passes(tmp_path: Path, policy: guard.SourcePolicy) -> None:
    root = _repo(tmp_path, "src/module.py", "def f():\n    return 1\n")
    assert guard.scan(root, policy) == []


# --------------------------------------------------------------------------
# Fail closed: no rules means no run.
# --------------------------------------------------------------------------


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_source_boundary.py", *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_missing_policy_fails_closed(tmp_path: Path) -> None:
    completed = _run(["--policy", str(tmp_path / "absent.json")])
    assert completed.returncode == 2
    assert "cannot read the rendered source policy" in completed.stderr


def test_unparseable_policy_fails_closed(tmp_path: Path) -> None:
    broken = tmp_path / "policy.json"
    broken.write_text("{not json", encoding="utf-8")
    completed = _run(["--policy", str(broken)])
    assert completed.returncode == 2
    assert "not valid JSON" in completed.stderr


def test_empty_rules_fail_closed(tmp_path: Path) -> None:
    document = json.loads(guard.POLICY_PATH.read_text(encoding="utf-8"))
    document["enforcement"]["path_tokens"] = []
    completed = _run(["--policy", str(_write_policy(tmp_path, document))])
    assert completed.returncode == 2
    assert "path_tokens" in completed.stderr


def test_unknown_schema_fails_closed(tmp_path: Path) -> None:
    document = json.loads(guard.POLICY_PATH.read_text(encoding="utf-8"))
    document["schema_version"] = 99
    completed = _run(["--policy", str(_write_policy(tmp_path, document))])
    assert completed.returncode == 2
    assert "unknown policy schema" in completed.stderr


def test_missing_enforcement_block_fails_closed(tmp_path: Path) -> None:
    document = json.loads(guard.POLICY_PATH.read_text(encoding="utf-8"))
    del document["enforcement"]
    completed = _run(["--policy", str(_write_policy(tmp_path, document))])
    assert completed.returncode == 2
    assert "enforcement" in completed.stderr


def test_unclassified_repository_fails_closed() -> None:
    completed = _run(["--repo", "repository-with-no-manifest-entry"])
    assert completed.returncode == 2
    assert "not classified as a public repository" in completed.stderr


def test_this_repository_is_classified_public(policy: guard.SourcePolicy) -> None:
    assert "OpenAdapt" in policy.public_repositories
    assert policy.public_repositories["OpenAdapt"]["classification"] == "public"
    assert policy.public_repositories["OpenAdapt"]["must_not_contain"]
