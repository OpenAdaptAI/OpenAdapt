"""Show what a run authorization allows before anyone signs it.

``openadapt-flow local-seal authorize sign STATEMENT --key ~/.ssh/id_ed25519
--bundle BUNDLE`` prints a plain-text summary (no colour, about 30 lines):
who is signing and whether that key is the one GitHub lists, the program the
statement names (read from a local bundle whose recomputed content digest
equals ``bundle_content_digest``; any other bundle is refused), an outline of
its steps with the one consequential write and the stop rules, the scope, and
what the authorization does not allow. Then it asks, and only on "y" runs
``ssh-keygen -Y sign -n openadapt-run-authorization``. A key GitHub doesn't
list for the authorizer gets a warning and needs "yes" typed out.
``--dry-run`` prints the summary and signs nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_flow import ssh_sig
from openadapt_flow.run_authorization import (
    NAMESPACE,
    RunAuthorizationV1,
    _paths,
)

_WRITE_RISKS = {"irreversible", "consequential"}


_ANSI = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


def clean(value: Any) -> str:
    """Text safe to print: ANSI escapes and every control or format character removed."""
    import unicodedata

    text = _ANSI.sub("", str(value))
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("C"))


class SignRefused(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def bundle_digest(bundle_dir: Path) -> str:
    """Recompute a bundle's content digest from its files (not from its manifest)."""
    from openadapt_flow.bundle_validation import (
        compute_content_digest,
        compute_file_hashes,
    )
    from openadapt_flow.ir import Workflow

    workflow = Workflow.model_validate_json(
        (Path(bundle_dir) / "workflow.json").read_text(encoding="utf-8")
    )
    return compute_content_digest(workflow, compute_file_hashes(workflow, bundle_dir))


def matching_bundle(digest: str, candidates: Sequence[Path]) -> Optional[Path]:
    for candidate in candidates:
        try:
            if (Path(candidate) / "workflow.json").is_file() and bundle_digest(
                Path(candidate)
            ) == digest:
                return Path(candidate)
        except Exception:  # an unreadable bundle simply doesn't match
            continue
    return None


# --- the program ------------------------------------------------------------------


def _label(step: Any) -> str:
    intent = str(getattr(step, "intent", "") or "")
    m = re.match(r"^\w+ '(.+)'$", intent)
    return m.group(1) if m else intent or str(step.id)


def _field(step: Any) -> str:
    label = getattr(step, "field_label", None)
    if label and not str(label).startswith("form_"):
        return str(label)
    return str(step.param)


def _is_write(step: Any) -> bool:
    return bool(getattr(step, "effects", None)) or (
        str(getattr(step, "risk", "") or "").lower() in _WRITE_RISKS
    )


def _kind(postcondition: Any) -> str:
    kind = getattr(postcondition, "kind", "")
    return str(getattr(kind, "value", kind))


@dataclass(frozen=True)
class ProgramOutline:
    name: str
    app: str
    steps: int
    sections: list[tuple[str, int]]
    writes: list[str]
    stops: list[str]


