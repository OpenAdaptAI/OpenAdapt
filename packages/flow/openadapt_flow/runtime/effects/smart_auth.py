"""SMART Backend Services access tokens for the ``fhir`` record check.

EHR FHIR APIs that serve backend clients usually issue short-lived tokens
through SMART Backend Services (HL7 SMART App Launch 2.x, "Backend Services"
and "Client Authentication: Asymmetric"). A static bearer token in
``access_token_env`` stops working when it expires, and every read then comes
back unreadable. This module gets a fresh token when one is needed:

1. Build a one-time client assertion, a JWT signed with the client's private
   key (``RS384`` or ``ES384``). Header: ``alg``, ``kid``, ``typ: JWT`` and an
   optional ``jku``. Claims: ``iss`` and ``sub`` are the ``client_id``, ``aud``
   is the token URL, ``exp`` is at most five minutes ahead, ``jti`` is random.
2. POST it to the token URL as an OAuth 2.0 ``client_credentials`` grant with
   ``client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer``.
3. Keep the returned token until shortly before ``expires_in`` runs out.

The token URL comes from the config or from ``{fhir base}/.well-known/
smart-configuration``. Only read scopes are accepted (``.read`` in SMART v1,
any of ``r``/``s`` in v2), because a record check never writes.

The private key is given by reference: a PEM file, an environment variable
holding PEM text, or a JWKS file holding private JWKs. It is loaded once, when
the verifier is built, and stays in memory. Neither the key, the assertion,
nor a token is ever logged, put in an exception message, or shown by
``repr``. A failed token request makes the read unreadable, so the check is
INDETERMINATE and the run halts. It is never read as "record absent".
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
SMART_CONFIGURATION_PATH = "/.well-known/smart-configuration"

#: Seconds the assertion is valid for. The spec allows at most 300; a little
#: less leaves room for clock drift between this host and the server.
ASSERTION_LIFETIME_S = 240

#: A token is replaced this many seconds before it expires (capped at a fifth
#: of its lifetime for very short tokens).
EXPIRY_MARGIN_S = 30

#: SMART read scopes: ``system/Observation.read`` (v1) or
#: ``system/Observation.rs`` (v2), optionally with a v2 query suffix.
_READ_SCOPE = re.compile(
    r"^(system|user|patient)/(\*|[A-Z][A-Za-z]*)\.(read|rs|r|s)(\?\S+)?$"
)

SmartAlg = Literal["RS384", "ES384"]


class SmartTokenError(RuntimeError):
    """A token could not be obtained. The message never holds a secret."""


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _require_tls(url: str, what: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and _is_loopback(parsed.hostname):
        return
    raise ValueError(f"{what} must be https (plain http only on loopback)")


def _require_verified_tls(url: Optional[str], verify_tls: bool, what: str) -> None:
    """Refuse ``verify_tls: false`` for anything but a loopback host.

    Without certificate checks, anyone on the path can take a signed client
    assertion and redeem it for a token that reads patient records.
    """

    if verify_tls or url is None:
        return
    if not _is_loopback(urlparse(url).hostname):
        raise ValueError(
            f"verify_tls can be false only for a loopback {what}; refused for "
            f"{urlparse(url).hostname or 'an unknown host'}"
        )


class SmartBackendConfig(BaseModel):
    """``effects.smart_backend`` in ``deployment.yaml`` or ``verify.yaml``.

    Exactly one key source: ``private_key_file``, ``private_key_env`` or
    ``jwks_file``. ``kid`` is required for a PEM key; for a JWKS it selects the
    key and defaults to the only private key in the set.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str
    #: Space-separated SMART scopes; read scopes only.
    scope: str
    #: The token endpoint. Omitted: read from the FHIR server's
    #: ``.well-known/smart-configuration``.
    token_url: Optional[str] = None
    kid: Optional[str] = None
    #: Omitted: RS384 for an RSA key, ES384 for a P-384 key.
    alg: Optional[SmartAlg] = None
    private_key_file: Optional[str] = None
    private_key_env: Optional[str] = None
    #: Env var holding the password of an encrypted PEM key.
    private_key_password_env: Optional[str] = None
    jwks_file: Optional[str] = None
    #: Optional ``jku`` header: the URL of the client's public JWKS, as
    #: registered with the server.
    jku: Optional[str] = None
    timeout_s: float = 10.0
    verify_tls: bool = True

    @model_validator(mode="after")
    def _check(self) -> "SmartBackendConfig":
        sources = [self.private_key_file, self.private_key_env, self.jwks_file]
        if sum(s is not None for s in sources) != 1:
            raise ValueError(
                "smart_backend needs exactly one of private_key_file, "
                "private_key_env or jwks_file"
            )
        if self.jwks_file is None and not self.kid:
            raise ValueError("smart_backend needs kid for a PEM private key")
        if not self.client_id.strip():
            raise ValueError("smart_backend.client_id is empty")
        scopes = self.scope.split()
        if not scopes:
            raise ValueError("smart_backend.scope is empty")
        bad = [s for s in scopes if not _READ_SCOPE.match(s)]
        if bad:
            raise ValueError(
                "smart_backend.scope must hold read scopes only "
                "(e.g. system/Observation.rs); refused: " + " ".join(bad)
            )
        if self.token_url is not None:
            _require_tls(self.token_url, "smart_backend.token_url")
        _require_verified_tls(self.token_url, self.verify_tls, "token_url")
        if self.jku is not None and urlparse(self.jku).scheme != "https":
            raise ValueError("smart_backend.jku must be https")
        return self


