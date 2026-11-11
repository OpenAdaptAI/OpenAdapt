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
    # Docstrings and attribute declarations, even with nested types, are stubs,
    # and so are both branches of the ``if TYPE_CHECKING`` base alias.
    assert len(result.new_stubs) == 7
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


def _init(tmp_path: Path, files: dict[str, str]) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _fails_as_new_code(repo: Path, qualname_part: str) -> None:
    result = _verify(repo)
    assert any(qualname_part in u.qualname for u in result.new_bodies), (
        result.new_bodies,
        result.new_stubs,
    )
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 1


def test_code_in_a_type_checking_else_branch_is_new_code(repo: Path) -> None:
    """The ``else`` branch runs at import time; it is not skipped with the block."""
    added = (
        "\nfrom typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    import json\nelse:\n    os.getcwd()\n"
    )
    _write(repo, f"{PKG}/replayer.py", BASE + added)
    _fails_as_new_code(repo, "<statement line")


def test_code_in_a_type_checking_branch_must_survive_a_move(tmp_path: Path) -> None:
    base = BASE + (
        "\nfrom typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    import json\nelse:\n    os.getcwd()\n"
    )
    repo = _init(tmp_path, {f"{PKG}/replayer.py": base, f"{PKG}/__init__.py": ""})
    _write(repo, f"{PKG}/replayer.py", BASE)

    assert [u.qualname for u in _verify(repo).missing] == ["<statement line 27>"]


def test_a_call_behind_an_attribute_is_not_a_reexport(repo: Path) -> None:
    _write(repo, f"{PKG}/replayer.py", BASE + "\nexported = os.getcwd().upper\n")
    _fails_as_new_code(repo, "<statement line")


def test_a_dotted_name_is_still_a_reexport(repo: Path) -> None:
    _write(repo, f"{PKG}/replayer.py", BASE + "\nexported = os.path.join\n")

    result = _verify(repo)

    assert result.ok, result.new_bodies
    assert len(result.new_stubs) == 1


@pytest.mark.parametrize(
    "added",
    [
        "@os.getcwd()\ndef stub(): ...\n",
        "def stub(x=os.getcwd()): ...\n",
        "def stub() -> os.getcwd(): ...\n",
        "class Made(os.getcwd()):\n    x: int\n",
        "class Made(metaclass=os.getcwd()):\n    x: int\n",
        "class Made:\n    x: os.getcwd()\n",
    ],
    ids=["decorator", "default", "returns", "base", "metaclass", "annotation"],
)
def test_a_call_in_a_stub_or_class_header_is_new_code(repo: Path, added: str) -> None:
    """Stubs and declarations are allowed only while they run no code."""
    _write(repo, f"{PKG}/replayer.py", BASE + "\n\n" + added)
    result = _verify(repo)
    assert result.new_bodies, result.new_stubs
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 1


TWINS = """\
class First:
    def same(self):
        return 1


class Second:
    def same(self):
        return 1
"""


def test_a_surviving_identical_copy_does_not_hide_a_deletion(
    tmp_path: Path,
) -> None:
    repo = _init(tmp_path, {f"{PKG}/twins.py": TWINS, f"{PKG}/__init__.py": ""})
    _write(repo, f"{PKG}/twins.py", TWINS.split("\n\n\n")[0] + "\n")

    result = _verify(repo)

    assert [u.qualname for u in result.missing] == ["Second.same"]
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 1


def test_identical_copies_moved_to_two_files_pass(tmp_path: Path) -> None:
    repo = _init(tmp_path, {f"{PKG}/twins.py": TWINS, f"{PKG}/__init__.py": ""})
    first, second = TWINS.split("\n\n\n")
    _write(repo, f"{PKG}/twins.py", "from .a import First\nfrom .b import Second\n")
    _write(repo, f"{PKG}/a.py", first + "\n")
    _write(repo, f"{PKG}/b.py", second + "\n")

    result = _verify(repo)

    assert result.ok, (result.missing, result.new_bodies)
    assert result.moved == 2


# One item of each unit kind, to move between the branches of a
# ``TYPE_CHECKING`` block. Each branch keeps an anchor that never moves, so a
# failure can only come from the item.
BRANCH_ITEMS = {
    "statement": ("", "    FOUND = os.getcwd()\n"),
    "function": ("", "    def found():\n        return 1\n"),
    "method": (
        "    class Host:\n        'host'\n",
        "    class Host:\n        'host'\n\n        def found(self):\n"
        "            return 1\n",
    ),
}


def _branches(if_body: str, else_body: str) -> str:
    return (
        "import os\nfrom typing import TYPE_CHECKING\n\n"
        f"if TYPE_CHECKING:\n    ANCHOR = 1\n{if_body}"
        f"else:\n    ANCHOR = 2\n{else_body}"
    )


def _branch_case(tmp_path: Path, kind: str, direction: str, moved: bool) -> Path:
    plain, with_item = BRANCH_ITEMS[kind]
    if direction == "if->else":
        base = _branches(with_item, plain)
        after = _branches(plain, with_item) if moved else base
    else:
        base = _branches(plain, with_item)
        after = _branches(with_item, plain) if moved else base
    repo = _init(tmp_path, {f"{PKG}/branches.py": base, f"{PKG}/__init__.py": ""})
    # A re-export keeps the file changed in the controls too.
    _write(repo, f"{PKG}/branches.py", after + "\nALIAS = os.sep\n")
    return repo


CASES = [(k, d) for k in BRANCH_ITEMS for d in ("if->else", "else->if")]


@pytest.mark.parametrize("kind, direction", CASES)
def test_code_moved_between_type_checking_branches_fails(
    tmp_path: Path, kind: str, direction: str
) -> None:
    """Which branch code sits in decides whether it runs."""
    repo = _branch_case(tmp_path, kind, direction, moved=True)

    result = _verify(repo)

    assert not result.ok
    assert result.missing, result.new_bodies
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 1


@pytest.mark.parametrize("kind, direction", CASES)
def test_code_kept_in_its_type_checking_branch_passes(
    tmp_path: Path, kind: str, direction: str
) -> None:
    repo = _branch_case(tmp_path, kind, direction, moved=False)

    result = _verify(repo)

    assert result.ok, (result.missing, result.new_bodies)
    assert verify_moved_code.main(["--root", str(repo), "--base", "HEAD"]) == 0
