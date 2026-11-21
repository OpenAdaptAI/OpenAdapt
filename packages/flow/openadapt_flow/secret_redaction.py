"""One conservative encoding model for secret detection and public redaction.

Unknown nested encodings are bounded. Exhausting that bound refuses disclosure;
raw transaction evidence remains on the separate encrypted persistence path.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from collections import deque
from typing import Iterable
from urllib.parse import quote, quote_plus, unquote, unquote_plus

MASK = "•" * 4
_MAX_VIEWS = 128
_JSON_ESCAPES = re.compile(r'(?:\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt]))+')
_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")


def secret_forms(value: str) -> set[str]:
    """Common direct representations, retained for wire-check callers."""
    if not value:
        return set()
    raw = value.encode("utf-8")
    forms = {
        value,
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        quote(value, safe="").lower(),
        quote_plus(value).lower(),
        json.dumps(value)[1:-1],
        html.escape(value),
        base64.b64encode(raw).decode("ascii"),
        base64.urlsafe_b64encode(raw).decode("ascii"),
    }
    return {form for item in forms for form in (item, item.rstrip("=")) if form}


def _json_unescape(text: str) -> str:
    return _JSON_ESCAPES.sub(lambda m: str(json.loads('"' + m[0] + '"')), text)


def carries_secret(text: str | bytes, secrets: Iterable[str]) -> bool:
    """Detect the same forms redaction uses, including encoded binary contents.

    Percent/form, JSON escapes, HTML and base64 can be nested or mixed. The
    bounded decode graph also handles partial percent/unicode escaping, rather
    than assuming an encoder escaped the entire secret in one canonical way.
    """
    forms = {form for value in secrets if value for form in secret_forms(value)}
    if not forms:
        return False
    byte_forms = {form.encode("utf-8") for form in forms}
    pending: deque[str | bytes] = deque([text])
    seen: set[str | bytes] = {text}
    while pending:
        current = pending.popleft()
        decoded: list[str | bytes]
        if isinstance(current, bytes):
            if any(form in current for form in byte_forms):
                return True
            decoded = [current.decode("utf-8", errors="replace")]
        else:
            if any(form in current for form in forms):
                return True
            decoded = [
                unquote(current),
                unquote_plus(current),
                html.unescape(current),
                _json_unescape(current),
            ]
            compact = "".join(current.split())
            if compact != current and _BASE64_TOKEN.fullmatch(compact):
                decoded.append(compact)
            for match in _BASE64_TOKEN.finditer(current):
                token = match[0]
                if len(token) % 4 == 1:
                    continue
                try:
                    decoded.append(
                        base64.b64decode(
                            token + "=" * (-len(token) % 4),
                            altchars=b"-_",
                            validate=True,
                        )
                    )
                except (binascii.Error, ValueError):
                    continue
        for view in decoded:
            if view in seen:
                continue
            if len(seen) >= _MAX_VIEWS:
                return True
            seen.add(view)
            pending.append(view)
    return False


def mask_secrets(text: str, secrets: Iterable[str], mask: str = MASK) -> str:
    """Replace direct forms; withhold a leaf if decoding still exposes a secret."""
    values = tuple(value for value in secrets if value)
    forms = {form for value in values for form in secret_forms(value)}
    for form in sorted(forms, key=lambda item: (-len(item), item)):
        text = text.replace(form, mask)
    return mask if carries_secret(text, values) else text


def credential_header(name: str) -> bool:
    """Credentials are private even when their values were not declared."""
    key = name.strip().lower().replace("-", "").replace("_", "")
    return key in {
        "authorization",
        "proxyauthorization",
        "authentication",
        "authenticationinfo",
        "proxyauthenticationinfo",
        "cookie",
        "setcookie",
    } or key.endswith(("token", "apikey", "secret"))


def mask_header(name: str, value: str, secrets: Iterable[str]) -> str:
    return MASK if credential_header(name) else mask_secrets(value, secrets)
