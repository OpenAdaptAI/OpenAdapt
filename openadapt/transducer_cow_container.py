"""Container-wide copy-on-write diff transaction and OS postcondition probes.

``ContainerDiffTransaction`` snapshots every watched root (SHA-256 manifest plus
a tar backup kept next to the roots), reports what a step changed, and restores
either single protected files or everything. ``synthesize_probes`` builds
read-only shell checks from the trusted task text for tasks with no test suite.
Nothing here is specific to one app or benchmark.
"""

from __future__ import annotations

import enum
import fnmatch
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Collection, Mapping, Sequence

ExecFn = Callable[[str], "tuple[int, str, str]"]

BASELINE_TAR = "/tmp/.oa_cow_baseline.tar.gz"
DEFAULT_WATCHED_ROOTS = ("/home/user", "/etc/ssh")
DEFAULT_PROTECTED_PATTERNS = (
    ".bashrc",
    "test_*.py",
    "*_test.py",
    "*judge*.py",
    "*eval*.py",
    "sshd_config",
)

_HASH_CMD = 'H="sha256sum"; command -v sha256sum >/dev/null 2>&1 || H="shasum -a 256"; '


@dataclass
class ContainerDiffReport:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    protected_violations: list[str] = field(default_factory=list)
    unauthorized_paths: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        return sorted(set(self.added) | set(self.modified) | set(self.deleted))


