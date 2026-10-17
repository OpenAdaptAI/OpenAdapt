"""RFC 3161 trusted timestamps over a SHA-256 digest.

A timestamp token is a CMS ``SignedData`` whose content is a ``TSTInfo``: the
TSA's statement that it saw a given hash at ``genTime``. Only the 32-byte
digest is sent to a TSA, never the receipt or anything it describes.

Two authorities implement :class:`TimestampAuthority`:

* :class:`HttpTsa` posts an RFC 3161 request to any TSA URL.
* :class:`LocalTsa` signs tokens with a local key and self-signed certificate.
  It exists for tests and air-gapped trials. Its clock is the operator's
  clock, so its tokens prove nothing to a third party about time.

:func:`verify_timestamp_token` checks a token offline against TSA
certificates the verifier pinned. A certificate that travels inside the token
or the receipt document is used to build the path, never as a trust anchor.

ASN.1 parsing and encoding use ``asn1crypto`` (pure Python, no transitive
dependencies); signatures and X.509 use ``cryptography``, which Flow already
requires.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

__all__ = [
    "HttpTsa",
    "LocalTsa",
    "TimestampAuthority",
    "TimestampError",
    "TimestampVerification",
    "build_timestamp_request",
    "load_pem_certificates",
    "token_from_response",
    "verify_timestamp_token",
]

#: Media types from RFC 3161 section 3.4.
QUERY_MEDIA_TYPE = "application/timestamp-query"
REPLY_MEDIA_TYPE = "application/timestamp-reply"
MAX_RESPONSE_BYTES = 65536

#: UUID-derived OID (ITU-T X.667 arc 2.25) naming the local TSA's policy.
#: Not a registered TSA policy; a verifier should treat it as "test".
LOCAL_TSA_POLICY = "2.25." + str(uuid.UUID("6f8a3b0e-5d1c-4c1e-9a52-3161a0a1d7e3").int)

_HASHES = {"sha256": 32, "sha384": 48, "sha512": 64}

#: (url, request body) -> (HTTP status, response body). Injected by tests.
Transport = Callable[[str, bytes], "tuple[int, bytes]"]


#: Default RFC 3161 service: Sigstore's public-good timestamp authority. It
#: is free, its tokens state 1-second accuracy, and it receives only the
#: 32-byte digest and a nonce.
SIGSTORE_TSA_URL = "https://timestamp.sigstore.dev/api/v1/timestamp"
#: Sigstore's TSA root, shipped with Flow and pinned by default when the
#: verifier passes no ``--tsa-cert``. Fetched from
#: ``https://timestamp.sigstore.dev/api/v1/timestamp/certchain`` on
#: 2026-10-17. SHA-256 fingerprint 2A:CA:8F:EA:5D:3C:E4:8B:01:CC:77:07:62:93:
#: C2:80:E6:C2:3F:FE:44:03:47:57:EE:78:33:CA:9F:45:D6:33. Valid until 2035-04-06.
SIGSTORE_TSA_ROOT_RESOURCE = "trust/sigstore-tsa-root.pem"


def sigstore_tsa_root_pem() -> bytes:
    """The bundled Sigstore TSA root certificate (PEM)."""

    from importlib import resources

    return (
        resources.files("openadapt_flow.countersign")
        .joinpath(SIGSTORE_TSA_ROOT_RESOURCE)
        .read_bytes()
    )


class TimestampError(RuntimeError):
    """A closed reason a timestamp could not be obtained."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class TimestampAuthority(Protocol):
    """Anything that turns a SHA-256 digest into a DER TimeStampToken."""

    @property
    def name(self) -> str: ...

    def timestamp(self, digest: bytes) -> bytes: ...


def _asn1() -> Any:
    try:
        from asn1crypto import algos, cms, core, tsp, x509  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise TimestampError(
            "verifier_unavailable",
            "install 'openadapt-flow[countersign]' (asn1crypto) for RFC 3161 timestamps",
        ) from exc
    import asn1crypto

    return asn1crypto


