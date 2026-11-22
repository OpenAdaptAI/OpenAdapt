"""Shadow mode's hold: capture the agent's write, then refuse it.

:class:`ShadowHoldGuard` is a :class:`~openadapt_flow.agent_guard.commit_guard.CommitGuard`
that never lets a write through. Every request to the app's origin that is
not a plain ``GET`` (``POST``, ``PUT``, ``PATCH``, ``DELETE``, ``HEAD``,
``OPTIONS``, an unknown method), and every GraphQL mutation sent with
``GET``, is held, recorded as a proposed write and refused with the commit
guard's 409 and ``X-OpenAdapt-Guard`` notice. Autosaves, drafts and locks are
held like the Save button. The only writes that go on are the ones the
operator listed in ``not_saves`` (a sign-in form).

What is recorded for each held request (``held/<n>.json``, mode 0600, in the
run directory, schema ``openadapt.proposed-write/v1``): the method, the URL
path and origin, when it was held, the identity check on the open record, and
the body parsed to fields. A screenshot and the screen text sit beside it.
``proposed_write.json`` is the held request the readiness comparator reads:
the first one whose method and path are the human's save (``save_target``),
or the first held request when no target is given.

Field parsing is general and never guesses a value:

1. When an entity lens (``openadapt.entity-lens/v1``, for example from
   :func:`openadapt_flow.tx.induce.induce_lens_from_har`) declares the
   request's endpoint, its JSON body is read as fields (``parse: "lens"``).
2. Otherwise an ``application/x-www-form-urlencoded`` or ``multipart/form-data``
   body (``"form"``) or a JSON object body (``"json"``) is read as name and
   value pairs. Nested JSON objects use dotted names.
3. Anything else is one field named ``<body>`` with no value
   (``"uncompared"``). A repeated form name, a file part and a missing body
   are ``"uncompared"`` too.

Session tokens and password-like fields (the network capture's name rules)
are withheld: never written and never compared. Their count is recorded.

Defence in depth: the page the guard acts through is wrapped, and every
request the guard lets go on is checked again there. A non-``GET`` request to
the app's origin that is not listed in ``not_saves`` and still went on is
counted in ``landed_writes``; one makes the run ``FAILED_SCOPE``.

Other tabs, popups, browser contexts and workers: the readiness runner's
:class:`~openadapt_flow.readiness.targets.TargetGate` hands their requests to
this guard too (request ids with the gate's prefix). A write from one of them
is held and refused like any other, recorded in ``held/`` with
``"target": "other"``, never becomes the proposed write, and is counted in
``extra_target_writes`` (the case is ``extra_target``: the identity check
reads the guarded tab, so such a write can't be checked).

The contamination check's screen (:attr:`ShadowHoldGuard.contamination_screen`,
``before_screen.txt``) is the first screen text that showed the task's
record, plus the first value seen in each form control from then until the
first write was held (the page's ``form_values``, when it has one). A form
that opens later already filled in is seen; a field the agent typed into was
first seen empty. The identity check still reads the screen text only, so a
name typed into a search box is not the record.

No guard ledger row is written for a shadow hold (``ledger_dir`` is None):
nothing was saved, so there is nothing to attest.

Not covered: ``GET`` requests with side effects, WebSocket writes, and
writes sent before the guard attached.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit

from openadapt_flow.agent_guard.commit_guard import CommitGuard, GuardPage
from openadapt_flow.agent_guard.requests import (
    HeldRequest,
    NotASave,
    classify,
    graphql_operations,
    origin_of,
)
from openadapt_flow.guard import GuardedSave, GuardRefused, GuardTask, check_identity

__all__ = [
    "PROPOSED_WRITE_SCHEMA",
    "SHADOW_HOLD_CODE",
    "SHADOW_HOLD_MESSAGE",
    "ParsedField",
    "ShadowHoldGuard",
    "is_withheld_name",
    "parse_body_fields",
]

PROPOSED_WRITE_SCHEMA = "openadapt.proposed-write/v1"
SHADOW_HOLD_CODE = "shadow_hold"
SHADOW_HOLD_MESSAGE = "stopped before save (shadow mode)"
#: The status the summary carries once the target save was held.
HELD_VERDICT = "STOPPED_BEFORE_SAVE"

_UNCOMPARED_BODY = "<body>"


@dataclass(frozen=True)
class ParsedField:
    """One field of a write. ``value`` is None for an uncompared field."""

    name: str
    value: Optional[str]
    parse: str  # "lens" | "form" | "json" | "uncompared"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "parse": self.parse}


def is_withheld_name(name: str) -> bool:
    """A session token or password-like field: never stored or compared."""

    from openadapt_flow.network_capture import SECRET_NAME, TOKEN_NAME

    leaf = name.rsplit(".", 1)[-1]
    return bool(TOKEN_NAME.search(leaf) or SECRET_NAME.search(leaf))


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and (
        value == "<secret>" or value.startswith("<session:")
    )


def _scalar(value: Any) -> Optional[str]:
    """A JSON leaf as text: ``None`` for null, ``true``/``false``, numbers as
    written, lists as canonical JSON."""

    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _flatten(obj: Mapping[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    for key, value in obj.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            yield from _flatten(value, name)
        else:
            yield name, value


def _lens_endpoint_matches(lens: Any, method: str, path: str) -> bool:
    for endpoint in (
        getattr(lens, "write_endpoint", None),
        getattr(lens, "create_endpoint", None),
    ):
        if endpoint is None or endpoint.method != method.upper():
            continue
        template = urlsplit(endpoint.path_template).path or endpoint.path_template
        pattern = "^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(template)) + "$"
        if re.match(pattern, path):
            return True
    return False


def parse_body_fields(
    *,
    method: str,
    url: str,
    content_type: str,
    body: Optional[str],
    lens: Any = None,
    form_pairs: Optional[Sequence[Sequence[str]]] = None,
    body_omitted: bool = False,
) -> tuple[list[ParsedField], int]:
    """The write's fields and how many were withheld (see the module docstring).

    ``form_pairs`` is an already split form body (the network capture's
    ``form``); ``body_omitted`` marks a body the capture did not keep.
    """

    def finish(pairs: Iterable[tuple[str, Any]], parse: str) -> tuple[list, int]:
        seen: dict[str, int] = {}
        items: list[tuple[str, Any]] = []
        withheld = 0
        for name, value in pairs:
            if is_withheld_name(name) or _is_placeholder(value):
                withheld += 1
                continue
            seen[name] = seen.get(name, 0) + 1
            items.append((name, value))
        out: list[ParsedField] = []
        done: set[str] = set()
        for name, value in items:
            if name in done:
                continue
            done.add(name)
            if seen[name] > 1:
                # A repeated name has no single value to compare.
                out.append(ParsedField(name, None, "uncompared"))
            else:
                out.append(ParsedField(name, _scalar(value), parse))
        return out, withheld

    uncompared = ([ParsedField(_UNCOMPARED_BODY, None, "uncompared")], 0)
    if body_omitted:
        return uncompared
    if form_pairs is not None:
        return finish(
            ((str(p[0]), p[1] if len(p) > 1 else "") for p in form_pairs), "form"
        )
    if body is None or body == "":
        if method.upper() in ("POST", "PUT", "PATCH"):
            return uncompared  # a write whose body Flow didn't see
        return [], 0
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    path = urlsplit(url).path or "/"
    if "json" in kind or (not kind and body.lstrip().startswith("{")):
        try:
            data = json.loads(body)
        except ValueError:
            return uncompared
        if not isinstance(data, dict):
            return uncompared
        parse = (
            "lens"
            if lens is not None and _lens_endpoint_matches(lens, method, path)
            else "json"
        )
        return finish(_flatten(data), parse)
    if kind == "application/x-www-form-urlencoded":
        return finish(parse_qsl(body, keep_blank_values=True), "form")
    if kind == "multipart/form-data":
        from openadapt_flow.network_capture import parse_multipart

        parsed = parse_multipart(body.encode("utf-8"), content_type)
        if parsed is None:
            return uncompared
        pairs, files = parsed
        fields, withheld = finish(((p[0], p[1]) for p in pairs), "form")
        return [
            ParsedField(f.name, None, "uncompared") if f.name in files else f
            for f in fields
        ], withheld
    return uncompared


def _is_extra(request: HeldRequest) -> bool:
    from openadapt_flow.readiness.targets import is_extra_request

    return is_extra_request(request.request_id)


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class _CountingPage:
    """The guard's page, checked once more where requests actually go on."""

    def __init__(self, inner: GuardPage, owner: "ShadowHoldGuard") -> None:
        self._inner = inner
        self._owner = owner

    def continue_request(self, request_id: str) -> None:
        self._owner._check_forwarded(request_id)
        self._inner.continue_request(request_id)

    def block_request(self, request_id: str, **kwargs: Any) -> None:
        self._owner._seen.pop(request_id, None)
        self._inner.block_request(request_id, **kwargs)

    def screen_text(self) -> Optional[str]:
        return self._inner.screen_text()

    def form_values(self) -> Optional[list[tuple[str, str]]]:
        read = getattr(self._inner, "form_values", None)
        return read() if callable(read) else None

    def screenshot(self) -> Optional[bytes]:
        return self._inner.screenshot()

    def notify(self, message: str, *, level: str) -> None:
        self._inner.notify(message, level=level)

    def clear_notice(self) -> None:
        self._inner.clear_notice()