def _default_docker_diff(container_id: str) -> "tuple[int, str]":
    try:
        proc = subprocess.run(
            ["docker", "diff", container_id],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout


def _is_allowed(path: str, allowed: Collection[str]) -> bool:
    for entry in allowed:
        base = entry.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _matches_protected(path: str, patterns: Sequence[str]) -> bool:
    name = posixpath.basename(path)
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _under_roots(path: str, roots: Sequence[str]) -> bool:
    return _is_allowed(path, roots)


class ContainerDiffTransaction:
    def __init__(
        self,
        exec_fn: ExecFn,
        container_id: str = "",
        watched_roots: Sequence[str] = DEFAULT_WATCHED_ROOTS,
        protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
        *,
        baseline_tar: str = BASELINE_TAR,
        docker_diff_fn: Callable[[str], "tuple[int, str]"] | None = None,
    ) -> None:
        self._exec = exec_fn
        self.container_id = container_id
        self.watched_roots = tuple(r.rstrip("/") or "/" for r in watched_roots)
        self.protected_patterns = tuple(protected_patterns)
        self.baseline_tar = baseline_tar
        self._docker_diff = docker_diff_fn or _default_docker_diff
        self._baseline: dict[str, str] = {}
        self._begun = False

    # -- scanning ---------------------------------------------------------
    def _scan(self) -> dict[str, str]:
        roots = " ".join(shlex.quote(r) for r in self.watched_roots)
        cmd = (
            _HASH_CMD + f"for r in {roots}; do "
            '[ -d "$r" ] && find "$r" -type f -exec $H {} + 2>/dev/null; '
            "done; true"
        )
        code, out, _ = self._exec(cmd)
        if code != 0:
            raise RuntimeError("container scan failed")
        scan: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            digest, path = parts[0], parts[1].lstrip("*").strip()
            if path == self.baseline_tar:
                continue
            scan[path] = digest
        return scan

    def begin(self) -> dict[str, str]:
        self._baseline = self._scan()
        roots = " ".join(shlex.quote(r) for r in self.watched_roots)
        cmd = (
            f"set --; for r in {roots}; do "
            '[ -d "$r" ] && set -- "$@" "$r"; done; '
            f'tar -czPf {shlex.quote(self.baseline_tar)} -T /dev/null "$@"'
        )
        code, _, err = self._exec(cmd)
        if code != 0:
            raise RuntimeError(f"baseline backup failed: {err.strip()}")
        self._begun = True
        return dict(self._baseline)

    # -- diff -------------------------------------------------------------
    def _docker_paths(self) -> "dict[str, str]":
        if not self.container_id:
            return {}
        code, out = self._docker_diff(self.container_id)
        if code != 0:
            return {}
        kinds: dict[str, str] = {}
        for line in out.splitlines():
            if len(line) < 3 or line[0] not in "ACD":
                continue
            path = line[2:].strip()
            if path and _under_roots(path, self.watched_roots):
                kinds[path] = line[0]
        return kinds

    def inspect_step_diff(
        self, allowed_write_paths: Collection[str] = ()
    ) -> ContainerDiffReport:
        if not self._begun:
            raise RuntimeError("begin() must run before inspect_step_diff()")
        now = self._scan()
        base = self._baseline
        added = sorted(p for p in now if p not in base)
        modified = sorted(p for p in now if p in base and now[p] != base[p])
        deleted = sorted(p for p in base if p not in now)
        # docker diff catches changes (e.g. mode or metadata-only adds) the
        # hash scan can miss; it only ever adds paths, never clears them.
        for path, kind in self._docker_paths().items():
            if path in added or path in modified or path in deleted:
                continue
            if kind == "A" and path in now and path not in base:
                added.append(path)
            elif kind == "D" and path in base and path not in now:
                deleted.append(path)
        report = ContainerDiffReport(
            added=sorted(added), modified=sorted(modified), deleted=sorted(deleted)
        )
        for path in report.changed:
            if _matches_protected(path, self.protected_patterns):
                report.protected_violations.append(path)
            if not _is_allowed(path, allowed_write_paths):
                report.unauthorized_paths.append(path)
        return report

    # -- rollback ---------------------------------------------------------
    def rollback_paths(self, paths: Sequence[str]) -> list[str]:
        """Restore only the given paths from the baseline; return those restored.

        Paths that did not exist at baseline are removed instead.
        """
        done: list[str] = []
        for path in paths:
            if not _under_roots(path, self.watched_roots):
                continue
            if path in self._baseline:
                cmd = (
                    f"tar -xzPf {shlex.quote(self.baseline_tar)} -- {shlex.quote(path)}"
                )
            else:
                cmd = f"rm -f -- {shlex.quote(path)}"
            code, _, _ = self._exec(cmd)
            if code == 0:
                done.append(path)
        return done

    def rollback_all(self, allowed_write_paths: Collection[str] = ()) -> bool:
        if not self._begun:
            return False
        now = self._scan()
        for path in sorted(now):
            if path not in self._baseline and not _is_allowed(
                path, allowed_write_paths
            ):
                code, _, _ = self._exec(f"rm -f -- {shlex.quote(path)}")
                if code != 0:
                    return False
        code, _, _ = self._exec(f"tar -xzPf {shlex.quote(self.baseline_tar)}")
        if code != 0:
            return False
        after = self._scan()
        for path, digest in self._baseline.items():
            if after.get(path) != digest:
                return False
        for path in after:
            if path not in self._baseline and not _is_allowed(
                path, allowed_write_paths
            ):
                return False
        return True

    def cleanup(self) -> None:
        self._exec(f"rm -f -- {shlex.quote(self.baseline_tar)}")
        self._begun = False


# -- postcondition probes ---------------------------------------------------


@dataclass(frozen=True)
class OsPostconditionProbe:
    probe_id: str
    description: str
    shell_cmd: str
    expect_exit_zero: bool = True
    expect_stdout_contains: str = ""
    expect_stdout_absent: str = ""


class ProbeStatus(enum.Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class ProbeSuiteResult:
    all_passed: bool
    passed_ids: list[str] = field(default_factory=list)
    failed_details: list[str] = field(default_factory=list)


_SECRET_VAR = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SECRET_HINT = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API")
_PROFILE = re.compile(r"\.bashrc|\.bash_profile|\.profile|\.zshrc|bashrc|profile")
_REVOKE = re.compile(
    r"revok|restrict|deny|denied|remove\s+(?:\w+\s+)?access|no longer|lock\s*down|"
    r"disable\s+access|chmod|permission",
    re.IGNORECASE,
)
_ABS_PATH = re.compile(r"(?<![\w.])(/(?:[\w.\-]+/?)+)")


def _profile_path(baseline_files: Mapping[str, str]) -> str:
    for path in sorted(baseline_files):
        if posixpath.basename(path) == ".bashrc":
            return shlex.quote(path)
    return '"$HOME/.bashrc"'


def synthesize_probes(
    task_instruction: str,
    *,
    baseline_files: Mapping[str, str] | None = None,
    watched_roots: Sequence[str] = DEFAULT_WATCHED_ROOTS,
    baseline_tar: str = BASELINE_TAR,
    protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
) -> list[OsPostconditionProbe]:
    """Build probes using the transaction's path settings and hash manifest."""
    baseline_files = baseline_files or {}
    probes: list[OsPostconditionProbe] = []

    if _PROFILE.search(task_instruction):
        rc = _profile_path(baseline_files)
        seen: list[str] = []
        for var in _SECRET_VAR.findall(task_instruction):
            if _SECRET_HINT.search(var) and var not in seen:
                seen.append(var)
        for var in seen:
            probes.append(
                OsPostconditionProbe(
                    probe_id=f"env_preserved_{var.lower()}",
                    description=f"{var} is still defined in the shell profile",
                    shell_cmd=f"test -s {rc} && bash -n {rc} && "
                    f"grep -q {shlex.quote(var)} {rc}",
                )
            )
        if not seen:
            probes.append(
                OsPostconditionProbe(
                    probe_id="profile_intact",
                    description="shell profile exists, is non-empty and parses",
                    shell_cmd=f"test -s {rc} && bash -n {rc}",
                )
            )

    if _REVOKE.search(task_instruction):
        used: list[str] = []
        for raw in _ABS_PATH.findall(task_instruction):
            path = raw.rstrip("/.,;:") or "/"
            if path != "/" and path not in used:
                used.append(path)
        for path in used:
            q = shlex.quote(path)
            probes.append(
                OsPostconditionProbe(
                    probe_id="perm_" + re.sub(r"\W+", "_", path).strip("_"),
                    description=f"permissions on {path} are readable and recorded",
                    shell_cmd=f"test -e {q} && "
                    f"(command -v getfacl >/dev/null 2>&1 && getfacl -p {q} "
                    f"|| ls -ld {q})",
                )
            )

    # Profile and SSH settings are covered by the transaction and can be
    # legitimate task targets. Guard the baseline oracle files here.
    oracle_patterns = tuple(
        p for p in protected_patterns if p not in (".bashrc", "sshd_config")
    )
    checks = []
    for path, digest in sorted(baseline_files.items()):
        if (
            path == baseline_tar
            or not _under_roots(path, watched_roots)
            or not _matches_protected(path, oracle_patterns)
        ):
            continue
        quoted = shlex.quote(path)
        checks.append(
            f"test -f {quoted} && test ! -L {quoted} && "
            f"digest=$($H < {quoted}) && "
            f'test "${{digest%% *}}" = {shlex.quote(digest)}'
        )
    probes.append(
        OsPostconditionProbe(
            probe_id="oracle_files_untouched",
            description="protected baseline oracle files still match their hashes",
            shell_cmd=_HASH_CMD + " && ".join(checks) if checks else "true",
        )
    )
    return probes


def _judge(probe: OsPostconditionProbe, code: int, out: str) -> str:
    """Return an empty string if the probe passes, else the failure reason."""
    if probe.expect_exit_zero and code != 0:
        return f"exit {code}"
    if not probe.expect_exit_zero and code == 0:
        return "unexpected exit 0"
    if probe.expect_stdout_contains and probe.expect_stdout_contains not in out:
        return f"stdout missing {probe.expect_stdout_contains!r}"
    if probe.expect_stdout_absent and probe.expect_stdout_absent in out:
        return f"stdout contains {probe.expect_stdout_absent!r}"
    return ""


def run_probes(
    probes: Sequence[OsPostconditionProbe], exec_fn: ExecFn
) -> ProbeSuiteResult:
    passed: list[str] = []
    failed: list[str] = []
    if not probes:
        failed.append("no probes to run")
    for probe in probes:
        try:
            code, out, _ = exec_fn(probe.shell_cmd)
        except (OSError, RuntimeError, ValueError) as exc:
            failed.append(f"{probe.probe_id}: exec error {exc}")
            continue
        reason = _judge(probe, code, out)
        status = ProbeStatus.FAILED if reason else ProbeStatus.PASSED
        if status is ProbeStatus.PASSED:
            passed.append(probe.probe_id)
        else:
            failed.append(f"{probe.probe_id}: {reason}")
    return ProbeSuiteResult(
        all_passed=len(failed) == 0 and len(passed) > 0,
        passed_ids=passed,
        failed_details=failed,
    )
