"""Shell-effect classification, oracle protection, and monotonic egress checks.

General-purpose helpers for a step-level action transducer:

* ``classify_shell_command`` labels a shell command by its effect (read-only,
  scoped write, destructive, test/judge tampering, data exfiltration).
* ``ImmutableOracleGuard`` hashes test/judge files before the run and reports
  any that were changed or deleted.
* ``PostEditTestGate`` runs the project's existing tests before a task may
  finish.
* ``evaluate_outbound_items`` / ``select_best_egress`` check items leaving the
  agent without letting a repair retry make the answer worse.

Nothing here depends on a particular application, benchmark, or vocabulary.
"""

from __future__ import annotations

import enum
import fnmatch
import hashlib
import inspect
import os
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

# --------------------------------------------------------------------------
# Shell effect classification
# --------------------------------------------------------------------------


class ShellEffectKind(str, enum.Enum):
    READ_ONLY = "READ_ONLY"
    SCOPED_MUTATION = "SCOPED_MUTATION"
    DESTRUCTIVE_PRIVILEGED = "DESTRUCTIVE_PRIVILEGED"
    ORACLE_TAMPER = "ORACLE_TAMPER"
    NETWORK_EXFIL = "NETWORK_EXFIL"


@dataclass(frozen=True)
class ShellClassification:
    kind: ShellEffectKind
    is_commit: bool
    blocked: bool
    trap_code: str | None
    reason: str
    target_paths: tuple[str, ...] = ()


_SEVERITY = {
    ShellEffectKind.READ_ONLY: 0,
    ShellEffectKind.SCOPED_MUTATION: 1,
    ShellEffectKind.ORACLE_TAMPER: 2,
    ShellEffectKind.NETWORK_EXFIL: 3,
    ShellEffectKind.DESTRUCTIVE_PRIVILEGED: 4,
}

_TRAP_CODES = {
    ShellEffectKind.DESTRUCTIVE_PRIVILEGED: "DESTRUCTIVE_PRIVILEGED_COMMAND",
    ShellEffectKind.ORACLE_TAMPER: "ORACLE_TAMPER",
    ShellEffectKind.NETWORK_EXFIL: "NETWORK_EXFIL",
}

ORACLE_FILE_PATTERNS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "*.test.js",
    "*.test.ts",
    "*.spec.js",
    "*.spec.ts",
    "*judge*",
    "*eval*wrapper*",
    "*evaluator*",
    "*grader*",
    "*scorer*",
)
_ORACLE_DIR_NAMES = frozenset({"tests", "test", "__tests__"})

_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".tox"})