# -- keys and signing ----------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_int(value: str) -> int:
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


def _private_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    kty = jwk.get("kty")
    if kty == "RSA":
        n, e = _b64url_int(jwk["n"]), _b64url_int(jwk["e"])
        public = rsa.RSAPublicNumbers(e, n)
        return rsa.RSAPrivateNumbers(
            p=_b64url_int(jwk["p"]),
            q=_b64url_int(jwk["q"]),
            d=_b64url_int(jwk["d"]),
            dmp1=_b64url_int(jwk["dp"]),
            dmq1=_b64url_int(jwk["dq"]),
            iqmp=_b64url_int(jwk["qi"]),
            public_numbers=public,
        ).private_key()
    if kty == "EC":
        if jwk.get("crv") != "P-384":
            raise ValueError("an EC key must use curve P-384 for ES384")
        public_ec = ec.EllipticCurvePublicNumbers(
            _b64url_int(jwk["x"]), _b64url_int(jwk["y"]), ec.SECP384R1()
        )
        return ec.EllipticCurvePrivateNumbers(
            _b64url_int(jwk["d"]), public_ec
        ).private_key()
    raise ValueError("a JWK must have kty RSA or EC")


def _load_jwks_key(path: Path, kid: Optional[str]) -> tuple[Any, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read smart_backend.jwks_file {path}") from exc
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        raise ValueError("smart_backend.jwks_file has no keys list")
    private = [k for k in keys if isinstance(k, dict) and "d" in k]
    if kid is not None:
        private = [k for k in private if k.get("kid") == kid]
    if len(private) != 1:
        raise ValueError(
            "smart_backend.jwks_file must hold exactly one private key"
            + (f" with kid {kid!r}" if kid else "; set kid to choose one")
        )
    jwk = private[0]
    chosen = jwk.get("kid")
    if not isinstance(chosen, str) or not chosen:
        raise ValueError("the private JWK has no kid")
    try:
        return _private_key_from_jwk(jwk), chosen
    except (KeyError, ValueError, TypeError) as exc:
        # The JWK's own fields may be named; their values never are.
        raise ValueError(f"cannot load the private JWK {chosen!r}") from exc


def _load_pem_key(pem: bytes, password: Optional[bytes]) -> Any:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    try:
        return load_pem_private_key(pem, password=password)
    except (ValueError, TypeError) as exc:
        raise ValueError("cannot load the PEM private key") from exc


def _alg_for(key: Any, requested: Optional[str]) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise ValueError("an RSA key must be at least 2048 bits")
        alg = "RS384"
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        if not isinstance(key.curve, ec.SECP384R1):
            raise ValueError("an EC key must use curve P-384 for ES384")
        alg = "ES384"
    else:
        raise ValueError("the private key must be RSA or EC P-384")
    if requested is not None and requested != alg:
        raise ValueError(f"smart_backend.alg {requested} does not fit the key")
    return alg


def _sign(key: Any, alg: str, data: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
    )

    if alg == "RS384":
        return key.sign(data, padding.PKCS1v15(), hashes.SHA384())
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA384())))
    return r.to_bytes(48, "big") + s.to_bytes(48, "big")


