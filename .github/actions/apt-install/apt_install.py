#!/usr/bin/env python3
"""Install Ubuntu packages on a CI runner without depending on one mirror.

Used by the composite action in this directory. Stdlib only, Python 3.8+, so
it runs on the runner's system python3 before any setup-python step.

Subcommands:

  plan      Resolve the package list (including a pinned Playwright's own
            browser dependency list) and print the cache key and directory.
  install   Install the packages from cached .deb files first, with no
            `apt-get update` and no network. Only packages the cache did not
            cover come from the network, trying the runner's default mirror,
            then archive.ubuntu.com and security.ubuntu.com, then
            mirror://mirrors.ubuntu.com/mirrors.txt.

The install subcommand writes outputs to $GITHUB_OUTPUT and a result record to
--result, and ends with a GitHub ::error:: annotation that names the cause when
it fails, so a mirror outage reads differently from a test failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

APT_CONF = Path("/etc/apt/apt.conf.d/80-openadapt-apt-install")
APT_CONF_TEXT = """\
// Written by .github/actions/apt-install: retry and bound every download.
Acquire::Retries "5";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
Acquire::ftp::Timeout "20";
"""

# The two files that hold the distribution's own archive entries. Third-party
# sources (PPAs, vendor repositories) live in other files and are never touched.
DEB822_SOURCES = Path("/etc/apt/sources.list.d/ubuntu.sources")
LEGACY_SOURCES = Path("/etc/apt/sources.list")

ARCHIVE = "http://archive.ubuntu.com/ubuntu"
SECURITY = "http://security.ubuntu.com/ubuntu"
PORTS = "http://ports.ubuntu.com/ubuntu-ports"
MIRRORS_TXT = "mirror://mirrors.ubuntu.com/mirrors.txt"

UPDATE_TIMEOUT = 150
DOWNLOAD_TIMEOUT = 420

DNS_FAILURE = re.compile(
    r"Temporary failure resolving|Could not resolve|Name or service not known"
)

NOT_FOUND = re.compile(
    r"Unable to locate package|has no installation candidate|"
    r"Couldn't find any package by"
)


# --------------------------------------------------------------------------
# Source rewriting (pure functions; tested in tests/test_apt_install_action.py)
# --------------------------------------------------------------------------


def _is_distro_suite(suite: str, codename: str) -> bool:
    return suite == codename or suite.startswith(codename + "-")


def _target(suites: list[str], archive: str, security: str) -> str:
    if suites and all(suite.endswith("-security") for suite in suites):
        return security
    return archive


def rewrite_deb822(text: str, codename: str, archive: str, security: str) -> str:
    """Point every distribution stanza of a deb822 .sources file at a mirror.

    A stanza is the distribution's when each of its suites is the release
    codename or codename-<pocket>. Security-only stanzas get `security`; the
    rest get `archive`. Comments, other fields and other stanzas are kept.
    """
    out: list[str] = []
    for stanza in re.split(r"(\n\s*\n)", text):
        if not stanza.strip() or re.fullmatch(r"\n\s*\n", stanza):
            out.append(stanza)
            continue
        lines = stanza.split("\n")
        suites: list[str] = []
        for line in lines:
            match = re.match(r"^Suites:\s*(.*)$", line, re.IGNORECASE)
            if match:
                suites = match.group(1).split()
        if not suites or not all(_is_distro_suite(s, codename) for s in suites):
            out.append(stanza)
            continue
        uri = _target(suites, archive, security)
        out.append(
            "\n".join(
                re.sub(
                    r"^(URIs:\s*).*$",
                    lambda m: m.group(1) + uri,
                    line,
                    flags=re.IGNORECASE,
                )
                for line in lines
            )
        )
    return "".join(out)


def rewrite_legacy(text: str, codename: str, archive: str, security: str) -> str:
    """Point every distribution line of a one-line sources.list at a mirror."""
    out = []
    for line in text.split("\n"):
        match = re.match(
            r"^(\s*deb(?:-src)?\s+)(\[[^\]]*\]\s+)?(\S+)(\s+)(\S+)(.*)$", line
        )
        if not match or not _is_distro_suite(match.group(5), codename):
            out.append(line)
            continue
        uri = _target([match.group(5)], archive, security)
        out.append(
            match.group(1)
            + (match.group(2) or "")
            + uri
            + match.group(4)
            + match.group(5)
            + match.group(6)
        )
    return "\n".join(out)


FAILED_FETCH = re.compile(r"(?:^Err:\d+\s+|Failed to fetch\s+)(\S+)", re.MULTILINE)


def update_reached_distro(code: int, output: str, third_party: set[str]) -> bool:
    """Whether `apt-get update` refreshed the distribution's own indexes.

    apt exits 0 when an index fails to download and keeps the old one, so
    the exit code alone cannot tell. A failed fetch from a third-party
    repository (one listed outside the distribution's source files) is not a
    mirror failure and is ignored here, as plain `apt-get update` would.
    """
    if code != 0:
        return False
    for url in FAILED_FETCH.findall(output):
        if urlparse(url).hostname not in third_party:
            return False
    return True


def third_party_hosts(root: Path = Path("/etc/apt/sources.list.d")) -> set[str]:
    """Hosts of the repositories listed outside the distribution's source files."""
    hosts: set[str] = set()
    paths = sorted(root.glob("*.list")) + sorted(root.glob("*.sources"))
    for path in paths:
        if path.name == DEB822_SOURCES.name:
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        uris: list[str] = []
        for line in text.splitlines():
            one_line = re.match(r"^\s*deb(?:-src)?\s+(?:\[[^\]]*\]\s+)?(\S+)", line)
            if one_line:
                uris.append(one_line.group(1))
            field = re.match(r"^URIs:\s*(.*)$", line, re.IGNORECASE)
            if field:
                uris.extend(field.group(1).split())
        hosts.update(host for host in (urlparse(uri).hostname for uri in uris) if host)
    return hosts


def stages(arch: str) -> list[tuple[str, str | None, str | None]]:
    """(name, archive URI, security URI) in the order to try; None keeps the default."""
    if arch in ("amd64", "i386"):
        return [
            ("runner default mirror", None, None),
            ("archive.ubuntu.com and security.ubuntu.com", ARCHIVE, SECURITY),
            ("mirrors.ubuntu.com/mirrors.txt", MIRRORS_TXT, MIRRORS_TXT),
        ]
    # Other architectures are served only from ports.ubuntu.com.
    return [
        ("runner default mirror", None, None),
        ("ports.ubuntu.com", PORTS, PORTS),
    ]


# --------------------------------------------------------------------------
# Playwright dependency list
# --------------------------------------------------------------------------

SHIM = """#!/bin/sh
# Records the command instead of running it.
printf '%s\\037' "$(basename "$0")" "$@" >> "$OPENADAPT_APT_SHIM_LOG"
printf '\\n' >> "$OPENADAPT_APT_SHIM_LOG"
"""

SUDO_SHIM = """#!/bin/sh
# Runs the command without elevation so the apt-get shim records it.
if [ "$1" = "--" ]; then shift; fi
exec "$@"
"""


def parse_shim_log(text: str) -> list[str]:
    """The package names from the recorded `apt-get install` invocation."""
    packages: list[str] = []
    for line in text.splitlines():
        args = [arg for arg in line.split("\x1f") if arg]
        if len(args) < 2 or args[0] != "apt-get" or "install" not in args:
            continue
        after = args[args.index("install") + 1 :]
        packages.extend(arg for arg in after if not arg.startswith("-"))
    return packages


def resolve_playwright(python_cmd: str, target: str, cwd: str) -> list[str]:
    """Ask the pinned Playwright which packages it would install, offline.

    Runs `<python> -m playwright install-deps <target>` with `apt-get`, `sudo`
    and `su` shimmed so the command is recorded, not run. The list comes from
    the Playwright release the caller pinned, and nothing touches the network.
    (`install-deps --dry-run` would run `apt-get install -s`, which needs
    downloaded package indexes, so it is not used.)
    """
    with tempfile.TemporaryDirectory() as tmp:
        shim_dir = Path(tmp)
        log = shim_dir / "calls.log"
        log.write_text("")
        for name, body in (("apt-get", SHIM), ("sudo", SUDO_SHIM), ("su", SHIM)):
            path = shim_dir / name
            path.write_text(body)
            path.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        env["OPENADAPT_APT_SHIM_LOG"] = str(log)
        command = shlex.split(python_cmd) + ["-m", "playwright", "install-deps", target]
        result = subprocess.run(
            command,
            cwd=cwd or None,
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
        packages = parse_shim_log(log.read_text())
        if result.returncode != 0 or not packages:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(
                f"::error title=Playwright dependency list::could not read the package list "
                f"from `{' '.join(command)}` (exit {result.returncode}); this is a CI setup "
                "problem, not a mirror outage"
            )
        return packages


# --------------------------------------------------------------------------
# Runner side effects
# --------------------------------------------------------------------------


def _sudo(command: list[str]) -> list[str]:
    return command if os.geteuid() == 0 else ["sudo", "-n", *command]


def _run(command: list[str], timeout: int | None = None) -> tuple[int, str]:
    print("+ " + " ".join(shlex.quote(part) for part in command), flush=True)
    try:
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.output or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        print(output, flush=True)
        print(f"timed out after {timeout}s", flush=True)
        return 124, output
    print(proc.stdout, end="", flush=True)
    return proc.returncode, proc.stdout


def _write_root(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(text)
    _run(_sudo(["install", "-m", "0644", handle.name, str(path)]))
    os.unlink(handle.name)


def _os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value.strip().strip('"')
    except OSError:
        pass
    return data


def _arch() -> str:
    return (
        subprocess.run(
            ["dpkg", "--print-architecture"], text=True, capture_output=True
        ).stdout.strip()
        or "amd64"
    )


def _installed_names() -> set[str]:
    """Installed package names, plus every name they provide (virtual packages)."""
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Status}\t${Package}\t${Provides}\n"],
        text=True,
        capture_output=True,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        status, _, rest = line.partition("\t")
        if status != "installed":
            continue
        package, _, provides = rest.partition("\t")
        names.add(package)
        for item in provides.split(","):
            name = item.strip().split(" ", 1)[0]
            if name:
                names.add(name)
    return names


def _missing(packages: list[str]) -> list[str]:
    installed = _installed_names()
    return [p for p in packages if p.split("=", 1)[0].split(":", 1)[0] not in installed]


class Sources:
    """The runner's original distribution sources, and rewrites of them."""

    def __init__(self) -> None:
        self.codename = _os_release().get("VERSION_CODENAME", "")
        self.original = {
            path: path.read_text()
            for path in (DEB822_SOURCES, LEGACY_SOURCES)
            if path.is_file()
        }

    def apply(self, archive: str | None, security: str | None) -> None:
        for path, text in self.original.items():
            if archive is None or security is None:
                new = text
            elif path.suffix == ".sources":
                new = rewrite_deb822(text, self.codename, archive, security)
            else:
                new = rewrite_legacy(text, self.codename, archive, security)
            if new != path.read_text():
                _write_root(path, new)


def _github_output(values: dict[str, str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _finish(
    result: Path | None, values: dict[str, str], error: str | None = None
) -> int:
    _github_output(values)
    if result:
        result.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")
    if error:
        print(error, flush=True)
        return 1
    return 0


def _apt(args: list[str], archives: Path, recommends: bool) -> list[str]:
    command = ["apt-get", "-y", "-o", f"Dir::Cache::Archives={archives}/"]
    if not recommends:
        command.append("--no-install-recommends")
    return _sudo(command + args)


def _deb_fields(deb: Path) -> dict[str, str]:
    fields = subprocess.run(
        ["dpkg-deb", "-f", str(deb), "Package", "Version"],
        text=True,
        capture_output=True,
    ).stdout
    return dict(line.split(": ", 1) for line in fields.splitlines() if ": " in line)


def _installed_version(package: str) -> str:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", package],
        text=True,
        capture_output=True,
    )
    status, _, version = result.stdout.strip().partition(" ")
    return version if result.returncode == 0 and status == "installed" else ""


def usable_debs(archives: Path) -> list[Path]:
    """Cached .deb files that would install or upgrade something.

    A cache restored from an older runner image can hold versions older than
    what the new image already has; installing those would be a downgrade,
    which apt refuses, so they are left out.
    """
    usable = []
    for deb in sorted(archives.glob("*.deb")):
        info = _deb_fields(deb)
        if not info.get("Package") or not info.get("Version"):
            continue
        current = _installed_version(info["Package"])
        if (
            current
            and subprocess.run(
                ["dpkg", "--compare-versions", current, "ge", info["Version"]]
            ).returncode
            == 0
        ):
            continue
        usable.append(deb)
    return usable


def _install_from_cache(archives: Path, recommends: bool) -> bool:
    """Install cached .deb files with no `apt-get update` and no download."""
    debs = usable_debs(archives)
    if not debs:
        print("no usable cached .deb files", flush=True)
        return False
    print(
        f"::group::Install {len(debs)} cached .deb files without the network",
        flush=True,
    )
    code, _ = _run(
        _apt(["install", "--no-download", *map(str, debs)], archives, recommends)
    )
    print("::endgroup::", flush=True)
    return code == 0


def _prune_cache(archives: Path) -> None:
    """Keep only the .deb files that match what is now installed, readable by the runner."""
    for path in (archives / "partial", archives / "lock"):
        _run(_sudo(["rm", "-rf", str(path)]))
    _run(_sudo(["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(archives)]))
    for deb in archives.glob("*.deb"):
        info = _deb_fields(deb)
        if not info.get("Package") or _installed_version(info["Package"]) != info.get(
            "Version"
        ):
            deb.unlink()


def cmd_plan(args: argparse.Namespace) -> int:
    packages = args.packages.split()
    if args.playwright_deps:
        packages += resolve_playwright(
            args.playwright_python, args.playwright_deps, args.cwd
        )
    packages = sorted(set(packages))
    release = os.environ.get("ImageOS") or _os_release().get(
        "ID", "linux"
    ) + _os_release().get("VERSION_ID", "")
    image_version = os.environ.get("ImageVersion") or "unversioned"
    digest = hashlib.sha256(
        json.dumps({"packages": packages, "recommends": args.recommends}).encode()
    ).hexdigest()[:20]
    # The exact key names the runner image build; the prefix lets a run on a
    # newer image start from the files an older image build cached.
    prefix = f"apt-install-v1-{release}-{_arch()}-{digest}-"
    if args.key_suffix:
        prefix += f"{args.key_suffix}-"
    key = prefix + image_version
    _github_output(
        {
            "packages": " ".join(packages),
            "key": key,
            "restore-prefix": prefix,
            "dir": str(Path(args.cache_root) / digest),
        }
    )
    print(f"{len(packages)} packages: {' '.join(packages)}")
    print(f"cache key: {key}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    packages = args.packages.split()
    recommends = args.recommends == "true"
    archives = Path(args.cache_dir)
    (archives / "partial").mkdir(parents=True, exist_ok=True)
    result = Path(args.result) if args.result else None
    names = " ".join(packages) or "(none)"

    if os.environ.get("RUNNER_OS", "Linux") != "Linux" or not shutil.which("apt-get"):
        return _finish(
            result,
            {"source": "none", "mirror": "", "reason": "not-apt"},
            "::error title=apt-install::this action needs an apt-based Linux runner",
        )

    _write_root(APT_CONF, APT_CONF_TEXT)
    missing = _missing(packages)
    print(
        f"{len(packages)} requested, {len(missing)} not installed: {' '.join(missing)}"
    )

    exact = args.cache_hit == "exact"
    source = ""
    if not missing:
        source = "already-installed"
    elif args.cache_hit in ("exact", "partial"):
        # The primary defense: cached .deb files, no index refresh, no network.
        _install_from_cache(archives, recommends)
        missing = _missing(packages)
        if not missing:
            source = "cache"
        else:
            print(
                f"::warning::the cache did not cover {len(missing)} packages "
                f"({' '.join(missing)}); fetching only those from the network"
            )

    if source and args.refresh_index != "true":
        print(f"installed from: {source}")
        values = {"source": source, "mirror": "", "reason": ""}
        if source == "cache" and not exact:
            _prune_cache(archives)
            values["save"] = "true"
        return _finish(result, values)

    sources = Sources()
    tried: list[str] = []
    dns_failures = 0
    for name, archive, security in stages(_arch()):
        tried.append(name)
        print(f"::group::apt via {name}", flush=True)
        sources.apply(archive, security)
        code, output = _run(_sudo(["apt-get", "update"]), timeout=UPDATE_TIMEOUT)
        if not update_reached_distro(code, output, third_party_hosts()):
            print("::endgroup::", flush=True)
            dns_failures += bool(DNS_FAILURE.search(output))
            print(
                f"::warning::apt-get update failed via {name}; trying the next mirror"
            )
            continue
        if source:
            # Only the index refresh was requested; the packages are present.
            print("::endgroup::", flush=True)
            print(f"apt index refreshed via {name}; later apt calls in this job use it")
            return _finish(result, {"source": source, "mirror": name, "reason": ""})
        # Files already in the cache directory are reused; only the rest download.
        code, output = _run(
            _apt(["install", "--download-only", *missing], archives, recommends),
            timeout=DOWNLOAD_TIMEOUT,
        )
        print("::endgroup::", flush=True)
        if code != 0 and NOT_FOUND.search(output):
            return _finish(
                result,
                {"source": "", "mirror": name, "reason": "package-not-found"},
                "::error title=apt package not found::apt reached the Ubuntu mirror "
                f"({name}) but cannot find a requested package. Check the package list: "
                f"{' '.join(missing)}",
            )
        if code != 0:
            dns_failures += bool(DNS_FAILURE.search(output))
            print(
                f"::warning::downloading packages failed via {name}; trying the next mirror"
            )
            continue
        print(f"::group::Install {len(missing)} packages", flush=True)
        code, _ = _run(
            _apt(["install", "--no-download", *missing], archives, recommends)
        )
        print("::endgroup::", flush=True)
        if code != 0 or _missing(missing):
            return _finish(
                result,
                {"source": "", "mirror": name, "reason": "install-failed"},
                "::error title=apt install failed::the packages downloaded via "
                f"{name} but dpkg could not install them. This is not a mirror outage.",
            )
        _prune_cache(archives)
        print(f"installed from the network via {name}")
        values = {"source": "network", "mirror": name, "reason": ""}
        if not exact:
            values["save"] = "true"
        return _finish(result, values)

    sources.apply(None, None)
    when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    if dns_failures == len(tried):
        return _finish(
            result,
            {"source": "", "mirror": "", "reason": "dns-unreachable"},
            "::error title=Ubuntu package hosts unreachable (DNS)::Ubuntu package hosts "
            "unreachable (DNS); infrastructure outage, not a test failure. No cached .deb "
            f"files covered the list and every mirror ({'; '.join(tried)}) failed to resolve "
            f"at {when}. Packages not installed: {' '.join(missing) or names}. Re-run the job "
            "when DNS recovers.",
        )
    return _finish(
        result,
        {"source": "", "mirror": "", "reason": "mirror-unreachable"},
        "::error title=Ubuntu package mirror unreachable::Infrastructure outage, not a "
        f"test failure: no Ubuntu mirror answered ({'; '.join(tried)}) at {when}, so these "
        f"packages could not be installed: {' '.join(missing) or names}. Re-run the job "
        "when the mirror recovers.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--packages", default="")
    plan.add_argument("--playwright-deps", default="")
    plan.add_argument("--playwright-python", default="python")
    plan.add_argument("--cwd", default="")
    plan.add_argument("--recommends", default="false")
    plan.add_argument("--cache-root", required=True)
    plan.add_argument("--key-suffix", default="")
    plan.set_defaults(func=cmd_plan)

    install = sub.add_parser("install")
    install.add_argument("--packages", default="")
    install.add_argument("--recommends", default="false")
    install.add_argument("--cache-dir", required=True)
    install.add_argument(
        "--cache-hit", choices=("exact", "partial", "none"), default="none"
    )
    install.add_argument("--refresh-index", default="false")
    install.add_argument("--result", default="")
    install.set_defaults(func=cmd_install)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
