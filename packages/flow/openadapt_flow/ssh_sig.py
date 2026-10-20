"""Verify ``ssh-keygen -Y sign`` signatures (SSHSIG) offline. Ed25519 keys only.

A person signs a file with the SSH key they already publish on GitHub::

    ssh-keygen -Y sign -n NAMESPACE -f ~/.ssh/id_ed25519 FILE    # writes FILE.sig

This module checks such a signature without ``ssh-keygen``, following the
format in OpenSSH's PROTOCOL.sshsig: the signed blob is ``"SSHSIG"``, the
namespace, a reserved string, the hash name and the hash of the file, each
as an SSH string. It also reads and writes the allowed-signers format
``ssh-keygen -Y verify`` uses, and fetches a GitHub user's published keys
from ``https://github.com/<user>.keys`` (the host is fixed).
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

MAGIC = b"SSHSIG"
_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_END = "-----END SSH SIGNATURE-----"
_HASHES = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}
GITHUB_HOST = "github.com"
_GITHUB_USER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_TIMEOUT = 30


class SshSigError(ValueError):
    """A signature, key line or allowed-signers file that can't be read."""


def _string(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise SshSigError("truncated")
    (length,) = struct.unpack(">I", buf[pos : pos + 4])
    end = pos + 4 + length
    if end > len(buf):
        raise SshSigError("truncated")
    return buf[pos + 4 : end], end


def _ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def ed25519_blob(raw: bytes) -> bytes:
    """The SSH wire encoding of an Ed25519 public key."""
    return _ssh_string(b"ssh-ed25519") + _ssh_string(raw)


def raw_from_blob(blob: bytes) -> bytes:
    kind, pos = _string(blob, 0)
    if kind != b"ssh-ed25519":
        raise SshSigError(f"not an Ed25519 key ({kind.decode('ascii', 'replace')})")
    raw, pos = _string(blob, pos)
    if len(raw) != 32 or pos != len(blob):
        raise SshSigError("bad Ed25519 key")
    return raw


def key_line(raw: bytes) -> str:
    """``ssh-ed25519 AAAA...``, as in ``authorized_keys`` and GitHub's ``.keys``."""
    return "ssh-ed25519 " + base64.b64encode(ed25519_blob(raw)).decode("ascii")


def parse_key_line(line: str) -> bytes:
    parts = line.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise SshSigError("not an ssh-ed25519 key line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise SshSigError("bad base64 in key line") from exc
    return raw_from_blob(blob)


def fingerprint(raw: bytes) -> str:
    """OpenSSH's ``SHA256:...`` fingerprint, as ``ssh-keygen -lf`` prints it."""
    digest = hashlib.sha256(ed25519_blob(raw)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class SshSignature:
    public_raw: bytes
    namespace: str
    hash_algorithm: str
    signature: bytes


def parse_armored(text: str) -> SshSignature:
    body = text.strip()
    if not (body.startswith(_BEGIN) and body.endswith(_END)):
        raise SshSigError("not an SSH signature")
    b64 = "".join(body[len(_BEGIN) : -len(_END)].split())
    try:
        blob = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise SshSigError("bad base64") from exc
    if blob[:6] != MAGIC or len(blob) < 10:
        raise SshSigError("missing SSHSIG preamble")
    (version,) = struct.unpack(">I", blob[6:10])
    if version != 1:
        raise SshSigError(f"unsupported SSHSIG version {version}")
    pos = 10
    key_blob, pos = _string(blob, pos)
    namespace, pos = _string(blob, pos)
    _reserved, pos = _string(blob, pos)
    hash_alg, pos = _string(blob, pos)
    sig_blob, pos = _string(blob, pos)
    if pos != len(blob):
        raise SshSigError("trailing bytes")
    sig_kind, spos = _string(sig_blob, 0)
    sig_raw, spos = _string(sig_blob, spos)
    if sig_kind != b"ssh-ed25519" or spos != len(sig_blob) or len(sig_raw) != 64:
        raise SshSigError("not an Ed25519 signature")
    try:
        return SshSignature(
            public_raw=raw_from_blob(key_blob),
            namespace=namespace.decode("ascii"),
            hash_algorithm=hash_alg.decode("ascii"),
            signature=sig_raw,
        )
    except UnicodeDecodeError as exc:
        raise SshSigError("bad namespace or hash name") from exc


def signed_blob(message: bytes, namespace: str, hash_algorithm: str) -> bytes:
    """The bytes an SSHSIG signature covers."""
    if hash_algorithm not in _HASHES:
        raise SshSigError(f"unsupported hash {hash_algorithm}")
    return (
        MAGIC
        + _ssh_string(namespace.encode("ascii"))
        + _ssh_string(b"")
        + _ssh_string(hash_algorithm.encode("ascii"))
        + _ssh_string(_HASHES[hash_algorithm](message).digest())
    )


def verify(message: bytes, armored: str, namespace: str) -> bytes:
    """Return the signer's raw Ed25519 key if ``armored`` signs ``message``.

    Raises :class:`SshSigError` for anything else: a bad signature, another
    namespace, another key type.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig = parse_armored(armored)
    if sig.namespace != namespace:
        raise SshSigError(f"signed for namespace {sig.namespace!r}, not {namespace!r}")
    try:
        Ed25519PublicKey.from_public_bytes(sig.public_raw).verify(
            sig.signature, signed_blob(message, namespace, sig.hash_algorithm)
        )
    except InvalidSignature as exc:
        raise SshSigError("signature does not verify") from exc
    return sig.public_raw


def armor(public_raw: bytes, namespace: str, signature: bytes) -> str:
    """Wrap a raw Ed25519 SSHSIG signature (used by tests and fixtures)."""
    blob = (
        MAGIC
        + struct.pack(">I", 1)
        + _ssh_string(ed25519_blob(public_raw))
        + _ssh_string(namespace.encode("ascii"))
        + _ssh_string(b"")
        + _ssh_string(b"sha512")
        + _ssh_string(_ssh_string(b"ssh-ed25519") + _ssh_string(signature))
    )
    b64 = base64.b64encode(blob).decode("ascii")
    lines = [b64[i : i + 70] for i in range(0, len(b64), 70)]
    return "\n".join([_BEGIN, *lines, _END]) + "\n"


# --- allowed signers ----------------------------------------------------------


@dataclass(frozen=True)
class AllowedSigner:
    principals: tuple[str, ...]
    namespaces: Optional[tuple[str, ...]]  # None: any namespace
    public_raw: bytes


def allowed_signers_line(principal: str, namespace: str, raw: bytes) -> str:
    return f'{principal} namespaces="{namespace}" {key_line(raw)}'


def parse_allowed_signers(text: str) -> list[AllowedSigner]:
    """Read the ``ssh-keygen`` allowed-signers lines this module writes.

    Only plain Ed25519 lines with an optional ``namespaces="..."`` option are
    accepted; certificate authorities and other options are refused.
    """
    out: list[AllowedSigner] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(
            r'^(\S+)\s+(?:namespaces="([^"]*)"\s+)?(ssh-ed25519\s+\S+)(?:\s+.*)?$', line
        )
        if not m:
            raise SshSigError(f"unsupported allowed-signers line: {line[:60]}")
        principals = tuple(p for p in m.group(1).split(",") if p)
        namespaces = tuple(n for n in m.group(2).split(",")) if m.group(2) else None
        out.append(AllowedSigner(principals, namespaces, parse_key_line(m.group(3))))
    return out


def is_allowed(
    signers: list[AllowedSigner], principal: str, namespace: str, raw: bytes
) -> bool:
    return any(
        s.public_raw == raw
        and principal in s.principals
        and (s.namespaces is None or namespace in s.namespaces)
        for s in signers
    )


# --- GitHub ---------------------------------------------------------------------


def github_keys_url(user: str) -> str:
    if not _GITHUB_USER.match(user):
        raise SshSigError(f"not a GitHub user name: {user!r}")
    return f"https://{GITHUB_HOST}/{user}.keys"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise SshSigError(f"github.com answered with a redirect ({code}); not followed")


def _github_opener() -> urllib.request.OpenerDirector:
    """HTTPS to github.com only: no proxy (whatever the environment says), no redirects."""
    import ssl

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def fetch_github_keys(user: str, opener: Optional[Any] = None) -> list[str]:
    """The key lines GitHub publishes for ``user`` (all types; callers filter).

    Fetches ``https://github.com/<user>.keys`` with certificate checks, no
    proxy and no redirects, and refuses any answer that isn't from that URL.
    """
    url = github_keys_url(user)
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    with (opener or _github_opener()).open(request, timeout=_TIMEOUT) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != GITHUB_HOST:
            raise SshSigError(f"answer came from {response.geturl()}, not {url}")
        if getattr(response, "status", 200) != 200:
            raise SshSigError(f"github.com answered {response.status}")
        text = response.read(1_000_000).decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def ed25519_keys(lines: list[str]) -> list[bytes]:
    keys = []
    for line in lines:
        try:
            keys.append(parse_key_line(line))
        except SshSigError:
            continue
    return keys
