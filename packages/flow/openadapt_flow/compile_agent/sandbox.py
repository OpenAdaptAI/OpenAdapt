"""A read-only shell for the code-as-action agent, in one working directory.

The agent keeps notes and page listings as files in its working directory and
reads them back with ordinary text tools (``grep``, ``head``, ``wc``). That is
the whole shell: it can't change the app, start programs, or reach a network.

The rules, all checked before anything runs:

* Only the commands in :data:`FLAGS` run, from ``/usr/bin`` or ``/bin``, each
  with the short flags listed for it and nothing else: no long options, no
  flag that names a file, writes one, or runs a program (``sort
  --compress-program``, ``grep -f``, ``sort -o``, ``tail -f`` are all
  refused).
* Commands join with ``|`` only. ``;``, ``&``, ``&&``, ``||``, subshells,
  backticks and input redirection are refused.
* ``> file`` and ``>> file`` write into the working directory only, to a
  plain file name.
* Every other argument must stay inside the working directory as a path: no
  absolute paths, no ``~``, no ``..``.
* The environment is empty except ``PATH=/usr/bin:/bin``, ``LC_ALL=C`` and
  ``HOME`` (the working directory). Each command gets 5 seconds in its own
  process group, killed whole on timeout; output is cut to 16,000 characters
  (head and tail kept).

On macOS each command also runs under ``sandbox-exec``: no network, no
program but the one named, file reads only in the working directory and the
system's own directories, and writes only in the working directory. On Linux
it runs under ``unshare --net`` when unprivileged user namespaces work.
Elsewhere the flag allow-list is the only fence. :attr:`Sandbox.isolation`
says which applies.
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

__all__ = ["ALLOWED", "FLAGS", "Sandbox", "ShellRefused", "ShellResult"]

#: Each allowed command: (flags that take no value, flags that take one).
#: A flag not listed is refused, and so is every long option.
FLAGS: dict[str, tuple[str, str]] = {
    "cat": ("nbsuv", ""),
    "column": ("t", "sc"),
    "comm": ("123i", ""),
    "cut": ("ns", "dfcb"),
    "date": ("u", ""),
    "diff": ("uqiwbB", ""),
    "echo": ("neE", ""),
    "egrep": ("ivnclowxEFhHsLr", "eABCm"),
    "expand": ("", "t"),
    "false": ("", ""),
    "fgrep": ("ivnclowxEFhHsLr", "eABCm"),
    "fold": ("sb", "w"),
    "grep": ("ivnclowxEFhHsLr", "eABCm"),
    "head": ("", "nc"),
    "ls": ("laA1trShRF", ""),
    "nl": ("", ""),
    "paste": ("s", "d"),
    "printf": ("", ""),
    "rev": ("", ""),
    "sort": ("rnufbdhMV", "kt"),
    "tail": ("", "nc"),
    "tr": ("dscC", ""),
    "true": ("", ""),
    "uniq": ("cdui", "fs"),
    "wc": ("lwcm", ""),
}
ALLOWED = frozenset(FLAGS)

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


def _tokens(command: str) -> list[tuple[str, bool]]:
    """``(token, is_operator)`` pairs. Characters such as ``|`` and ``>`` are
    operators only outside quotes; a quoted ``'|'`` is an ordinary word."""

    marks = {
        "|": "\x01",
        ">": "\x02",
        "&": "\x03",
        ";": "\x04",
        "<": "\x05",
        "(": "\x06",
        ")": "\x07",
    }
    if any(ch in command for ch in marks.values()):
        raise ShellRefused("control characters are not allowed")
    out: list[str] = []
    quote: Optional[str] = None
    for ch in command:
        if quote:
            if ch == quote:
                quote = None
            out.append(marks.get(ch, ch) if quote else ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch in marks:
            out.append(f" {ch} ")
        else:
            out.append(ch)
    try:
        words = shlex.split("".join(out).replace(">  >", ">>"))
    except ValueError as exc:
        raise ShellRefused(f"can't parse the command: {exc}") from exc
    back = {v: k for k, v in marks.items()}
    tokens: list[tuple[str, bool]] = []
    for word in words:
        if word in marks or word == ">>":
            tokens.append((word, True))
        else:
            tokens.append(("".join(back.get(c, c) for c in word), False))
    return tokens


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
        tokens = _tokens(command)
        if not tokens:
            raise ShellRefused("empty command")
        stages: list[list[str]] = [[]]
        out: Optional[tuple[str, bool]] = None
        i = 0
        while i < len(tokens):
            token, operator = tokens[i]
            if operator and token not in ("|", ">", ">>"):
                raise ShellRefused(
                    f"{token!r} is not allowed; join commands with | only"
                )
            if operator and token == "|":
                if out is not None or not stages[-1]:
                    raise ShellRefused("a | must sit between two commands")
                stages.append([])
            elif operator:
                if out is not None or i + 2 != len(tokens) or tokens[i + 1][1]:
                    raise ShellRefused("> FILE must end the command, once")
                self.file(tokens[i + 1][0])
                out = (tokens[i + 1][0], token == ">>")
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
        if name not in FLAGS:
            raise ShellRefused(
                f"{name!r} is not allowed; the shell has {', '.join(sorted(FLAGS))}"
            )
        switches, valued = FLAGS[name]
        options = True
        i = 1
        while i < len(argv):
            arg = argv[i]
            i += 1
            if options and arg == "--":
                options = False
                continue
            if options and arg.startswith("-") and len(arg) > 1:
                if arg.startswith("--"):
                    raise ShellRefused(
                        f"{name}: long options such as {arg!r} are not allowed"
                    )
                for pos, letter in enumerate(arg[1:], 1):
                    if letter in switches:
                        continue
                    if letter in valued:
                        if pos == len(arg) - 1:
                            if i >= len(argv):
                                raise ShellRefused(f"{name} -{letter} needs a value")
                            i += 1  # the next token is the value, not a path
                        break  # the rest of this token is the value
                    allowed = "".join(sorted(switches + valued)) or "none"
                    raise ShellRefused(
                        f"{name} -{letter} is not allowed (flags: {allowed})"
                    )
                continue
            self._check_path(arg)

    def _check_path(self, arg: str) -> None:
        root = self.workdir
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
                    start_new_session=True,
                )
                if prev is not None:
                    prev.close()
                prev = proc.stdout
                procs.append(proc)
            try:
                data, _ = procs[-1].communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                for proc in procs:
                    _kill_group(proc)
                raise ShellRefused(
                    f"the command ran past {self.timeout_s:g} s and was stopped"
                ) from None
            for proc in procs[:-1]:
                proc.wait(timeout=self.timeout_s)
            code = procs[-1].returncode
        finally:
            for proc in procs:
                if proc.poll() is None:
                    _kill_group(proc)
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
            return [
                "/usr/bin/sandbox-exec",
                "-p",
                _darwin_profile(self.workdir, argv[0]),
                *argv,
            ]
        if self.isolation == "unshare-net":
            return ["unshare", "--net", "--map-root-user", *argv]
        return argv


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _darwin_profile(workdir: Path, binary: str) -> str:
    """No network, no other program, reads only in the working directory and
    the system's own directories, writes only in the working directory."""

    root = _quote(str(workdir))
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny process-exec)\n"
        f"(allow process-exec (literal {_quote(binary)}))\n"
        "(deny file-read*)\n"
        '(allow file-read* (subpath "/usr") (subpath "/System") (subpath "/bin")'
        ' (subpath "/Library/Apple") (subpath "/private/var/db/timezone")'
        ' (subpath "/dev") (literal "/") (literal "/private")'
        ' (literal "/private/var") (literal "/private/etc/localtime")'
        f" (subpath {root}))\n"
        "(deny file-write*)\n"
        f"(allow file-write* (subpath {root}))\n"
        '(allow file-write-data (literal "/dev/null"))\n'
    )


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


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
