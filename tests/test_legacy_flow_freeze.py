"""Tests for scripts/check_legacy_flow_freeze.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_legacy_flow_freeze as freeze  # noqa: E402


def _make_synthetic_repo(tmp_path: Path) -> Path:
    manifest = {
        "deleted_directories": ["form_tx"],
        "frozen_directories": {
            "tx": ["tx/__init__.py", "tx/receipt.py"],
            "txplan": ["txplan/__init__.py"],
        },
        "frozen_root_shadow_modules": ["shadow.py"],
        "frozen_flow_cli_commands": ["compile", "plan", "replay"],
        "allowed_launcher_imports": {
            "openadapt/cli.py": ["txplan"],
            "openadapt/receipt_verify.py": ["tx"],
        },
    }
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts/legacy_flow_freeze_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    flow_pkg = tmp_path / "packages/flow/openadapt_flow"
    (flow_pkg / "tx").mkdir(parents=True)
    (flow_pkg / "tx/__init__.py").write_text("", encoding="utf-8")
    (flow_pkg / "tx/receipt.py").write_text("", encoding="utf-8")
    (flow_pkg / "txplan").mkdir(parents=True)
    (flow_pkg / "txplan/__init__.py").write_text("", encoding="utf-8")
    (flow_pkg / "shadow.py").write_text("", encoding="utf-8")

    snap_dir = tmp_path / "packages/flow/tests/fixtures"
    snap_dir.mkdir(parents=True)
    (snap_dir / "cli_surface.json").write_text(
        json.dumps(
            {
                "tree": {
                    "openadapt-flow": {
                        "commands": {"compile": [], "plan": [], "replay": []}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    (tmp_path / "openadapt").mkdir(parents=True)
    (tmp_path / "openadapt/cli.py").write_text(
        'MODULES = ("openadapt_flow.txplan.cli",)\n', encoding="utf-8"
    )
    (tmp_path / "openadapt/receipt_verify.py").write_text(
        "from openadapt_flow.tx.receipt import verify_tx_receipt\n", encoding="utf-8"
    )

    kernel_src = tmp_path / "packages/kernel/src/openadapt_kernel"
    kernel_src.mkdir(parents=True)
    (kernel_src / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def test_real_repo_passes_legacy_flow_freeze() -> None:
    assert freeze.check_all(ROOT) == []
    assert freeze.main(["--root", str(ROOT)]) == 0


def test_deleting_frozen_module_or_stack_is_allowed(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    (repo / "packages/flow/openadapt_flow/tx/receipt.py").unlink()
    (repo / "packages/flow/openadapt_flow/txplan/__init__.py").unlink()
    (repo / "packages/flow/openadapt_flow/txplan").rmdir()
    assert freeze.check_all(repo) == []


def test_adding_new_module_in_frozen_stack_fails(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    (repo / "packages/flow/openadapt_flow/tx/new_feature.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    violations = freeze.check_all(repo)
    assert len(violations) == 1
    assert "tx/new_feature.py" in violations[0]


def test_adding_new_shadow_module_fails(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    (repo / "packages/flow/openadapt_flow/shadow_v2.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    violations = freeze.check_all(repo)
    assert len(violations) == 1
    assert "shadow_v2.py" in violations[0]


def test_adding_new_flow_cli_command_fails(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    snap_path = repo / "packages/flow/tests/fixtures/cli_surface.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["tree"]["openadapt-flow"]["commands"]["brand-new-cmd"] = []
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    violations = freeze.check_all(repo)
    assert len(violations) == 1
    assert "brand-new-cmd" in violations[0]


def test_adding_new_launcher_import_of_frozen_stack_fails(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    (repo / "openadapt/helper.py").write_text(
        "from openadapt_flow.tx import engine\n", encoding="utf-8"
    )
    violations = freeze.check_all(repo)
    assert len(violations) == 1
    assert "openadapt/helper.py" in violations[0]
    assert "openadapt_flow.tx" in violations[0]


def test_kernel_importing_openadapt_flow_fails(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    (repo / "packages/kernel/src/openadapt_kernel/bad.py").write_text(
        "import openadapt_flow.tx\n", encoding="utf-8"
    )
    violations = freeze.check_all(repo)
    assert len(violations) == 1
    assert "openadapt_flow.tx" in violations[0]


def test_recreating_deleted_stack_fails(tmp_path: Path) -> None:
    repo = _make_synthetic_repo(tmp_path)
    (repo / "packages/flow/openadapt_flow/form_tx").mkdir(parents=True)
    violations = freeze.check_all(repo)
    assert len(violations) == 1
    assert "form_tx/" in violations[0]