# -- token source --------------------------------------------------------------


class SmartBackendTokenSource:
    """Gets, caches and renews a SMART Backend Services access token.

    ``access_token()`` returns a valid token or None. ``invalidate()`` drops the
    cached token (the FHIR verifier calls it on a 401). Thread-safe.
    """

    def __init__(
        self,
        *,
        client_id: str,
        scope: str,
        key: Any,
        kid: str,
        alg: str,
        token_url: Optional[str] = None,
        fhir_base_url: Optional[str] = None,
        jku: Optional[str] = None,
        timeout_s: float = 10.0,
        verify_tls: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if token_url is None and fhir_base_url is None:
            raise ValueError("smart_backend needs token_url or a FHIR base URL")
        _require_verified_tls(token_url, verify_tls, "token_url")
        _require_verified_tls(fhir_base_url, verify_tls, "FHIR base URL")
        self.client_id = client_id
        self.scope = scope
        self.kid = kid
        self.alg = alg
        self.jku = jku
        self.timeout_s = timeout_s
        self.verify_tls = verify_tls
        self._key = key
        self._token_url = token_url
        self._fhir_base_url = fhir_base_url.rstrip("/") if fhir_base_url else None
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._renew_at = 0.0
        #: Why the last request failed, safe to show (no secret in it).
        self.last_error: Optional[str] = None

    @classmethod
    def from_config(
        cls,
        cfg: SmartBackendConfig,
        *,
        fhir_base_url: Optional[str],
        env: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> "SmartBackendTokenSource":
        """Load the key now (fail loud); request no token yet."""

        source = os.environ if env is None else env
        password: Optional[bytes] = None
        if cfg.private_key_password_env:
            value = source.get(cfg.private_key_password_env, "")
            if not value:
                raise ValueError(
                    "smart_backend.private_key_password_env names "
                    f"{cfg.private_key_password_env!r}, which is not set"
                )
            password = value.encode("utf-8")
        if cfg.jwks_file is not None:
            key, kid = _load_jwks_key(Path(cfg.jwks_file), cfg.kid)
        elif cfg.private_key_file is not None:
            path = Path(cfg.private_key_file)
            try:
                pem = path.read_bytes()
            except OSError as exc:
                raise ValueError(
                    f"cannot read smart_backend.private_key_file {path}"
                ) from exc
            key, kid = _load_pem_key(pem, password), str(cfg.kid)
        else:
            name = str(cfg.private_key_env)
            value = source.get(name, "")
            if not value:
                raise ValueError(
                    f"smart_backend.private_key_env names {name!r}, which is not set"
                )
            key, kid = _load_pem_key(value.encode("utf-8"), password), str(cfg.kid)
        if fhir_base_url is not None:
            # The token is sent to this URL on every read.
            _require_tls(fhir_base_url, "the FHIR base URL")
        return cls(
            client_id=cfg.client_id,
            scope=cfg.scope,
            key=key,
            kid=kid,
            alg=_alg_for(key, cfg.alg),
            token_url=cfg.token_url,
            fhir_base_url=fhir_base_url,
            jku=cfg.jku,
            timeout_s=cfg.timeout_s,
            verify_tls=cfg.verify_tls,
            clock=clock,
        )

    def __repr__(self) -> str:
        return (
            f"SmartBackendTokenSource(client_id={self.client_id!r}, "
            f"kid={self.kid!r}, alg={self.alg!r}, scope={self.scope!r})"
        )

    __str__ = __repr__

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("SmartBackendTokenSource cannot be pickled")

    # -- public -------------------------------------------------------------

    def access_token(self) -> Optional[str]:
        """A token valid for at least the expiry margin, or None."""

        with self._lock:
            if self._token is not None and self._clock() < self._renew_at:
                return self._token
            self._token = None
            try:
                token, expires_in = self._request()
            except SmartTokenError as exc:
                self.last_error = str(exc)
                logger.warning("SMART token request failed: %s", exc)
                return None
            except Exception as exc:  # noqa: BLE001 - its text could hold data
                self.last_error = f"token request failed ({type(exc).__name__})"
                logger.warning("SMART %s", self.last_error)
                return None
            self.last_error = None
            margin = min(EXPIRY_MARGIN_S, expires_in / 5)
            self._token = token
            self._renew_at = self._clock() + expires_in - margin
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._renew_at = 0.0

    def client_assertion(self, token_url: str) -> str:
        """A fresh signed assertion for ``token_url``."""

        now = int(self._clock())
        header: dict[str, Any] = {"alg": self.alg, "kid": self.kid, "typ": "JWT"}
        if self.jku:
            header["jku"] = self.jku
        claims = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": token_url,
            "iat": now,
            "exp": now + ASSERTION_LIFETIME_S,
            "jti": secrets.token_urlsafe(32),
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        )
        signature = _sign(self._key, self.alg, signing_input.encode("ascii"))
        return signing_input + "." + _b64url(signature)

    # -- internals ----------------------------------------------------------

    def _client(self) -> Any:
        import httpx

        return httpx.Client(verify=self.verify_tls, timeout=self.timeout_s)

    def _discover(self) -> str:
        url = f"{self._fhir_base_url}{SMART_CONFIGURATION_PATH}"
        try:
            with self._client() as client:
                resp = client.get(url, headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            raise SmartTokenError(
                f"smart-configuration request failed ({type(exc).__name__})"
            ) from None
        if resp.status_code // 100 != 2:
            raise SmartTokenError(
                f"smart-configuration returned HTTP {resp.status_code}"
            )
        try:
            doc = resp.json()
        except Exception:  # noqa: BLE001
            raise SmartTokenError("smart-configuration is not JSON") from None
        token_url = doc.get("token_endpoint") if isinstance(doc, dict) else None
        if not isinstance(token_url, str) or not token_url:
            raise SmartTokenError("smart-configuration has no token_endpoint")
        try:
            _require_tls(token_url, "the discovered token_endpoint")
            _require_verified_tls(
                token_url, self.verify_tls, "discovered token_endpoint"
            )
        except ValueError as exc:
            raise SmartTokenError(str(exc)) from None
        methods = doc.get("token_endpoint_auth_methods_supported")
        if isinstance(methods, list) and "private_key_jwt" not in methods:
            raise SmartTokenError("the server does not list private_key_jwt")
        algs = doc.get("token_endpoint_auth_signing_alg_values_supported")
        if isinstance(algs, list) and self.alg not in algs:
            raise SmartTokenError(f"the server does not list {self.alg}")
        return token_url

    def _request(self) -> tuple[str, float]:
        if self._token_url is None:
            self._token_url = self._discover()
        token_url = self._token_url
        form = {
            "grant_type": "client_credentials",
            "scope": self.scope,
            "client_assertion_type": CLIENT_ASSERTION_TYPE,
            "client_assertion": self.client_assertion(token_url),
        }
        try:
            with self._client() as client:
                resp = client.post(
                    token_url, data=form, headers={"Accept": "application/json"}
                )
        except Exception as exc:  # noqa: BLE001
            # The exception text can echo the request; report its type only.
            raise SmartTokenError(
                f"token request failed ({type(exc).__name__})"
            ) from None
        body: Any
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None
        if resp.status_code // 100 != 2:
            raise SmartTokenError(
                f"token endpoint returned HTTP {resp.status_code}" + _oauth_error(body)
            )
        if not isinstance(body, dict):
            raise SmartTokenError("token response is not a JSON object")
        token = body.get("access_token")
        token_type = body.get("token_type")
        expires_in = body.get("expires_in")
        if not isinstance(token, str) or not token:
            raise SmartTokenError("token response has no access_token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise SmartTokenError("token response token_type is not bearer")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise SmartTokenError("token response has no positive expires_in")
        return token, float(expires_in)


def _oauth_error(body: Any) -> str:
    """The OAuth ``error`` code, if it is a plain code. Never the description."""

    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
        return f" ({code})"
    return ""
