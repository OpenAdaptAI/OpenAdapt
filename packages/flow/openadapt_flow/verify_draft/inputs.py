"""What the model drafts from: the system's description, one trace, the task.

- **Description**: an OpenAPI (or Swagger) document, a GraphQL schema (SDL
  text or an introspection result), a FHIR CapabilityStatement, the
  database schema read from the sandbox (``db``), or none. Each is cut down
  to the parts a check needs (paths and bodies, types and fields,
  resources and interactions, tables and keys).
- **Trace**: a HAR file of one correct demonstration. The writes in it are
  what the sandbox replays; sign-in headers and secret-looking fields are
  masked before anything is sent to the model.
- **Task**: ``task.yaml`` with the instruction and the values the
  demonstration used (``params``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.secret_field_names import names_secret_field
from openadapt_flow.verify_draft.sandbox import Request

#: Longest description text sent to the model.
MAX_DESCRIPTION_CHARS = 40_000


def is_secret_name(name: object) -> bool:
    """True when a field, header, query key or column name looks secret.

    One shared token matcher (:mod:`openadapt_flow.secret_field_names`), so
    ``otpCode``, ``userPin``, ``X-Amz-Signature`` and a ``cookie`` key are
    masked while ``author``, ``author_id`` and ``session_count`` stay clear.
    """

    return names_secret_field(name)


_SECRET_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-csrf-token",
        "proxy-authorization",
    }
)
_READS = frozenset({"GET", "HEAD", "OPTIONS"})


class InputError(ValueError):
    """An input can't be read; the message says why."""


@dataclass
class Description:
    kind: str  # openapi | graphql | fhir | database | none
    text: str
    source: str = ""


@dataclass
class Task:
    instruction: str
    params: dict[str, Any]


def load_task(path: str | Path) -> Task:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("params"), dict):
        raise InputError("task.yaml needs instruction and params")
    params = dict(data["params"])
    if "actor_id" in params:
        raise InputError("actor_id is reserved: the sandbox binds it")
    return Task(str(data.get("instruction") or ""), params)


# -- description ---------------------------------------------------------------


def _load_doc(path: Path) -> Any:
    import yaml

    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        doc = yaml.safe_load(raw)
        if isinstance(doc, (dict, list)):
            return doc
    except (yaml.YAMLError, ValueError):
        pass
    return raw


def load_description(source: Optional[str], *, schema_text: str = "") -> Description:
    """``source``: a file, ``db`` (the sandbox's introspected schema), or
    None / ``none``."""

    if source in (None, "", "none"):
        return Description("none", "", "none")
    if source == "db":
        return Description("database", schema_text[:MAX_DESCRIPTION_CHARS], "db")
    path = Path(source)
    if not path.is_file():
        raise InputError(f"description {source} is not a file")
    doc = _load_doc(path)
    if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc):
        return Description("openapi", _openapi(doc), path.name)
    if isinstance(doc, dict) and doc.get("resourceType") == "CapabilityStatement":
        return Description("fhir", _fhir(doc), path.name)
    if isinstance(doc, dict) and (
        "__schema" in doc or "__schema" in (doc.get("data") or {})
    ):
        return Description(
            "graphql", _graphql(doc.get("data", doc)["__schema"]), path.name
        )
    if isinstance(doc, str) and re.search(r"\b(type|schema|input)\s+\w+\s*\{", doc):
        return Description("graphql", doc[:MAX_DESCRIPTION_CHARS], path.name)
    raise InputError(
        f"{source}: not an OpenAPI document, a GraphQL schema or a FHIR CapabilityStatement"
    )