def _check_digest(digest: bytes) -> None:
    if not isinstance(digest, (bytes, bytearray)) or len(digest) != 32:
        raise TimestampError("malformed", "the digest must be 32 bytes of SHA-256")


def build_timestamp_request(digest: bytes, nonce: int) -> bytes:
    """DER ``TimeStampReq`` for a SHA-256 digest, asking for the TSA certificate."""
    _check_digest(digest)
    tsp = _asn1().tsp
    request = tsp.TimeStampReq(
        {
            "version": "v1",
            "message_imprint": {
                "hash_algorithm": {"algorithm": "sha256"},
                "hashed_message": bytes(digest),
            },
            "nonce": nonce,
            "cert_req": True,
        }
    )
    return bytes(request.dump())


def _tst_info(token: Any) -> Any:
    if token["content_type"].native != "signed_data":
        raise TimestampError("malformed", "the token is not CMS SignedData")
    encap = token["content"]["encap_content_info"]
    if encap["content_type"].native != "tst_info":
        raise TimestampError("malformed", "the token does not carry TSTInfo")
    return encap["content"].parsed


def token_from_response(body: bytes, *, digest: bytes, nonce: Optional[int]) -> bytes:
    """Extract the token from a ``TimeStampResp``, checking imprint and nonce.

    This checks that the TSA answered the question asked. It does not check
    the TSA's signature; :func:`verify_timestamp_token` does that.
    """
    a = _asn1()
    tsp, core = a.tsp, a.core
    try:
        # Read the status on its own: a refusal carries no token, and
        # asn1crypto's TimeStampResp treats the token as required.
        outer = core.Sequence.load(body)
        status = tsp.PKIStatusInfo.load(outer[0].dump())["status"].native
    except Exception as exc:  # noqa: BLE001 - any parse failure is malformed
        raise TimestampError("malformed", "unparseable TimeStampResp") from exc
    if status not in ("granted", "granted_with_mods"):
        raise TimestampError("tsa_refused", str(status))
    try:
        token = tsp.TimeStampResp.load(body)["time_stamp_token"]
    except Exception as exc:  # noqa: BLE001
        raise TimestampError("malformed", "a granted response without a token") from exc
    try:
        tst = _tst_info(token)
        imprint = tst["message_imprint"]
        algorithm = imprint["hash_algorithm"]["algorithm"].native
        hashed = imprint["hashed_message"].native
        got_nonce = tst["nonce"].native
    except TimestampError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TimestampError("malformed", "unparseable TSTInfo") from exc
    if algorithm != "sha256" or hashed != bytes(digest):
        raise TimestampError("imprint_mismatch", "the TSA stamped a different hash")
    if nonce is not None and got_nonce != nonce:
        raise TimestampError("nonce_mismatch", "the TSA answered another request")
    return bytes(token.dump())


def _default_transport(url: str, body: bytes) -> "tuple[int, bytes]":
    import httpx

    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        response = client.post(
            url,
            content=body,
            headers={"Content-Type": QUERY_MEDIA_TYPE, "Accept": REPLY_MEDIA_TYPE},
        )
        return response.status_code, response.content[: MAX_RESPONSE_BYTES + 1]


