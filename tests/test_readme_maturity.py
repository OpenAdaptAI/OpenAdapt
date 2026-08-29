"""Contract tests for the README Production lifecycle consumer."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_readme_maturity", ROOT / "scripts" / "render_readme_maturity.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _source() -> dict:
    commit = "a" * 40
    return {
        "schema_version": MODULE.SOURCE_SCHEMA,
        "repository": "OpenAdaptAI/openadapt-ops",
        "source_commit": commit,
        "files": {
            key: {
                "path": path,
                "url": (
                    "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-ops/"
                    f"{commit}/{path}"
                ),
                "sha256": "sha256:" + str(index) * 64,
            }
            for index, (key, path) in enumerate(MODULE.EXPECTED_SOURCE_FILES.items(), 1)
        },
    }


def _admission(
    *,
    target: str,
    sequence: int,
    issued_at: str = "2026-08-20T00:00:00Z",
    expires_at: str = "2026-08-30T00:00:00Z",
    revoked_at: str | None = None,
) -> dict:
    return {
        "admission_id": f"production:{target}:{sequence}",
        "target": target,
        "claim_scope": (
            "qualified_workflow_launcher_release"
            if target == "openadapt"
            else "qualified_workflow_runtime_release"
        ),
        "release_identity": {
            "schema_version": "openadapt.monotonic-production-release/v1",
            "channel": "production",
            "sequence": sequence,
            "previous_admission_sha256": None,
        },
        "policy_revision": 1,
        "release": {
            "kind": "public_package",
            "version": "2.0.0",
            "tag": "v2.0.0",
            "source_commit": "b" * 40,
            "immutable_release_url": "https://github.com/OpenAdaptAI/OpenAdapt/releases/tag/v2.0.0",
            "artifacts": [],
        },
        "acceptance_evidence": {
            "summary_url": "https://github.com/OpenAdaptAI/openadapt-evals/raw/"
            + "c" * 40
            + "/summary.json",
            "summary_sha256": "sha256:" + "1" * 64,
            "attestation_bundle_url": "https://github.com/OpenAdaptAI/openadapt-evals/raw/"
            + "c" * 40
            + "/summary.sigstore.json",
            "attestation_bundle_sha256": "sha256:" + "2" * 64,
            "authority_source_commit": "c" * 40,
        },
        "issued_at": issued_at,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }


def _projection(records: list[dict] | None = None) -> dict:
    records = records or []
    targets = []
    for target_id in sorted(MODULE.EXPECTED_TARGETS):
        history = sorted(
            [item for item in records if item["target"] == target_id],
            key=lambda item: item["release_identity"]["sequence"],
        )
        targets.append(
            {
                "id": target_id,
                "display_name": target_id.title(),
                "lifecycle_scope": "repository",
                "lifecycle_subject": target_id,
                "source_repository": f"OpenAdaptAI/{target_id}",
                "release_kind": "public_package",
                "required_claim_scope": (
                    "qualified_workflow_launcher_release"
                    if target_id == "openadapt"
                    else "qualified_workflow_runtime_release"
                ),
                "required_artifact_kinds": ["sdist", "wheel"],
                "package_index_project": target_id,
                "artifact_authority_by_kind": {"sdist": "pypi", "wheel": "pypi"},
                "latest_admission": history[-1] if history else None,
                "admission_history": history,
            }
        )
    return {
        "$schema": MODULE.PROJECTION_JSON_SCHEMA,
        "schema_version": MODULE.PROJECTION_SCHEMA,
        "source": {
            "schema_version": "openadapt.production-lifecycle-source/v1",
            "repository": "OpenAdaptAI/.github",
            "source_commit": "d" * 40,
            "files": {
                key: {
                    "path": path,
                    "url": (
                        "https://raw.githubusercontent.com/OpenAdaptAI/.github/"
                        f"{'d' * 40}/{path}"
                    ),
                    "sha256": "sha256:" + f"{index:x}" * 64,
                }
                for index, (key, path) in enumerate(
                    sorted(MODULE.EXPECTED_CANONICAL_FILES.items()), 1
                )
            },
        },
        "policy_revision": 1,
        "maximum_admission_days": 30,
        "derivation": {
            "mode": "latest_signed_admission_at_read_time",
            "static_production_state": False,
            "expired_or_revoked_latest_behavior": "no_production",
            "fallback_to_older_release": False,
        },
        "targets": targets,
    }


NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


class ReadmeMaturityContractTests(unittest.TestCase):
    def test_source_rejects_a_mutable_url(self) -> None:
        source = _source()
        source["files"]["projection"]["url"] = (
            "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-ops/main/"
            "docs/production-lifecycle.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.MaturityError, "exact commit"):
                MODULE.load_source(path)

    def test_source_digest_mismatch_fails_closed(self) -> None:
        source = _source()
        with self.assertRaisesRegex(MODULE.MaturityError, "digest changed"):
            MODULE.fetch_source_files(source, fetch=lambda _url: b"changed")

    def test_empty_registry_uses_positive_qualification_contract(self) -> None:
        projection = _projection()
        block = MODULE.render_block(projection, {}, now=NOW)

        self.assertIn(MODULE.LIVE_RECORD_URL, block)
        self.assertIn("Production is per qualified workflow", block)
        self.assertIn("exact compiled version", block)
        self.assertIn("expiring, revocable", block)
        self.assertNotIn("Production status", block)
        self.assertNotIn("No current signed admission", block)
        self.assertNotIn("seven product targets", block)
        self.assertNotIn("at least three trials", block)
        self.assertNotIn("silent incorrect", block)
        self.assertNotIn("Beta", block)
        self.assertNotIn("not Production", block)

    def test_an_active_flow_admission_does_not_admit_the_launcher(self) -> None:
        flow = _admission(target="flow", sequence=1)
        projection = _projection([flow])
        block = MODULE.render_block(
            projection,
            {"flow": flow["admission_id"]},
            now=NOW,
        )

        self.assertNotIn(flow["admission_id"], block)
        self.assertIn("Production is per qualified workflow", block)
        self.assertNotIn("Production status", block)
        self.assertNotIn("No current signed admission", block)

    def test_active_launcher_renders_only_a_durable_registry_record(self) -> None:
        launcher = _admission(target="openadapt", sequence=1)
        projection = _projection([launcher])
        block = MODULE.render_block(
            projection,
            {"openadapt": launcher["admission_id"]},
            now=NOW,
        )

        self.assertIn(launcher["admission_id"], block)
        self.assertIn(launcher["issued_at"], block)
        self.assertIn(launcher["expires_at"], block)
        self.assertIn("historical record", block)
        self.assertIn("missing these required target admissions", block)
        self.assertIn("`flow`", block)
        self.assertNotIn("OpenAdapt is Production", block)

    def test_openadapt_only_admission_does_not_imply_combined_production(self) -> None:
        launcher = _admission(target="openadapt", sequence=1)
        projection = _projection([launcher])

        block = MODULE.render_block(
            projection,
            {"openadapt": launcher["admission_id"]},
            now=NOW,
        )

        self.assertIn("OpenAdapt target admission record", block)
        self.assertIn("does not establish combined-product Production", block)
        self.assertIn("missing these required target admissions", block)
        for target in MODULE.EXPECTED_TARGETS - {"openadapt"}:
            self.assertIn(f"`{target}`", block)

    def test_expired_latest_never_falls_back_to_an_older_record(self) -> None:
        old = _admission(
            target="openadapt",
            sequence=1,
            issued_at="2026-08-01T00:00:00Z",
            expires_at="2026-08-29T00:00:00Z",
        )
        latest = _admission(
            target="openadapt",
            sequence=2,
            issued_at="2026-08-02T00:00:00Z",
            expires_at="2026-08-20T12:00:00Z",
        )
        projection = _projection([old, latest])

        self.assertIsNone(MODULE.active_admission(projection, "openadapt", now=NOW))
        block = MODULE.render_block(projection, {}, now=NOW)
        self.assertNotIn(old["admission_id"], block)
        self.assertNotIn(latest["admission_id"], block)

    def test_revoked_latest_never_falls_back_to_an_older_record(self) -> None:
        old = _admission(target="openadapt", sequence=1)
        latest = _admission(
            target="openadapt",
            sequence=2,
            revoked_at="2026-08-20T11:00:00Z",
        )
        projection = _projection([old, latest])

        self.assertIsNone(MODULE.active_admission(projection, "openadapt", now=NOW))
        block = MODULE.render_block(projection, {}, now=NOW)
        self.assertNotIn(old["admission_id"], block)

    def test_replace_block_refuses_missing_or_duplicate_markers(self) -> None:
        with self.assertRaisesRegex(MODULE.MaturityError, "one complete"):
            MODULE.replace_block("no markers", MODULE._qualification_block())
        duplicate = f"{MODULE.BEGIN}\n{MODULE.END}\n{MODULE.BEGIN}\n{MODULE.END}"
        with self.assertRaisesRegex(MODULE.MaturityError, "one complete"):
            MODULE.replace_block(duplicate, MODULE._qualification_block())


if __name__ == "__main__":
    unittest.main()