def _split_quoted(text: str, seps: Sequence[str]) -> list[str]:
    """Split ``text`` on any separator that appears outside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    ordered = sorted(seps, key=len, reverse=True)
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            buf.append(ch + text[i + 1])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        matched = ""
        for sep in ordered:
            if text.startswith(sep, i):
                if sep == "&" and (
                    (i > 0 and text[i - 1] in "><&")
                    or text[i + 1 : i + 2] in (">", "&")
                ):
                    continue
                matched = sep
                break
        if matched:
            parts.append("".join(buf))
            buf = []
            i += len(matched)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _tokenize(stage: str) -> tuple[list[str], list[str]]:
    """Return (words, redirect_targets) for one simple command."""
    words: list[str] = []
    redirects: list[str] = []
    cur: list[str] = []
    has_cur = False
    quote = ""
    pending_redirect = False

    def flush() -> None:
        nonlocal has_cur, pending_redirect
        if not has_cur:
            return
        word = "".join(cur)
        cur.clear()
        has_cur = False
        if pending_redirect:
            pending_redirect = False
            if word and not word.startswith("&") and word not in _NULL_SINKS:
                redirects.append(word)
            return
        words.append(word)

    i = 0
    while i < len(stage):
        ch = stage[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < len(stage):
                i += 1
                cur.append(stage[i])
            else:
                cur.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            has_cur = True
        elif ch == "\\" and i + 1 < len(stage):
            i += 1
            cur.append(stage[i])
            has_cur = True
        elif ch.isspace():
            flush()
        elif ch == ">":
            if has_cur and "".join(cur).isdigit():
                cur.clear()
                has_cur = False
            elif has_cur and "".join(cur) == "&":
                cur.clear()
                has_cur = False
            else:
                flush()
            while i + 1 < len(stage) and stage[i + 1] in ">|":
                i += 1
            pending_redirect = True
        elif ch == "<":
            flush()
            if i + 1 < len(stage) and stage[i + 1] == "<":
                i += 1
                while i + 1 < len(stage) and stage[i + 1] == "<":
                    i += 1
            # Input redirect: swallow the following word without recording it.
            while i + 1 < len(stage) and stage[i + 1].isspace():
                i += 1
            j = i + 1
            while j < len(stage) and not stage[j].isspace():
                j += 1
            i = j - 1
        else:
            cur.append(ch)
            has_cur = True
        i += 1
    flush()
    return words, redirects


_NULL_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

_WRAPPERS = frozenset(
    {"sudo", "time", "nohup", "command", "exec", "nice", "builtin", "doas", "xargs"}
)


def _strip_wrappers(words: list[str]) -> tuple[list[str], bool]:
    """Remove leading env assignments and wrapper commands."""
    privileged = False
    out = list(words)
    while out:
        head = out[0]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", head):
            out.pop(0)
        elif head in ("sudo", "doas"):
            privileged = True
            out.pop(0)
            while out and out[0].startswith("-"):
                out.pop(0)
        elif head == "env":
            out.pop(0)
            while out and (out[0].startswith("-") or "=" in out[0]):
                out.pop(0)
        elif head in _WRAPPERS:
            out.pop(0)
            while out and out[0].startswith("-"):
                out.pop(0)
        elif head == "uv" and len(out) > 1 and out[1] == "run":
            out = out[2:]
        else:
            break
    return out, privileged


def _args(words: list[str]) -> list[str]:
    return [w for w in words[1:] if not w.startswith("-") or w == "-"]


def _flags(words: list[str]) -> str:
    return "".join(
        w.lstrip("-") if not w.startswith("--") else ""
        for w in words[1:]
        if w.startswith("-")
    )


def _normalize(path: str, workspace_root: str) -> str:
    expanded = os.path.expanduser(path)
    if workspace_root and not os.path.isabs(expanded):
        expanded = os.path.join(workspace_root, expanded)
    return os.path.normpath(expanded)


def _is_oracle_pattern(path: str) -> bool:
    pure = PurePosixPath(path.replace("\\", "/"))
    name = pure.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in ORACLE_FILE_PATTERNS):
        return True
    return any(part.lower() in _ORACLE_DIR_NAMES for part in pure.parts[:-1])


def _is_protected(path: str, protected: Collection[str], workspace_root: str) -> bool:
    norm = _normalize(path, workspace_root)
    for item in protected:
        other = _normalize(item, workspace_root)
        if norm == other or norm.startswith(other.rstrip("/") + "/"):
            return True
        if not os.path.isabs(item) and norm.endswith("/" + item.lstrip("./")):
            return True
    return False


_TEST_EDIT_AUTH = re.compile(
    r"\b(edit|modify|update|change|fix|write|add|create|rewrite|extend|refactor|rename|delete|remove)\b"
    r"[^.\n]{0,60}\b(tests?|test files?|test suites?|specs?|evaluators?|graders?|scorers?|judges?)\b",
    re.IGNORECASE,
)


def _oracle_authorized(instruction: str) -> bool:
    return bool(_TEST_EDIT_AUTH.search(instruction or ""))


def _instruction_mentions(instruction: str, *words: str) -> bool:
    return any(
        re.search(
            rf"(?<![\w-]){re.escape(w)}(?![\w-])", instruction or "", re.IGNORECASE
        )
        for w in words
    )


_DANGEROUS_HOME = ("~", "$HOME", "${HOME}")
_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/var", "/home", "/root", "/boot",
    "/opt", "/Users", "/System", "/Library", "/dev", "/sys", "/proc", "/mnt",
)  # fmt: skip


def _is_dangerous_target(target: str, workspace_root: str) -> bool:
    t = target.rstrip("/") or "/"
    if t in ("/", "/*"):
        return True
    for home in _DANGEROUS_HOME:
        if t == home or t.startswith(home + "/"):
            return True
    if t == ".." or t.startswith("../"):
        return True
    if os.path.isabs(t):
        norm = os.path.normpath(t.replace("/*", ""))
        if workspace_root:
            root = os.path.normpath(workspace_root)
            return not (norm == root or norm.startswith(root + os.sep))
        parts = [p for p in norm.split("/") if p]
        return (
            len(parts) <= 1
            or any(norm == p or norm.startswith(p + "/") for p in _SYSTEM_PREFIXES)
            and len(parts) <= 2
        )
    return False


_POWER_CMDS = frozenset({"shutdown", "reboot", "halt", "poweroff", "init"})
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:?\s*&?[^}]*\}\s*;?\s*:")

_NET_TOOLS = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "scp",
        "sftp",
        "rsync",
        "ftp",
        "socat",
        "telnet",
    }
)
_NET_PY = re.compile(
    r"requests\.(post|put)|httpx\.(post|put)|urlopen|http\.client|socket\.",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?:^|[/\s'\"=@~$])"
    r"(?:\.bashrc|\.bash_profile|\.zshrc|\.profile|\.env(?![a-z])|\.netrc|\.npmrc|\.pypirc|"
    r"\.ssh|\.aws|\.gnupg|\.kube|\.docker/config\.json|id_rsa|id_ed25519|credentials?|"
    r"secrets?|[\w.-]+\.pem|[\w.-]+\.key|etc/passwd|etc/shadow)(?![\w])",
    re.IGNORECASE,
)

_READ_ONLY_CMDS = frozenset(
    {
        "cat", "ls", "grep", "egrep", "fgrep", "rg", "ag", "head", "tail", "wc", "pwd",
        "echo", "printf", "cd", "which", "type", "diff", "sort", "uniq", "cut", "tr",
        "stat", "file", "du", "df", "test", "[", "true", "false", "date", "whoami", "id",
        "uname", "tree", "less", "more", "basename", "dirname", "realpath", "readlink",
        "pytest", "tac", "nl", "column", "jq", "hostname", "ps", "env", "printenv", "sleep",
        "export", "set", "unset", "alias", "ll", "md5sum", "sha256sum", "shasum", "xxd",
        "od", "strings", "lsof", "history", "man", "help", "tput", "clear",
    }
)  # fmt: skip
_READ_ONLY_GIT = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "branch",
        "ls-files",
        "blame",
        "rev-parse",
        "remote",
        "grep",
    }
)


def _write_targets(
    cmd: str, words: list[str], redirects: list[str], raw: str
) -> list[str]:
    """Paths this single command may write to, removed, or move."""
    targets = list(redirects)
    args = _args(words)
    if cmd == "tee":
        targets += args
    elif cmd == "sed" and re.search(r"(^|\s)-\w*i", " ".join(words[1:])):
        targets += args[1:] if len(args) > 1 else []
    elif cmd in ("mv", "cp", "ln", "install", "rsync"):
        targets += args
    elif cmd in (
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "truncate",
        "touch",
        "chmod",
        "chown",
        "mkdir",
    ):
        targets += args[1:] if cmd in ("chmod", "chown") and args else args
    elif cmd in ("python", "python3", "perl", "ruby", "node") or cmd.startswith(
        "python"
    ):
        if re.search(
            r"open\([^)]*['\"][wa]\+?b?['\"]|write_text|write_bytes|\.write\(|shutil\.(move|copy|rmtree)|os\.(remove|unlink|rename)|writeFile",
            raw,
        ):
            targets += re.findall(
                r"""['"]([\w./~$-][^'"\s]*\.[\w]+|/[^'"\s]+)['"]""", raw
            )
    elif cmd == "awk":
        if re.search(r"(print|printf)[^;}]*>", raw) or "system(" in raw:
            targets += re.findall(r"""['"]([\w./~-]+)['"]""", raw)
    elif cmd == "find":
        if "-delete" in words or "-exec" in words or "-execdir" in words:
            targets += [
                w for w in words[1:] if not w.startswith("-") and not w.startswith("!")
            ][:1]
    return [t for t in targets if t]


