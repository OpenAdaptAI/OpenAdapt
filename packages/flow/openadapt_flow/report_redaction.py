"""Remove session tokens and credentials from a run report before it is sealed.

A run report (``report.json``) is a shareable artifact: an offline Seal signs
its exact bytes and the signed report can be published as a receipt. URLs the
run saw can carry the application's session: OpenEMR puts its per-session
CSRF token in ``main.php?token_main=...``, OAuth redirects carry ``code`` and
``state``, and a captured header can hold ``Authorization: Bearer ...`` or a
``Cookie``. None of that belongs in a receipt.

:func:`redact_report` walks every string in the report and replaces

* the value of a secret query, fragment or form parameter (``token_main=``,
  ``csrf_token_form=``, ``PHPSESSID=``, ``access_token=``, ``code=``,
  ``state=``, ``signature=`` and the rest of :data:`DEFAULT_PARAMS`);
* the value of a credential header (``Authorization``, ``Cookie``,
  ``Set-Cookie`` and the rest of :data:`DEFAULT_HEADERS`), in a
  ``Name: value`` line, a JSON key, or a ``{"name": ..., "value": ...}`` pair;
* a bearer token, JWT, cloud or source-host access key, or private key block
  found anywhere in a string

with ``<redacted>``. Parameter, header and cookie names stay, so the report
still says where the run went. Each change is recorded in the report's
``redactions`` list as a JSON pointer and a reason, never the value.

The same walk, without writing, is the check a Seal runs first
(:func:`find_unredacted`): ``local-seal run`` refuses a report that still holds
such a value unless ``--allow-unredacted`` is passed.

The parameter list can be extended per app, keyed by the URL's host, in a JSON
policy file (``OPENADAPT_FLOW_REPORT_REDACTION``, else
``$OPENADAPT_HOME/report-redaction.json``)::

    {
      "params": ["tenant_key"],
      "headers": ["X-Tenant-Key"],
      "apps": {
        "emr.example.org": {"params": ["portal_ticket"], "keep": ["state"]}
      }
    }

``keep`` may only name the parameters that are also ordinary words in some
apps (:data:`KEEPABLE_PARAMS`, such as ``state`` for a US state field); a
credential name such as ``token_main`` or ``access_token`` is always redacted.
Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Optional
from urllib.parse import urlsplit

REDACTED: Final = "<redacted>"
ENV_POLICY: Final = "OPENADAPT_FLOW_REPORT_REDACTION"
POLICY_FILE: Final = "report-redaction.json"

#: Query, fragment and form parameters whose value is a session, CSRF token,
#: credential, OAuth grant or request signature. Compared case-insensitively.
DEFAULT_PARAMS: Final = frozenset(
    {
        # CSRF and per-session tokens (OpenEMR, Laravel, Rails, Django, ...)
        "token_main",
        "csrf_token_form",
        "csrf_token",
        "csrf",
        "_csrf",
        "csrfmiddlewaretoken",
        "xsrf_token",
        "_token",
        "token",
        "authenticity_token",
        # sessions
        "session",
        "session_id",
        "sessionid",
        "sid",
        "phpsessid",
        "jsessionid",
        # credentials and OAuth / OIDC
        "auth",
        "auth_token",
        "access_token",
        "id_token",
        "refresh_token",
        "code",
        "state",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        # signed URLs (S3, Azure SAS, generic)
        "signature",
        "sig",
        "x-amz-signature",
        "x-amz-security-token",
        "x-amz-credential",
    }
)

#: Default parameters an app's policy may ``keep``: words that are not a
#: secret in every app. Everything else in :data:`DEFAULT_PARAMS` always goes.
KEEPABLE_PARAMS: Final = frozenset({"code", "state", "session", "sid", "auth"})

#: Headers whose value is a credential. Compared case-insensitively.
DEFAULT_HEADERS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
    }
)

#: JSON object keys whose whole string value is a credential, besides headers.
_SECRET_KEYS: Final = frozenset(
    {
        "token_main",
        "csrf_token",
        "csrf_token_form",
        "access_token",
        "id_token",
        "refresh_token",
        "client_secret",
        "phpsessid",
        "jsessionid",
    }
)

# A parameter name starts a string or follows ?, &, # or ; (matrix parameters
# such as ;jsessionid=), plain or percent-encoded (a redirect URL nested in
# another URL's query). Only the name is matched here, so a parameter inside
# another parameter's value is still found. The value runs to the next
# separator, space or quote.
_PARAM_NAME = re.compile(
    r"(?:^|(?<=[?&#;])|(?<=%3[Ff])|(?<=%26)|(?<=%23))"
    r"(?P<name>[A-Za-z0-9_.\-\[\]]{1,64})(?:=|%3[Dd])"
)
_PARAM_VALUE = re.compile(r"(?:[^?&#;\s\"'<>%]|%(?!26|23|3[FfBb])[0-9A-Fa-f]{2})+")
# "Name: value" at a line start or after a quote or space (curl -H '...').
_HEADER = re.compile(
    r"(?P<pre>(?:^|(?<=[\s'\"]))(?P<name>[A-Za-z][A-Za-z0-9-]{0,63})[ \t]*:[ \t]*)"
    r"(?P<value>[^\r\n'\"]+)",
    re.MULTILINE,
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{1,15}://[^\s\"'<>]+")
_AUTH_VALUE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9_-]*)\s+\S")

#: Token shapes redacted wherever they appear: (reason, pattern, kept group).
_PATTERNS: Final = (
    ("pattern:bearer", re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/\-]{8,}=*"), 1),
    (
        "pattern:jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
        0,
    ),
    ("pattern:aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    (
        "pattern:github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
        0,
    ),
    ("pattern:slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    (
        "pattern:private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        0,
    ),
)


class RedactionPolicyError(ValueError):
    """A report redaction policy file that cannot be used."""


@dataclass(frozen=True)
class AppRule:
    """Extra parameters to redact, and default ones to keep, for one host."""

    params: frozenset[str] = frozenset()
    keep: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RedactionPolicy:
    params: frozenset[str] = DEFAULT_PARAMS
    headers: frozenset[str] = DEFAULT_HEADERS
    apps: Mapping[str, AppRule] = field(default_factory=dict)
    source: str = "default"

    def params_for(self, host: Optional[str]) -> frozenset[str]:
        rule = self.apps.get(host or "")
        if rule is None and host and ":" in host:
            rule = self.apps.get(host.rsplit(":", 1)[0])
        if rule is None:
            return self.params
        return (self.params | rule.params) - rule.keep


DEFAULT_POLICY: Final = RedactionPolicy()


@dataclass(frozen=True)
class Redaction:
    pointer: str
    reason: str
    #: ``"redact-report"`` when applied to an existing run's report.
    by: Optional[str] = None

    def wire(self) -> dict[str, str]:
        out = {"pointer": self.pointer, "reason": self.reason}
        if self.by:
            out["by"] = self.by
        return out


def _names(value: Any, where: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise RedactionPolicyError(f"{where} must be a list of names")
    return frozenset(v.strip().lower() for v in value)


def parse_policy(data: Any, *, source: str = "policy") -> RedactionPolicy:
    """Build a policy from its JSON form; the defaults are always included."""
    if not isinstance(data, dict):
        raise RedactionPolicyError("a redaction policy is a JSON object")
    unknown = set(data) - {"params", "headers", "apps"}
    if unknown:
        raise RedactionPolicyError(f"unknown policy keys: {sorted(unknown)}")
    apps: dict[str, AppRule] = {}
    raw_apps = data.get("apps") or {}
    if not isinstance(raw_apps, dict):
        raise RedactionPolicyError("apps must map a host to its rule")
    for host, rule in raw_apps.items():
        if not isinstance(rule, dict) or set(rule) - {"params", "keep"}:
            raise RedactionPolicyError(f"apps[{host!r}] takes only params and keep")
        keep = _names(rule.get("keep"), f"apps[{host!r}].keep")
        locked = keep - KEEPABLE_PARAMS
        if locked:
            raise RedactionPolicyError(
                f"apps[{host!r}].keep names {sorted(locked)}; only "
                f"{sorted(KEEPABLE_PARAMS)} may be kept"
            )
        apps[str(host).strip().lower()] = AppRule(
            params=_names(rule.get("params"), f"apps[{host!r}].params"), keep=keep
        )
    return RedactionPolicy(
        params=DEFAULT_PARAMS | _names(data.get("params"), "params"),
        headers=DEFAULT_HEADERS | _names(data.get("headers"), "headers"),
        apps=apps,
        source=source,
    )


def policy_path(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    env = os.environ if env is None else env
    named = env.get(ENV_POLICY)
    if named:
        return Path(named).expanduser()
    home = env.get("OPENADAPT_HOME")
    path = (
        Path(home).expanduser() if home else Path.home() / ".openadapt"
    ) / POLICY_FILE
    return path if path.is_file() else None


def load_policy(
    path: Optional[Path] = None, *, env: Optional[Mapping[str, str]] = None
) -> RedactionPolicy:
    """The configured policy, or the default when none is configured.

    Raises :class:`RedactionPolicyError` when a configured file is missing or
    malformed: a policy that was meant to redact more must not silently
    become the default.
    """
    path = path or policy_path(env)
    if path is None:
        return DEFAULT_POLICY
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RedactionPolicyError(f"cannot read {path}: {exc}") from exc
    return parse_policy(data, source=str(path))


def _pointer(parts: tuple[str, ...]) -> str:
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def _host_at(text: str, index: int) -> Optional[str]:
    for match in _URL.finditer(text):
        if match.start() <= index < match.end():
            try:
                return (urlsplit(match.group(0)).netloc.rsplit("@", 1)[-1]).lower()
            except ValueError:
                return None
    return None


def _header_value(name: str, value: str) -> str:
    """Keep the auth scheme and cookie names; drop every secret value."""
    name = name.lower()
    stripped = value.strip()
    if name in {"cookie", "set-cookie"} and "=" in stripped:
        pairs = stripped.split(";")
        if name == "set-cookie":
            # Only the first pair is the cookie; Path, Expires, ... stay.
            pairs = [_cookie_pair(pairs[0])] + pairs[1:]
        else:
            pairs = [_cookie_pair(p) for p in pairs]
        return ";".join(pairs)
    if name in {"authorization", "proxy-authorization"}:
        scheme = _AUTH_VALUE.match(stripped)
        if scheme:
            return f"{scheme.group('scheme')} {REDACTED}"
    return REDACTED


def _cookie_pair(pair: str) -> str:
    if "=" not in pair:
        return pair
    name, _ = pair.split("=", 1)
    return f"{name}={REDACTED}"


def _redact_params(text: str, policy: RedactionPolicy, reasons: list[str]) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _PARAM_NAME.finditer(text):
        if match.start() < cursor:
            continue  # inside a value already redacted
        name = match.group("name").lower()
        if name not in policy.params_for(_host_at(text, match.start())):
            continue
        value = _PARAM_VALUE.match(text, match.end())
        if value is None:
            continue
        pieces.append(text[cursor : match.end()] + REDACTED)
        cursor = value.end()
        reasons.append(f"url_param:{name}")
    if not pieces:
        return text
    return "".join(pieces) + text[cursor:]


def redact_text(
    text: str, policy: RedactionPolicy = DEFAULT_POLICY
) -> tuple[str, list[str]]:
    """Redact one string. Returns the new text and the reasons, in order."""
    reasons: list[str] = []

    def header(match: re.Match[str]) -> str:
        name = match.group("name")
        if name.lower() not in policy.headers:
            return match.group(0)
        new = _header_value(name, match.group("value"))
        if new == match.group("value") or new.strip() == match.group("value").strip():
            return match.group(0)
        reasons.append(f"header:{name.lower()}")
        return match.group("pre") + new

    out = _HEADER.sub(header, text) if ":" in text else text

    if "=" in out or "%3" in out:
        out = _redact_params(out, policy, reasons)

    for reason, pattern, kept in _PATTERNS:

        def token(match: re.Match[str], reason: str = reason, kept: int = kept) -> str:
            reasons.append(reason)
            return (match.group(kept) if kept else "") + REDACTED

        out = pattern.sub(token, out)
    return out, reasons


def _redact_node(
    node: Any, parts: tuple[str, ...], policy: RedactionPolicy, found: list[Redaction]
) -> Any:
    if isinstance(node, str):
        new, reasons = redact_text(node, policy)
        pointer = _pointer(parts)
        for reason in dict.fromkeys(reasons):
            found.append(Redaction(pointer, reason))
        return new
    if isinstance(node, list):
        return [
            _redact_node(item, parts + (str(i),), policy, found)
            for i, item in enumerate(node)
        ]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    # A {"name": "Cookie", "value": "..."} pair (HAR, CDP, network capture).
    pair_name = node.get("name")
    pair_header = (
        pair_name.lower()
        if isinstance(pair_name, str)
        and pair_name.lower() in policy.headers
        and isinstance(node.get("value"), str)
        else None
    )
    for key, item in node.items():
        here = parts + (str(key),)
        lowered = str(key).lower()
        header_key = lowered if lowered in policy.headers else None
        if key == "value" and pair_header:
            header_key = pair_header
        if isinstance(item, str) and (header_key or lowered in _SECRET_KEYS):
            new = _header_value(header_key or lowered, item) if item else item
            if item and new != item:
                reason = f"header:{header_key}" if header_key else f"key:{lowered}"
                found.append(Redaction(_pointer(here), reason))
                out[key] = new
                continue
        out[key] = _redact_node(item, here, policy, found)
    return out


def redact_report(
    report: Mapping[str, Any],
    policy: RedactionPolicy = DEFAULT_POLICY,
    *,
    by: Optional[str] = None,
) -> tuple[dict[str, Any], list[Redaction]]:
    """Return a redacted copy of a report dict and what was redacted.

    The report's own ``redactions`` list is not walked. The returned dict's
    ``redactions`` holds the earlier entries followed by the new ones, and is
    only added when there is at least one entry.
    """
    body = {k: v for k, v in report.items() if k != "redactions"}
    found: list[Redaction] = []
    redacted = _redact_node(body, (), policy, found)
    earlier = report.get("redactions") or []
    if not isinstance(earlier, list):
        earlier = []
    seen = {(e.get("pointer"), e.get("reason")) for e in earlier if isinstance(e, dict)}
    new = [
        Redaction(r.pointer, r.reason, by)
        for r in dict.fromkeys(found)
        if (r.pointer, r.reason) not in seen
    ]
    if earlier or new:
        redacted["redactions"] = list(earlier) + [r.wire() for r in new]
    elif "redactions" in report:
        redacted["redactions"] = report["redactions"]
    return redacted, new


def find_unredacted(
    report: Mapping[str, Any], policy: RedactionPolicy = DEFAULT_POLICY
) -> list[Redaction]:
    """What :func:`redact_report` would still redact: empty for a clean report."""
    return redact_report(report, policy)[1]


def describe(found: list[Redaction], limit: int = 3) -> str:
    shown = ", ".join(f"{r.pointer} ({r.reason})" for r in found[:limit])
    more = f", and {len(found) - limit} more" if len(found) > limit else ""
    return f"{shown}{more}"


def dumps_report(report: Mapping[str, Any]) -> bytes:
    """The bytes ``RunReport.save`` writes for this dict (two-space JSON)."""
    return json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8")


# -- redact-report: an existing run ------------------------------------------

UNREDACTED_DIR: Final = ("private", "unredacted")
_REPORT_FILES: Final = ("report.json", "report.pre-seal.json")
_SEAL_FILES: Final = ("seal.json",)
_ANCHOR_FILE: Final = "seal.anchor.json"


class RedactRunError(RuntimeError):
    """Why an existing run's report was not redacted."""


