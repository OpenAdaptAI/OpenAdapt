"""Throwaway real lab keys; none are copied into test evidence or source."""

from pathlib import Path

import pytest

from openadapt_flow import teach_authorization as ta
from openadapt_flow.integrations.agents.lab_authority import LabAuthorityError, create_lab_authorities
from openadapt_flow.jit import authority


def test_lab_handles_use_normal_registered_signatures_without_approving_a_task(tmp_path):
    tmp_path.chmod(0o700)
    handles = create_lab_authorities(tmp_path / "lab")
    assert handles.operator.principal == "lab-demo"
    assert handles.admitter.principal_id == "lab-demo:admitter"
    assert handles.admitter.kind == "admitter_key"
    assert (handles.key_dir / "admitter.pem").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob(ta.STATEMENT_FILE))
    payload = {"admitted_by_principal_id": handles.admitter.principal_id, "admitter_public_key": handles.admitter.public_key}
    assert authority.verify_admitter(payload, handles.admitter.sign(payload), handles.key_dir) == ("admitter_key", handles.admitter.principal_id)
    # Only the explicit caller's next operation signs this exact synthetic
    # scope. The bootstrap did not invent inputs, scope, approval or consent.
    spec = {
        "task": {"task_id": "lab.synthetic", "summary": "Synthetic lab task"},
        "purposes": ["teach", "repair"], "origins": ["http://127.0.0.1:9"],
        "inputs": {"value": {"value": "synthetic"}}, "constants": [],
        "writes": {"record_kind": "note", "identity": {"type": {"value": "note"}}, "fields": {"value": {"value": "synthetic"}}},
    }
    statement = handles.operator.issue(tmp_path / "explicit-scope", spec)
    verification = ta.require(statement, purpose="teach", key_dir=handles.key_dir, root=handles.operator.root)
    assert verification.statement is not None
    assert verification.statement.authorizer == "lab-demo"


@pytest.mark.parametrize("case", ["existing", "dangling", "parent_alias", "git", "shared_parent", "relative"])
def test_bootstrap_refuses_nonfresh_or_unowned_source_scope_before_keys(tmp_path, case):
    tmp_path.chmod(0o700)
    parent = tmp_path / "private"; parent.mkdir(mode=0o700)
    root = parent / "lab"
    if case == "existing": root.mkdir()
    elif case == "dangling": root.symlink_to(parent / "absent")
    elif case == "parent_alias":
        alias = tmp_path / "alias"; alias.symlink_to(parent, target_is_directory=True); root = alias / "lab"
    elif case == "git": (parent / ".git").write_text("source-checkout-marker")
    elif case == "shared_parent": parent.chmod(0o755)
    else: root = Path("relative-lab")
    with pytest.raises(LabAuthorityError, match="^LAB_AUTHORITY_UNAVAILABLE$"):
        create_lab_authorities(root)
    assert not list(tmp_path.rglob("admitter.pem"))


def test_bootstrap_never_reuses_or_overwrites_an_existing_lab_identity(tmp_path):
    tmp_path.chmod(0o700)
    root = tmp_path / "lab"
    handles = create_lab_authorities(root)
    before = (handles.key_dir / "admitter.pem").stat()
    with pytest.raises(LabAuthorityError): create_lab_authorities(root)
    after = (handles.key_dir / "admitter.pem").stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_size) == (after.st_ino, after.st_mtime_ns, after.st_size)