def _classify_stage(
    stage: str, instruction: str, workspace_root: str
) -> tuple[ShellEffectKind, str, list[str], list[str]]:
    """Classify one simple command. Returns (kind, reason, targets, oracle_checkable_targets)."""
    words, redirects = _tokenize(stage)
    words, _privileged = _strip_wrappers(words)
    if not words:
        if redirects:
            return (
                ShellEffectKind.SCOPED_MUTATION,
                "redirect writes a file",
                redirects,
                redirects,
            )
        return ShellEffectKind.READ_ONLY, "empty command", [], []
    cmd = os.path.basename(words[0])
    args = _args(words)
    flags = _flags(words)

    # ---- destructive / privileged ----
    destructive = ""
    auth_words: tuple[str, ...] = ()
    if cmd in ("mount", "umount", "remount", "unmount") or cmd.startswith("mount."):
        destructive, auth_words = (
            f"{cmd} changes filesystem mounts",
            ("mount", "remount", "umount", "unmount"),
        )
    elif cmd == "unshare" and (
        _privileged
        or "m" in flags
        or "r" in flags
        or any(w.startswith(("--mount", "--map-root-user")) for w in words[1:])
    ):
        destructive, auth_words = (
            "unshare creates a mount or user namespace",
            ("unshare", "namespace"),
        )
    elif cmd.startswith("mkfs") or cmd in ("fdisk", "parted", "wipefs", "mke2fs"):
        destructive, auth_words = (
            f"{cmd} writes a filesystem or partition table",
            (cmd, "mkfs", "format"),
        )
    elif cmd == "dd" and any(w.startswith("of=/dev/") for w in words[1:]):
        destructive, auth_words = "dd writes to a raw device", ("dd",)
    elif cmd == "rm" and ("r" in flags.lower() or "--recursive" in words):
        bad = [a for a in args if _is_dangerous_target(a, workspace_root)]
        if bad:
            destructive, auth_words = (
                f"recursive rm outside the workspace scope: {bad[0]}",
                ("rm -rf", "rm -r", "delete everything", "wipe"),
            )
    elif cmd in ("chmod", "chown", "chgrp") and "R" in flags:
        bad = [
            a
            for a in args[1:]
            if _is_dangerous_target(a, workspace_root)
            or a.rstrip("/") in ("/home", "/etc", "/usr", "/var")
        ]
        if bad:
            destructive, auth_words = (
                f"recursive {cmd} on {bad[0]}",
                (f"{cmd} -R", f"recursive {cmd}"),
            )
    elif cmd == "kill" and "-9" in words and "-1" in words:
        destructive, auth_words = "kill -9 -1 terminates every process", ("kill -9 -1",)
    elif cmd in (
        "ufw",
        "iptables",
        "ip6tables",
        "nft",
        "setenforce",
        "firewall-cmd",
        "pfctl",
    ):
        joined = " ".join(words[1:])
        if re.search(
            r"(?<!\S)(disable|-F|--flush|flush|0|permissive|off|-X)(?!\S)", joined
        ):
            destructive, auth_words = (
                f"{cmd} disables the firewall or access control",
                ("firewall", cmd),
            )
    elif (
        cmd in ("systemctl", "service")
        and re.search(r"\b(stop|disable|mask)\b", " ".join(words[1:]))
        and re.search(
            r"firewall|ufw|iptables|sshd|auditd|selinux|apparmor", " ".join(words), re.I
        )
    ):
        destructive, auth_words = (
            f"{cmd} stops a security service",
            ("firewall", "sshd", "auditd"),
        )
    elif cmd in ("passwd", "usermod", "visudo", "userdel", "deluser") and (
        cmd != "passwd" or "-d" in words
    ):
        destructive, auth_words = (
            f"{cmd} changes accounts or authentication",
            (cmd, "password", "sudoers"),
        )
    elif cmd in _POWER_CMDS:
        destructive, auth_words = f"{cmd} stops the machine", (cmd,)
    if destructive:
        if _instruction_mentions(instruction, *auth_words):
            return (
                ShellEffectKind.SCOPED_MUTATION,
                f"authorized by instruction: {destructive}",
                args,
                [],
            )
        return ShellEffectKind.DESTRUCTIVE_PRIVILEGED, destructive, args, []

    # ---- writes ----
    targets = _write_targets(cmd, words, redirects, stage)
    if targets:
        return (
            ShellEffectKind.SCOPED_MUTATION,
            f"{cmd} writes {targets[0]}",
            targets,
            targets,
        )

    # ---- read only ----
    if cmd == "curl" and not any(
        w in ("-o", "-O", "--output", "--remote-name", "-T", "--upload-file")
        for w in words
    ):
        return ShellEffectKind.READ_ONLY, "curl prints to stdout", [], []
    if cmd in _READ_ONLY_CMDS or cmd in ("awk", "find", "sed", "gawk", "sort"):
        return ShellEffectKind.READ_ONLY, f"{cmd} only reads", [], []
    if cmd == "git":
        sub = next((a for a in args), "")
        if sub in _READ_ONLY_GIT:
            return ShellEffectKind.READ_ONLY, f"git {sub} only reads", [], []
        return (
            ShellEffectKind.SCOPED_MUTATION,
            f"git {sub} changes repository state",
            [],
            [],
        )
    joined = " ".join(words)
    if re.search(r"(^|\s)(pytest|unittest)(\s|$)", joined) and cmd.startswith(
        ("python", "pytest", "tox", "nox")
    ):
        return ShellEffectKind.READ_ONLY, "test run", [], []
    if cmd in ("python", "python3") and "-m" in words:
        mod = words[words.index("-m") + 1 : words.index("-m") + 2]
        if mod and mod[0] in ("pytest", "unittest"):
            return ShellEffectKind.READ_ONLY, "test run", [], []
    return ShellEffectKind.SCOPED_MUTATION, f"{cmd} may change workspace state", [], []


