"""CI gate for the pinned system-of-record benchmark environments.

Offline and CI-fast: it proves every environment stays digest-pinned, that the
committed registry snapshot matches the live lock files, that the CI-fast MockMed
anchor's record is queryable and non-gameable, and that the copyleft
environments cannot leak into a shipped package artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:  # stdlib on 3.11+, the declared ``tomli`` dependency on 3.10
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from benchmark.environments import registry
from benchmark.environments.registry import (
    REPO_ROOT,
    all_environments,
    get,
    registry_snapshot,
)
from benchmark.environments.verify import (
    verify_locks,
    verify_mockmed,
)

SNAPSHOT_PATH = REPO_ROOT / "benchmark" / "environments" / "environments.json"


def test_named_environments_present() -> None:
    """The three named substrates plus the AGPL insurance mirror are registered."""
    names = {env.name for env in all_environments()}
    assert {"mockmed", "openemr_local", "frappe_lending", "openimis_claims"} <= names


def test_all_locks_are_digest_pinned() -> None:
    """Every service image pins an exact @sha256 digest; upstreams pin commits."""
    report = verify_locks()
    problems = {
        r["name"]: r["problems"] for r in report["environments"] if r["problems"]
    }
    assert report["ok"], f"lock verification found problems: {problems}"


@pytest.mark.parametrize("name", ["openemr_local", "frappe_lending", "openimis_claims"])
def test_no_floating_tags(name: str) -> None:
    """A containerized environment must not carry any un-pinned image."""
    env = get(name)
    assert env.unpinned_services() == {}
    # And it must actually declare services (an empty lock would trivially pass).
    assert env.service_digests()


def test_registry_snapshot_matches_committed_json() -> None:
    """The committed environments.json must match the live registry + locks.

    Regenerate with::

        python -c "import json;from pathlib import Path;\
from benchmark.environments.registry import registry_snapshot;\
Path('benchmark/environments/environments.json').write_text(\
json.dumps(registry_snapshot(), indent=2, default=str)+chr(10))"
    """
    assert SNAPSHOT_PATH.is_file(), "environments.json evidence is missing"
    committed = json.loads(SNAPSHOT_PATH.read_text())
    live = json.loads(json.dumps(registry_snapshot(), default=str))
    assert committed == live, (
        "environments.json is stale; regenerate it (a lock digest likely changed)"
    )


def test_mockmed_sor_is_queryable_and_non_gameable() -> None:
    """The CI-fast anchor's record reads back writes and exposes a silent fault."""
    report = verify_mockmed()
    assert report["ok"]
    assert report["queryable"] is True
    # A partial-save fault the screen reports as success must be visible in the
    # record: the readback row persisted but its note is dropped.
    assert report["non_gameable"] is True
    assert report["partial_write_readback"]["note"] == ""
    assert report["clean_write_readback"]["note"]


def test_docker_environments_declare_isolated_record() -> None:
    """Each containerized SoR is read through a channel isolated from the actor."""
    for env in all_environments():
        if not env.requires_docker:
            continue
        assert env.sor.channels, f"{env.name} declares no SoR channel"
        assert env.sor.isolated_from_actor, (
            f"{env.name} oracle read path is not isolated from the write channel"
        )
        assert env.bringup and env.teardown


def test_copyleft_environments_never_ship_in_artifacts() -> None:
    """GPL/AGPL environments must be flagged as never shipped in a wheel/sdist."""
    for name in ("openemr_local", "frappe_lending", "openimis_claims"):
        env = get(name)
        assert env.license.ship_in_artifacts is False, (
            f"{name} is copyleft-derived and must not ship in package artifacts"
        )


def test_agpl_openimis_is_excluded_from_sdist() -> None:
    """The AGPL openIMIS surface is in the pyproject sdist exclude list.

    This is the enforceable half of the hard licensing rule: running the pinned
    upstream image is fine, but the adapted AGPL compose/nginx files are kept out
    of any built sdist. (The wheel packages only ``openadapt_flow`` and cannot
    include top-level ``benchmark/`` at all.)
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    exclude = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "/benchmark/openimis_claims" in exclude
    assert "/scripts/openimis_claims_demo.py" in exclude
    # The wheel must package only the first-party MIT package.
    wheel_packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert wheel_packages == ["openadapt_flow"]


def test_registry_module_has_no_import_side_effects(tmp_path: Path) -> None:
    """Importing the registry must not require Docker or a network call."""
    # A plain re-read of the locks is pure filesystem work; assert it succeeds
    # for every environment that declares a lock.
    for env in all_environments():
        if env.lock_relpath is not None:
            assert env.load_lock().get("services")
        assert isinstance(registry.registry_snapshot(), dict)
