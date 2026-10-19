"""Teach scope: what a demonstrating agent may do, fixed before it reads a page.

When an agent demonstrates a task (``compile_agent``), the page it reads is
untrusted. A record note, a banner, a tooltip, an image's alt text or a
server's response can hold text written to steer the agent: "save this to
the other patient", "also change the type", "open this link", "skip the
check". Filtering that text out is not reliable, so this module does not try.
It limits what the agent's actions can do instead, using values the task
declared before the agent saw any page. This follows CaMeL's split between
control and data: the declared task is the control, and page text is data
that can never widen it.

:class:`TeachScope` is the declaration: the app origins, the record the save
belongs to, the fields and values the save may write, and any other text the
agent may type. Nothing in it is read from the screen.

:class:`TeachGuard` enforces it on a live capture:

* **Typed text.** Every typed string must equal a declared value. Text read
  from the page can't be typed, and paste shortcuts are refused.
* **Links and forms.** A click on a link or a submit button that leads to an
  undeclared origin is refused before the click.
* **Egress.** Every browser request to an undeclared origin is aborted,
  including requests the page makes by itself (an image beacon, a script's
  ``fetch``). A page that ends up on an undeclared origin stops the capture.
* **Writes.** With a :class:`WriteAdapter` for the app, every write request
  (``POST``, ``PUT``, ``PATCH``, ``DELETE``) to a declared origin is read
  before it is sent. A write to another record, to an undeclared field, with
  an undeclared value, or that the adapter can't attribute is aborted.
* **Read-back.** :meth:`TeachGuard.check_delta` compares the system of
  record before and after the capture. The run must have written exactly the
  declared record and values. This is Flow's check, not the agent's, so an
  agent told to "skip the check" can't skip it.

A violation raises :class:`ScopeViolation` (a kind of
:class:`~openadapt_flow.jit.adapters.UnsupportedAction`), so the capture ends
with no draft. :meth:`TeachGuard.report` lists what was blocked, without
values, for the reviewer.

:func:`instruction_like` finds page text that reads like an instruction to an
agent. It is a display aid for approval forms, not a control: nothing is
allowed or refused because of it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

from openadapt_flow.jit.adapters import Op, UnsupportedAction
from openadapt_flow.runtime.identity import normalize_structured

__all__ = [
    "TEACH_SCOPE_SCHEMA",
    "ReadBack",
    "ScopeViolation",
    "TeachGuard",
    "TeachScope",
    "WriteAdapter",
    "WriteClaim",
    "instruction_like",
    "link_target_at",
    "origin_of",
    "provenance_for",
]

TEACH_SCOPE_SCHEMA = "openadapt.teach-scope/v1"

#: HTTP methods that can change the system of record.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: URL schemes that never leave the browser.
_LOCAL_SCHEMES = frozenset({"data", "blob", "about", "javascript"})

#: Keys that paste the clipboard. The clipboard can hold page text.
_PASTE_KEYS = {("Control", "v"), ("Meta", "v"), ("Shift", "insert")}

_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: Optional[str]) -> Optional[str]:
    """``scheme://host[:port]`` of ``url``, lowercased, default port dropped.

    Returns ``None`` for a URL with no network origin (``data:``,
    ``about:blank``, an empty string).
    """

    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _is_local_url(url: Optional[str]) -> bool:
    if not url:
        return True
    scheme = urlsplit(url.strip()).scheme.lower()
    return scheme in _LOCAL_SCHEMES or scheme == "chrome-error"


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _same(a: Any, b: Any) -> bool:
    return normalize_structured(str(a)) == normalize_structured(str(b))


# -- what the task declares ---------------------------------------------------


@dataclass(frozen=True)
class WriteClaim:
    """Which record a write belongs to, and which field values it writes."""

    record: Mapping[str, Any]
    fields: Mapping[str, Any]


class WriteAdapter(Protocol):
    """Reads one app's writes. Written for the app, never by the agent.

    ``claim`` gets a browser write request (method, URL, body) and returns
    what it writes, or ``None`` when it can't say; ``None`` is refused.
    ``claim_row`` gets one row from the record reader and returns the same
    shape, for the read-back.
    """

    def claim(
        self, method: str, url: str, body: Optional[bytes]
    ) -> Optional[WriteClaim]: ...

    def claim_row(self, row: Mapping[str, Any]) -> Optional[WriteClaim]: ...


def _frozen(values: Mapping[str, Any], what: str) -> Mapping[str, str]:
    out: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{what} names must be non-empty text")
        if value is None or not str(value).strip():
            raise ValueError(f"{what}.{name} must be non-empty")
        out[name] = str(value)
    return MappingProxyType(out)


@dataclass(frozen=True)
class TeachScope:
    """What one teach run may do. Declared by whoever assigned the task.

    Args:
        task_id: The task this scope belongs to.
        origins: The app's origins (``scheme://host[:port]``). The browser may
            reach only these; any URL is reduced to its origin.
        record: The record the save belongs to, as the record reader names
            it (``{"patient_id": "p1"}``).
        fields: The fields the save may write and the exact value of each
            (``{"note": "...", "type": "Triage"}``).
        constants: Other text the agent may type, such as a test account's
            user name. Record and field values may always be typed.
        expected_rows: How many rows the run must add. Default 1.
        write_adapter: Reads the app's write requests and record rows. With
            one, writes are checked before they are sent; without one, only
            the read-back checks them.
        allowed_write_paths: URL paths on a declared origin that may take a
            write the adapter does not attribute (a sign-in form). Exact
            match; no prefixes.
    """

    task_id: str
    origins: tuple[str, ...]
    record: Mapping[str, str]
    fields: Mapping[str, str]
    constants: tuple[str, ...] = ()
    expected_rows: int = 1
    write_adapter: Optional[WriteAdapter] = field(default=None, compare=False)
    allowed_write_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("a teach scope names its task")
        origins = []
        for raw in self.origins:
            origin = origin_of(raw)
            if origin is None:
                raise ValueError(f"not an http(s) origin: {raw!r}")
            origins.append(origin)
        if not origins:
            raise ValueError("a teach scope names at least one app origin")
        object.__setattr__(self, "origins", tuple(dict.fromkeys(origins)))
        if not self.record:
            raise ValueError("a teach scope names the record the save belongs to")
        object.__setattr__(self, "record", _frozen(self.record, "record"))
        object.__setattr__(self, "fields", _frozen(self.fields, "fields"))
        overlap = set(self.record) & set(self.fields)
        if overlap:
            raise ValueError(
                "a name is both record and field: " + ", ".join(sorted(overlap))
            )
        object.__setattr__(
            self, "constants", tuple(str(c) for c in self.constants if str(c))
        )
        if self.expected_rows < 1:
            raise ValueError("expected_rows must be at least 1")
        for path in self.allowed_write_paths:
            if not path.startswith("/"):
                raise ValueError(f"allowed write path must start with '/': {path!r}")

    @classmethod
    def from_guard_task(
        cls,
        task: Any,
        *,
        origins: Sequence[str],
        constants: Sequence[str] = (),
        write_adapter: Optional[WriteAdapter] = None,
    ) -> "TeachScope":
        """Build from an ``openadapt.guard-task/v1`` task (:mod:`openadapt_flow.guard`)."""

        return cls(
            task_id=task.task_id,
            origins=tuple(origins),
            record=dict(task.expected_record),
            fields={k: str(v) for k, v in task.intended_fields.items()},
            constants=tuple(constants),
            write_adapter=write_adapter,
        )

    @property
    def typeable(self) -> frozenset[str]:
        """Every string the agent may type: declared values and constants."""

        return frozenset(
            [*self.record.values(), *self.fields.values(), *self.constants]
        )

    def allows_origin(self, url: Optional[str]) -> bool:
        return origin_of(url) in self.origins

    def to_dict(self) -> dict[str, Any]:
        """The scope without its values: names, origins and value digests."""

        return {
            "schema_version": TEACH_SCOPE_SCHEMA,
            "task_id": self.task_id,
            "origins": list(self.origins),
            "record": sorted(self.record),
            "fields": sorted(self.fields),
            "values_sha256": _digest(
                {"record": dict(self.record), "fields": dict(self.fields)}
            ),
            "constants": len(self.constants),
            "expected_rows": self.expected_rows,
            "write_adapter": type(self.write_adapter).__name__
            if self.write_adapter is not None
            else None,
            "allowed_write_paths": list(self.allowed_write_paths),
        }

    def describe(self) -> str:
        """One line for an approval form: what this task may touch."""

        return (
            f"record {', '.join(sorted(self.record))}; "
            f"fields {', '.join(sorted(self.fields)) or 'none'}; "
            f"origins {', '.join(self.origins)}"
        )


# -- enforcement ----------------------------------------------------------------


class ScopeViolation(UnsupportedAction):
    """An action or request went outside the declared scope. ``code`` is closed.

    Codes: ``typed_value_not_declared``, ``paste_refused``,
    ``link_origin_not_declared``, ``egress_origin_not_declared``,
    ``navigated_off_scope``, ``write_wrong_record``,
    ``write_field_not_declared``, ``write_value_not_declared``,
    ``write_unattributed``.
    """

    def __init__(self, code: str, reason: str) -> None:
        ValueError.__init__(
            self, f"outside the task's declared scope ({code}): {reason}"
        )
        self.dialect = "scope"
        self.action = code
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class ReadBack:
    """What the system of record shows after a teach run."""

    #: ``verified``, ``missing``, ``mismatch`` or ``unreadable``.
    status: str
    #: Closed codes, no values: ``readback_wrong_record``,
    #: ``readback_wrong_value``, ``readback_row_count``,
    #: ``readback_row_removed``, ``readback_unattributed``.
    problems: tuple[str, ...] = ()
    added: int = 0
    removed: int = 0

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problems": list(self.problems),
            "added": self.added,
            "removed": self.removed,
        }


_LINK_PROBE = """([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const link = el.closest('a[href], area[href]');
  if (link) return {kind: 'link', url: link.href};
  const control = el.closest('button, input[type=submit], input[type=image]');
  if (control && control.form && (control.type || 'submit') === 'submit') {
    const action = control.getAttribute('formaction') || control.form.action;
    return {kind: 'form', url: new URL(action, document.baseURI).href};
  }
  return null;
}"""


def link_target_at(page: Any, x: int, y: int) -> Optional[dict[str, str]]:
    """The link or form submit under ``(x, y)``: ``{"kind", "url"}``, or ``None``.

    ``url`` is absolute. ``None`` also when the page can't be asked.
    """

    try:
        target = page.evaluate(_LINK_PROBE, [int(x), int(y)])
    except Exception:  # noqa: BLE001 - no page script, no link check
        return None
    return dict(target) if target else None


class TeachGuard:
    """Enforce one :class:`TeachScope` on one live capture.

    Install it on the browser page with :meth:`install` before the page
    loads, call :meth:`check_ops` before each action and :meth:`after_action`
    after it, and :meth:`check_delta` once the agent is done.
    """

    #: Milliseconds the page gets after each action to issue its requests,
    #: so a write the action started is judged before the next action.
    SETTLE_MS = 150

    def __init__(self, scope: TeachScope) -> None:
        self.scope = scope
        self._page: Any = None
        #: Every refusal, in order: ``{"code", "detail"}``. No values.
        self.violations: list[dict[str, Any]] = []
        #: Requests aborted at the network: ``{"code", "method", "origin", "path"}``.
        self.blocked: list[dict[str, Any]] = []
        #: Write requests that matched the scope and were sent.
        self.writes_allowed = 0
        self._raised = 0

    # -- browser ------------------------------------------------------------------

    def install(self, page: Any) -> None:
        """Route every request of ``page``'s browser context through the guard."""

        self._page = page
        page.context.route("**/*", self._route)

    def _route(self, route: Any) -> None:
        request = route.request
        url = str(request.url)
        method = str(request.method).upper()
        if _is_local_url(url):
            route.continue_()
            return
        code = self._request_refusal(method, url, request)
        if code is None:
            if method in WRITE_METHODS:
                self.writes_allowed += 1
            route.continue_()
            return
        parts = urlsplit(url)
        self.blocked.append(
            {
                "code": code,
                "method": method,
                "origin": origin_of(url),
                "path": parts.path,
            }
        )
        self._violate(code, f"{method} to {origin_of(url)}{parts.path} was blocked")
        route.abort("blockedbyclient")

    def _request_refusal(self, method: str, url: str, request: Any) -> Optional[str]:
        if not self.scope.allows_origin(url):
            return "egress_origin_not_declared"
        if method not in WRITE_METHODS:
            return None
        adapter = self.scope.write_adapter
        if adapter is None:
            # No adapter: the write goes, and the read-back judges it.
            return None
        try:
            body = request.post_data_buffer
        except Exception:  # noqa: BLE001 - an unreadable body can't be attributed
            body = None
        try:
            claim = adapter.claim(method, url, body)
        except Exception:  # noqa: BLE001 - an adapter failure is a refusal
            claim = None
        if claim is None:
            if urlsplit(url).path in self.scope.allowed_write_paths:
                return None
            return "write_unattributed"
        return self._claim_refusal(claim, write=True)

    def _claim_refusal(self, claim: WriteClaim, *, write: bool) -> Optional[str]:
        prefix = "write" if write else "readback"
        for name, value in self.scope.record.items():
            if name not in claim.record or not _same(claim.record[name], value):
                return f"{prefix}_wrong_record"
        for name, value in claim.fields.items():
            if name not in self.scope.fields:
                if value is None or not str(value).strip():
                    continue  # an empty undeclared field writes nothing
                return (
                    f"{prefix}_field_not_declared" if write else f"{prefix}_wrong_value"
                )
            if not _same(value, self.scope.fields[name]):
                return (
                    f"{prefix}_value_not_declared" if write else f"{prefix}_wrong_value"
                )
        return None

    # -- actions ------------------------------------------------------------------

    def check_ops(
        self,
        ops: Sequence[Op],
        *,
        page: Any = None,
        pointer: Optional[tuple[int, int]] = None,
    ) -> None:
        """Refuse an action before any of it runs. Raises :class:`ScopeViolation`."""

        typeable = self.scope.typeable
        for op in ops:
            if op.kind == "type":
                if (op.text or "") not in typeable:
                    self._raise(
                        "typed_value_not_declared",
                        "the agent typed text the task did not declare; only "
                        "declared values may be typed",
                    )
            elif op.kind == "hotkey":
                key = (op.key or "").lower()
                if any((mod, key) in _PASTE_KEYS for mod in op.modifiers):
                    self._raise(
                        "paste_refused",
                        "pasting is refused: the clipboard can hold page text",
                    )
            elif op.kind in ("click", "double_click") and page is not None:
                at = (op.x, op.y) if op.x is not None and op.y is not None else pointer
                if at is None:
                    continue
                target = link_target_at(page, at[0], at[1])
                if target and not _is_local_url(target.get("url")):
                    if not self.scope.allows_origin(target.get("url")):
                        self._raise(
                            "link_origin_not_declared",
                            f"the clicked {target.get('kind', 'link')} leads to "
                            f"{origin_of(target.get('url'))}, which the task did "
                            "not declare",
                        )

    def after_action(self, page: Any = None) -> None:
        """Raise for a refusal the network saw, or a page off the declared origins."""

        page = page if page is not None else self._page
        self._pump(page)
        if page is not None:
            url = str(getattr(page, "url", "") or "")
            if not _is_local_url(url) and not self.scope.allows_origin(url):
                self._violate(
                    "navigated_off_scope",
                    f"the page is on {origin_of(url)}, which the task did not declare",
                )
        self.raise_pending()

    def finish(self) -> None:
        """Let the page's last requests reach the guard, then raise any refusal."""

        self._pump(self._page)
        self.raise_pending()

    def _pump(self, page: Any) -> None:
        # Playwright's sync API runs route handlers only while it waits.
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            try:
                wait(self.SETTLE_MS)
            except Exception:  # noqa: BLE001 - a closed page has nothing pending
                pass

    def raise_pending(self) -> None:
        """Raise the first violation not raised yet, if any."""

        if len(self.violations) > self._raised:
            first = self.violations[self._raised]
            self._raised = len(self.violations)
            raise ScopeViolation(first["code"], first["detail"])

    def _violate(self, code: str, detail: str) -> None:
        self.violations.append({"code": code, "detail": detail})

    def _raise(self, code: str, detail: str) -> None:
        self._violate(code, detail)
        self._raised = len(self.violations)
        raise ScopeViolation(code, detail)

    # -- read-back ----------------------------------------------------------------

    def check_delta(
        self,
        before: Optional[Iterable[Mapping[str, Any]]],
        after: Optional[Iterable[Mapping[str, Any]]],
    ) -> ReadBack:
        """Whether the system of record shows exactly the declared write."""

        if before is None or after is None:
            return ReadBack("unreadable")
        key = lambda row: json.dumps(dict(row), sort_keys=True, default=str)  # noqa: E731
        old = {key(r): r for r in before}
        new = {key(r): r for r in after}
        added = [new[k] for k in new if k not in old]
        removed = [old[k] for k in old if k not in new]
        problems: list[str] = []
        if removed:
            problems.append("readback_row_removed")
        for row in added:
            claim = self._row_claim(row)
            if claim is None:
                problems.append("readback_unattributed")
                continue
            code = self._claim_refusal(claim, write=False)
            if code is not None:
                problems.append(code)
        if not added:
            return ReadBack("missing", tuple(dict.fromkeys(problems)), 0, len(removed))
        if len(added) != self.scope.expected_rows:
            problems.append("readback_row_count")
        status = "mismatch" if problems else "verified"
        return ReadBack(
            status, tuple(dict.fromkeys(problems)), len(added), len(removed)
        )

    def _row_claim(self, row: Mapping[str, Any]) -> Optional[WriteClaim]:
        adapter = self.scope.write_adapter
        if adapter is not None:
            try:
                return adapter.claim_row(row)
            except Exception:  # noqa: BLE001 - an adapter failure is unattributed
                return None
        if not all(name in row for name in self.scope.record):
            return None
        return WriteClaim(
            record={k: row[k] for k in self.scope.record},
            fields={k: row.get(k) for k in self.scope.fields},
        )

    def report(self) -> dict[str, Any]:
        """What the guard saw, for the candidate and the reviewer. No values."""

        return {
            "scope": self.scope.to_dict(),
            "violations": list(self.violations),
            "blocked_requests": list(self.blocked),
            "writes_allowed": self.writes_allowed,
        }


# -- display aid ------------------------------------------------------------------

_INSTRUCTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|previous|above|rules?)\b",
        r"\b(note|message|instructions?) (to|for) (the )?(ai|assistant|agent|model)\b",
        r"\b(ai|the) (assistant|agent)\b\s*[:,]",
        r"^\s*(assistant|system|agent)\s*:",
        r"\bsystem (notice|prompt|message|override)\b",
        r"\b(do not|don't|no need to|skip)\s+(the\s+)?(verify|verification|check|"
        r"checking|confirm|read[- ]?back)\b",
        r"\breport (the|this) (save|task|run)\b.{0,20}\b(verified|done|complete)\b",
        r"\bnew instructions?\b",
        r"\byou (must|should|need to) (now )?(save|click|open|navigate|visit|type|enter|send)\b",
    )
]


def instruction_like(text: Optional[str], *, limit: int = 8) -> list[str]:
    """Lines of ``text`` that read like an instruction to an agent.

    For approval forms only: it shows a person what on the page may have
    steered the agent. It decides nothing, and missing a line is expected.
    """

    found: list[str] = []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        if any(p.search(line) for p in _INSTRUCTION_PATTERNS):
            found.append(line[:300])
            if len(found) >= limit:
                break
    return found


def provenance_for(
    *,
    actor: str,
    reason: Optional[str],
    seen_text: Optional[str],
    scope: Optional[TeachScope] = None,
    scrub: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """The ``provenance`` an approval request carries (see ``item_approval``).

    ``seen_text`` is the screen text the agent was given; it is scrubbed with
    ``scrub`` when one is given and cut to 2,000 characters. ``reason`` is
    the agent's own account and is labelled as such where it is shown.
    """

    seen = seen_text or ""
    if scrub is not None and seen:
        seen = scrub(seen)
    return {
        "actor": actor[:128],
        "reason": (reason or "")[:512] or None,
        "seen_text": seen[:2000] or None,
        "instruction_like": instruction_like(seen),
        "task_scope": scope.describe()[:512] if scope is not None else None,
    }
