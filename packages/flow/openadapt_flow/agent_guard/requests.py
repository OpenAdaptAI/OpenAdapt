"""Which of the agent's browser requests is a save.

The commit guard pauses the page's requests with the CDP ``Fetch`` domain and
asks :func:`classify` what each one is:

``read``
    ``GET``, ``HEAD`` and ``OPTIONS``, and a GraphQL request whose every
    operation is a ``query`` or a ``subscription``. Let through at once.
``save``
    Any other request to the app's origin: ``POST``, ``PUT``, ``PATCH``,
    ``DELETE``, an unknown method, or a GraphQL ``mutation``. Held until the
    right-record check passes. A GraphQL body Flow can't read (a persisted
    query with no text, a multipart upload) counts as a save.
``not_a_save``
    A write the operator listed as not a save (a sign-in form, a search that
    posts, an audit beacon). Let through.
``other_origin``
    A write to another origin that the operator didn't list. Blocked when the
    guard is closed: Flow can't tell whether it is the save.

Nothing here reads a value the request carries except a GraphQL operation's
type.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "HeldRequest",
    "NotASave",
    "RequestKind",
    "classify",
    "graphql_operations",
    "origin_of",
]

RequestKind = Literal["read", "save", "not_a_save", "other_origin"]

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_GRAPHQL_KEYS = frozenset({"query", "variables", "operationName", "extensions"})

#: GraphQL operation keywords, after comments and fragments are stripped.
_GRAPHQL_COMMENT = re.compile(r"#[^\n\r]*")
_GRAPHQL_STRING = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"')
_GRAPHQL_OPERATION = re.compile(
    r"(?:^|[\s}])(query|mutation|subscription|fragment)\b|(^|})\s*\{"
)


@dataclass(frozen=True)
class HeldRequest:
    """One request the page paused at, as ``Fetch.requestPaused`` reports it."""

    request_id: str
    method: str
    url: str
    resource_type: str = "Other"
    post_data: Optional[str] = None
    content_type: str = ""
    network_id: Optional[str] = None

    @classmethod
    def from_cdp(cls, params: Mapping[str, Any]) -> "HeldRequest":
        request = params.get("request") or {}
        headers = {
            str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()
        }
        return cls(
            request_id=str(params.get("requestId") or ""),
            method=str(request.get("method") or "GET").upper(),
            url=str(request.get("url") or ""),
            resource_type=str(params.get("resourceType") or "Other"),
            post_data=request.get("postData"),
            content_type=headers.get("content-type", ""),
            network_id=params.get("networkId"),
        )

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"


@dataclass(frozen=True)
class NotASave:
    """A write the operator says is not a save, by method and path pattern.

    ``path`` is an ``fnmatch`` pattern on the URL path (``/chart/signin``,
    ``/api/search*``). ``origin`` defaults to the app's origin; give it to
    let through a write to another origin (an analytics host).
    """

    method: str
    path: str
    origin: Optional[str] = None

    def matches(self, request: HeldRequest, app_origin: str) -> bool:
        if self.method != "*" and self.method.upper() != request.method:
            return False
        if origin_of(request.url) != (self.origin or app_origin):
            return False
        return fnmatch.fnmatchcase(request.path, self.path)


def origin_of(url: str) -> str:
    """``scheme://host[:port]``, lower-cased; ``""`` for a URL with none."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    netloc = host if port in (None, default) else f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}"


def _operation_types(document: str) -> list[str]:
    """The operation types in one GraphQL document (``query`` for a bare
    selection set). Fragments are not operations."""

    text = _GRAPHQL_STRING.sub('""', _GRAPHQL_COMMENT.sub("", document)).strip()
    found: list[str] = []
    for match in _GRAPHQL_OPERATION.finditer(text):
        keyword = match.group(1)
        if keyword is None:
            found.append("query")  # shorthand ``{ ... }``
        elif keyword != "fragment":
            found.append(keyword)
    return found


def graphql_operations(request: HeldRequest) -> Optional[list[str]]:
    """The GraphQL operation types the request runs, or None if it isn't one
    Flow can read.

    Reads a JSON body (one request or a batch) or a ``query=`` URL parameter.
    Returns ``["unknown"]`` for a GraphQL request with no query text (a
    persisted query), so it is treated as a save.
    """

    documents: list[Any] = []
    if request.post_data and "json" in request.content_type.lower():
        try:
            body = json.loads(request.post_data)
        except ValueError:
            return None
        items = body if isinstance(body, list) else [body]
        for item in items:
            # Only a body made of GraphQL request keys is GraphQL: a save
            # that happens to have a "query" field is still a save.
            if not isinstance(item, dict) or not item or set(item) - _GRAPHQL_KEYS:
                return None
            if "query" in item:
                documents.append(item.get("query"))
            elif "extensions" in item and "persistedQuery" in (
                item.get("extensions") or {}
            ):
                documents.append(None)
            else:
                return None
    else:
        query = parse_qs(urlsplit(request.url).query).get("query")
        if not query:
            return None
        documents.append(query[0])
    types: list[str] = []
    for document in documents:
        if not isinstance(document, str):
            types.append("unknown")
            continue
        found = _operation_types(document)
        types.extend(found or ["unknown"])
    return types


def classify(
    request: HeldRequest,
    *,
    app_origin: str,
    not_saves: Sequence[NotASave] = (),
) -> RequestKind:
    """What ``request`` is to the commit guard (see the module docstring)."""

    operations = graphql_operations(request)
    if request.method in READ_METHODS:
        # A GraphQL mutation sent with GET still changes data.
        if operations and any(op in ("mutation", "unknown") for op in operations):
            pass
        else:
            return "read"
    elif operations and all(op in ("query", "subscription") for op in operations):
        return "read"
    if any(rule.matches(request, app_origin) for rule in not_saves):
        return "not_a_save"
    if origin_of(request.url) != app_origin:
        return "other_origin"
    return "save"
