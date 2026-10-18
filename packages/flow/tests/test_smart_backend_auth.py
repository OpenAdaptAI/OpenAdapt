"""SMART Backend Services tokens for the fhir record check.

A local authorization server checks each client assertion the way the spec
says a server must (signature, kid, alg, iss/sub/aud, exp at most five minutes
ahead, one-time jti) and issues short-lived tokens; a local FHIR endpoint
accepts only those tokens. Keys are generated per test run; none is stored in
the repository.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    FhirEffectVerifier,
    SmartBackendConfig,
    SmartBackendTokenSource,
    ValueExpr,
)
from openadapt_flow.runtime.effects.smart_auth import CLIENT_ASSERTION_TYPE

CLIENT_ID = "test-backend-client"
SCOPE = "system/Observation.rs"


# -- keys (generated, never committed) -----------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64int(value: int, length: Optional[int] = None) -> str:
    size = length or (value.bit_length() + 7) // 8
    return _b64(value.to_bytes(size, "big"))


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ec_key():
    return ec.generate_private_key(ec.SECP384R1())


def _pem(key: Any) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _ec_jwk(key: Any, kid: str) -> dict:
    priv = key.private_numbers()
    pub = priv.public_numbers
    return {
        "kty": "EC",
        "crv": "P-384",
        "kid": kid,
        "x": _b64int(pub.x, 48),
        "y": _b64int(pub.y, 48),
        "d": _b64int(priv.private_value, 48),
    }


# -- local authorization server + FHIR endpoint --------------------------------


class Server:
    def __init__(self, keys: dict[str, Any], *, expires_in: int = 300) -> None:
        self.keys = keys  # kid -> public key
        self.expires_in = expires_in
        self.issued: set[str] = set()
        self.revoked: set[str] = set()
        self.jtis: set[str] = set()
        self.token_requests = 0
        self.forms: list[dict[str, str]] = []
        self.reject_with: Optional[str] = None
        self.auth_methods = ["private_key_jwt"]
        self._n = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self.token_url = f"{self.base}/auth/token"
        self.fhir = f"{self.base}/fhir"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def check_assertion(self, jwt: str) -> Optional[str]:
        try:
            h64, c64, s64 = jwt.split(".")
            header = json.loads(_unb64(h64))
            claims = json.loads(_unb64(c64))
            sig = _unb64(s64)
        except Exception:
            return "malformed"
        if header.get("typ") != "JWT":
            return "typ"
        key = self.keys.get(header.get("kid"))
        if key is None:
            return "kid"
        data = f"{h64}.{c64}".encode()
        try:
            if header.get("alg") == "RS384" and isinstance(key, rsa.RSAPublicKey):
                key.verify(sig, data, padding.PKCS1v15(), hashes.SHA384())
            elif header.get("alg") == "ES384" and isinstance(
                key, ec.EllipticCurvePublicKey
            ):
                if len(sig) != 96:
                    return "sig-length"
                der = encode_dss_signature(
                    int.from_bytes(sig[:48], "big"), int.from_bytes(sig[48:], "big")
                )
                key.verify(der, data, ec.ECDSA(hashes.SHA384()))
            else:
                return "alg"
        except Exception:
            return "signature"
        now = time.time()
        if claims.get("iss") != CLIENT_ID or claims.get("sub") != CLIENT_ID:
            return "iss"
        if claims.get("aud") != self.token_url:
            return "aud"
        if not (now < claims.get("exp", 0) <= now + 300):
            return "exp"
        jti = claims.get("jti")
        if not jti or jti in self.jtis:
            return "jti"
        self.jtis.add(jti)
        return None

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/fhir/.well-known/smart-configuration":
                    self._send(
                        200,
                        {
                            "token_endpoint": server.token_url,
                            "token_endpoint_auth_methods_supported": (
                                server.auth_methods
                            ),
                            "token_endpoint_auth_signing_alg_values_supported": [
                                "RS384",
                                "ES384",
                            ],
                        },
                    )
                    return
                if path == "/fhir/Observation":
                    auth = self.headers.get("Authorization", "")
                    token = auth.removeprefix("Bearer ")
                    if token not in server.issued or token in server.revoked:
                        self._send(401, {"resourceType": "OperationOutcome"})
                        return
                    resource = {
                        "resourceType": "Observation",
                        "id": "obs-1",
                        "status": "final",
                        "subject": {"reference": "Patient/9"},
                        "valueString": "BP recheck",
                        "code": {"text": "Clinical note"},
                    }
                    self._send(
                        200,
                        {
                            "resourceType": "Bundle",
                            "type": "searchset",
                            "entry": [{"resource": resource}],
                        },
                    )
                    return
                self._send(404, {})

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/auth/token":
                    self._send(404, {})
                    return
                length = int(self.headers.get("Content-Length", 0))
                form = {
                    k: v[0]
                    for k, v in parse_qs(self.rfile.read(length).decode()).items()
                }
                server.token_requests += 1
                server.forms.append(form)
                if server.reject_with:
                    self._send(
                        401,
                        {
                            "error": server.reject_with,
                            "error_description": "secret-looking detail xyz",
                        },
                    )
                    return
                if (
                    form.get("grant_type") != "client_credentials"
                    or form.get("client_assertion_type") != CLIENT_ASSERTION_TYPE
                ):
                    self._send(400, {"error": "invalid_request"})
                    return
                problem = server.check_assertion(form.get("client_assertion", ""))
                if problem:
                    self._send(401, {"error": "invalid_client", "why": problem})
                    return
                server._n += 1
                token = f"issued-token-{server._n}-{'q' * 24}"
                server.issued.add(token)
                self._send(
                    200,
                    {
                        "access_token": token,
                        "token_type": "bearer",
                        "expires_in": server.expires_in,
                        "scope": form.get("scope"),
                    },
                )

        return Handler


@pytest.fixture
def rsa_server(rsa_key):
    server = Server({"rsa-kid-1": rsa_key.public_key()})
    yield server
    server.close()


@pytest.fixture
def ec_server(ec_key):
    server = Server({"ec-kid-1": ec_key.public_key()})
    yield server
    server.close()


def _pem_file(tmp_path: Path, key: Any) -> Path:
    path = tmp_path / "client.pem"
    path.write_bytes(_pem(key))
    path.chmod(0o600)
    return path


def _fhir_cfg(server: Server, smart: dict) -> EffectsConfig:
    return EffectsConfig.model_validate(
        {
            "kind": "fhir",
            "base_url": server.fhir,
            "resource_type": "Observation",
            "timeout_s": 2.0,
            "smart_backend": smart,
        }
    )


# -- end to end ------------------------------------------------------------------


def test_rs384_pem_key_gets_a_token_and_reuses_it(rsa_server, rsa_key, tmp_path):
    verifier = build_effect_verifier(
        _fhir_cfg(
            rsa_server,
            {
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "token_url": rsa_server.token_url,
                "kid": "rsa-kid-1",
                "private_key_file": str(_pem_file(tmp_path, rsa_key)),
            },
        )
    )
    assert isinstance(verifier, FhirEffectVerifier)
    assert rsa_server.token_requests == 0  # nothing is requested at build time
    first = verifier.capture_pre_state()
    second = verifier.capture_pre_state()
    assert first.reachable and second.reachable
    assert first.records[0]["note"] == "BP recheck"
    assert rsa_server.token_requests == 1  # cached until expiry
    [form] = rsa_server.forms
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion_type"] == CLIENT_ASSERTION_TYPE
    assert form["scope"] == SCOPE


def test_es384_jwks_key_with_discovery(ec_server, ec_key, tmp_path):
    jwks = tmp_path / "client.jwks.json"
    jwks.write_text(json.dumps({"keys": [_ec_jwk(ec_key, "ec-kid-1")]}))
    verifier = build_effect_verifier(
        _fhir_cfg(
            ec_server,
            {"client_id": CLIENT_ID, "scope": SCOPE, "jwks_file": str(jwks)},
        )
    )
    state = verifier.capture_pre_state()
    assert state.reachable
    assert verifier.token_source.alg == "ES384"
    assert verifier.token_source.kid == "ec-kid-1"


def test_token_is_renewed_shortly_before_expiry(rsa_key):
    server = Server({"rsa-kid-1": rsa_key.public_key()}, expires_in=60)
    try:
        now = [time.time()]
        source = SmartBackendTokenSource(
            client_id=CLIENT_ID,
            scope=SCOPE,
            key=rsa_key,
            kid="rsa-kid-1",
            alg="RS384",
            token_url=server.token_url,
            clock=lambda: now[0],
        )
        first = source.access_token()
        now[0] += 40
        assert source.access_token() == first
        now[0] += 9  # 49 s in: inside the 12 s margin of a 60 s token
        second = source.access_token()
        assert second and second != first
        assert server.token_requests == 2
    finally:
        server.close()


def test_a_401_drops_the_cached_token(rsa_server, rsa_key, tmp_path):
    verifier = build_effect_verifier(
        _fhir_cfg(
            rsa_server,
            {
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "token_url": rsa_server.token_url,
                "kid": "rsa-kid-1",
                "private_key_file": str(_pem_file(tmp_path, rsa_key)),
            },
        )
    )
    assert verifier.capture_pre_state().reachable
    rsa_server.revoked |= rsa_server.issued  # the server revokes it early
    assert not verifier.capture_pre_state().reachable
    assert verifier.capture_pre_state().reachable
    assert rsa_server.token_requests == 2


def test_token_failure_is_indeterminate_never_absent(rsa_server, rsa_key, tmp_path):
    rsa_server.reject_with = "invalid_client"
    verifier = build_effect_verifier(
        _fhir_cfg(
            rsa_server,
            {
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "token_url": rsa_server.token_url,
                "kid": "rsa-kid-1",
                "private_key_file": str(_pem_file(tmp_path, rsa_key)),
            },
        )
    )
    before = verifier.capture_pre_state()
    assert not before.reachable
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"note": ValueExpr(literal="BP recheck")},
        timeout_s=0.0,
    )
    verdict = verifier.verify(effect, before)
    assert not verdict.confirmed
    assert verdict.unavailable
    error = verifier.token_source.last_error
    assert error == "token endpoint returned HTTP 401 (invalid_client)"


def test_discovery_refuses_a_server_without_private_key_jwt(
    ec_server, ec_key, tmp_path
):
    ec_server.auth_methods = ["client_secret_basic"]
    jwks = tmp_path / "client.jwks.json"
    jwks.write_text(json.dumps({"keys": [_ec_jwk(ec_key, "ec-kid-1")]}))
    verifier = build_effect_verifier(
        _fhir_cfg(
            ec_server, {"client_id": CLIENT_ID, "scope": SCOPE, "jwks_file": str(jwks)}
        )
    )
    assert not verifier.capture_pre_state().reachable
    assert "private_key_jwt" in verifier.token_source.last_error
    assert ec_server.token_requests == 0


def test_no_key_assertion_or_token_reaches_logs_repr_or_errors(
    rsa_server, rsa_key, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    pem = _pem(rsa_key).decode()
    verifier = build_effect_verifier(
        _fhir_cfg(
            rsa_server,
            {
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "token_url": rsa_server.token_url,
                "kid": "rsa-kid-1",
                "private_key_file": str(_pem_file(tmp_path, rsa_key)),
            },
        )
    )
    verifier.capture_pre_state()
    rsa_server.revoked |= rsa_server.issued
    verifier.capture_pre_state()
    rsa_server.reject_with = "invalid_client"
    verifier.capture_pre_state()
    secrets_seen = list(rsa_server.issued) + [
        f["client_assertion"] for f in rsa_server.forms if "client_assertion" in f
    ]
    key_body = pem.splitlines()[1]
    surfaces = [
        caplog.text,
        repr(verifier.token_source),
        str(verifier.token_source),
        str(verifier.token_source.last_error),
        json.dumps(verifier.capture_pre_state().model_dump(mode="json")),
    ]
    for surface in surfaces:
        assert key_body not in surface
        assert "secret-looking detail" not in surface
        for secret in secrets_seen:
            assert secret not in surface


def test_assertion_follows_the_spec(rsa_key):
    source = SmartBackendTokenSource(
        client_id=CLIENT_ID,
        scope=SCOPE,
        key=rsa_key,
        kid="rsa-kid-1",
        alg="RS384",
        token_url="https://auth.example.test/token",
        jku="https://client.example.test/jwks.json",
    )
    one = source.client_assertion("https://auth.example.test/token")
    two = source.client_assertion("https://auth.example.test/token")
    header = json.loads(_unb64(one.split(".")[0]))
    claims = json.loads(_unb64(one.split(".")[1]))
    assert header == {
        "alg": "RS384",
        "kid": "rsa-kid-1",
        "typ": "JWT",
        "jku": "https://client.example.test/jwks.json",
    }
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["aud"] == "https://auth.example.test/token"
    assert 0 < claims["exp"] - time.time() <= 300
    assert claims["jti"] != json.loads(_unb64(two.split(".")[1]))["jti"]


# -- configuration refusals ------------------------------------------------------


def _smart(**overrides: Any) -> dict:
    base = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "token_url": "https://auth.example.test/token",
        "kid": "k1",
        "private_key_env": "SMART_TEST_KEY",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize(
    "scope",
    [
        "system/Observation.write",
        "system/*.cruds",
        "system/Patient.c",
        "system/Observation.rs launch",
        "",
    ],
)
def test_write_or_unknown_scopes_are_refused(scope):
    with pytest.raises(ValueError, match="scope"):
        SmartBackendConfig.model_validate(_smart(scope=scope))


def test_v1_and_v2_read_scopes_are_accepted():
    cfg = SmartBackendConfig.model_validate(
        _smart(scope="system/Observation.read system/Patient.rs system/*.s")
    )
    assert cfg.scope.split()[1] == "system/Patient.rs"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"jwks_file": "k.json"}, "exactly one"),
        ({"kid": None}, "kid"),
        ({"token_url": "http://auth.example.test/token"}, "https"),
        ({"jku": "http://client.example.test/jwks.json"}, "jku"),
    ],
)
def test_bad_smart_config_is_refused(overrides, message):
    with pytest.raises(ValueError, match=message):
        SmartBackendConfig.model_validate(_smart(**overrides))


def test_smart_backend_needs_fhir_and_no_static_token():
    with pytest.raises(ValueError, match="only to kind fhir"):
        EffectsConfig.model_validate(
            {"kind": "rest", "base_url": "https://x.test", "smart_backend": _smart()}
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        EffectsConfig.model_validate(
            {
                "kind": "fhir",
                "base_url": "https://x.test/fhir",
                "access_token_env": "TOKEN",
                "smart_backend": _smart(),
            }
        )


def test_key_problems_fail_at_build_time(rsa_key, ec_key, monkeypatch, tmp_path):
    monkeypatch.delenv("SMART_TEST_KEY", raising=False)
    with pytest.raises(ValueError, match="SMART_TEST_KEY"):
        SmartBackendTokenSource.from_config(
            SmartBackendConfig.model_validate(_smart()), fhir_base_url=None
        )
    monkeypatch.setenv("SMART_TEST_KEY", _pem(rsa_key).decode())
    with pytest.raises(ValueError, match="does not fit"):
        SmartBackendTokenSource.from_config(
            SmartBackendConfig.model_validate(_smart(alg="ES384")), fhir_base_url=None
        )
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    monkeypatch.setenv("SMART_TEST_KEY", _pem(small).decode())
    with pytest.raises(ValueError, match="2048"):
        SmartBackendTokenSource.from_config(
            SmartBackendConfig.model_validate(_smart()), fhir_base_url=None
        )
    p256 = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("SMART_TEST_KEY", _pem(p256).decode())
    with pytest.raises(ValueError, match="P-384"):
        SmartBackendTokenSource.from_config(
            SmartBackendConfig.model_validate(_smart()), fhir_base_url=None
        )
    monkeypatch.setenv("SMART_TEST_KEY", "not a key")
    with pytest.raises(ValueError, match="cannot load the PEM") as info:
        SmartBackendTokenSource.from_config(
            SmartBackendConfig.model_validate(_smart()), fhir_base_url=None
        )
    assert "not a key" not in str(info.value)
    jwks = tmp_path / "two.json"
    jwks.write_text(json.dumps({"keys": [_ec_jwk(ec_key, "a"), _ec_jwk(ec_key, "b")]}))
    with pytest.raises(ValueError, match="exactly one private key"):
        SmartBackendTokenSource.from_config(
            SmartBackendConfig.model_validate(
                _smart(private_key_env=None, kid=None, jwks_file=str(jwks))
            ),
            fhir_base_url=None,
        )
    chosen = SmartBackendConfig.model_validate(
        _smart(private_key_env=None, kid="b", jwks_file=str(jwks))
    )
    source = SmartBackendTokenSource.from_config(chosen, fhir_base_url=None)
    assert source.kid == "b" and source.alg == "ES384"
    with pytest.raises(ValueError, match="FHIR base URL must be https"):
        SmartBackendTokenSource.from_config(
            chosen, fhir_base_url="http://fhir.example.test/r4"
        )


def test_static_token_and_token_source_are_exclusive(rsa_key):
    source = SmartBackendTokenSource(
        client_id=CLIENT_ID,
        scope=SCOPE,
        key=rsa_key,
        kid="k",
        alg="RS384",
        token_url="https://auth.example.test/token",
    )
    with pytest.raises(ValueError, match="not both"):
        FhirEffectVerifier("https://x.test/fhir", access_token="t", token_source=source)
