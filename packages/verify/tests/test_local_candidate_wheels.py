"""Clean-install checks must select the locally built first-party artifacts."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

spec = spec_from_file_location(
    "candidate_install_check",
    Path(__file__).parents[1] / "scripts" / "check_clean_install.py",
)
assert spec and spec.loader
check = module_from_spec(spec)
spec.loader.exec_module(check)


def test_same_version_local_wheels_are_direct_requirements(tmp_path):
    names = ("openadapt_types", "openadapt_flow", "openadapt_verify")
    paths = []
    for name in names:
        path = tmp_path / f"{name}-0.1.0-py3-none-any.whl"
        path.touch()
        paths.append(str(path.resolve()))
    assert check.local_candidate_wheels([str(tmp_path)]) == sorted(paths)


def test_missing_or_ambiguous_first_party_build_is_refused(tmp_path):
    (tmp_path / "openadapt_verify-0.1.0-py3-none-any.whl").touch()
    with pytest.raises(ValueError, match="exactly one"):
        check.local_candidate_wheels([str(tmp_path)])
    for name in ("openadapt_types", "openadapt_flow"):
        (tmp_path / f"{name}-0.1.0-py3-none-any.whl").touch()
    (tmp_path / "openadapt_verify-0.2.0-py3-none-any.whl").touch()
    with pytest.raises(ValueError, match="exactly one"):
        check.local_candidate_wheels([str(tmp_path)])


def test_remote_links_leave_the_normal_install_requirement_intact():
    assert check.local_candidate_wheels(["https://example.test/wheels/"]) == []
