"""Where an offline Seal's signing key comes from.

By default the runner generates an Ed25519 key under the Seal home
(:mod:`openadapt_flow.execute.keys`). A production site signs with a key it
controls instead, named by ``OPENADAPT_SEAL_KEY`` (or ``--key``):

* ``file:/path/to/seal-key.pem``: a PKCS#8 PEM Ed25519 key the customer
  provides. ``OPENADAPT_SEAL_KEY_FILE=/path`` is the same thing. An encrypted
  PEM is opened with ``OPENADAPT_SEAL_KEY_PASSPHRASE``.
* ``keychain:NAME``: a key held in the OS keychain through the ``keyring``
  library (``pip install 'openadapt-flow[keychain]'``): macOS Keychain,
  Windows Credential Manager (encrypted with DPAPI), or the Linux Secret
  Service. ``openadapt-flow local-seal key create --key keychain:NAME`` makes
  one there without writing it to disk.
* ``pkcs11:token=...;object=...``: a key on a PKCS#11 hardware token or HSM
  (RFC 7512 URI, ``pip install 'openadapt-flow[pkcs11]'``). The key never
  leaves the token. The module comes from ``module-path=`` in the URI or
  ``OPENADAPT_SEAL_PKCS11_MODULE``; the PIN from ``pin-source=env:VAR`` or
  ``OPENADAPT_SEAL_PKCS11_PIN``. The token must support CKM_EDDSA.

When a key is configured and cannot be used, signing stops with
:class:`KeyUnavailable`. It never falls back to a generated key.
"""

from __future__ import annotations

import base64
import os
import stat
import sys
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from urllib.parse import unquote

from openadapt_types.seal import seal_key_id

ENV_KEY = "OPENADAPT_SEAL_KEY"
ENV_KEY_FILE = "OPENADAPT_SEAL_KEY_FILE"
ENV_KEY_PASSPHRASE = "OPENADAPT_SEAL_KEY_PASSPHRASE"
ENV_PKCS11_MODULE = "OPENADAPT_SEAL_PKCS11_MODULE"
ENV_PKCS11_PIN = "OPENADAPT_SEAL_PKCS11_PIN"
KEYCHAIN_SERVICE = "openadapt-seal"

KeySource = Literal["generated", "file", "keychain", "pkcs11"]


class KeyUnavailable(RuntimeError):
    """The configured Seal key cannot be used. Nothing was signed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _raw_public_of(private_key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


class SealSigner:
    """An Ed25519 key that signs Seals. Subclasses hold the key material."""

    source: KeySource = "generated"
    label: str = ""

    def public_raw(self) -> bytes:
        raise NotImplementedError

    def sign(self, payload: bytes) -> bytes:
        raise NotImplementedError

    def sign_ph(self, message: bytes) -> bytes:
        """Ed25519ph (prehashed, empty context) over ``message``, as Rekor checks it."""
        raise NotImplementedError

    @property
    def key_id(self) -> str:
        return seal_key_id(self.public_raw())

    def registry_entry(self) -> dict[str, str]:
        """This key as one pinned key-registry entry."""
        return {
            "key_id": self.key_id,
            "public_key": base64.b64encode(self.public_raw()).decode("ascii"),
            "status": "active",
        }

    def describe(self) -> str:
        return f"{self.source}:{self.label}" if self.label else self.source


class SoftwareSigner(SealSigner):
    """A key held in this process's memory: generated, file or keychain."""

    def __init__(self, private_key: Any, source: KeySource, label: str = "") -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        if not isinstance(private_key, Ed25519PrivateKey):
            raise KeyUnavailable("key_not_ed25519", f"{source} key is not Ed25519")
        self._key = private_key
        self.source = source
        self.label = label

    def public_raw(self) -> bytes:
        return _raw_public_of(self._key)

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload)

    def sign_ph(self, message: bytes) -> bytes:
        import hashlib

        from cryptography.hazmat.primitives import serialization

        from openadapt_flow.ed25519ph import sign_prehashed

        seed = self._key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return sign_prehashed(seed, hashlib.sha512(message).digest())


def configured_spec(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The key the environment names, or ``None`` for the generated key."""
    env = os.environ if env is None else env
    spec = str(env.get(ENV_KEY, "") or "").strip()
    path = str(env.get(ENV_KEY_FILE, "") or "").strip()
    if spec and path:
        raise KeyUnavailable(
            "key_config_conflict", f"set {ENV_KEY} or {ENV_KEY_FILE}, not both"
        )
    if path:
        return "file:" + path
    return spec or None


def resolve_signer(
    spec: Optional[str],
    *,
    home: Path,
    env: Optional[Mapping[str, str]] = None,
) -> SealSigner:
    """Open the key ``spec`` names. ``None`` or ``generated`` is the runner's key."""
    env = os.environ if env is None else env
    if spec is None or spec == "generated":
        from openadapt_flow.execute.keys import load_or_create_private_key

        return SoftwareSigner(load_or_create_private_key(home), "generated")
    kind, sep, rest = spec.partition(":")
    if not sep or not rest:
        raise KeyUnavailable(
            "key_config_invalid", "use file:PATH, keychain:NAME or pkcs11:URI"
        )
    if kind == "file":
        return _file_signer(Path(rest).expanduser(), env)
    if kind == "keychain":
        return _keychain_signer(rest)
    if kind == "pkcs11":
        return Pkcs11Signer.from_uri(spec, env)
    raise KeyUnavailable(
        "key_config_invalid",
        f"unknown key source {kind!r}; use file, keychain or pkcs11",
    )


def _check_file_mode(path: Path) -> None:
    if sys.platform == "win32":
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IWGRP):
        raise KeyUnavailable(
            "key_file_permissions",
            f"{path} is readable by others or writable by its group; chmod 600 it",
        )