def _is_exfil(chunk: str) -> bool:
    has_net = False
    for stage in _split_quoted(chunk, ["|", "(", ")", "`", "&&", "||", ";"]):
        words, _ = _tokenize(stage)
        words, _ = _strip_wrappers(words)
        if words and os.path.basename(words[0]) in _NET_TOOLS:
            has_net = True
    if not has_net and not _NET_PY.search(chunk):
        return False
    return bool(_SENSITIVE.search(chunk))


_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_MAX_SCRIPT_BYTES = 64 * 1024
_MAX_SCRIPT_DEPTH = 8


def _script_ref(stage: str) -> str:
    """Return the script file a simple command runs, or an empty string."""
    words, _ = _tokenize(stage)
    words, _ = _strip_wrappers(words)
    if not words:
        return ""
    head = os.path.basename(words[0])
    if head in _SHELL_NAMES:
        rest = [w for w in words[1:] if not w.startswith("-")]
        return rest[0] if rest and rest[0].endswith((".sh", ".bash")) else ""
    if words[0] in ("source", ".") and len(words) > 1:
        return words[1]
    if words[0].endswith((".sh", ".bash")):
        return words[0]
    return ""


def _find_script(ref: str, workspace_root: str) -> Path | None:
    root = os.path.realpath(workspace_root)
    cand = os.path.realpath(_normalize(ref, workspace_root))
    if (cand == root or cand.startswith(root + os.sep)) and os.path.isfile(cand):
        return Path(cand)
    name = os.path.basename(ref)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if name in filenames:
            return Path(dirpath) / name
    return None