def outline(bundle_dir: Path) -> ProgramOutline:
    """Sections (compiled ones if present, else one per field filled), writes, stops."""
    from openadapt_flow.ir import Workflow

    wf = Workflow.model_validate_json(
        (Path(bundle_dir) / "workflow.json").read_text(encoding="utf-8")
    )
    steps = list(wf.steps)
    env = getattr(getattr(wf, "qualification", None), "environment", None)
    app = str(getattr(env, "application", "") or "") or wf.name.split("-", 1)[0]
    sections: list[tuple[str, int]] = []
    compiled = getattr(wf, "sections", None)
    if compiled:
        for section in compiled:
            name = getattr(section, "name", None) or getattr(
                section, "title", "section"
            )
            count = len(
                getattr(section, "steps", None) or getattr(section, "step_ids", [])
            )
            sections.append((str(name), count))
    else:
        pending = 0
        for step in steps:
            if _is_write(step):
                continue
            pending += 1
            if getattr(step, "param", None):
                sections.append((_field(step), pending))
                pending = 0
            elif step is steps[-1] or _is_write(steps[steps.index(step) + 1]):
                sections.append((_label(step), pending))
                pending = 0
        if pending:
            sections.append(("other steps", pending))
    writes, stops = [], []
    write_steps = [s for s in steps if _is_write(s)]
    for step in write_steps:
        effects = list(getattr(step, "effects", None) or [])
        checked = f" · checked by {len(effects)} record read-backs" if effects else ""
        writes.append(f"{_label(step)} ({step.id}){checked}")
    if write_steps:
        first = steps.index(write_steps[0])
        before = steps[first - 1] if first > 0 else None
        for post in list(getattr(before, "expect", None) or []):
            if _kind(post) == "text_present" and getattr(post, "text", None):
                stops.append(f"'{post.text}' absent")
        if any(getattr(s, "identity_armed", None) for s in steps):
            stops.append("identity unreadable")
        if any(getattr(s, "effects", None) for s in write_steps):
            stops.append("record not confirmed")
    return ProgramOutline(wf.name, app, len(steps), sections, writes, stops)


def qualification_line(bundle_dir: Path) -> str:
    """'certified clinical-write · 6/6 cases · real-duplicate case halts (3/3)'."""
    line, _ = qualification(bundle_dir)
    return line


def qualification(bundle_dir: Path) -> tuple[str, bool]:
    """The qualification summary, and whether the bundle digest covers its report."""
    for folder in (Path(bundle_dir), Path(bundle_dir).parent):
        report = folder / "QUALIFICATION.md"
        if report.is_file():
            return (
                _parse_qualification(report.read_text(encoding="utf-8")),
                _covered_by_digest(Path(bundle_dir), report),
            )
    return "no qualification report next to the bundle", False


def _covered_by_digest(bundle_dir: Path, report: Path) -> bool:
    """True only when the report's exact bytes are a hashed file of the bundle."""
    import hashlib

    from openadapt_flow.bundle_validation import compute_file_hashes
    from openadapt_flow.ir import Workflow

    try:
        rel = report.resolve().relative_to(bundle_dir.resolve()).as_posix()
        wf = Workflow.model_validate_json(
            (bundle_dir / "workflow.json").read_text("utf-8")
        )
        hashes = compute_file_hashes(wf, bundle_dir)
    except (ValueError, OSError):
        return False
    return hashes.get(rel) == hashlib.sha256(report.read_bytes()).hexdigest()


def _table(lines: list[str], header_start: str) -> list[list[str]]:
    rows, inside = [], False
    for line in lines:
        if line.startswith(header_start):
            inside = True
            continue
        if inside:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= {"-"}:
                continue
            rows.append(cells)
    return rows


def _parse_qualification(text: str) -> str:
    parts = []
    m = re.search(r"Verdict:\s*(PASS|FAIL)(?:,\s*certified under `([^`]+)`)?", text)
    if m:
        parts.append(
            f"certified {m.group(2)}"
            if m.group(1) == "PASS" and m.group(2)
            else f"verdict {m.group(1)}"
        )
    lines = text.splitlines()
    cases: dict[str, bool] = {}
    for row in _table(lines, "| Case | Trial | Expected | Observed"):
        if len(row) >= 4:
            cases[row[0]] = cases.get(row[0], True) and row[2] == row[3]
    if cases:
        parts.append(f"{sum(cases.values())}/{len(cases)} cases")
    if "## Real-duplicate check" in text:
        section = text.split("## Real-duplicate check", 1)[1].splitlines()
        trials = _table(section, "| Trial |")
        passed = sum(1 for row in trials if row and row[-1].lower() == "yes")
        if trials:
            verdict = "halts" if passed == len(trials) else "did not always halt"
            parts.append(f"real-duplicate case {verdict} ({passed}/{len(trials)})")
    return " · ".join(parts) or "qualification report has no verdict"


