"""Registry index rules and verified pull, against a local index copy."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openadapt_flow import registry

ROOT = Path(__file__).resolve().parents[1]


def _local_registry(tmp_path: Path) -> Path:
    dest = tmp_path / "registry"
    shutil.copytree(ROOT / "registry", dest)
    return dest / "index.json"


def test_committed_index_is_current() -> None:
    import runpy

    build = runpy.run_path(str(ROOT / "scripts/build_registry_index.py"))["build"]
    committed = json.loads((ROOT / "registry/index.json").read_text())
    # The release workflow adds each entry's signature and archive.
    for item in committed["workflows"]:
        item.pop("signature", None)
        item.pop("archive", None)
    assert committed == build()


def test_no_entry_claims_certified_without_a_record() -> None:
    entries = registry.load_index(str(ROOT / "registry/index.json"))
    ids = {e.id: e for e in entries}
    assert ids["openemr/new-referral"].status == "example"
    assert ids["openimis/claim-intake"].status == "planned"
    with pytest.raises(registry.RegistryError, match="certification"):
        registry.validate_index(
            {"workflows": [{"id": "a/b", "status": "certified", "files": {"x": "0"}}]}
        )


def test_pull_verifies_and_loads(tmp_path: Path) -> None:
    index = _local_registry(tmp_path)
    dest = registry.pull(
        "openemr/new-referral",
        tmp_path / "out",
        index_url=str(index),
        allow_unsigned=True,
    )
    from openadapt_flow.ir import Workflow

    wf = Workflow.load(dest)
    assert "fname" in wf.param_specs
    with pytest.raises(registry.RegistryError, match="exists"):
        registry.pull(
            "openemr/new-referral", dest, index_url=str(index), allow_unsigned=True
        )


def test_pull_refuses_tampered_file(tmp_path: Path) -> None:
    index = _local_registry(tmp_path)
    wf_json = index.parent / "bundles/openemr/new-referral/workflow.json"
    wf_json.write_text(wf_json.read_text().replace("Elena", "Elenx", 1))
    with pytest.raises(registry.RegistryError, match="sha256"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "out",
            index_url=str(index),
            allow_unsigned=True,
        )
    assert not (tmp_path / "out").exists()


def test_pull_refuses_planned_and_unknown(tmp_path: Path) -> None:
    index = str(_local_registry(tmp_path))
    with pytest.raises(registry.RegistryError, match="planned"):
        registry.pull(
            "openimis/claim-intake",
            tmp_path / "o",
            index_url=index,
            allow_unsigned=True,
        )
    with pytest.raises(registry.RegistryError, match="no workflow"):
        registry.pull("nope/x", tmp_path / "o", index_url=index, allow_unsigned=True)


def test_unsafe_paths_rejected() -> None:
    with pytest.raises(registry.RegistryError, match="unsafe"):
        registry.validate_index(
            {"workflows": [{"id": "a/b", "status": "example", "files": {"../x": "0"}}]}
        )


def test_cli_list_and_pull(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from openadapt_flow.__main__ import main

    index = str(_local_registry(tmp_path))
    assert main(["pull", "--list", "--index", index]) == 0
    out = capsys.readouterr().out
    assert "example, not certified" in out and "planned" in out
    assert (
        main(
            [
                "pull",
                "openemr/new-referral",
                "--allow-unsigned",
                "--index",
                index,
                "--out",
                str(tmp_path / "b"),
            ]
        )
        == 0
    )
    assert "not certified" in capsys.readouterr().out