@dataclass(frozen=True)
class RunRedaction:
    run_dir: Path
    #: File name -> what was redacted in it. Empty when the run was clean.
    redactions: dict[str, list[Redaction]]
    #: Where the original files were kept, or None when nothing changed.
    kept: Optional[Path] = None
    #: Seal files moved there because they covered the original bytes.
    moved_seals: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return any(self.redactions.values())


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.redact-tmp")
    descriptor = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def redact_run(
    run_dir: Path | str,
    policy: Optional[RedactionPolicy] = None,
    *,
    now: Optional[datetime] = None,
) -> RunRedaction:
    """Apply the report redaction to an existing run, keeping the original.

    Rewrites ``report.json`` (and ``report.pre-seal.json`` when present) with
    secret values replaced, each entry marked ``"by": "redact-report"``. The
    original files go to ``private/unredacted/<UTC time>/`` with mode 0600,
    with a ``redaction.json`` that names both digests and no value. A
    ``seal.json`` over the original bytes moves there too: it can no longer
    verify, and the run is sealed again over the redacted report. A run whose
    Seal is anchored in a public log is refused, because its public entry
    names the original report. Nothing leaves the machine.
    """
    from openadapt_flow import sealed_set

    run = Path(run_dir)
    if not (run / "report.json").is_file():
        raise RedactRunError(f"{run} holds no report.json")
    if sealed_set.is_sealed(run):
        raise RedactRunError(
            "the run directory is encrypted at rest; redact it where it is decrypted"
        )
    if (run / _ANCHOR_FILE).exists():
        raise RedactRunError(
            f"{_ANCHOR_FILE} anchors the Seal over the original report in a public "
            "log; redacting now would leave that entry naming a report you no "
            "longer publish. Decide with whoever relies on the anchor first"
        )
    policy = policy or load_policy()
    originals: dict[str, bytes] = {}
    rewritten: dict[str, bytes] = {}
    found: dict[str, list[Redaction]] = {}
    for name in _REPORT_FILES:
        path = run / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            report = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RedactRunError(f"{name} is not JSON") from exc
        if not isinstance(report, dict):
            raise RedactRunError(f"{name} is not a JSON object")
        redacted, new = redact_report(report, policy, by="redact-report")
        found[name] = new
        if new:
            originals[name] = raw
            tail = b"\n" if raw.endswith(b"\n") else b""
            rewritten[name] = dumps_report(redacted) + tail
    if not originals:
        return RunRedaction(run, found)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    keep = run.joinpath(*UNREDACTED_DIR, stamp)
    for parent in (run / UNREDACTED_DIR[0], run.joinpath(*UNREDACTED_DIR)):
        if parent.is_symlink():
            raise RedactRunError(f"{parent} is a symlink")
    keep.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(keep, 0o700)
    for name, raw in originals.items():
        _write_private(keep / name, raw)
    moved: list[str] = []
    for name in _SEAL_FILES:
        if (run / name).is_file():
            shutil.move(str(run / name), str(keep / name))
            moved.append(name)
    record = {
        "schema": "openadapt.report-redaction/v1",
        "redacted_at": (now or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "policy": policy.source,
        "files": {
            name: {
                "sha256_before": hashlib.sha256(originals[name]).hexdigest(),
                "sha256_after": hashlib.sha256(rewritten[name]).hexdigest(),
                "redactions": [r.wire() for r in found[name]],
            }
            for name in originals
        },
        "moved_seals": moved,
    }
    _write_private(
        keep / "redaction.json",
        (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    for name, data in rewritten.items():
        _write_private(run / name, data)
    return RunRedaction(run, found, keep, tuple(moved))
