"""scripts/verify_moved_code.py tells a pure move from an edit."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_moved_code.py"
_spec = importlib.util.spec_from_file_location("verify_moved_code", SCRIPT)
assert _spec and _spec.loader
verify_moved_code = importlib.util.module_from_spec(_spec)
# dataclasses look the module up in sys.modules while the class is built.
sys.modules[_spec.name] = verify_moved_code
_spec.loader.exec_module(verify_moved_code)

PKG = "packages/demo/openadapt_demo"

BASE = '''\
"""Replayer."""
import os

LIMIT = 3


def helper(x):
    return x + LIMIT


class Replayer:
    def __init__(self):
        self.backend = os.name

    def run(self):
        return self._check() and helper(1)

    def _check(self):
        # the identity check
        return len(self.backend) > 0
'''

MOVED_REPLAYER = '''\
"""Replayer."""
import os

from openadapt_demo.replay.identity import LIMIT, IdentityChecks, helper  # noqa: F401


class Replayer(IdentityChecks):
    def __init__(self):
        self.backend = os.name

    def run(self):
        return self._check() and helper(1)
'''

MIXIN = """\
from typing import TYPE_CHECKING, Any, Optional, Protocol

LIMIT = 3


class _IdentityHost(Protocol):
    "What the identity checks read from the host."

    backend: str
    frames: Optional[dict[str, Any]]

    def run(self) -> bool: ...


if TYPE_CHECKING:
    _Base = _IdentityHost
else:
    _Base = object


def helper(x):
    return x + LIMIT


class IdentityChecks(_Base):
    "Identity checks."

    def _check(self):
        return len(self.backend) > 0
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _write(tmp_path, f"{PKG}/replayer.py", BASE)
    _write(tmp_path, f"{PKG}/__init__.py", "")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _verify(repo: Path) -> verify_moved_code.Result:
    return verify_moved_code.verify(repo, "HEAD", ["packages"])


def test_a_pure_move_into_a_mixin_passes(repo: Path) -> None:
    _write(repo, f"{PKG}/replayer.py", MOVED_REPLAYER)
    _write(repo, f"{PKG}/replay/__init__.py", "")
    _write(repo, f"{PKG}/replay/identity.py", MIXIN)

    result = _verify(repo)

    assert result.ok, (result.missing, result.new_bodies)
    assert result.moved == 3  # LIMIT, helper and Replayer._check
    assert any(
        "class Replayer() -> (IdentityChecks)" in c for c in result.changed_bases
    )
    stubs = {unit.qualname for unit in result.new_stubs}
    assert {"_IdentityHost.run"} <= stubs
    # Docstrings and attribute declarations, even with nested types, are stubs.
    assert len(result.new_stubs) == 5
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 0


def test_an_edit_hidden_in_a_move_fails(repo: Path) -> None:
    _write(repo, f"{PKG}/replayer.py", MOVED_REPLAYER)
    _write(repo, f"{PKG}/replay/__init__.py", "")
    _write(repo, f"{PKG}/replay/identity.py", MIXIN.replace("> 0", ">= 0"))

    result = _verify(repo)

    assert [unit.qualname for unit in result.missing] == ["Replayer._check"]
    assert [unit.qualname for unit in result.new_bodies] == ["IdentityChecks._check"]
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 1


def test_new_logic_fails_unless_allowed(repo: Path) -> None:
    _write(repo, f"{PKG}/replayer.py", BASE + "\n\ndef extra():\n    return 1\n")

    result = _verify(repo)

    assert result.missing == []
    assert [unit.qualname for unit in result.new_bodies] == ["extra"]
    args = ["--root", str(repo), "--base", "HEAD"]
    assert verify_moved_code.main(args) == 1
    assert verify_moved_code.main([*args, "--allow-new"]) == 0


def test_a_patch_through_the_old_module_is_flagged(repo: Path) -> None:
    _write(repo, f"{PKG}/replayer.py", MOVED_REPLAYER)
    _write(repo, f"{PKG}/replay/__init__.py", "")
    _write(repo, f"{PKG}/replay/identity.py", MIXIN)
    _write(
        repo,
        "packages/demo/tests/test_demo.py",
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr("openadapt_demo.replayer.helper", lambda x: 0)\n',
    )

    result = _verify(repo)

    assert result.stale_patches == [
        "packages/demo/tests/test_demo.py:2: patches openadapt_demo.replayer.helper; "
        "moved code reads it from openadapt_demo.replay.identity"
    ]
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 1


def test_a_patched_import_the_moved_code_reads_is_flagged_until_retargeted(
    tmp_path: Path,
) -> None:
    """The case the first real move hit: moved code reads an imported name."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    base = BASE.replace("return len(self.backend) > 0", "return len(os.sep) > 0")
    _write(tmp_path, f"{PKG}/replayer.py", base)
    _write(tmp_path, f"{PKG}/__init__.py", "")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _write(tmp_path, f"{PKG}/replayer.py", MOVED_REPLAYER)
    _write(tmp_path, f"{PKG}/replay/__init__.py", "")
    mixin = MIXIN.replace("LIMIT = 3", "import os\n\nLIMIT = 3", 1).replace(
        "return len(self.backend) > 0", "return len(os.sep) > 0"
    )
    _write(tmp_path, f"{PKG}/replay/identity.py", mixin)
    test = (
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr("openadapt_demo.replayer.os", None)\n'
    )
    _write(tmp_path, "packages/demo/tests/test_demo.py", test)

    assert _verify(tmp_path).stale_patches == [
        "packages/demo/tests/test_demo.py:2: patches openadapt_demo.replayer.os; "
        "moved code reads it from openadapt_demo.replay.identity"
    ]

    retargeted = test + (
        '    monkeypatch.setattr("openadapt_demo.replay.identity.os", None)\n'
    )
    _write(tmp_path, "packages/demo/tests/test_demo.py", retargeted)
    assert _verify(tmp_path).stale_patches == []