def _script_body(script: Path) -> str:
    try:
        with script.open("rb") as handle:
            data = handle.read(_MAX_SCRIPT_BYTES)
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def classify_shell_command(
    command: str,
    *,
    task_instruction: str = "",
    protected_oracle_paths: Collection[str] = (),
    workspace_root: str = "",
) -> ShellClassification:
    """Classify a shell command by its worst-case effect on the environment."""
    return _classify_impl(
        command,
        task_instruction,
        protected_oracle_paths,
        workspace_root,
        frozenset(),
    )


def _classify_impl(
    command: str,
    task_instruction: str,
    protected_oracle_paths: Collection[str],
    workspace_root: str,
    seen: frozenset[str],
) -> ShellClassification:
    text = command or ""
    if _FORK_BOMB.search(text):
        return ShellClassification(
            ShellEffectKind.DESTRUCTIVE_PRIVILEGED, True, True,
            _TRAP_CODES[ShellEffectKind.DESTRUCTIVE_PRIVILEGED], "fork bomb", (),
        )  # fmt: skip

    worst = ShellEffectKind.READ_ONLY
    reason = "no side effects detected"
    all_targets: list[str] = []
    oracle_hit: str | None = None
    oracle_ok = _oracle_authorized(task_instruction)

    def raise_to(kind: ShellEffectKind, why: str) -> None:
        nonlocal worst, reason
        if _SEVERITY[kind] > _SEVERITY[worst] or (
            kind == worst and worst == ShellEffectKind.READ_ONLY
        ):
            worst, reason = kind, why

    for chunk in _split_quoted(text, ["&&", "||", ";", "\n"]):
        if _is_exfil(chunk):
            raise_to(
                ShellEffectKind.NETWORK_EXFIL,
                "sensitive local data sent to a network tool",
            )
        for stage in _split_quoted(chunk, ["|", "&", "(", ")", "`", "{", "}"]):
            kind, why, targets, checkable = _classify_stage(
                stage, task_instruction, workspace_root
            )
            all_targets.extend(targets)
            ref = _script_ref(stage) if workspace_root else ""
            script = _find_script(ref, workspace_root) if ref else None
            if script is not None and str(script) not in seen:
                if len(seen) < _MAX_SCRIPT_DEPTH:
                    inner = _classify_impl(
                        _script_body(script),
                        task_instruction,
                        protected_oracle_paths,
                        workspace_root,
                        seen | {str(script)},
                    )
                    if inner.blocked:
                        raise_to(inner.kind, f"script {ref} contains: {inner.reason}")
            for path in checkable:
                explicit = _is_protected(path, protected_oracle_paths, workspace_root)
                pattern = _is_oracle_pattern(path)
                if explicit and not _instruction_mentions(
                    task_instruction, os.path.basename(path)
                ):
                    oracle_hit = path
                elif pattern and not oracle_ok:
                    oracle_hit = path
            raise_to(kind, why)
    if oracle_hit is not None:
        raise_to(
            ShellEffectKind.ORACLE_TAMPER,
            f"modifies test or evaluator file {oracle_hit}",
        )

    return ShellClassification(
        kind=worst,
        is_commit=worst != ShellEffectKind.READ_ONLY,
        blocked=worst in _TRAP_CODES,
        trap_code=_TRAP_CODES.get(worst),
        reason=reason,
        target_paths=tuple(dict.fromkeys(all_targets)),
    )


