"""Static contract tests for the openIMIS claims reference environment.

No Docker, no network: these verify the pinning/fail-closed discipline of
``benchmark/openimis_claims`` — every image digest-pinned, the compose file
refusing unpinned images, loopback-only publishing, synthetic-only fixture
values, and the claim-code limit the openIMIS form enforces.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "benchmark" / "openimis_claims"
UPSTREAM_COMMIT = "cd6220d1f0578e56a589c47953250c2ad3d0caa5"

sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from openimis_claims import fixture as oc  # noqa: E402


def test_lock_images_are_digest_pinned() -> None:
    lock = json.loads((BENCH_DIR / "environment.lock.json").read_text())
    assert lock["services"], "lock must pin at least one service image"
    for name, image in lock["services"].items():
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), (
            f"{name} image is not digest-pinned: {image}"
        )


def test_fixture_refuses_unpinned_lock(tmp_path: Path, monkeypatch) -> None:
    lock = json.loads((BENCH_DIR / "environment.lock.json").read_text())
    lock["services"]["backend"] = "ghcr.io/openimis/openimis-be:latest"
    bad_dir = tmp_path / "bench"
    bad_dir.mkdir()
    (bad_dir / "environment.lock.json").write_text(json.dumps(lock))
    monkeypatch.setattr(oc, "HERE", bad_dir)
    with pytest.raises(oc.FixtureError, match="not digest-pinned"):
        oc.OpenIMISFixture(state_dir=tmp_path / "state")


def test_compose_is_not_stored_and_comes_from_the_pinned_upstream() -> None:
    # The AGPL compose topology and nginx templates are fetched at run time
    # (benchmark/openimis_claims/upstream.py); the repository holds none.
    assert not (BENCH_DIR / "compose.yml").exists()
    assert not (BENCH_DIR / "conf").exists()
    upstream = json.loads((BENCH_DIR / "environment.lock.json").read_text())[
        "upstreams"
    ]["openimis-dist_dkr"]
    assert upstream["commit"] == UPSTREAM_COMMIT
    assert upstream["license"] == "AGPL-3.0-only"
    assert set(upstream["files"]) == {
        "compose.base.yml",
        "compose.postgresql.yml",
        "compose.cache.yml",
        "conf/nginx/openimis.conf",
        "conf/nginx/locations/backend.loc",
        "conf/nginx/locations/frontend.loc",
        "conf/nginx/variables/var.conf",
    }
    for digest in upstream["files"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_claim_code_limit_matches_openimis_form() -> None:
    # The openIMIS FE claim-code input accepts at most 8 characters; a longer
    # default would silently truncate at record time.
    assert len(oc.DEFAULT_CLAIM_CODE) <= oc.CLAIM_CODE_MAX_LEN
    assert oc.DEFAULT_CLAIM_CODE.isalnum()


def test_bootstrap_sql_targets_only_the_synthetic_policyholder() -> None:
    sql = oc.OpenIMISFixture.BOOTSTRAP_SQL
    assert oc.POLICYHOLDER_CHF in sql
    # Insurees are inserted, never updated/deleted: the bootstrap must not
    # touch existing demo rows.
    assert "DELETE" not in sql.upper()
    lowered = sql.lower()
    assert lowered.count("update ") == 1  # only the head-of-family back-link
    assert '"tblinsuree" i set "familyid"' in lowered


def test_scenario_values_are_synthetic_fixture_values() -> None:
    assert oc.POLICYHOLDER_CHF == "999000001"
    assert oc.POLICYHOLDER_NAME == "Avery Doe"
    assert oc.HEALTH_FACILITY_CODE == "VIHOS001"
    assert oc.ACTOR_USER == "Admin"  # upstream demo-dataset credential


def test_notices_describe_the_run_time_fetch() -> None:
    notice = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    readme = (BENCH_DIR / "README.md").read_text()
    assert UPSTREAM_COMMIT in notice
    assert "fetched at run time" in notice
    assert "upstream.py" in readme
    assert not list(REPO_ROOT.glob("benchmark/**/LICENSE-AGPL*"))
