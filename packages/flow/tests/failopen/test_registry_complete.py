"""Every ``@verdict_site`` has a spec, and every spec names real code.

A site on main is decorated where it is defined and has a ``site`` spec. A
site an open pull request owns has an ``owned`` spec with a strict xfail
until the owner adopts it, decorates it and turns the spec into a ``site``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from scripts.check_verdict_folds import ROOT, decorated_sites, load_config, scope_files
from tests.failopen.harness import load_all

SPECS = load_all()


def _decorated() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for path in scope_files(load_config()):
        for site, qualname in decorated_sites(path).items():
            assert site not in out, f"{site} decorated twice"
            out[site] = (path.relative_to(ROOT).as_posix(), qualname)
    return out


def _resolve(target: str) -> object:
    module, _, qualname = target.partition(":")
    if module.endswith(".py"):
        assert (ROOT / module).is_file(), target
        return None
    obj: object = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_decorated_site_has_a_spec() -> None:
    decorated = _decorated()
    sites = {s.id for s in SPECS.values() if s.kind == "site"}
    assert set(decorated) == sites, (
        f"decorated without a spec: {sorted(set(decorated) - sites)}; "
        f"spec without a decorated function: {sorted(sites - set(decorated))}"
    )


def test_every_site_spec_targets_its_decorated_function() -> None:
    decorated = _decorated()
    for spec in SPECS.values():
        if spec.kind != "site":
            continue
        path, qualname = decorated[spec.id]
        module, _, target_name = spec.target.partition(":")
        expected = (
            module
            if module.endswith(".py")
            else "packages/flow/" + module.replace(".", "/") + ".py"
        )
        if module.split(".")[0] == "openadapt":
            expected = module.replace(".", "/") + ".py"
        assert path == expected, spec.id
        assert qualname == target_name, spec.id
        target = _resolve(spec.target)
        if target is not None:  # a script path is checked by its decorator above
            assert getattr(target, "__verdict_site__", None) == spec.id


def test_owned_and_selftest_targets_exist() -> None:
    for spec in SPECS.values():
        if spec.kind == "owned":
            _resolve(spec.target)
            assert spec.xfail, spec.id
        elif spec.kind == "selftest":
            assert spec.id.startswith("selftest."), spec.id


def test_vendored_files_carry_their_source() -> None:
    selftest = Path(__file__).with_name("selftest")
    for path in sorted(selftest.glob("_v_*.py")):
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# vendored from "), path.name
        assert "for the harness self-test only" in first, path.name


def test_vendored_lines_match_history_when_it_is_available() -> None:
    """Each ``# vendored: sha:path a-b`` block equals those lines at that
    commit (re-indented). CI's shallow checkout has no history: skipped there."""

    import re
    import subprocess
    import textwrap

    import pytest

    marker = re.compile(
        r"# vendored: (\w+):(\S+) (\d+)-(\d+)\n\s*# fmt: off\n(.*?)\n\s*# fmt: on", re.S
    )
    checked = 0
    for path in sorted(Path(__file__).with_name("selftest").glob("_v_*.py")):
        for sha, src, a, b, block in marker.findall(path.read_text(encoding="utf-8")):
            got = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{sha}:{src}"],
                capture_output=True,
                text=True,
            )
            if got.returncode != 0:
                continue
            want = "\n".join(got.stdout.splitlines()[int(a) - 1 : int(b)])
            assert textwrap.dedent(block).rstrip() == textwrap.dedent(want).rstrip(), (
                f"{path.name} {sha}"
            )
            checked += 1
    if not checked:
        pytest.skip("no git history for the vendored commits")