def classify_file_write(
    path: str,
    content: str,
    *,
    task_instruction: str = "",
    protected_oracle_paths: Collection[str] = (),
    workspace_root: str = "",
) -> ShellClassification:
    """Classify writing ``content`` to ``path`` before it reaches the disk."""
    explicit = _is_protected(path, protected_oracle_paths, workspace_root)
    if explicit and not _instruction_mentions(task_instruction, os.path.basename(path)):
        hit = True
    else:
        hit = _is_oracle_pattern(path) and not _oracle_authorized(task_instruction)
    if hit:
        return ShellClassification(
            ShellEffectKind.ORACLE_TAMPER,
            True,
            True,
            _TRAP_CODES[ShellEffectKind.ORACLE_TAMPER],
            f"modifies test or evaluator file {path}",
            (path,),
        )
    body = content or ""
    if path.endswith((".sh", ".bash")) or body.startswith(
        ("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash", "#!/usr/bin/env sh")
    ):
        inner = classify_shell_command(
            "\n".join(
                ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
            ),
            task_instruction=task_instruction,
            protected_oracle_paths=protected_oracle_paths,
            workspace_root=workspace_root,
        )
        if inner.blocked:
            return ShellClassification(
                inner.kind,
                True,
                True,
                inner.trap_code,
                f"script {path} contains: {inner.reason}",
                (path,),
            )
    return ShellClassification(
        ShellEffectKind.SCOPED_MUTATION,
        True,
        False,
        None,
        f"writes {path}",
        (path,),
    )