def _file_signer(path: Path, env: Mapping[str, str]) -> SealSigner:
    from cryptography.hazmat.primitives import serialization

    if not path.is_file():
        raise KeyUnavailable("key_unavailable", f"no key file at {path}")
    _check_file_mode(path)
    passphrase = env.get(ENV_KEY_PASSPHRASE)
    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(),
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise KeyUnavailable(
            "key_unavailable", f"{path} is not a readable PEM private key ({exc})"
        ) from exc
    return SoftwareSigner(key, "file", str(path))


def _keyring() -> Any:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as exc:
        raise KeyUnavailable(
            "key_unavailable",
            "keychain keys need the keyring library: "
            "pip install 'openadapt-flow[keychain]'",
        ) from exc
    return keyring


def _keychain_signer(name: str) -> SealSigner:
    from cryptography.hazmat.primitives import serialization

    keyring = _keyring()
    try:
        pem = keyring.get_password(KEYCHAIN_SERVICE, name)
    except Exception as exc:  # a locked or absent keychain backend
        raise KeyUnavailable(
            "key_unavailable", f"the OS keychain refused: {type(exc).__name__}"
        ) from exc
    if not pem:
        raise KeyUnavailable(
            "key_unavailable", f"no key named {name!r} in the OS keychain"
        )
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise KeyUnavailable(
            "key_unavailable", f"keychain item {name!r} is not a PEM private key"
        ) from exc
    return SoftwareSigner(key, "keychain", name)


