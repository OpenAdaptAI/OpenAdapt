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
  content type and a keyed hash of the whole body.
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
like a value. Header values are handled by the credential redaction only.

Stdlib only, and no ``openadapt_flow`` import at module level, so the wire
tap and the recorder can load it lazily without changing their import cost.
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


class BodyMinimizer:
    """Minimize the bodies and URLs of one capture.

    ``keep_fields`` are the declared field names. A JSON value is kept when
    its leaf key or its dotted path (``data.id``, list indices dropped)
    equals a declared name, compared case-insensitively. One instance per
    capture: its hash key lives only as long as the instance.
    """

    def __init__(self, keep_fields: Iterable[str] = (), *, key: Optional[bytes] = None):
        self._keep = frozenset(str(f).lower() for f in keep_fields if str(f))
        self._key = key if key is not None else secrets.token_bytes(32)

    @property
    def keep_fields(self) -> frozenset[str]:
        return self._keep

    def tag(self, value: Any) -> str:
        """The keyed hash that stands in for ``value``."""
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
        else:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        digest = hmac.new(self._key, data, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def _declared(self, path: list[str]) -> bool:
        if not path or not self._keep:
            return False
        named = [p for p in path if not p.isdigit()]
        if not named:
            return False
        return named[-1].lower() in self._keep or ".".join(named).lower() in self._keep

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
            from openadapt_flow.network_capture import parse_multipart

            parsed = parse_multipart(data, full_type)
            if parsed is None:
                return None
            fields, _files = parsed
            return "form", self.pairs(fields)
        if text.lstrip().startswith(_JSON_START):
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
        """``url`` with every undeclared query value replaced by its hash."""
        if not isinstance(url, str) or "?" not in url:
            return url
        parts = up.urlsplit(url)
        if not parts.query:
            return url
        items = []
        for name, value in up.parse_qsl(parts.query, keep_blank_values=True):
            segment = self._segment(name)
            keep = self._declared([segment])
            items.append((segment, value if keep or not value else self.tag(value)))
        return up.urlunsplit(parts._replace(query=up.urlencode(items)))


def restore_kept_json(minimized: Mapping[str, Any]) -> bytes:
    """A JSON body holding only the kept fields of a minimized JSON body.

    Lets a replay of a minimized HAR still read declared id and version
    fields. Every withheld value is absent, not guessed. Empty for any other
    format.
    """
    if minimized.get("format") != "json":
        return b""
    root: dict[str, Any] = {}
    for item in minimized.get("kept") or []:
        if not isinstance(item, list) or len(item) != 2:
            continue
        path, value = str(item[0]), item[1]
        names = [p for p in path.split(".") if p and not p.isdigit()]
        if not names:
            continue
        node = root
        for name in names[:-1]:
            child = node.get(name)
            if not isinstance(child, dict):
                child = {}
                node[name] = child
            node = child
        node.setdefault(names[-1], value)
    return json.dumps(root, sort_keys=True).encode("utf-8") if root else b""


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

    Request ``postData.text`` and response ``content.text`` become empty and
    the minimized description goes in ``_openadapt_minimized`` beside them.
    Request URLs, ``queryString`` values, ``redirectURL`` and ``Location``
    headers are minimized like any other URL.
    """
    doc = json.loads(json.dumps(har))
    log = doc.get("log") if isinstance(doc, dict) else None
    if not isinstance(log, dict):
        return doc
    log[HAR_FIELD] = {"schema": MINIMIZED_SCHEMA, "bodies": MINIMIZED}
    for entry in log.get("entries") or []:
        if not isinstance(entry, dict):
            continue
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
            post = request.get("postData")
            if isinstance(post, dict):
                _minimize_har_body(post, minimizer, str(post.get("mimeType") or ""))
                if isinstance(post.get("params"), list):
                    post["params"] = []
        response = entry.get("response")
        if isinstance(response, dict):
            if isinstance(response.get("redirectURL"), str):
                response["redirectURL"] = minimizer.url(response["redirectURL"])
            for header in response.get("headers") or []:
                if (
                    isinstance(header, dict)
                    and str(header.get("name", "")).lower() == "location"
                ):
                    header["value"] = minimizer.url(header.get("value"))
            content = response.get("content")
            if isinstance(content, dict):
                _minimize_har_body(
                    content, minimizer, str(content.get("mimeType") or "")
                )
        meta = entry.get("_tx")
        if isinstance(meta, dict):
            # Wire-tap metadata keeps classifications, ids and versions only.
            entry["_tx"] = {
                k: v
                for k, v in meta.items()
                if k
                in (
                    "blocked",
                    "block_reason",
                    "is_mutation",
                    "is_graphql_mutation",
                    "extracted_entity_id",
                    "etag",
                    "version",
                )
            }
    return doc


def _minimize_har_body(
    container: dict[str, Any], minimizer: BodyMinimizer, content_type: str
) -> None:
    raw = _har_text(container)
    container.pop("encoding", None)
    container["text"] = ""
    container[HAR_FIELD] = minimizer.body(raw, content_type)


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
        out["form_minimized"] = minimizer.pairs(out.pop("form") or [])
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
    return out
