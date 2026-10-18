"""A read-only shell for the code-as-action agent, in one working directory.

The agent keeps notes and page listings as files in its working directory and
reads them back with ordinary text tools (``grep``, ``head``, ``wc``). That is
the whole shell: it can't change the app, start programs, or reach a network.

The rules, all checked before anything runs:

* Only the commands in :data:`ALLOWED` run, from ``/usr/bin`` or ``/bin``.
  None of them can open a network connection or run another program.
* Commands join with ``|`` only. ``;``, ``&``, ``&&``, ``||``, subshells,
  backticks and input redirection are refused.
* ``> file`` and ``>> file`` write into the working directory only, to a
  plain file name.
* An argument that names a path must stay inside the working directory:
  no absolute paths, no ``~``, no ``..``.
* A few flags that write files or follow forever are refused
  (:data:`REFUSED_FLAGS`).
* The environment is empty except ``PATH=/usr/bin:/bin``, ``LC_ALL=C`` and
  ``HOME`` (the working directory). Each command gets 5 seconds and its output
  is cut to 16,000 characters (head and tail kept).

On macOS each command also runs under ``sandbox-exec`` with network access
denied and file writes allowed only in the working directory. On Linux it runs
under ``unshare --net`` when unprivileged user namespaces work. Elsewhere the
allow-list is the only fence. :attr:`Sandbox.isolation` says which applies.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = ["ALLOWED", "REFUSED_FLAGS", "Sandbox", "ShellRefused", "ShellResult"]

#: Text tools that read files and print. None of them can open a socket,
#: start another program, or write anywhere but stdout.
ALLOWED = frozenset(
    {
        "cat",
        "column",
        "comm",
        "cut",
        "date",
        "diff",
        "echo",
        "egrep",
        "expand",
        "false",
        "fgrep",
        "fold",
        "grep",
        "head",
        "ls",
        "nl",
        "paste",
        "printf",
        "rev",
        "sort",
        "tail",
        "tr",
        "true",
        "uniq",
        "wc",
    }
)

#: Flags that would write a file, follow a file forever, or run something.
REFUSED_FLAGS: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o", "--output", "--compress-program"}),
    "tail": frozenset({"-f", "-F", "--follow"}),
    "date": frozenset({"-s", "--set"}),
}

TIMEOUT_S = 5.0
MAX_OUTPUT = 16_000
_SYSTEM_PATH = "/usr/bin:/bin"
_FORBIDDEN_TOKENS = {";", "&", "&&", "||", "<", "<<", "(", ")", "|&", ";;"}


class ShellRefused(ValueError):
    """The command breaks a sandbox rule. Nothing ran."""


@dataclass
class ShellResult:
    command: str
    output: str
    exit_code: int
    isolation: str
    wrote: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def _cut(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half] + f"\n[... {len(text) - limit} characters cut ...]\n" + text[-half:]
    )


@dataclass
class Sandbox:
    """Run allow-listed read-only commands in ``workdir``."""

    workdir: Path
    timeout_s: float = TIMEOUT_S
    isolation: str = field(init=False)

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.isolation = _detect_isolation()

    # -- files the agent keeps ----------------------------------------------------

    def file(self, name: str) -> Path:
        """The path of a plain file name in the working directory."""

        name = str(name).strip()
        if (
            not name
            or "/" in name
            or "\\" in name
            or name.startswith(("~", "."))
            or "\x00" in name
        ):
            raise ShellRefused(
                f"{name!r} is not a plain file name in the working directory"
            )
        return self.workdir / name

    def write(self, name: str, text: str, *, append: bool = False) -> Path:
        path = self.file(name)
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") or not text else text + "\n")
        return path

    # -- commands ---------------------------------------------------------------------

    def parse(self, command: str) -> tuple[list[list[str]], Optional[tuple[str, bool]]]:
        """Split ``command`` into a pipeline and an optional output file.

        Raises:
            ShellRefused: any rule is broken.
        """

        if "`" in command or "$(" in command or "\n" in command:
            raise ShellRefused("subshells, backticks and line breaks are not allowed")
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise ShellRefused(f"can't parse the command: {exc}") from exc
        if not tokens:
            raise ShellRefused("empty command")
        stages: list[list[str]] = [[]]
        out: Optional[tuple[str, bool]] = None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in _FORBIDDEN_TOKENS or (
                set(token) <= set("|&;<>()") and token not in ("|", ">", ">>")
            ):
                raise ShellRefused(
                    f"{token!r} is not allowed; join commands with | only"
                )
            if token == "|":
                if out is not None or not stages[-1]:
                    raise ShellRefused("a | must sit between two commands")
                stages.append([])
            elif token in (">", ">>"):
                if out is not None or i + 1 >= len(tokens) or i + 2 != len(tokens):
                    raise ShellRefused("> FILE must end the command, once")
                self.file(tokens[i + 1])
                out = (tokens[i + 1], token == ">>")
                i += 1
            else:
                stages[-1].append(token)
            i += 1
        if not stages[-1]:
            raise ShellRefused("the command ends with |")
        for argv in stages:
            self._check(argv)
        return stages, out

    def _check(self, argv: list[str]) -> None:
        name = argv[0]
        if name not in ALLOWED:
            raise ShellRefused(
                f"{name!r} is not allowed; the shell has {', '.join(sorted(ALLOWED))}"
            )
        refused = REFUSED_FLAGS.get(name, frozenset())
        root = self.workdir
        for arg in argv[1:]:
            flag = arg.split("=", 1)[0]
            if flag in refused or any(
                arg.startswith(f) and len(f) == 2 and not arg.startswith("--")
                for f in refused
            ):
                raise ShellRefused(f"{name} {flag} is not allowed")
            if arg.startswith("-"):
                continue
            if arg.startswith(("/", "~")) or ".." in Path(arg).parts:
                raise ShellRefused(
                    f"{arg!r} leaves the working directory; use plain file names"
                )
            if "/" in arg:
                resolved = (root / arg).resolve()
                if resolved != root and root not in resolved.parents:
                    raise ShellRefused(f"{arg!r} leaves the working directory")

    def run(self, command: str) -> ShellResult:
        """Run ``command``. A broken rule raises :class:`ShellRefused` first."""

        stages, out = self.parse(command)
        env = {"PATH": _SYSTEM_PATH, "LC_ALL": "C", "HOME": str(self.workdir)}
        procs: list[subprocess.Popen[bytes]] = []
        prev = None
        try:
            for argv in stages:
                binary = shutil.which(argv[0], path=_SYSTEM_PATH)
                if binary is None:
                    raise ShellRefused(f"{argv[0]!r} is not installed here")
                full = self._wrap([binary, *argv[1:]])
                proc = subprocess.Popen(  # noqa: S603 - argv checked above
                    full,
                    stdin=prev if prev is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=self.workdir,
                    env=env,
                )
                if prev is not None:
                    prev.close()
                prev = proc.stdout
                procs.append(proc)
            try:
                data, _ = procs[-1].communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                for proc in procs:
                    proc.kill()
                raise ShellRefused(
                    f"the command ran past {self.timeout_s:g} s and was stopped"
                ) from None
            for proc in procs[:-1]:
                proc.wait(timeout=self.timeout_s)
            code = procs[-1].returncode
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
        text = data.decode("utf-8", errors="replace")
        wrote = None
        if out is not None:
            name, append = out
            self.write(name, text, append=append)
            wrote = name
            text = f"(wrote {len(text)} characters to {name})"
        return ShellResult(command, _cut(text), int(code or 0), self.isolation, wrote)

    def _wrap(self, argv: list[str]) -> list[str]:
        if self.isolation == "sandbox-exec":
            return ["/usr/bin/sandbox-exec", "-p", _darwin_profile(self.workdir), *argv]
        if self.isolation == "unshare-net":
            return ["unshare", "--net", "--map-root-user", *argv]
        return argv


def _darwin_profile(workdir: Path) -> str:
    root = str(workdir).replace("\\", "\\\\").replace('"', '\\"')
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{root}"))\n'
        '(allow file-write-data (literal "/dev/null"))\n'
    )


_ISOLATION: Optional[str] = None


def _detect_isolation() -> str:
    """``sandbox-exec`` (macOS), ``unshare-net`` (Linux) or ``allow-list``."""

    global _ISOLATION
    if _ISOLATION is not None:
        return _ISOLATION
    found = "allow-list"
    probe: Optional[list[str]] = None
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        probe = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)",
            "/usr/bin/true",
        ]
        candidate = "sandbox-exec"
    elif sys.platform.startswith("linux") and shutil.which("unshare"):
        probe = ["unshare", "--net", "--map-root-user", "/bin/true"]
        candidate = "unshare-net"
    if probe is not None:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                probe, capture_output=True, timeout=5, env={"PATH": _SYSTEM_PATH}
            )
            if done.returncode == 0:
                found = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    _ISOLATION = found
    return found