# --- the signer ---------------------------------------------------------------------


def key_public_raw(key_path: Path, run: Callable[..., Any] = subprocess.run) -> bytes:
    """The Ed25519 public key for an SSH private key file (from KEY.pub, else ssh-keygen -y)."""
    pub = Path(str(key_path) + ".pub")
    if pub.is_file():
        return ssh_sig.parse_key_line(pub.read_text(encoding="utf-8"))
    out = run(["ssh-keygen", "-y", "-f", str(key_path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise SignRefused("key_unreadable", f"ssh-keygen -y -f {key_path} failed")
    return ssh_sig.parse_key_line(out.stdout)


def _snapshot(statement_path: Path) -> tuple[list[ssh_sig.AllowedSigner], str]:
    _, _, snapshot_path, _ = _paths(statement_path)
    text = snapshot_path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text else ""
    when = first.rsplit(" at ", 1)[-1] if first.startswith("#") else "unknown time"
    return ssh_sig.parse_allowed_signers(text), when


def render(
    statement_path: Path,
    statement: RunAuthorizationV1,
    statement_sha256: str,
    bundle_dir: Path,
    key_raw: bytes,
    on_github: bool,
    snapshot_time: str,
) -> str:
    prog = outline(bundle_dir)
    user = statement.github_user()
    tick = "✓" if on_github else "✗"
    listed = "listed on" if on_github else "NOT listed on"
    digest = statement.bundle_content_digest
    scope = statement.scope
    sections = [(clean(n), c) for n, c in prog.sections]
    width = max([len(n) for n, _ in sections] + [12])
    qual, bound = qualification(bundle_dir)
    lines = [
        "You are authorizing OpenAdapt to run this program",
        f"  Authorizer   {statement.authorizer}",
        f"  Signing key  ED25519 {ssh_sig.fingerprint(key_raw)} (the key the statement names)",
        f"               {tick} {listed} github.com/{user}.keys "
        f"(copy taken {clean(snapshot_time)})",
        "",
        f"Program        {clean(prog.name)}",
        f"  App          {clean(prog.app)}",
        f"  Bundle       {digest[:12]}…{digest[-4:]} ({prog.steps} steps)",
        f"  Qualified    {clean(qual)}",
        *(
            []
            if bound
            else [
                "               (QUALIFICATION.md beside the bundle; not covered by its digest)"
            ]
        ),
        "",
        "Outline",
        *(
            f"  {name:<{width}}  {n} step{'s' if n != 1 else ''}"
            for name, n in sections
        ),
        *(f"  ▶ writes: {clean(w)}" for w in prog.writes),
        *(
            ["  stops if: " + " · ".join(clean(x) for x in prog.stops)]
            if prog.stops
            else []
        ),
        "",
        "Scope",
        f"  Items        {', '.join(scope.items)}",
        f"  Profile      {scope.profile}",
        f"  Seal keys    {', '.join(scope.seal_key_ids)}",
        f"  Issued at    {statement.issued_at}",
        "  Each item   "
        + ("may be used for many runs" if scope.reusable else "one run only"),
        "  Not allowed  any other bundle, item, profile or Seal key",
        "",
        f"Statement      sha256 {statement_sha256}",
        f"  Path         {Path(statement_path).resolve()}",
    ]
    return "\n".join(lines)


def prepare(
    statement_path: Path,
    key_path: Path,
    bundles: Sequence[Path],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[str, bool, bytes]:
    """``(summary, key_on_github, statement bytes shown)``; refuse what can't be shown truthfully."""
    import hashlib

    from openadapt_flow.offline_seal import loads_strict

    statement_path, sig_path, _, _ = _paths(Path(statement_path))
    try:
        raw = statement_path.read_bytes()
        statement = RunAuthorizationV1.model_validate(loads_strict(raw))
    except Exception as exc:
        raise SignRefused("auth_malformed", str(statement_path)) from exc
    if sig_path.exists():
        raise SignRefused("auth_already_signed", f"{sig_path} exists")
    candidates = list(bundles) or [statement_path.parent / "bundle"]
    bundle = matching_bundle(statement.bundle_content_digest, candidates)
    if bundle is None:
        checked = ", ".join(str(c) for c in candidates)
        raise SignRefused(
            "auth_bundle_unknown",
            f"no local bundle has content digest {statement.bundle_content_digest} "
            f"(checked {checked}); pass the bundle with --bundle",
        )
    key_raw = key_public_raw(Path(key_path), run=run)
    if ssh_sig.fingerprint(key_raw) != statement.signer_fingerprint:
        raise SignRefused(
            "auth_signer_mismatch",
            f"this statement names key {statement.signer_fingerprint}; "
            f"{key_path} is {ssh_sig.fingerprint(key_raw)}",
        )
    try:
        signers, when = _snapshot(statement_path)
    except (OSError, ssh_sig.SshSigError):
        signers, when = [], "missing"
    on_github = ssh_sig.is_allowed(signers, statement.authorizer, NAMESPACE, key_raw)
    summary = render(
        statement_path,
        statement,
        hashlib.sha256(raw).hexdigest(),
        bundle,
        key_raw,
        on_github,
        when,
    )
    return summary, on_github, raw


def sign(
    statement_path: Path,
    key_path: Path,
    bundles: Sequence[Path],
    *,
    dry_run: bool = False,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    run: Callable[..., Any] = subprocess.run,
) -> int:
    """Print the summary, ask, and sign with ssh-keygen. 0 signed or dry run, 1 not signed."""
    from openadapt_flow.run_authorization import verify

    statement_path, sig_path, _, _ = _paths(Path(statement_path))
    try:
        summary, on_github, shown = prepare(statement_path, key_path, bundles, run=run)
    except SignRefused as exc:
        say(f"not signed: {exc}")
        return 1
    say(summary)
    if dry_run:
        say("\n(dry run: nothing signed)")
        return 0
    if on_github:
        answer = ask("\nSign this authorization? [y/N] ").strip().lower()
        agreed = answer in ("y", "yes")
    else:
        say(
            "\nWARNING: the copy of the GitHub keys next to this statement does not "
            "list this key for the authorizer."
        )
        agreed = ask('Type "yes" to sign anyway: ').strip() == "yes"
    if not agreed:
        say("not signed")
        return 1
    # Sign exactly the bytes shown, from a private copy, so an edit to the
    # file during the prompt can't be what gets signed.
    signature = _sign_copy(shown, key_path, run)
    if signature is None:
        say("not signed: ssh-keygen did not write a signature")
        return 1
    if statement_path.read_bytes() != shown:
        say("not signed: the statement changed while you were reviewing it")
        return 1
    try:
        fd = os.open(sig_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        say(f"not signed: {sig_path} appeared while you were reviewing")
        return 1
    with os.fdopen(fd, "wb") as fh:
        fh.write(signature)
    result = verify(statement_path)
    if not result.ok:
        say(f"signed: {sig_path}, but it does not verify: {result.reason}")
        return 1
    say(f"signed: {sig_path} (signature by {result.signer_fingerprint} verifies)")
    return 0


def _sign_copy(
    shown: bytes, key_path: Path, run: Callable[..., Any]
) -> Optional[bytes]:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="openadapt-sign-") as tmp:
        os.chmod(tmp, 0o700)
        copy = Path(tmp) / "run-authorization.json"
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(shown)
        done = run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-n",
                NAMESPACE,
                "-f",
                str(key_path),
                "--",
                str(copy),
            ]
        )
        sig = copy.with_name(copy.name + ".sig")
        if getattr(done, "returncode", 1) != 0 or not sig.is_file():
            return None
        return sig.read_bytes()