def create_key(spec: str) -> SealSigner:
    """Generate a new Ed25519 key at ``spec`` (``file:`` or ``keychain:``).

    Refuses to replace a key that already exists. A PKCS#11 key is made with
    the token's own tools, so the private half never exists outside it.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    kind, _, rest = spec.partition(":")
    if kind not in ("file", "keychain") or not rest:
        raise KeyUnavailable(
            "key_config_invalid",
            "create makes file:PATH or keychain:NAME keys; make PKCS#11 keys "
            "with the token's tools",
        )
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    if kind == "file":
        path = Path(rest).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise KeyUnavailable("key_exists", f"{path} already exists") from exc
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        return SoftwareSigner(key, "file", str(path))
    keyring = _keyring()
    if keyring.get_password(KEYCHAIN_SERVICE, rest):
        raise KeyUnavailable("key_exists", f"keychain already holds {rest!r}")
    keyring.set_password(KEYCHAIN_SERVICE, rest, pem.decode("ascii"))
    return SoftwareSigner(key, "keychain", rest)


def _parse_pkcs11_uri(uri: str) -> tuple[dict[str, str], dict[str, str]]:
    """Split an RFC 7512 URI into path and query attributes (percent-decoded)."""
    body = uri[len("pkcs11:") :]
    path_part, _, query_part = body.partition("?")

    def pairs(text: str, sep: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in filter(None, text.split(sep)):
            name, eq, value = item.partition("=")
            if not eq:
                raise KeyUnavailable(
                    "key_config_invalid", f"bad PKCS#11 URI part {item!r}"
                )
            out[name.strip()] = value
        return out

    return pairs(path_part, ";"), pairs(query_part, "&")


class Pkcs11Signer(SealSigner):
    """An Ed25519 key that stays on a PKCS#11 token; each signature opens a session."""

    source: KeySource = "pkcs11"

    def __init__(
        self,
        module: str,
        token: str,
        *,
        label: Optional[str] = None,
        key_id: Optional[bytes] = None,
        pin: Optional[str] = None,
    ) -> None:
        if not (label or key_id):
            raise KeyUnavailable(
                "key_config_invalid", "a PKCS#11 key needs object= or id="
            )
        self.module = module
        self.token = token
        self.object_label = label
        self.object_id = key_id
        self._pin = pin
        self.label = f"token={token};object={label or key_id.hex()}"  # type: ignore[union-attr]
        self._public: Optional[bytes] = None

    @classmethod
    def from_uri(cls, uri: str, env: Mapping[str, str]) -> "Pkcs11Signer":
        attrs, query = _parse_pkcs11_uri(uri)
        module = unquote(query.get("module-path", "")) or env.get(ENV_PKCS11_MODULE, "")
        if not module:
            raise KeyUnavailable(
                "key_config_invalid",
                f"name the PKCS#11 module with module-path= or {ENV_PKCS11_MODULE}",
            )
        token = unquote(attrs.get("token", ""))
        if not token:
            raise KeyUnavailable("key_config_invalid", "a PKCS#11 key needs token=")
        pin_source = unquote(query.get("pin-source", ""))
        if "pin-value" in query:
            raise KeyUnavailable(
                "key_config_invalid",
                "put the PIN in an environment variable (pin-source=env:VAR), "
                "not in the URI",
            )
        if pin_source.startswith("env:"):
            pin = env.get(pin_source[4:])
        elif pin_source:
            raise KeyUnavailable("key_config_invalid", "pin-source must be env:VAR")
        else:
            pin = env.get(ENV_PKCS11_PIN)
        raw_id = attrs.get("id")
        return cls(
            module,
            token,
            label=unquote(attrs["object"]) if "object" in attrs else None,
            key_id=unquote(raw_id).encode("latin-1") if raw_id else None,
            pin=pin,
        )

    def _session(self) -> Any:
        try:
            import pkcs11  # type: ignore[import-not-found]
        except ImportError as exc:
            raise KeyUnavailable(
                "key_unavailable",
                "PKCS#11 keys need python-pkcs11: pip install 'openadapt-flow[pkcs11]'",
            ) from exc
        try:
            lib = pkcs11.lib(self.module)
            token = lib.get_token(token_label=self.token)
            return pkcs11, token.open(user_pin=self._pin)
        except Exception as exc:
            raise KeyUnavailable(
                "key_unavailable",
                f"PKCS#11 token {self.token!r} unavailable: {type(exc).__name__}",
            ) from exc

    def _find(self, pkcs11: Any, session: Any, object_class: Any) -> Any:
        return session.get_key(
            object_class=object_class,
            key_type=pkcs11.KeyType.EC_EDWARDS,
            label=self.object_label,
            id=self.object_id,
        )

    def public_raw(self) -> bytes:
        if self._public is None:
            pkcs11, session = self._session()
            try:
                with session:
                    pub = self._find(pkcs11, session, pkcs11.ObjectClass.PUBLIC_KEY)
                    point = bytes(pub[pkcs11.Attribute.EC_POINT])
            except KeyUnavailable:
                raise
            except Exception as exc:
                raise KeyUnavailable(
                    "key_unavailable",
                    f"no Ed25519 public key {self.label} on the token: "
                    f"{type(exc).__name__}",
                ) from exc
            self._public = _ec_point_raw(point)
        return self._public

    def _sign(self, data: bytes, mechanism_param: Any = None) -> bytes:
        pkcs11, session = self._session()
        try:
            with session:
                priv = self._find(pkcs11, session, pkcs11.ObjectClass.PRIVATE_KEY)
                kwargs: dict[str, Any] = {"mechanism": pkcs11.Mechanism.EDDSA}
                if mechanism_param is not None:
                    kwargs["mechanism_param"] = mechanism_param
                signature = bytes(priv.sign(data, **kwargs))
        except KeyUnavailable:
            raise
        except Exception as exc:
            raise KeyUnavailable(
                "key_unavailable",
                f"the token did not sign with {self.label}: {type(exc).__name__}",
            ) from exc
        if len(signature) != 64:
            raise KeyUnavailable(
                "key_unavailable", "the token returned a bad signature"
            )
        return signature

    def sign(self, payload: bytes) -> bytes:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        signature = self._sign(payload)
        # Check the token's answer against its public key before trusting it.
        try:
            Ed25519PublicKey.from_public_bytes(self.public_raw()).verify(
                signature, payload
            )
        except InvalidSignature as exc:
            raise KeyUnavailable(
                "key_unavailable", "the token's signature does not verify"
            ) from exc
        return signature

    def sign_ph(self, message: bytes) -> bytes:
        import hashlib

        from openadapt_flow.ed25519ph import verify_prehashed

        digest = hashlib.sha512(message).digest()
        # PKCS#11 3.0 CKM_EDDSA with phFlag set. Tokens differ on whether they
        # hash the input themselves, so try the message, then the digest, and
        # keep only a signature that verifies.
        for data in (message, digest):
            signature = self._sign(data, mechanism_param=(True, None))
            if verify_prehashed(self.public_raw(), digest, signature):
                return signature
        raise KeyUnavailable(
            "key_unavailable", "the token did not produce a valid Ed25519ph signature"
        )


def _ec_point_raw(point: bytes) -> bytes:
    """CKA_EC_POINT for Ed25519: 32 raw bytes, or a DER OCTET STRING of them."""
    if len(point) == 32:
        return point
    if len(point) == 34 and point[0] == 0x04 and point[1] == 32:
        return point[2:]
    raise KeyUnavailable("key_not_ed25519", "the token's key is not Ed25519")