class HttpTsa:
    """RFC 3161 client for any TSA URL. Sends only the digest and a nonce."""

    def __init__(self, url: str, *, transport: Optional[Transport] = None) -> None:
        if not url.startswith(("https://", "http://")):
            raise TimestampError(
                "malformed", "a TSA URL starts with http:// or https://"
            )
        self.url = url
        self._transport = transport or _default_transport

    @property
    def name(self) -> str:
        return self.url

    def timestamp(self, digest: bytes) -> bytes:
        nonce = int.from_bytes(secrets.token_bytes(8), "big") | 1
        request = build_timestamp_request(digest, nonce)
        try:
            status, body = self._transport(self.url, request)
        except Exception as exc:  # noqa: BLE001 - network failure is a closed code
            raise TimestampError("tsa_unreachable", type(exc).__name__) from exc
        if status != 200:
            raise TimestampError("tsa_unreachable", f"HTTP {status}")
        if len(body) > MAX_RESPONSE_BYTES:
            raise TimestampError("malformed", "the TSA response is too large")
        return token_from_response(body, digest=digest, nonce=nonce)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class LocalTsa:
    """An offline TSA with its own key: for tests and air-gapped trials only.

    Its tokens are structurally real RFC 3161 tokens (``openssl ts -verify``
    accepts them against its certificate), but the time in them is whatever
    this machine's clock said. Never present one as third-party evidence.
    """

    def __init__(
        self,
        private_key: Any,
        certificate: Any,
        *,
        clock: Callable[[], datetime] = _utc_now,
        policy: str = LOCAL_TSA_POLICY,
    ) -> None:
        self._key = private_key
        self.certificate = certificate
        self._clock = clock
        self._policy = policy

    @property
    def name(self) -> str:
        return "local:" + self.certificate.subject.rfc4514_string()

    @classmethod
    def generate(
        cls,
        *,
        common_name: str = "OpenAdapt local TSA (test only, not trusted)",
        clock: Callable[[], datetime] = _utc_now,
        valid_from: Optional[datetime] = None,
        valid_days: int = 3650,
    ) -> "LocalTsa":
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        start = valid_from or (clock() - timedelta(days=1))
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + timedelta(days=valid_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=True,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), True
            )
            .sign(key, hashes.SHA256())
        )
        return cls(key, cert, clock=clock)

    @classmethod
    def load_or_create(cls, directory: Path) -> "LocalTsa":
        """Keep the key (mode 0600) and certificate in ``directory``."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = directory / "tsa-key.pem"
        cert_path = directory / "tsa-cert.pem"
        if key_path.is_file() and cert_path.is_file():
            key = serialization.load_pem_private_key(key_path.read_bytes(), None)
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            return cls(key, cert)
        tsa = cls.generate()
        pem = tsa._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        from openadapt_flow.countersign.vault import write_private_bytes

        write_private_bytes(key_path, pem)
        cert_path.write_bytes(tsa.certificate_pem())
        return tsa

    def certificate_pem(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return bytes(self.certificate.public_bytes(serialization.Encoding.PEM))

    def timestamp(self, digest: bytes, *, nonce: Optional[int] = None) -> bytes:
        _check_digest(digest)
        a = _asn1()
        cms, tsp, x509 = a.cms, a.tsp, a.x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        gen_time = self._clock().astimezone(timezone.utc)
        info: dict[str, Any] = {
            "version": "v1",
            "policy": self._policy,
            "message_imprint": {
                "hash_algorithm": {"algorithm": "sha256"},
                "hashed_message": bytes(digest),
            },
            "serial_number": int.from_bytes(secrets.token_bytes(16), "big") >> 1,
            "gen_time": gen_time,
            "accuracy": {"seconds": 1},
            "ordering": False,
        }
        if nonce is not None:
            info["nonce"] = nonce
        tst = tsp.TSTInfo(info)
        tst_der = tst.dump()
        cert_der = self.certificate.public_bytes(serialization.Encoding.DER)
        cert = x509.Certificate.load(cert_der)
        signed_attrs = cms.CMSAttributes(
            [
                {"type": "content_type", "values": ["tst_info"]},
                {
                    "type": "message_digest",
                    "values": [hashlib.sha256(tst_der).digest()],
                },
                {
                    "type": "signing_certificate_v2",
                    "values": [
                        {
                            "certs": [
                                {
                                    "hash_algorithm": {"algorithm": "sha256"},
                                    "cert_hash": hashlib.sha256(cert_der).digest(),
                                }
                            ]
                        }
                    ],
                },
            ]
        )
        signature = self._key.sign(signed_attrs.dump(), ec.ECDSA(hashes.SHA256()))
        signer = cms.SignerInfo(
            {
                "version": "v1",
                "sid": cms.SignerIdentifier(
                    name="issuer_and_serial_number",
                    value={"issuer": cert.issuer, "serial_number": cert.serial_number},
                ),
                "digest_algorithm": {"algorithm": "sha256"},
                "signed_attrs": signed_attrs,
                "signature_algorithm": {"algorithm": "sha256_ecdsa"},
                "signature": signature,
            }
        )
        signed_data = cms.SignedData(
            {
                "version": "v3",
                "digest_algorithms": [{"algorithm": "sha256"}],
                "encap_content_info": {"content_type": "tst_info", "content": tst},
                "certificates": [cert],
                "signer_infos": [signer],
            }
        )
        token = cms.ContentInfo({"content_type": "signed_data", "content": signed_data})
        return bytes(token.dump())


# -- offline verification ---------------------------------------------------


@dataclass(frozen=True)
class TimestampVerification:
    """Result of an offline token check. ``ok`` only when every check passed."""

    ok: bool
    reason: str
    gen_time: Optional[str] = None
    tsa_subject: Optional[str] = None
    tsa_certificate_sha256: Optional[str] = None
    trust_anchor_subject: Optional[str] = None
    serial_number: Optional[str] = None
    policy: Optional[str] = None
    accuracy_seconds: Optional[float] = None
    imprint_sha256: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def load_pem_certificates(data: bytes) -> list[Any]:
    """Every certificate in a PEM bundle."""
    from cryptography import x509

    return list(x509.load_pem_x509_certificates(data))


def _spki_sha256(cert: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki).hexdigest()


def _is_pinned(cert: Any, pinned: Sequence[Any]) -> Optional[Any]:
    # Match on subject and key, so a pinned self-signed root also anchors a
    # cross-signed copy of the same CA that a TSA ships in its token.
    for anchor in pinned:
        if anchor.subject == cert.subject and _spki_sha256(anchor) == _spki_sha256(
            cert
        ):
            return anchor
    return None


def _valid_at(cert: Any, when: datetime) -> bool:
    return bool(cert.not_valid_before_utc <= when <= cert.not_valid_after_utc)


def _chain_to_pin(
    leaf: Any, pool: Sequence[Any], pinned: Sequence[Any], when: datetime
) -> Optional[Any]:
    """The pinned anchor that ``leaf`` chains to, checked at ``when``, or None."""
    from cryptography import x509

    current = leaf
    seen: set[bytes] = set()
    for _ in range(6):
        anchor = _is_pinned(current, pinned)
        if anchor is not None:
            return anchor
        fp = current.fingerprint(_sha256())
        if fp in seen:
            return None
        seen.add(fp)
        issuer = None
        for candidate in list(pool) + list(pinned):
            if candidate.subject != current.issuer or candidate is current:
                continue
            try:
                constraints = candidate.extensions.get_extension_for_class(
                    x509.BasicConstraints
                ).value
            except x509.ExtensionNotFound:
                continue
            if not constraints.ca or not _valid_at(candidate, when):
                continue
            try:
                current.verify_directly_issued_by(candidate)
            except Exception:  # noqa: BLE001 - wrong issuer or bad signature
                continue
            issuer = candidate
            break
        if issuer is None:
            return None
        current = issuer
    return None


def _sha256() -> Any:
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()


def _hash(name: str) -> Any:
    from cryptography.hazmat.primitives import hashes

    return {
        "sha256": hashes.SHA256(),
        "sha384": hashes.SHA384(),
        "sha512": hashes.SHA512(),
    }[name]


def _verify_signature(cert: Any, signer: Any, data: bytes, digest_name: str) -> None:
    """Raise unless ``signer``'s signature over ``data`` verifies under ``cert``."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

    algo = signer["signature_algorithm"]
    sig_algo = algo.signature_algo
    try:
        hash_name = algo.hash_algo
    except ValueError:
        hash_name = digest_name
    signature = signer["signature"].native
    key = cert.public_key()
    if sig_algo == "ed25519" and isinstance(key, ed25519.Ed25519PublicKey):
        key.verify(signature, data)
        return
    if hash_name not in _HASHES:
        raise TimestampError("unsupported_algorithm", str(hash_name))
    if sig_algo == "rsassa_pkcs1v15" and isinstance(key, rsa.RSAPublicKey):
        key.verify(signature, data, padding.PKCS1v15(), _hash(hash_name))
        return
    if sig_algo == "rsassa_pss" and isinstance(key, rsa.RSAPublicKey):
        params = algo["parameters"]
        pss_hash = params["hash_algorithm"]["algorithm"].native
        if pss_hash not in _HASHES:
            raise TimestampError("unsupported_algorithm", str(pss_hash))
        key.verify(
            signature,
            data,
            padding.PSS(
                mgf=padding.MGF1(_hash(pss_hash)),
                salt_length=int(params["salt_length"].native),
            ),
            _hash(pss_hash),
        )
        return
    if sig_algo == "ecdsa" and isinstance(key, ec.EllipticCurvePublicKey):
        key.verify(signature, data, ec.ECDSA(_hash(hash_name)))
        return
    raise TimestampError("unsupported_algorithm", str(sig_algo))


