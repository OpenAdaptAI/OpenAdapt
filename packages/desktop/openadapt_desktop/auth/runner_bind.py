"""Parse-only grammar for ``openadapt://runner`` authoring bind URIs.

Tauri validates the same fields first. Python parses again so neither IPC nor
an operating-system protocol invocation can become a general command. This
module does not claim, store, or poll.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlsplit

AUTHORING_ORIGIN = "https://openadapt.ai"
MAX_URI_BYTES = 2048
ALLOWED_FIELDS = frozenset({"pack", "bind", "origin"})

BIND_TOKEN_RE = re.compile(r"^oab_[A-Za-z0-9_-]{43}$")
LEASE_SECRET_RE = re.compile(r"^oals_[a-f0-9]{64}$")
PACK_ALIAS_RE = re.compile(r"^p\.[A-Za-z0-9_-]{12}$")
PACK_CIPHER_RE = re.compile(r"^v1\.[A-Za-z0-9_-]{32,2000}$")
CLOUD_RUNNER_TOKEN_RE = re.compile(r"^oar_[a-f0-9]{64}$")
PAIRING_SECRET_RE = re.compile(r"^oap_[A-Za-z0-9_-]{43}$")
BIND_HEX_BODY_RE = re.compile(r"^oab_[a-f0-9]{64}$")
LEASE_BASE64URL_BODY_RE = re.compile(r"^oals_[A-Za-z0-9_-]{43}$")


class RunnerBindError(RuntimeError):
    """A safe, user-facing runner-link failure with no secret-bearing text."""


def valid_bind_token(value: object) -> bool:
    """Return whether ``value`` is exactly one ``oab_`` bind token."""

    if not isinstance(value, str):
        return False
    if (
        CLOUD_RUNNER_TOKEN_RE.fullmatch(value) is not None
        or PAIRING_SECRET_RE.fullmatch(value) is not None
        or BIND_HEX_BODY_RE.fullmatch(value) is not None
    ):
        return False
    return BIND_TOKEN_RE.fullmatch(value) is not None


def valid_lease_secret(value: object) -> bool:
    """Return whether ``value`` is exactly one ``oals_`` mailbox lease secret."""

    if not isinstance(value, str):
        return False
    if (
        CLOUD_RUNNER_TOKEN_RE.fullmatch(value) is not None
        or PAIRING_SECRET_RE.fullmatch(value) is not None
        or LEASE_BASE64URL_BODY_RE.fullmatch(value) is not None
    ):
        return False
    return LEASE_SECRET_RE.fullmatch(value) is not None


def valid_pack_id(value: object) -> bool:
    """Return whether ``value`` is a ``p.`` alias or ``v1.`` ciphertext id."""

    if not isinstance(value, str):
        return False
    return PACK_ALIAS_RE.fullmatch(value) is not None or PACK_CIPHER_RE.fullmatch(value) is not None


def canonical_authoring_origin(value: object) -> str:
    """Return the pinned production authoring origin, or raise."""

    if not isinstance(value, str):
        raise RunnerBindError("Runner link does not name the OpenAdapt authoring origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RunnerBindError("Runner link does not name the OpenAdapt authoring origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "openadapt.ai"
        or parsed.netloc != "openadapt.ai"
        or parsed.username
        or parsed.password
        or parsed.path not in ("",)
        or parsed.query
        or parsed.fragment
        or port is not None
        or value != AUTHORING_ORIGIN
    ):
        raise RunnerBindError("Runner link does not name the OpenAdapt authoring origin")
    return AUTHORING_ORIGIN


def parse_runner_uri(uri: object) -> dict[str, str]:
    """Parse the fixed runner action and reject ambiguity or extra fields."""

    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_BYTES:
        raise RunnerBindError("Invalid OpenAdapt runner link")
    parsed = urlparse(uri)
    if (
        parsed.scheme != "openadapt"
        or parsed.netloc != "runner"
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RunnerBindError("Invalid OpenAdapt runner link")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise RunnerBindError("Invalid OpenAdapt runner link") from exc
    if set(query) - ALLOWED_FIELDS or any(len(values) != 1 for values in query.values()):
        raise RunnerBindError("Runner link contains unknown or duplicate fields")
    if set(query) != ALLOWED_FIELDS:
        raise RunnerBindError("Runner link is missing pack, bind, or origin")

    pack = query["pack"][0]
    bind = query["bind"][0]
    origin = canonical_authoring_origin(query["origin"][0])
    if not valid_pack_id(pack):
        raise RunnerBindError("Pack id is malformed")
    if CLOUD_RUNNER_TOKEN_RE.fullmatch(bind) or PAIRING_SECRET_RE.fullmatch(bind):
        raise RunnerBindError("Bind token is malformed")
    if not valid_bind_token(bind):
        raise RunnerBindError("Bind token is malformed")
    return {"pack": pack, "bind": bind, "origin": origin}
