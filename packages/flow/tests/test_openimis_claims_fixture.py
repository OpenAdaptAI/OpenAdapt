"""Static contract tests for the openIMIS claims reference environment.

No Docker, no network: these verify the pinning/fail-closed discipline of
``benchmark/openimis_claims`` — every image digest-pinned, the compose file
refusing unpinned images, loopback-only publishing, synthetic-only fixture
values, and the claim-code limit the openIMIS form enforces.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "benchmark" / "openimis_claims"
UPSTREAM_COMMIT = "cd6220d1f0578e56a589c47953250c2ad3d0caa5"
AGPL_LICENSE = BENCH_DIR / "conf" / "nginx" / "LICENSE-AGPL-3.0.md"
AGPL_LICENSE_SHA256 = "f5b26d7915d3528f340e14e14abc97518e6235be9a4286fa37cda4c882061fd6"

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


def test_compose_requires_pinned_images_and_binds_loopback() -> None:
    compose = (BENCH_DIR / "compose.yml").read_text()
    for var in (
        "OPENIMIS_BE_IMAGE",
        "OPENIMIS_FE_IMAGE",
        "OPENIMIS_PGSQL_IMAGE",
        "OPENIMIS_REDIS_IMAGE",
        "OPENIMIS_RABBITMQ_IMAGE",
    ):
        assert f"${{{var}:?" in compose, f"compose must fail closed on {var}"
    # No literal image tags: every image comes from a required variable.
    for line in compose.splitlines():
        if "image:" in line:
            assert ":?" in line, f"unpinned literal image line: {line.strip()}"
    # Published ports must bind loopback only.
    for line in compose.splitlines():
        if re.search(r"^\s+- \"?\d|^\s+- \"127", line) and ":80" in line:
            assert "127.0.0.1" in line, f"non-loopback port binding: {line.strip()}"


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


def test_every_adapted_distribution_file_has_exact_license_and_provenance() -> None:
    lock = json.loads((BENCH_DIR / "environment.lock.json").read_text())
    upstream = lock["upstreams"]["openimis-dist_dkr"]
    adapted = upstream["adapted_files"]
    config_root = BENCH_DIR / "conf" / "nginx"
    actual_configs = {
        str(path.relative_to(REPO_ROOT))
        for path in config_root.rglob("*")
        if path.is_file() and path != AGPL_LICENSE
    }
    actual_configs.add(str((BENCH_DIR / "compose.yml").relative_to(REPO_ROOT)))

    assert upstream["commit"] == UPSTREAM_COMMIT
    assert upstream["license"] == "AGPL-3.0-only"
    assert set(adapted) == actual_configs
    for local_path, provenance in adapted.items():
        text = (REPO_ROOT / local_path).read_text()
        source_url = (
            "https://github.com/openimis/openimis-dist_dkr/blob/"
            f"{UPSTREAM_COMMIT}/{provenance['upstream_path']}"
        )
        assert text.startswith("# SPDX-License-Identifier: AGPL-3.0-only\n")
        assert source_url in text
        assert "Full license:" in text
        assert re.fullmatch(r"[0-9a-f]{64}", provenance["upstream_sha256"])
        assert provenance["modification_status"] == "adapted"
        assert (
            hashlib.sha256((REPO_ROOT / local_path).read_bytes()).hexdigest()
            == (provenance["local_sha256"])
        )
        for additional_path, digest in provenance.get(
            "additional_upstream_sources", {}
        ).items():
            assert (
                "https://github.com/openimis/openimis-dist_dkr/blob/"
                f"{UPSTREAM_COMMIT}/{additional_path}"
            ) in text
            assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_complete_upstream_agpl_license_and_notices_are_retained() -> None:
    license_bytes = AGPL_LICENSE.read_bytes()
    # The upstream Markdown file omits a final newline; normalize only that
    # transport-level difference and require every license byte to match.
    assert hashlib.sha256(license_bytes.rstrip(b"\n")).hexdigest() == (
        AGPL_LICENSE_SHA256
    )
    license_text = license_bytes.decode()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 19 November 2007" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text

    notice = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    readme = (BENCH_DIR / "README.md").read_text()
    root_readme = (REPO_ROOT / "README.md").read_text()
    for path in json.loads((BENCH_DIR / "environment.lock.json").read_text())[
        "upstreams"
    ]["openimis-dist_dkr"]["adapted_files"]:
        assert path in notice
        assert path.removeprefix("benchmark/openimis_claims/") in readme
    assert UPSTREAM_COMMIT in notice
    assert "does not relicense those adapted files." in readme
    assert "Git checkout or GitHub-generated source archive" in root_readme
    assert (
        "https://github.com/OpenAdaptAI/openadapt-flow/blob/main/THIRD_PARTY_NOTICES.md"
    ) in root_readme
    assert "Published PyPI wheels and source distributions exclude" in root_readme