# --------------------------------------------------------------------------
# Oracle immutability and regression gate
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class ImmutableOracleGuard:
    """Detect edits to test, judge, and evaluator files after Step 0."""

    @staticmethod
    def snapshot_directory(root: Path) -> dict[str, str]:
        root = Path(root)
        snap: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                if _is_oracle_pattern(rel):
                    try:
                        snap[rel] = _sha256(full)
                    except OSError:
                        continue
        return snap

    @staticmethod
    def verify_intact(
        root: Path, snapshot: Mapping[str, str]
    ) -> tuple[bool, list[str]]:
        root = Path(root)
        bad: list[str] = []
        for rel, digest in snapshot.items():
            full = root / rel
            try:
                if not full.is_file() or _sha256(full) != digest:
                    bad.append(rel)
            except OSError:
                bad.append(rel)
        return (not bad, sorted(bad))


class PostEditTestGate:
    """Run existing project tests before allowing a task to finish."""

    @staticmethod
    def verify_regression(
        run_test_fn: Callable[[], tuple[bool, str]],
    ) -> tuple[bool, str]:
        try:
            passed, summary = run_test_fn()
        except Exception as exc:  # the runner itself failing is a failed gate
            return False, f"test runner raised {type(exc).__name__}: {exc}"
        if passed:
            return True, ""
        return False, summary or "existing tests failed"


# --------------------------------------------------------------------------
# Monotonic egress guard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EgressCheckResult:
    action: str  # ALLOW | STRIP_MONOTONIC | REPAIR_ONCE | ABSTAIN
    safe_items: list[str] = field(default_factory=list)
    stripped_items: list[str] = field(default_factory=list)
    missing_anchors: list[str] = field(default_factory=list)
    trap_code: str | None = None
    reason: str = ""


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        out: list[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_flatten(item))
        return out
    if isinstance(value, Sequence | set | frozenset):
        return [s for item in value for s in _flatten(item)]
    return [str(value)]


def _anchors_present(anchors: Sequence[str], items: Sequence[str]) -> set[str]:
    haystack = _norm(" \n ".join(items))
    return {a for a in anchors if _norm(a) in haystack}