def _attr(signed_attrs: Any, name: str) -> Any:
    values = [a["values"] for a in signed_attrs if a["type"].native == name]
    if len(values) != 1 or len(values[0]) != 1:
        return None
    return values[0][0]


def verify_timestamp_token(
    token: bytes, digest: bytes, trusted: Sequence[Any]
) -> TimestampVerification:
    """Check an RFC 3161 token over ``digest`` offline, fail closed.

    ``trusted`` are certificates the verifier pinned: the TSA's own
    certificate or a CA above it. Checks, in order: the token parses; its
    imprint is SHA-256 of exactly ``digest``; one signer, found by its
    identifier; signed attributes bind the TSTInfo (content type and message
    digest) and the signer certificate (ESS signing-certificate v1 or v2);
    the signature verifies; the signer certificate has the critical
    ``timeStamping`` extended key usage and was valid at ``genTime``; and it
    chains to a pinned certificate, every link valid at ``genTime``.

    Not checked: revocation (CRL/OCSP need the network) and whether the TSA's
    clock was right.
    """
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    try:
        a = _asn1()
    except TimestampError as exc:
        return TimestampVerification(False, exc.code)
    if not trusted:
        return TimestampVerification(False, "no_pinned_tsa")
    try:
        content_info = a.cms.ContentInfo.load(bytes(token))
        tst = _tst_info(content_info)
        signed_data = content_info["content"]
        tst_der = bytes(signed_data["encap_content_info"]["content"].contents)
        imprint = tst["message_imprint"]
        algorithm = imprint["hash_algorithm"]["algorithm"].native
        hashed = imprint["hashed_message"].native
        gen_time = tst["gen_time"].native
        serial = tst["serial_number"].native
        policy = tst["policy"].native
        accuracy = tst["accuracy"]
        accuracy_seconds: Optional[float] = None
        if accuracy.native is not None:
            acc = accuracy.native
            accuracy_seconds = (
                float(acc.get("seconds") or 0)
                + (float(acc.get("millis") or 0) / 1e3)
                + (float(acc.get("micros") or 0) / 1e6)
            )
        signers = list(signed_data["signer_infos"])
        pool = []
        for choice in signed_data["certificates"] or []:
            if choice.name == "certificate":
                pool.append(x509.load_der_x509_certificate(choice.chosen.dump()))
    except TimestampError as exc:
        return TimestampVerification(False, exc.code)
    except Exception:  # noqa: BLE001 - any parse failure is malformed
        return TimestampVerification(False, "malformed")
    base = {
        "gen_time": gen_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "serial_number": format(serial, "x"),
        "policy": policy,
        "accuracy_seconds": accuracy_seconds,
        "imprint_sha256": hashed.hex() if isinstance(hashed, bytes) else None,
    }
    if algorithm != "sha256" or hashed != bytes(digest):
        return TimestampVerification(False, "imprint_mismatch", **base)
    if len(signers) != 1:
        return TimestampVerification(False, "malformed", **base)
    signer = signers[0]
    sid = signer["sid"]
    candidates = pool + list(trusted)
    signer_cert = None
    for cert in candidates:
        if sid.name == "issuer_and_serial_number":
            want = sid.chosen
            if (
                cert.serial_number == want["serial_number"].native
                and a.x509.Name.load(cert.issuer.public_bytes()) == want["issuer"]
            ):
                signer_cert = cert
                break
        elif sid.name == "subject_key_identifier":
            try:
                ski = cert.extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier
                ).value.digest
            except x509.ExtensionNotFound:
                continue
            if ski == sid.chosen.native:
                signer_cert = cert
                break
    if signer_cert is None:
        return TimestampVerification(False, "untrusted_tsa", **base)
    from cryptography.hazmat.primitives import serialization

    cert_der = signer_cert.public_bytes(serialization.Encoding.DER)
    base["tsa_subject"] = signer_cert.subject.rfc4514_string()
    base["tsa_certificate_sha256"] = hashlib.sha256(cert_der).hexdigest()
    signed_attrs = signer["signed_attrs"]
    digest_name = signer["digest_algorithm"]["algorithm"].native
    if not signed_attrs or digest_name not in _HASHES:
        return TimestampVerification(False, "malformed", **base)
    content_type = _attr(signed_attrs, "content_type")
    message_digest = _attr(signed_attrs, "message_digest")
    if content_type is None or content_type.native != "tst_info":
        return TimestampVerification(False, "malformed", **base)
    if (
        message_digest is None
        or message_digest.native != hashlib.new(digest_name, tst_der).digest()
    ):
        return TimestampVerification(False, "content_mismatch", **base)
    ess_v2 = _attr(signed_attrs, "signing_certificate_v2")
    ess_v1 = _attr(signed_attrs, "signing_certificate")
    if ess_v2 is not None:
        first = ess_v2["certs"][0]
        ess_hash = first["hash_algorithm"]["algorithm"].native
        if (
            ess_hash not in _HASHES
            or first["cert_hash"].native != hashlib.new(ess_hash, cert_der).digest()
        ):
            return TimestampVerification(False, "signer_certificate_mismatch", **base)
    elif ess_v1 is not None:
        if ess_v1["certs"][0]["cert_hash"].native != hashlib.sha1(cert_der).digest():
            return TimestampVerification(False, "signer_certificate_mismatch", **base)
    else:
        return TimestampVerification(False, "malformed", **base)
    to_verify = b"\x31" + signed_attrs.dump()[1:]
    try:
        _verify_signature(signer_cert, signer, to_verify, digest_name)
    except TimestampError as exc:
        return TimestampVerification(False, exc.code, **base)
    except Exception:  # noqa: BLE001 - InvalidSignature or a key mismatch
        return TimestampVerification(False, "bad_signature", **base)
    try:
        eku = signer_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        return TimestampVerification(False, "not_a_tsa_certificate", **base)
    if not eku.critical or ExtendedKeyUsageOID.TIME_STAMPING not in eku.value:
        return TimestampVerification(False, "not_a_tsa_certificate", **base)
    if not _valid_at(signer_cert, gen_time):
        return TimestampVerification(False, "certificate_not_valid_at_gen_time", **base)
    anchor = _chain_to_pin(signer_cert, pool, trusted, gen_time)
    if anchor is None:
        return TimestampVerification(False, "untrusted_tsa", **base)
    base["trust_anchor_subject"] = anchor.subject.rfc4514_string()
    return TimestampVerification(True, "verified", **base)
