"""Keep only declared fields of captured request and response bodies.

A network capture (``network.jsonl``) or a saved wire-tap HAR can hold
patient data in its bodies: a search result, a chart page, a JSON record.
Credential redaction (:mod:`openadapt_flow.secret_redaction`,
:func:`openadapt_flow.network_capture.redact`) removes passwords, cookies and
tokens, but leaves every business value in place. This module is the next
step, applied in memory on the device before anything is written:

* A JSON body, a ``application/x-www-form-urlencoded`` body and a
  ``multipart/form-data`` body without files are parsed. The value of a
  *declared* field (a recording's parameter and identifier fields, or a wire
  policy's id and version fields) is kept. Every other scalar value is
  replaced by a keyed hash.
* Anything that does not parse that way (HTML, plain text, binary, a cut
  body, a multipart body that carries a file) keeps only its length, its
  content type and a keyed hash of the whole body. JSON is parsed only from
  a body labelled JSON (``application/json``, ``*/*+json``) or with no
  content type at all; JSON-looking text in any other type, including an
  explicit ``application/octet-stream``, is opaque.
* URL query values are treated the same way, because a ``GET`` form puts the
  person's input there. Path segments are kept (see "Not covered").

The hash is HMAC-SHA256 under a key made for one capture and held only in
memory. It is never written, so a hash of a short value (a birth date, a
name) cannot be reversed by trying every candidate offline. The cost: hashes
from two captures cannot be compared; within one capture, equal values have
equal hashes, so "the value sent was the value read back" stays checkable.

Not covered: URL path segments (``/patients/1234``) and JSON object keys
that look like ordinary field names are kept as written. A key that does not
look like a field name (spaces, punctuation, over 64 characters) is hashed
like a value. Header values are handled by the credential redaction only,
except URL-valued HAR headers (``Location``, ``Referer``), whose query is
minimized like any URL.

Stdlib only (no ``openadapt_flow`` import), so the wire tap and the
recorder load it without changing their import cost.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import urllib.parse as up
from typing import Any, Iterable, Mapping, Optional

#: Marks a body or a whole capture whose bodies were minimized.
MINIMIZED_SCHEMA = "openadapt.capture-minimized/1"
#: The ``bodies`` value of a capture header.
MINIMIZED = "minimized"
RAW = "raw"
BODY_MODES = (MINIMIZED, RAW)

#: HAR custom field (HAR 1.2 allows names starting with ``_``).
HAR_FIELD = "_openadapt_minimized"

_FIELD_NAME = re.compile(r"^[A-Za-z_$@][A-Za-z0-9_.$@:\-\[\]]{0,63}$")
_JSON_START = ("{", "[")
#: A value this module already replaced (left as is when minimized again).
TAG = re.compile(r"hmac-sha256:[0-9a-f]{64}")
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class BodyMinimizer:
    """Minimize the bodies and URLs of one capture.

    ``keep_fields`` are the declared field names. A JSON value is kept when
    its leaf key or a dotted suffix of its path (``id``, ``meta.versionId``,
    ``data.id``; list indices dropped) equals a declared name, compared
    case-insensitively. ``withhold`` names are never kept, whatever their
    case (a declared secret). One instance per capture: its hash key lives
    only as long as the instance.
    """

    def __init__(
        self,
        keep_fields: Iterable[str] = (),
        *,
        key: Optional[bytes] = None,
        withhold: Iterable[str] = (),
    ):
        withheld = {str(f).lower() for f in withhold}
        self._keep = frozenset(
            str(f).lower()
            for f in keep_fields
            if str(f) and str(f).lower() not in withheld
        )
        self._key = key if key is not None else secrets.token_bytes(32)

    @property
    def keep_fields(self) -> frozenset[str]:
        return self._keep

    def tag(self, value: Any) -> str:
        """The keyed hash that stands in for ``value``."""
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
        else:
            # surrogatepass: a lone escaped surrogate parses but isn't UTF-8.
            data = json.dumps(value, ensure_ascii=False).encode(
                "utf-8", "surrogatepass"
            )
        digest = hmac.new(self._key, data, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def _declared(self, path: list[str]) -> bool:
        if not path or not self._keep:
            return False
        named = [p for p in path if not p.isdigit()]
        if not named:
            return False
        lowered = [p.lower() for p in named]
        return any(".".join(lowered[i:]) in self._keep for i in range(len(lowered)))

    def declared(self, name: str) -> bool:
        """Whether a top-level field ``name`` is kept."""
        return self._declared([self._segment(str(name))])

    def _segment(self, key: str) -> str:
        return key if _FIELD_NAME.match(key) else self.tag(key)

    # -- bodies ----------------------------------------------------------

    def _walk(
        self,
        node: Any,
        path: list[str],
        kept: list[list[Any]],
        withheld: list[list[str]],
    ) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                self._walk(v, path + [self._segment(str(k))], kept, withheld)
            return
        if isinstance(node, list):
            for i, v in enumerate(node):
                self._walk(v, path + [str(i)], kept, withheld)
            return
        dotted = ".".join(path)
        if self._declared(path) and (
            node is None or isinstance(node, (str, int, float))
        ):
            kept.append([dotted, node])
        else:
            withheld.append([dotted, self.tag(node)])

    def pairs(self, pairs: Iterable[Iterable[Any]]) -> dict[str, Any]:
        """Minimize ``[name, value]`` pairs (a parsed form body)."""
        kept: list[list[Any]] = []
        withheld: list[list[str]] = []
        for pair in pairs:
            name, value = (list(pair) + ["", ""])[:2]
            segment = self._segment(str(name))
            if self._declared([segment]):
                kept.append([segment, value])
            else:
                withheld.append([segment, self.tag(value)])
        return {"kept": kept, "withheld": withheld}

    def body(
        self,
        raw: bytes | str | None,
        content_type: str = "",
        *,
        truncated: bool = False,
    ) -> Optional[dict[str, Any]]:
        """A minimized description of one body, or None for an empty body."""
        if raw is None:
            return None
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if not data:
            return None
        ctype = (content_type or "").split(";")[0].strip().lower()
        out: dict[str, Any] = {
            "schema": MINIMIZED_SCHEMA,
            "bytes": len(data),
            "content_type": ctype,
            "hmac": self.tag(data),
        }
        if truncated:
            out["format"] = "opaque"
            out["truncated"] = True
            return out
        parsed = self._parse(data, ctype, content_type or "")
        if parsed is None:
            out["format"] = "opaque"
            return out
        fmt, result = parsed
        out["format"] = fmt
        out.update(result)
        return out

    def opaque(
        self, raw: bytes, content_type: str = "", *, truncated: bool = False
    ) -> Optional[dict[str, Any]]:
        """Size, type and keyed hash of ``raw`` as sent (bytes, undecoded);
        nothing of its content. None for an empty body."""
        if not raw:
            return None
        out: dict[str, Any] = {
            "schema": MINIMIZED_SCHEMA,
            "bytes": len(raw),
            "content_type": (content_type or "").split(";")[0].strip().lower(),
            "hmac": self.tag(bytes(raw)),
            "format": "opaque",
        }
        if truncated:
            out["truncated"] = True
        return out

    def _parse(
        self, data: bytes, ctype: str, full_type: str
    ) -> Optional[tuple[str, dict[str, Any]]]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if ctype == "application/x-www-form-urlencoded":
            return "form", self.pairs(up.parse_qsl(text, keep_blank_values=True))
        if ctype == "multipart/form-data":
            parsed = parse_multipart(data, full_type)
            if parsed is None:
                return None
            fields, _files = parsed
            return "form", self.pairs(fields)
        # JSON only when labelled JSON or unlabelled (no content type at
        # all). An explicit octet-stream, HTML or text body stays opaque.
        json_type = ctype == "" or ctype.endswith(("/json", "+json"))
        if json_type and text.lstrip().startswith(_JSON_START):
            try:
                document = json.loads(text)
            except ValueError:
                return None
            kept: list[list[Any]] = []
            withheld: list[list[str]] = []
            self._walk(document, [], kept, withheld)
            return "json", {"kept": kept, "withheld": withheld}
        return None

    # -- URLs ------------------------------------------------------------

    def url(self, url: Any) -> Any:
        """``url`` with every undeclared query value replaced by its hash.

        Also a query inside the fragment (``#/search?q=...``, ``#q=...``) and
        a bare search term (``?term``, which has no name to declare).
        """
        if not isinstance(url, str) or ("?" not in url and "#" not in url):
            return url
        parts = up.urlsplit(url)
        fragment = parts.fragment
        if "?" in fragment:
            head, _, tail = fragment.partition("?")
            fragment = head + "?" + self.query(tail)
        elif "=" in fragment:
            fragment = self.query(fragment)
        return up.urlunsplit(
            parts._replace(query=self.query(parts.query), fragment=fragment)
        )

    def query(self, query: str) -> str:
        """A query string with undeclared values (and bare terms) hashed."""
        items = []
        for component in query.split("&"):
            if not component:
                continue
            name, sep, value = component.partition("=")
            name = up.unquote_plus(name)
            if not sep:
                items.append(
                    up.quote_plus(name if TAG.fullmatch(name) else self.tag(name))
                )
                continue
            value = up.unquote_plus(value)
            segment = self._segment(name)
            keep = not value or TAG.fullmatch(value) or self._declared([segment])
            items.append(up.urlencode([(segment, value if keep else self.tag(value))]))
        return "&".join(items)

    def refilter(self, minimized: Mapping[str, Any]) -> dict[str, Any]:
        """An already minimized body under this policy: a kept value that
        this policy doesn't declare is withheld (hashed)."""
        out = dict(minimized)
        if isinstance(out.get("kept"), list):
            kept: list[Any] = []
            withheld = list(out.get("withheld") or [])
            for item in out["kept"]:
                if not isinstance(item, list) or len(item) != 2:
                    continue
                path = [p for p in str(item[0]).split(".") if p]
                if self._declared(path):
                    kept.append(item)
                else:
                    withheld.append([str(item[0]), self.tag(item[1])])
            out["kept"], out["withheld"] = kept, withheld
        return out


def restore_kept_json(minimized: Mapping[str, Any]) -> bytes:
    """A JSON body holding only the kept fields of a minimized JSON body.

    Lets a replay of a minimized HAR still read declared id and version
    fields. Every withheld value is absent, not guessed. Empty for any other
    format.
    """
    if minimized.get("format") != "json":
        return b""
    holder: dict[str, Any] = {}
    for item in minimized.get("kept") or []:
        if not isinstance(item, list) or len(item) != 2:
            continue
        segments = [p for p in str(item[0]).split(".") if p]
        if segments and not _place(holder, [""] + segments, item[1]):
            return b""  # paths that collide: restore nothing rather than guess
    root = holder.get("")
    return json.dumps(root, sort_keys=True).encode("utf-8") if root else b""


def _place(node: Any, segments: list[str], value: Any) -> bool:
    """Set ``value`` at ``segments`` (digits index lists); False on a clash."""
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if isinstance(node, list):
            if not seg.isdigit() or int(seg) > 10_000:
                return False
            index: Any = int(seg)
            while len(node) <= index:
                node.append(None)
        elif isinstance(node, dict):
            index = seg
        else:
            return False
        current = node[index] if isinstance(node, list) else node.get(index)
        if last:
            if current is not None:
                return False
            node[index] = value
            return True
        if current is None:
            current = [] if segments[i + 1].isdigit() else {}
            node[index] = current
        if isinstance(current, list) != segments[i + 1].isdigit():
            return False
        node = current
    return False


def _har_text(container: Mapping[str, Any]) -> Optional[bytes]:
    text = container.get("text")
    if not isinstance(text, str) or not text:
        return None
    if container.get("encoding") == "base64":
        try:
            return base64.b64decode(text)
        except (ValueError, TypeError):
            return text.encode("utf-8")
    return text.encode("utf-8")


def minimize_har(har: Mapping[str, Any], minimizer: BodyMinimizer) -> dict[str, Any]:
    """A copy of a HAR 1.2 document with every body minimized.

    Request ``postData.text`` (or ``params``) and response ``content.text``
    become empty and the minimized description goes in
    ``_openadapt_minimized`` beside them. Request URLs, ``queryString``
    values, ``redirectURL``, URL-valued headers (``Location``, ``Referer``,
    ``Content-Location``, ``Refresh``) and page titles are
    minimized like any other URL. Every entry is minimized on its own, so a
    raw entry added to an already minimized HAR is minimized too, and a body
    minimized before is re-checked against this policy's fields. Wire-tap
    ids and versions survive only when they are a kept value.
    """
    doc = json.loads(json.dumps(har))
    log = doc.get("log") if isinstance(doc, dict) else None
    if not isinstance(log, dict):
        return doc
    log[HAR_FIELD] = {"schema": MINIMIZED_SCHEMA, "bodies": MINIMIZED}
    for page in log.get("pages") or []:
        if isinstance(page, dict) and isinstance(page.get("title"), str):
            page["title"] = minimizer.url(page["title"])
    for entry in log.get("entries") or []:
        if isinstance(entry, dict):
            _minimize_har_entry(entry, minimizer)
    return doc


_TX_KEPT = ("blocked", "block_reason", "is_mutation", "is_graphql_mutation", "etag")
_TX_VALUES = ("extracted_entity_id", "version")


def _minimize_har_entry(entry: dict[str, Any], minimizer: BodyMinimizer) -> None:
    kept_values: list[Any] = []
    request = entry.get("request")
    if isinstance(request, dict):
        if isinstance(request.get("url"), str):
            request["url"] = minimizer.url(request["url"])
        if isinstance(request.get("queryString"), list):
            request["queryString"] = [
                {"name": n, "value": v}
                for n, v in up.parse_qsl(
                    up.urlsplit(request.get("url") or "").query,
                    keep_blank_values=True,
                )
            ]
        _minimize_url_headers(request, minimizer)
        post = request.get("postData")
        if isinstance(post, dict):
            kept_values += _minimize_har_body(
                post, minimizer, str(post.get("mimeType") or "")
            )
    response = entry.get("response")
    if isinstance(response, dict):
        if isinstance(response.get("redirectURL"), str):
            response["redirectURL"] = minimizer.url(response["redirectURL"])
        _minimize_url_headers(response, minimizer)
        content = response.get("content")
        if isinstance(content, dict):
            kept_values += _minimize_har_body(
                content, minimizer, str(content.get("mimeType") or "")
            )
    meta = entry.get("_tx")
    if isinstance(meta, dict):
        # Wire-tap metadata keeps classifications; an id or version only
        # when the body kept it, else its hash.
        out = {k: v for k, v in meta.items() if k in _TX_KEPT}
        for k in _TX_VALUES:
            if k in meta:
                value = meta[k]
                out[k] = (
                    value
                    if value is None or value in kept_values
                    else minimizer.tag(value)
                )
        entry["_tx"] = out


#: Headers whose value is a URL, so a query in it is minimized like one.
_URL_HEADERS = frozenset({"location", "content-location", "referer", "refresh"})


def _minimize_url_headers(side: dict[str, Any], minimizer: BodyMinimizer) -> None:
    for header in side.get("headers") or []:
        if isinstance(header, dict) and (
            str(header.get("name", "")).lower() in _URL_HEADERS
        ):
            header["value"] = minimizer.url(header.get("value"))


def _minimize_har_body(
    container: dict[str, Any], minimizer: BodyMinimizer, content_type: str
) -> list[Any]:
    """Minimize one HAR body in place; returns the values it kept."""
    raw = _har_text(container)
    previous = container.get(HAR_FIELD)
    params = container.get("params")
    container.pop("encoding", None)
    container["text"] = ""
    if isinstance(params, list):
        container["params"] = []
    if isinstance(previous, Mapping) and (
        raw is None or raw == restore_kept_json(previous)
    ):
        # Minimized before (and maybe reloaded, which put its kept fields
        # back as text): keep its description, re-checked against this
        # policy. Any other text is new and is minimized afresh.
        descriptor: Optional[dict[str, Any]] = minimizer.refilter(previous)
    elif raw is None and isinstance(params, list) and params:
        descriptor = _minimize_params(params, minimizer)
    else:
        descriptor = minimizer.body(raw, content_type)
    container[HAR_FIELD] = descriptor
    if not descriptor:
        return []
    return [item[1] for item in descriptor.get("kept") or [] if isinstance(item, list)]


def _minimize_params(params: list[Any], minimizer: BodyMinimizer) -> dict[str, Any]:
    pairs = [p for p in params if isinstance(p, Mapping)]
    out: dict[str, Any] = {"schema": MINIMIZED_SCHEMA, "content_type": ""}
    if any(p.get("fileName") or p.get("contentType") for p in pairs) or len(
        pairs
    ) != len(params):
        out.update(
            format="opaque", hmac=minimizer.tag(json.dumps(params, sort_keys=True))
        )
        return out
    out["format"] = "form"
    out.update(minimizer.pairs([p.get("name", ""), p.get("value", "")] for p in pairs))
    return out


def mutation_floor(entry: Mapping[str, Any]) -> bool:
    """Whether a minimized HAR entry must replay as a write.

    Replay classifies from what was kept, which can make a write look like a
    read (a GraphQL document hashed in a ``GET`` query, an operation lost
    from a batch). The wire tap's recorded ``is_mutation`` is the floor;
    without one, a withheld or opaque write body or a hashed GraphQL
    document is.
    """
    meta = entry.get("_tx")
    if isinstance(meta, Mapping) and isinstance(meta.get("is_mutation"), bool):
        return meta["is_mutation"]
    request = entry.get("request")
    if not isinstance(request, Mapping):
        return False
    method = str(request.get("method") or "GET").upper()
    post = request.get("postData")
    body = post.get(HAR_FIELD) if isinstance(post, Mapping) else None
    if method not in _READ_METHODS and isinstance(body, Mapping):
        if body.get("withheld") or body.get("format") not in ("json", "form"):
            return True
    parts = up.urlsplit(str(request.get("url") or ""))
    graphql = parts.path.rstrip("/").lower().endswith("/graphql")
    return any(
        (graphql or name == "query") and TAG.fullmatch(value)
        for name, value in up.parse_qsl(parts.query, keep_blank_values=True)
    )


def is_minimized_har(har: Mapping[str, Any]) -> bool:
    log = har.get("log")
    return isinstance(log, Mapping) and isinstance(log.get(HAR_FIELD), Mapping)


def minimize_line(line: Mapping[str, Any], minimizer: BodyMinimizer) -> dict[str, Any]:
    """One ``network.jsonl`` line with its bodies and URLs minimized.

    ``body``, ``form`` and ``response_text`` are removed; ``body_minimized``,
    ``form_minimized`` and ``response_minimized`` describe them instead.
    Readers that need a body find none and propose nothing from it.
    """
    out = dict(line)
    for key in ("url", "frame_url", "location"):
        if key in out:
            out[key] = minimizer.url(out[key])
    request_type = str(out.get("request_type") or "")
    if "form" in out:
        form = out.pop("form") or []
        if out.get("request_truncated"):
            # A cut form may end mid-value: keep nothing of it.
            out["form_minimized"] = {
                "format": "opaque",
                "truncated": True,
                "hmac": minimizer.tag(json.dumps(form)),
            }
        else:
            out["form_minimized"] = minimizer.pairs(form)
    if "body" in out:
        out["body_minimized"] = minimizer.body(
            out.pop("body"),
            request_type,
            truncated=bool(out.get("request_truncated")),
        )
    if "response_text" in out:
        out["response_minimized"] = minimizer.body(
            out.pop("response_text"),
            str(out.get("response_type") or ""),
            truncated=bool(out.get("response_truncated")),
        )
    # The capture measured and hashed each body as sent, before decoding,
    # redaction or truncation: those, not the redacted text's, describe it.
    for key, digest_key in (
        ("body_minimized", DIGEST_REQUEST),
        ("response_minimized", DIGEST_RESPONSE),
    ):
        digest = out.pop(digest_key, None)
        if isinstance(out.get(key), dict) and isinstance(digest, Mapping):
            out[key] = {**out[key], **digest}
    return strip_private(out)


#: In-memory keys of a capture line: the size and keyed hash of a body as
#: sent. Never written under these names (:func:`strip_private`).
DIGEST_REQUEST = "_request_digest"
DIGEST_RESPONSE = "_response_digest"


def strip_private(line: Mapping[str, Any]) -> dict[str, Any]:
    """``line`` without its in-memory-only keys (those starting with ``_``)."""
    return {k: v for k, v in line.items() if not str(k).startswith("_")}


def parse_multipart(
    raw: bytes, content_type: str
) -> Optional[tuple[list[list[str]], list[str]]]:
    """The fields of a ``multipart/form-data`` body that carries no file.

    Returns ``([[name, value], ...], [file field names])`` in the order sent:
    a file input left empty is a part with ``filename=""`` and no content,
    kept as ``[name, ""]`` and named in the second list. ``None`` when a part
    carries a file, a part has a header other than its disposition and an
    ``application/octet-stream`` type for an empty file, a value isn't UTF-8,
    or the body doesn't parse: those bodies aren't kept.
    """

    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.I)
    if not m:
        return None
    delimiter = b"--" + (m.group(1) or m.group(2)).encode("latin-1")
    chunks = raw.split(delimiter)
    if len(chunks) < 2 or chunks[0].strip() or not chunks[-1].startswith(b"--"):
        return None
    pairs: list[list[str]] = []
    files: list[str] = []
    for chunk in chunks[1:-1]:
        if not chunk.startswith(b"\r\n") or not chunk.endswith(b"\r\n"):
            return None
        head, sep, value = chunk[2:-2].partition(b"\r\n\r\n")
        if not sep:
            return None
        try:
            head_lines = head.decode("utf-8").split("\r\n")
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        disposition = head_lines[0]
        extra = [h.lower() for h in head_lines[1:]]
        name = re.fullmatch(
            r'(?i)content-disposition: form-data; name="([^"]*)"(; filename="([^"]*)")?',
            disposition,
        )
        if name is None:
            return None
        if name.group(2) is None:
            if extra:
                return None
            pairs.append([name.group(1), text])
            continue
        if (
            name.group(3)
            or text
            or extra not in ([], ["content-type: application/octet-stream"])
        ):
            return None  # a file was attached
        pairs.append([name.group(1), ""])
        files.append(name.group(1))
    return pairs, files