class _NoStore:
    """Stands in for a :class:`GuardedSave` when no verify config is given.

    It has no state directory or verify config; code that reads them gets
    None (and the shadow hold never reads back)."""

    approval = "not_gated"
    state_dir: Optional[Path] = None
    verify: Any = None
    ledger_dir: Optional[Path] = None


class ShadowHoldGuard(CommitGuard):
    """Capture-and-refuse every write of one task (see the module docstring)."""

    def __init__(
        self,
        guarded: Optional[GuardedSave],
        task: GuardTask,
        page: GuardPage,
        *,
        app_origin: str,
        run_dir: Path | str,
        not_saves: Sequence[NotASave] = (),
        lens: Any = None,
        save_target: Optional[tuple[str, str]] = None,
        agent_id: str = "shadow-agent",
        events_path: Optional[Path] = None,
        allowed_origins: Optional[Sequence[str]] = None,
        clock: Optional[Callable[[], float]] = None,
        wall_clock: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(
            _dt.timezone.utc
        ),
    ) -> None:
        self._seen: dict[str, HeldRequest] = {}
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        super().__init__(
            guarded if guarded is not None else _NoStore(),  # type: ignore[arg-type]
            task,
            _CountingPage(page, self),
            app_origin=app_origin,
            not_saves=not_saves,
            observe_only=False,
            agent_id=agent_id,
            events_path=events_path,
            scope=None,
            **kwargs,
        )
        # A shadow hold saves nothing: no guard ledger row for it.
        self.ledger_dir = None
        self._store = guarded
        self.summary.mode = "shadow_hold"
        self.run_dir = Path(run_dir)
        self.lens = lens
        self.save_target = (
            (save_target[0].upper(), save_target[1]) if save_target else None
        )
        self._wall = wall_clock
        #: Every held request, in order (values local).
        self.held: list[dict[str, Any]] = []
        #: The held request the comparator reads, once there is one.
        self.proposed: Optional[dict[str, Any]] = None
        #: Writes that went on although they are not ``not_saves``.
        self.landed_writes = 0
        self.landed: list[dict[str, Any]] = []
        #: Writes to another origin the guard refused.
        self.other_origin_refused = 0
        #: Writes from another tab, popup, context or worker (held, refused).
        self.extra_target_writes = 0
        #: With a list, every request (``GET`` included) to an origin outside
        #: it is refused, so a page can't carry data out in a URL.
        self.allowed_origins = (
            None
            if allowed_origins is None
            else frozenset(origin_of(o) or o for o in [app_origin, *allowed_origins])
        )
        #: The first screen text on which the task's record showed, read
        #: before any write was held (the contamination check reads it).
        self.before_screen: Optional[str] = None
        #: The first value seen in each form control once the record showed.
        self._first_values: dict[str, str] = {}

    # -- what is a write ----------------------------------------------------------

    def _is_app_write(self, request: HeldRequest) -> bool:
        if origin_of(request.url) != self.app_origin:
            return False
        if request.method != "GET":
            return True
        operations = graphql_operations(request) or []
        return any(op in ("mutation", "unknown") for op in operations)

    def _listed(self, request: HeldRequest) -> bool:
        return any(rule.matches(request, self.app_origin) for rule in self.not_saves)

    # -- requests --------------------------------------------------------------------

    def on_request(self, request: HeldRequest) -> None:
        self._seen[request.request_id] = request
        if self._is_app_write(request):
            if self._listed(request):
                self.summary.requests["not_a_save"] = (
                    self.summary.requests.get("not_a_save", 0) + 1
                )
                self.page.continue_request(request.request_id)
                self._log(request, "not_a_save", "continued")
                return
            self.summary.requests["save"] = self.summary.requests.get("save", 0) + 1
            if _is_extra(request):
                self._hold_extra(request, self._clock())
                return
            self._on_save(request, self._clock())
            return
        kind = classify(request, app_origin=self.app_origin, not_saves=self.not_saves)
        if kind == "read" and not self._origin_allowed(request.url):
            kind = "other_origin"
        self.summary.requests[kind] = self.summary.requests.get(kind, 0) + 1
        if kind == "other_origin":
            self.other_origin_refused += 1
            self._refuse(
                request,
                kind,
                self._clock(),
                "other_origin",
                "the request goes to another site than the app, and the operator "
                "didn't list it as not a save",
            )
            return
        self.page.continue_request(request.request_id)

    def _origin_allowed(self, url: str) -> bool:
        if self.allowed_origins is None:
            return True
        if url.startswith(("data:", "about:")):
            return True  # no network
        if url.startswith("blob:"):
            url = url[len("blob:") :]  # a blob URL carries its creator's origin
        return origin_of(url) in self.allowed_origins

    def _check_forwarded(self, request_id: str) -> None:
        request = self._seen.pop(request_id, None)
        if request is None:
            return
        if self._is_app_write(request) and not self._listed(request):
            self.landed_writes += 1
            self.landed.append({"method": request.method, "url_path": request.path})

    def observe_screen(self) -> None:
        """Before any write is held: keep the first screen that shows the
        task's record, then the first value of each form control (for the
        contamination check)."""

        if self.held:
            return
        if self.before_screen is None:
            screen = self.page.screen_text()
            status, _ = check_identity(self.task.expected_record, screen)
            if status != "verified":
                return
            self.before_screen = screen
        read = getattr(self.page, "form_values", None)
        for key, value in (read() if callable(read) else None) or []:
            self._first_values.setdefault(key, value)

    @property
    def contamination_screen(self) -> Optional[str]:
        """The record's first screen plus each control's first value (see the
        module docstring); None when the record never showed before a write."""

        if self.before_screen is None:
            return None
        values = [v for v in self._first_values.values() if v]
        return self.before_screen + ("\n" + "\n".join(values) if values else "")

    # -- the hold ----------------------------------------------------------------------

    def _identity(self, screen: Optional[str]) -> dict[str, Any]:
        """The identity check, through :meth:`GuardedSave.begin` when a verify
        config is given. Only an ``identity_*`` refusal means the wrong
        record; any other refusal (no baseline, a prior task state) is not
        about the record on screen, so the same identity check decides."""

        if self._store is not None:
            try:
                self._store.begin(self.task, screen_text=screen, agent_id=self.agent_id)
                return {"ok": True, "code": "verified"}
            except GuardRefused as exc:
                if exc.code.startswith("identity_"):
                    return {"ok": False, "code": exc.code}
        status, _ = check_identity(self.task.expected_record, screen)
        return {
            "ok": status == "verified",
            "code": "verified" if status == "verified" else f"identity_{status}",
        }

    def _hold_extra(self, request: HeldRequest, held_at: float) -> None:
        """A write from another tab, popup, context or worker: held, refused,
        never the proposed write (see the module docstring)."""

        self.summary.saves_held += 1
        self.extra_target_writes += 1
        index = len(self.held)
        fields, withheld = parse_body_fields(
            method=request.method,
            url=request.url,
            content_type=request.content_type,
            body=request.post_data,
            lens=self.lens,
        )
        record: dict[str, Any] = {
            "schema": PROPOSED_WRITE_SCHEMA,
            "task_id": self.task.task_id,
            "request_index": index,
            "method": request.method,
            "url_path": request.path,
            "origin": origin_of(request.url),
            "held_at": self._wall().isoformat(timespec="milliseconds"),
            "identity": {"ok": False, "code": "extra_target"},
            "fields": [f.to_dict() for f in fields],
            "withheld_fields": withheld,
            "resource_type": request.resource_type,
            "target": "other",
            "refused": True,
        }
        data = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        _write_private(self.run_dir / "held" / f"{index}.json", data)
        self.held.append(record)
        self._refuse(
            request,
            "save",
            held_at,
            "extra_target",
            "stopped before save (shadow mode): a write from another tab, "
            "popup, context or worker",
        )

    def _on_save(self, request: HeldRequest, held_at: float) -> None:
        self.summary.saves_held += 1
        index = len(self.held)
        screen = self.page.screen_text()
        shot = self.page.screenshot()
        identity = self._identity(screen)
        fields, withheld = parse_body_fields(
            method=request.method,
            url=request.url,
            content_type=request.content_type,
            body=request.post_data,
            lens=self.lens,
        )
        held_dir = self.run_dir / "held"
        record: dict[str, Any] = {
            "schema": PROPOSED_WRITE_SCHEMA,
            "task_id": self.task.task_id,
            "request_index": index,
            "method": request.method,
            "url_path": request.path,
            "origin": origin_of(request.url),
            "held_at": self._wall().isoformat(timespec="milliseconds"),
            "identity": identity,
            "fields": [f.to_dict() for f in fields],
            "withheld_fields": withheld,
            "resource_type": request.resource_type,
            "refused": True,
        }
        if shot:
            _write_private(held_dir / f"{index}.png", shot)
            record["screenshot"] = f"held/{index}.png"
        if screen is not None:
            _write_private(held_dir / f"{index}.screen.txt", screen.encode("utf-8"))
            record["screen_text"] = f"held/{index}.screen.txt"
        data = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        _write_private(held_dir / f"{index}.json", data)
        self.held.append(record)
        if self.proposed is None and (
            self.save_target is None
            or (request.method, request.path) == self.save_target
        ):
            self.proposed = record
            _write_private(self.run_dir / "proposed_write.json", data)
            self.summary.verdict = HELD_VERDICT
        self._refuse(request, "save", held_at, SHADOW_HOLD_CODE, SHADOW_HOLD_MESSAGE)

    # -- the outcome ---------------------------------------------------------------------

    def outcome(self) -> dict[str, Any]:
        """What the hold saw, without record values."""

        same: dict[tuple[str, str, str], int] = {}
        for record in self.held:
            key = (
                record["method"],
                record["url_path"],
                json.dumps(record["fields"], sort_keys=True),
            )
            same[key] = same.get(key, 0) + 1
        return {
            "held": len(self.held),
            "proposed": self.proposed is not None,
            "landed_writes": self.landed_writes,
            "landed": list(self.landed),
            "other_origin_refused": self.other_origin_refused,
            "extra_target_writes": self.extra_target_writes,
            "double_save": any(n > 1 for n in same.values()),
            "requests": dict(self.summary.requests),
        }