def evaluate_outbound_items(
    candidate_items: Sequence[str],
    *,
    task_prompt: str,
    containers: Mapping[str, Any] | None = None,
    required_anchors: Sequence[str] = (),
    item_auditor: Callable[..., bool] | None = None,
) -> EgressCheckResult:
    """Decide whether outbound items may leave, are stripped, or need one repair.

    ``containers`` maps a container name to its contents. A container is in
    scope when the task prompt names it. An item that appears only in an
    out-of-scope container came from somewhere the task never mentioned.
    ``item_auditor(task_prompt, item)`` or ``item_auditor(task_prompt, item,
    context_meta)`` is an optional independent judge: ``True`` keeps the item,
    ``False`` marks it foreign. ``context_meta`` holds ``source_containers``,
    ``all_containers`` and ``sibling_items``.
    """
    items = [str(i) for i in candidate_items]
    prompt_norm = _norm(task_prompt)
    in_scope_text: list[str] = []
    out_scope_text: list[str] = []
    container_text: dict[str, list[str]] = {}
    three_arg = False
    if item_auditor is not None:
        try:
            params = inspect.signature(item_auditor).parameters.values()
            three_arg = any(p.kind == p.VAR_POSITIONAL for p in params) or (
                len(
                    [
                        p
                        for p in params
                        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    ]
                )
                >= 3
            )
        except (TypeError, ValueError):
            three_arg = False
    for name, content in (containers or {}).items():
        container_text[str(name)] = [_norm(s) for s in _flatten(content)]
        target = (
            in_scope_text
            if _norm(name) and _norm(name) in prompt_norm
            else out_scope_text
        )
        target.extend(_norm(s) for s in _flatten(content))

    safe: list[str] = []
    stripped: list[str] = []
    for item in items:
        n = _norm(item)
        foreign = False
        if n and n not in prompt_norm and not any(n in s for s in in_scope_text):
            foreign = any(n in s for s in out_scope_text)
        if not foreign and item_auditor is not None:
            if three_arg:
                meta = {
                    "source_containers": [
                        k
                        for k, texts in container_text.items()
                        if any(n in t for t in texts)
                    ]
                    if n
                    else [],
                    "all_containers": list(container_text),
                    "sibling_items": list(candidate_items),
                }
                foreign = not item_auditor(task_prompt, item, meta)
            else:
                foreign = not item_auditor(task_prompt, item)
        (stripped if foreign else safe).append(item)

    present = _anchors_present(required_anchors, safe)
    missing = [a for a in required_anchors if a not in present]

    if not items or not safe:
        return EgressCheckResult(
            "ABSTAIN",
            safe,
            stripped,
            missing,
            "EMPTY_EGRESS",
            "no in-scope items to send",
        )
    if missing:
        return EgressCheckResult(
            "REPAIR_ONCE", safe, stripped, missing, "MISSING_REQUIRED_ANCHOR",
            f"required anchors absent from in-scope items: {missing}",
        )  # fmt: skip
    if stripped:
        return EgressCheckResult(
            "STRIP_MONOTONIC", safe, stripped, [], "FOREIGN_CONTEXT_BLEED",
            f"removed {len(stripped)} out-of-scope item(s)",
        )  # fmt: skip
    return EgressCheckResult("ALLOW", safe, [], [], None, "all items in scope")


def select_best_egress(
    turn1_items: Sequence[str],
    turn2_items: Sequence[str],
    *,
    required_anchors: Sequence[str] = (),
    flagged_bleed_items: Collection[str] = (),
) -> list[str]:
    """Pick the outbound list after a repair retry without losing utility.

    If the retry is empty or loses an anchor that turn 1 had, return turn 1 with
    only the flagged items removed. Otherwise return the retry with flagged
    items removed.
    """
    flagged = {_norm(i) for i in flagged_bleed_items}

    def clean(items: Sequence[str]) -> list[str]:
        return [i for i in items if _norm(i) not in flagged]

    turn1 = list(turn1_items)
    turn2 = list(turn2_items)
    if not turn2:
        return clean(turn1)
    had = _anchors_present(required_anchors, turn1)
    kept = _anchors_present(required_anchors, turn2)
    if had - kept:
        return clean(turn1)
    result = clean(turn2)
    return result if result else clean(turn1)


_UI_KEYS = (
    "elements",
    "screenshot",
    "a11y_tree",
    "ui_tree",
    "windows",
    "dom",
    "accessibility_tree",
)
_ANSWER_ACTIONS = frozenset(
    {
        "answer",
        "respond",
        "reply",
        "final_answer",
        "send_message",
        "say",
        "message",
        "return_answer",
    }
)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def is_single_turn_answer_task(observation: Any, action: Any) -> bool:
    """True when the task is a plain question-and-answer with no interface to reopen."""
    for key in ("interactive", "has_ui"):
        if _get(observation, key) is False:
            return True
    if _get(observation, "single_turn") is True:
        return True
    if any(_get(observation, key) for key in _UI_KEYS):
        return False
    kind = _get(action, "type") or _get(action, "kind") or _get(action, "action_type")
    if isinstance(action, str):
        kind = action
    kind_text = str(getattr(kind, "value", kind) or "").lower()
    if kind_text in _ANSWER_ACTIONS:
        return True
    return bool(_get(action, "answer") or _get(action, "final_answer"))


__all__ = [
    "EgressCheckResult",
    "ImmutableOracleGuard",
    "PostEditTestGate",
    "ShellClassification",
    "ShellEffectKind",
    "classify_shell_command",
    "evaluate_outbound_items",
    "is_single_turn_answer_task",
    "select_best_egress",
]
