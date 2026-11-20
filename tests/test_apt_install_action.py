"""The apt-install composite action's mirror rewriting and package-list parsing.

The network behaviour (fallback, warm cache, total outage) runs on real
runners in .github/workflows/apt-install-selftest.yml; these cover the pure
parts that decide what apt is pointed at.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "apt-install"


def _module():
    spec = importlib.util.spec_from_file_location(
        "apt_install", ACTION / "apt_install.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["apt_install"] = module
    spec.loader.exec_module(module)
    return module


NOBLE_RUNNER = """\
Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

## Security updates.
Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: https://ppa.launchpadcontent.net/example/ppa/ubuntu
Suites: jammy
Components: main
"""

JAMMY_RUNNER = """\
# See sources.list(5).
deb mirror+file:/etc/apt/apt-mirrors.txt jammy main restricted
deb mirror+file:/etc/apt/apt-mirrors.txt jammy-updates main restricted
deb [arch=amd64] http://unreachable.invalid/ubuntu jammy-backports universe
# deb http://archive.ubuntu.com/ubuntu jammy-proposed main
deb http://security.ubuntu.com/ubuntu jammy-security main restricted
deb https://packages.microsoft.com/repos/x stable main
"""


def test_deb822_points_archive_and_security_stanzas_at_their_hosts():
    apt = _module()
    out = apt.rewrite_deb822(NOBLE_RUNNER, "noble", apt.ARCHIVE, apt.SECURITY)
    stanzas = out.split("\n\n")
    assert (
        "URIs: http://archive.ubuntu.com/ubuntu\nSuites: noble noble-updates"
        in stanzas[0]
    )
    assert (
        "URIs: http://security.ubuntu.com/ubuntu\nSuites: noble-security" in stanzas[1]
    )
    assert "## Security updates." in stanzas[1]
    # A stanza for another release (here a PPA) is not the distribution's.
    assert stanzas[2] == NOBLE_RUNNER.split("\n\n")[2]
    assert out.count("Signed-By:") == 2


def test_deb822_mirrors_txt_stage_covers_security_too():
    apt = _module()
    out = apt.rewrite_deb822(NOBLE_RUNNER, "noble", apt.MIRRORS_TXT, apt.MIRRORS_TXT)
    assert out.count("URIs: mirror://mirrors.ubuntu.com/mirrors.txt") == 2


def test_legacy_sources_list_keeps_options_comments_and_third_parties():
    apt = _module()
    out = apt.rewrite_legacy(
        JAMMY_RUNNER, "jammy", apt.ARCHIVE, apt.SECURITY
    ).splitlines()
    assert out[0] == "# See sources.list(5)."
    assert out[1] == "deb http://archive.ubuntu.com/ubuntu jammy main restricted"
    assert (
        out[2] == "deb http://archive.ubuntu.com/ubuntu jammy-updates main restricted"
    )
    assert (
        out[3]
        == "deb [arch=amd64] http://archive.ubuntu.com/ubuntu jammy-backports universe"
    )
    assert out[4] == "# deb http://archive.ubuntu.com/ubuntu jammy-proposed main"
    assert (
        out[5] == "deb http://security.ubuntu.com/ubuntu jammy-security main restricted"
    )
    assert out[6] == "deb https://packages.microsoft.com/repos/x stable main"


def test_fallback_order_and_ports_architectures():
    apt = _module()
    assert [name for name, *_ in apt.stages("amd64")] == [
        "runner default mirror",
        "archive.ubuntu.com and security.ubuntu.com",
        "mirrors.ubuntu.com/mirrors.txt",
    ]
    assert apt.stages("amd64")[0][1:] == (None, None)
    assert apt.stages("arm64")[1][1:] == (apt.PORTS, apt.PORTS)


def test_update_counts_only_distribution_fetch_failures():
    apt = _module()
    third_party = {"packages.microsoft.com"}
    ok = "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"
    assert apt.update_reached_distro(0, ok, third_party)
    assert not apt.update_reached_distro(100, ok, third_party)
    outage = (
        "Err:1 http://azure.archive.ubuntu.com/ubuntu noble InRelease\n"
        "W: Failed to fetch http://azure.archive.ubuntu.com/ubuntu/dists/noble/InRelease\n"
        "W: Some index files failed to download. They have been ignored.\n"
    )
    assert not apt.update_reached_distro(0, outage, third_party)
    vendor = "Err:3 https://packages.microsoft.com/ubuntu/24.04/prod noble InRelease\n"
    assert apt.update_reached_distro(0, ok + vendor, third_party)


def test_dns_failures_are_told_apart():
    apt = _module()
    assert apt.DNS_FAILURE.search(
        "Err:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"
        "  Temporary failure resolving 'archive.ubuntu.com'\n"
    )
    assert not apt.DNS_FAILURE.search(
        "Could not connect to archive.ubuntu.com:80 (91.189.91.83)"
    )


def test_third_party_hosts_skip_the_distribution_file(tmp_path):
    apt = _module()
    (tmp_path / "ubuntu.sources").write_text(NOBLE_RUNNER)
    (tmp_path / "microsoft-prod.list").write_text(
        "deb [arch=amd64,signed-by=/k.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main\n"
    )
    (tmp_path / "docker.sources").write_text(
        "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\n"
    )
    assert apt.third_party_hosts(tmp_path) == {
        "packages.microsoft.com",
        "download.docker.com",
    }


def test_playwright_list_comes_from_the_recorded_install_command(tmp_path):
    apt = _module()
    log = (
        "apt-get\x1fupdate\x1f\n"
        "apt-get\x1finstall\x1f-y\x1f--no-install-recommends\x1flibnss3\x1fxvfb\x1f\n"
    )
    assert apt.parse_shim_log(log) == ["libnss3", "xvfb"]


def test_playwright_resolution_runs_install_deps_with_shims_only(tmp_path):
    """A fake `playwright` module stands in for the pinned release."""
    apt = _module()
    package = tmp_path / "playwright"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import subprocess, sys\n"
        "assert sys.argv[1:] == ['install-deps', 'chromium'], sys.argv\n"
        "subprocess.run(['sudo', '--', 'sh', '-c', 'apt-get update&& apt-get install -y "
        "--no-install-recommends libnss3 libgbm1'], check=True)\n"
    )
    packages = apt.resolve_playwright(sys.executable, "chromium", str(tmp_path))
    assert packages == ["libnss3", "libgbm1"]


def test_plan_writes_a_key_bound_to_image_and_sorted_packages(tmp_path, monkeypatch):
    apt = _module()
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("ImageOS", "ubuntu24")
    monkeypatch.setenv("ImageVersion", "20261102.1")
    monkeypatch.setattr(apt, "_arch", lambda: "amd64")
    apt.main(["plan", "--packages", "xvfb  dbus\nxvfb", "--cache-root", str(tmp_path)])
    first = dict(line.split("=", 1) for line in out.read_text().splitlines())
    out.write_text("")
    apt.main(["plan", "--packages", "dbus xvfb", "--cache-root", str(tmp_path)])
    second = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert first == second
    assert first["packages"] == "dbus xvfb"
    # The exact key ends in the image build; the restore prefix leaves it off,
    # so a newer image can start from an older build's files.
    assert first["key"].startswith("apt-install-v1-ubuntu24-amd64-")
    assert first["key"].endswith("-20261102.1")
    assert first["key"] == first["restore-prefix"] + "20261102.1"


def test_helper_runs_on_the_oldest_runner_python():
    """Jammy's system python3 is 3.10; the helper must not need anything newer."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ast,sys; ast.parse(open(sys.argv[1]).read(), feature_version=(3, 8))",
            str(ACTION / "apt_install.py"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_action_gates_cache_steps_and_reports_the_cause():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    steps = action["runs"]["steps"]
    cache_steps = [s for s in steps if s.get("uses", "").startswith("actions/cache/")]
    assert {s["uses"].split("@")[0] for s in cache_steps} == {
        "actions/cache/restore",
        "actions/cache/save",
    }
    assert all("inputs.use-cache == 'true'" in s["if"] for s in cache_steps)
    assert set(action["outputs"]) >= {"source", "mirror", "reason"}


WORKFLOWS = ROOT / ".github" / "workflows"
SELF = {"apt-install-selftest.yml", "apt-cache-seed.yml"}


def _cached_uses():
    """(workflow, job, with) for every cached apt-install use outside the self-test and seed."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path.name in SELF:
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if step.get("uses") != "./.github/actions/apt-install":
                    continue
                options = step.get("with") or {}
                if str(options.get("use-cache", "true")) == "false":
                    continue
                yield path.name, job_name, options


def _desktop_playwright_version():
    lock = (ROOT / "packages" / "desktop" / "uv.lock").read_text(encoding="utf-8")
    return lock.split('name = "playwright"\nversion = "', 1)[1].split('"', 1)[0]


def test_every_cached_package_set_has_a_seed_entry():
    seed = yaml.safe_load(
        (WORKFLOWS / "apt-cache-seed.yml").read_text(encoding="utf-8")
    )
    entries = seed["jobs"]["seed"]["strategy"]["matrix"]["include"]
    seeded = {
        (
            " ".join(sorted(str(e.get("packages", "")).split())),
            e["recommends"],
            e.get("playwright", ""),
        )
        for e in entries
    }
    uses = list(_cached_uses())
    assert len(uses) >= 7
    for workflow, job, options in uses:
        playwright = ""
        if options.get("playwright-deps"):
            assert options["playwright-deps"] == "chromium"
            python = options.get("playwright-python", "")
            playwright = (
                _desktop_playwright_version()
                if "uv run --locked" in python
                else "latest"
            )
        wanted = (
            " ".join(sorted(str(options.get("packages", "")).split())),
            str(options.get("install-recommends", "false")),
            playwright,
        )
        assert wanted in seeded, (workflow, job, wanted)