def _ref(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return f"[{_ref(schema.get('items'))}]"
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            return "|".join(_ref(s) for s in schema[key] if _ref(s))
    return str(schema.get("type", ""))


def _openapi(doc: dict) -> str:
    lines = [
        f"# {doc.get('info', {}).get('title', 'API')} {doc.get('info', {}).get('version', '')}"
    ]
    for path, ops in (doc.get("paths") or {}).items():
        for method, op in (ops or {}).items():
            if (
                not isinstance(op, dict)
                or method.startswith("x-")
                or method == "parameters"
            ):
                continue
            body = ""
            content = (op.get("requestBody") or {}).get("content") or {}
            for mime, media in content.items():
                body = f" body={_ref(media.get('schema'))} ({mime})"
                break
            params = ",".join(
                p.get("name", "")
                for p in op.get("parameters") or []
                if isinstance(p, dict)
            )
            lines.append(
                f"{method.upper()} {path}{body}"
                + (f" params={params}" if params else "")
                + (f" -- {op.get('summary')}" if op.get("summary") else "")
            )
    schemas = (
        (doc.get("components") or {}).get("schemas") or doc.get("definitions") or {}
    )
    lines.append("# schemas")
    for name, schema in schemas.items():
        props = (schema or {}).get("properties") or {}
        fields = ", ".join(f"{k}:{_ref(v)}" for k, v in props.items())
        lines.append(f"{name}{{{fields}}}")
    return "\n".join(lines)[:MAX_DESCRIPTION_CHARS]


def _fhir(doc: dict) -> str:
    lines = [f"# FHIR {doc.get('fhirVersion', '')}"]
    for rest in doc.get("rest") or []:
        for res in rest.get("resource") or []:
            inter = ",".join(i.get("code", "") for i in res.get("interaction") or [])
            search = ",".join(s.get("name", "") for s in res.get("searchParam") or [])
            lines.append(
                f"{res.get('type')}: {inter}" + (f" search={search}" if search else "")
            )
    return "\n".join(lines)[:MAX_DESCRIPTION_CHARS]


def _graphql(schema: dict) -> str:
    lines = []
    for t in schema.get("types") or []:
        if str(t.get("name", "")).startswith("__"):
            continue
        fields = ", ".join(
            f.get("name", "") for f in t.get("fields") or [] if isinstance(f, dict)
        )
        if fields:
            lines.append(f"{t.get('kind', '').lower()} {t['name']} {{{fields}}}")
    return "\n".join(lines)[:MAX_DESCRIPTION_CHARS]


# -- trace ---------------------------------------------------------------------


def load_trace(path: str | Path) -> list[Request]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"trace {path}: {exc}") from exc
    entries = ((doc or {}).get("log") or {}).get("entries")
    if not isinstance(entries, list):
        raise InputError("the trace is a HAR file (log.entries)")
    out = []
    for e in entries:
        req = e.get("request") or {}
        post = req.get("postData") or {}
        body = post.get("text")
        if body is None and post.get("params"):
            from urllib.parse import urlencode

            body = urlencode([(p["name"], p.get("value", "")) for p in post["params"]])
        res = e.get("response") or {}
        out.append(
            Request(
                method=str(req.get("method", "GET")).upper(),
                url=str(req.get("url", "")),
                headers={
                    h["name"]: h.get("value", "") for h in req.get("headers") or []
                },
                body=body,
                mime=str(post.get("mimeType") or ""),
                status=res.get("status"),
                response=((res.get("content") or {}).get("text")),
            )
        )
    return out


def writes(
    trace: list[Request], *, skip_paths: Collection[str] = frozenset()
) -> list[Request]:
    """The trace's write requests (anything but GET, HEAD and OPTIONS),
    without sign-in requests."""

    return [r for r in trace if r.method not in _READS and r.path not in skip_paths]


def _mask_json(value: Any, *, mask_scalars: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            k: _mask_json(
                v,
                mask_scalars=mask_scalars or is_secret_name(k),
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_json(v, mask_scalars=mask_scalars) for v in value]
    return "***" if mask_scalars else value


def _mask_multipart(body: str, mime: str) -> str:
    from email.parser import BytesParser
    from email.policy import default

    raw = f"Content-Type: {mime}\r\nMIME-Version: 1.0\r\n\r\n".encode(
        "utf-8"
    ) + body.encode("utf-8", errors="replace")
    msg = BytesParser(policy=default).parsebytes(raw)
    if not msg.is_multipart():
        return "<redacted multipart body>"
    fields: list[dict[str, str]] = []
    for part in msg.iter_parts():
        name = str(part.get_param("name", header="content-disposition") or "")
        filename = part.get_filename()
        if filename is not None:
            fields.append({"name": name, "filename": str(filename), "value": "***"})
            continue
        if not name or is_secret_name(name):
            fields.append({"name": name, "value": "***"})
            continue
        payload = part.get_content()
        text_val = payload if isinstance(payload, str) else "<redacted binary>"
        fields.append({"name": name, "value": text_val[:500]})
    if not fields:
        return "<redacted multipart body>"
    return json.dumps({"multipart_fields": fields})[:2000]


def _mask_body(body: Optional[str], mime: str) -> Optional[str]:
    if body is None:
        return None
    mime_low = (mime or "").lower()
    if "json" in mime_low:
        try:
            return json.dumps(_mask_json(json.loads(body)))[:2000]
        except ValueError:
            return "***"
    if "x-www-form-urlencoded" in mime_low:
        from urllib.parse import parse_qsl, urlencode

        return urlencode(
            [
                (k, "***" if is_secret_name(k) else v)
                for k, v in parse_qsl(body, keep_blank_values=True)
            ]
        )[:2000]
    if "multipart/form-data" in mime_low:
        return _mask_multipart(body, mime)
    return "<redacted body>"


def _mask_response(response: Optional[str], *, response_chars: int) -> Optional[str]:
    if response is None:
        return None
    stripped = response.lstrip()
    if stripped[:1] in "{[":
        try:
            return json.dumps(_mask_json(json.loads(response)))[:response_chars] or None
        except ValueError:
            return "<redacted non-json response>"
    return "<redacted non-json response>"


def masked(req: Request, *, response_chars: int = 1500) -> dict[str, Any]:
    """One request as the model sees it: no sign-in headers, no secret
    fields, a short response."""

    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(req.url)
    query = urlencode(
        [
            (k, "***" if is_secret_name(k) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    response = _mask_response(req.response, response_chars=response_chars)
    return {
        "method": req.method,
        "url": urlunsplit(("", "", parts.path, query, "")),
        "headers": {
            k: v
            for k, v in req.headers.items()
            if k.lower() not in _SECRET_HEADERS and not is_secret_name(k)
        },
        "body": _mask_body(req.body, req.mime),
        "status": req.status,
        "response": response,
    }
