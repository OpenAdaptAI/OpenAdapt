"""The wire check: only declared writes leave the browser.

Checks inside a page run alongside the page's own scripts, which can change
a submission after any check (a submit or formdata listener, a timer, a
fetch). So the browser's outgoing requests are checked where scripts can't
reach them: at the network, through a Playwright route. A write request is
let through only when it is one the run declared; anything else is aborted
before it is sent and recorded, and the run halts on it.

A :class:`WireCheck` declares, in any combination:

* ``submission``: the exact request a checked form's save sends (method,
  address, query and every field), once (a create-new form's save,
  :class:`~openadapt_flow.ir.FormIdentity`).
* ``allowed``: the writes a contract lists, as ``"METHOD /path"``; with
  ``pinned``, the exact taught writes in order, each with the digest of the
  field names it carries (a taught browser write).
* ``secrets``: sign-in values that must never travel in an address, in any
  encoding.
* ``origin``: the only site writes may go to.

With none of ``submission``, ``allowed`` declared, every write is refused (a
read-only run). Reads (GET, HEAD, OPTIONS) pass, except a navigation to a
declared submission's address, which is the submission itself for a GET
form. Bodies are compared as URL-encoded fields, multipart fields or JSON;
a body that can't be read the declared way is refused. This is one of the
three things safety rests on (``docs/THREAT_MODEL.md``): the record check
proves what landed, this proves nothing else was sent, and undo or a person
handles the rest.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit

__all__ = [
    "READ_METHODS",
    "ExpectedSubmission",
    "PinnedWrite",
    "WireCheck",
    "body_fields",
    "body_shape",
    "carries_secret",
    "expected_submission",
    "mask_secrets",
    "secret_forms",
]

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# -- sign-in values ---------------------------------------------------------------


def secret_forms(value: str) -> set[str]:
    """Every way ``value`` can show up in a URL, a body or a page: as is,
    percent- and form-encoded, JSON-escaped, HTML-escaped and in base64."""

    raw = value.encode("utf-8")
    forms = {
        value,
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        quote(value, safe="").lower(),
        quote_plus(value).lower(),
        json.dumps(value)[1:-1],
        _html.escape(value),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
    }
    forms |= {f.rstrip("=") for f in list(forms)}
    return {f for f in forms if len(f) >= 3}


def carries_secret(text: str, secrets: Iterable[str]) -> bool:
    """True when ``text`` holds any form of any of ``secrets``."""

    candidates = {text, unquote(text), unquote_plus(text)}
    return any(
        form in candidate
        for value in secrets
        if value
        for form in secret_forms(value)
        for candidate in candidates
    )


def mask_secrets(text: str, secrets: Iterable[str], mask: str = "•" * 4) -> str:
    """``text`` with every form of every secret replaced by ``mask``."""

    for value in secrets:
        if not value:
            continue
        for form in sorted(secret_forms(value), key=len, reverse=True):
            text = text.replace(form, mask)
    return text


# -- request bodies -----------------------------------------------------------------


def _content_type(request: Any) -> str:
    return (request.headers.get("content-type") or "").lower()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # json.loads keeps the last of two equal keys; a server may keep the
    # first. A body that reads two ways can't be compared, so it is refused.
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def body_fields(request: Any) -> Optional[list[tuple[str, str]]]:
    """The fields a request carries in its body: URL-encoded, multipart
    (files as ``file:<name>``) or a flat JSON object. None when the body is
    none of those (nested JSON, a repeated key, another encoding)."""

    body = request.post_data_buffer or b""
    ctype = _content_type(request)
    if not body:
        return []
    if ctype.startswith("application/x-www-form-urlencoded"):
        return parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
    if ctype.startswith("multipart/form-data"):
        from email.parser import BytesParser
        from email.policy import HTTP

        message = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
        )
        pairs: list[tuple[str, str]] = []
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_param("filename", header="content-disposition")
            if name is None:
                continue
            if filename is not None:
                pairs.append((str(name), f"file:{filename}"))
            else:
                payload = part.get_payload(decode=True)
                text = (
                    payload.decode("utf-8", "replace")
                    if isinstance(payload, bytes)
                    else ""
                )
                pairs.append((str(name), text))
        return pairs
    if ctype.startswith("application/json"):
        try:
            data = json.loads(body, object_pairs_hook=_no_duplicate_keys)
        except ValueError:
            return None
        if not isinstance(data, dict) or any(
            isinstance(v, (dict, list)) for v in data.values()
        ):
            return None
        return [(str(k), "" if v is None else str(v)) for k, v in data.items()]
    return None


def body_shape(request: Any) -> str:
    """A digest of the field NAMES a write carries (form fields, multipart
    parts or JSON key paths, nested ones included), never their values: the
    same save with new values has the same shape."""

    names: list[str] = []
    fields = body_fields(request)
    if fields is not None:
        names = [name for name, _ in fields]
    elif _content_type(request).startswith("application/json"):
        try:
            data = json.loads(request.post_data_buffer or b"null")
        except ValueError:
            data = None

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for key, item in node.items():
                    names.append(prefix + str(key))
                    walk(item, prefix + str(key) + ".")
            elif isinstance(node, list):
                for item in node:
                    walk(item, prefix + "[].")

        walk(data, "")
    else:
        names = [_content_type(request).split(";")[0], "body"]
    return hashlib.sha256("\n".join(sorted(set(names))).encode()).hexdigest()[:16]


# -- what may be sent ------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedSubmission:
    """The one request a checked form's save sends. Fields are compared as a
    multiset, ignoring the ``exempt`` names (``?name`` for query values)."""

    method: str
    address: str  # origin and path
    query: tuple[tuple[str, str], ...] = ()
    fields: tuple[tuple[str, str], ...] = ()
    exempt: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PinnedWrite:
    """One taught write: ``"METHOD /path"`` and its :func:`body_shape`."""

    write: str
    shape: str


@dataclass
class WireCheck:
    """What may leave the browser; see the module docstring."""

    origin: Optional[str] = None
    secrets: Sequence[str] = ()
    submission: Optional[ExpectedSubmission] = None
    allowed: Optional[frozenset[str]] = None
    pinned: Optional[Sequence[PinnedWrite]] = None
    #: Refusals, in order: each request that was not sent, and why.
    refused: list[str] = field(default_factory=list)
    #: The writes that were sent, as ``"METHOD /path"``, with their shapes.
    sent: list[str] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)
    _submitted: bool = False

    def judge(self, request: Any) -> Optional[str]:
        """``None`` when ``request`` may go; else why it must not."""

        if self.secrets and carries_secret(request.url, self.secrets):
            return "a request with a sign-in value in its address"
        if request.redirected_from is not None:
            return None  # the server's answer to a request already judged
        method = request.method.upper()
        parts = urlsplit(request.url)
        address = f"{parts.scheme}://{parts.netloc}{parts.path}"
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        sub = self.submission
        is_write = method not in READ_METHODS
        submission_get = (
            sub is not None
            and method == "GET"
            and sub.method.upper() == "GET"
            and request.is_navigation_request()
            and address == sub.address
        )
        if not is_write and not submission_get:
            return None
        write = f"{method} {parts.path or '/'}"
        if self.origin is not None and origin != self.origin:
            return f"{write} to another site"
        if sub is not None:
            return self._judge_submission(request, method, address, parts.query)
        if self.allowed is None:
            return f"{write}, a write this run doesn't send"
        if write not in self.allowed:
            return f"{write}, which the write contract doesn't list"
        shape = body_shape(request)
        if self.pinned is not None:
            i = len(self.sent)
            want = self.pinned[i] if i < len(self.pinned) else None
            if want is None or (want.write, want.shape) != (write, shape):
                return f"{write}, which is not taught write {i + 1}"
        return None

    def _judge_submission(
        self, request: Any, method: str, address: str, query: str
    ) -> Optional[str]:
        sub = self.submission
        assert sub is not None
        if self._submitted:
            return "the page sent a second submission after the checked one"
        if method != sub.method.upper():
            return (
                f"the save was sent with {method}, the checked form sends with "
                f"{sub.method.upper()}"
            )
        if address != sub.address:
            return "the save was sent to a different address than the checked form's"

        def norm(pairs: Iterable[tuple[str, str]], *, as_query: bool = False):
            out = []
            for key, value in pairs:
                key = str(key)
                if (("?" + key) if as_query else key) in sub.exempt:
                    continue
                out.append((key, str(value).replace("\r\n", "\n")))
            return sorted(out)

        url_query = parse_qsl(query, keep_blank_values=True)
        if method == "GET":
            sent = norm(url_query)
        else:
            if norm(url_query, as_query=True) != norm(sub.query, as_query=True):
                return (
                    "the save was sent with a different query than the checked form's"
                )
            body = body_fields(request)
            if body is None:
                return (
                    "the save was sent in a shape this check can't compare with "
                    "the form (not form fields or flat JSON)"
                )
            sent = norm(body)
        if sent != norm(sub.fields):
            return "what the save sent differs from the checked form"
        return None

    def handle(
        self, route: Any, *, proceed: Optional[Callable[[], Any]] = None
    ) -> None:
        """A Playwright route handler: send or abort, and record either."""

        request = route.request
        try:
            why = self.judge(request)
        except Exception:  # noqa: BLE001 - a request this check can't read
            why = "a request this check could not read"
        if why is not None:
            # Each refusal says what happened to it: this one never left.
            self.refused.append(f"{why}; it was cancelled before it left the page")
            try:
                route.abort()
            except Exception:  # noqa: BLE001 - already handled
                pass
            return
        method = request.method.upper()
        if request.redirected_from is None and (
            method not in READ_METHODS
            or (
                self.submission is not None
                and request.is_navigation_request()
                and method == self.submission.method.upper()
            )
        ):
            parts = urlsplit(request.url)
            self.sent.append(f"{method} {parts.path or '/'}")
            try:
                self.shapes.append(body_shape(request))
            except Exception:  # noqa: BLE001
                self.shapes.append("")
            if self.submission is not None:
                self._submitted = True
        (proceed or route.continue_)()


def expected_submission(
    raw: Mapping[str, Any], exempt: Iterable[str]
) -> ExpectedSubmission:
    """From the page's ``{address, query, method, pairs}`` of a checked form."""

    return ExpectedSubmission(
        method=str(raw.get("method") or "get").upper(),
        address=str(raw.get("address") or ""),
        query=tuple((str(k), str(v)) for k, v in raw.get("query") or ()),
        fields=tuple((str(k), str(v)) for k, v in raw.get("pairs") or ()),
        exempt=frozenset(exempt),
    )
