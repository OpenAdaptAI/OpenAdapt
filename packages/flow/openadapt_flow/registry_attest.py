"""The skills registry's attestation: who published an index, checked offline.

The registry release workflow (``.github/workflows/release-registry.yml``)
attests ``index.json`` and every bundle archive with GitHub's keyless artifact
attestations (``actions/attest``): Sigstore signs an in-toto statement naming
each file's sha256 with a short-lived certificate for the workflow's identity,
and records it in Rekor. The bundle is published beside the index as
``index.sigstore.json``.

``pull`` verifies that bundle with the ``sigstore`` package (the ``registry``
extra) against a pinned identity, never one the index names:

- the issuer is GitHub Actions (``token.actions.githubusercontent.com``);
- the certificate's SAN is ``release-registry.yml`` on a ``registry-*`` tag
  of OpenAdaptAI/OpenAdapt, and its source repository is that repository;
- the statement names the exact sha256 of the index that was read.

Verification is offline. It uses the Sigstore trusted root shipped with the
package (``registry_trusted_root.json``), or the one
``openadapt-flow pull --refresh-trust`` last fetched through Sigstore's TUF
repository into ``$OPENADAPT_HOME/registry/trusted_root.json``.

For a local index you are authoring, a local test signer stands in for the
workflow (:func:`sign_test`). Its seed is public on purpose, so ``pull``
accepts it only with ``--allow-test-key``, and only for an index on the local
disk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

#: The attestation bundle, published beside ``index.json``.
ATTESTATION_NAME = "index.sigstore.json"
#: The index's subject name in the statement (``actions/attest`` names a
#: subject by its file name).
INDEX_SUBJECT = "index.json"
#: The pinned Sigstore trusted root (Fulcio, Rekor and timestamp keys).
TRUSTED_ROOT_FILE = Path(__file__).with_name("registry_trusted_root.json")

IN_TOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"
TEST_MEDIA_TYPE = "application/vnd.openadapt.registry-test-attestation+json"
TEST_KEY_ID = "openadapt-registry-test"
#: The local test signer's seed. Public on purpose: it signs nothing anyone
#: else should trust, and ``pull`` takes it only for a local index with
#: ``--allow-test-key``.
TEST_KEY_SEED = hashlib.sha256(
    b"openadapt-registry local test signer: public, local indexes only"
).digest()


class AttestationError(RuntimeError):
    """The attestation doesn't hold, or can't be checked here."""


@dataclass(frozen=True)
class Identity:
    """The workflow identity an attestation must carry."""

    issuer: str
    san: re.Pattern[str]
    source_repository_uri: str


#: The only identity that may publish the registry.
REGISTRY_IDENTITY = Identity(
    issuer="https://token.actions.githubusercontent.com",
    san=re.compile(
        r"^https://github\.com/OpenAdaptAI/OpenAdapt/\.github/workflows/"
        r"release-registry\.yml@refs/tags/registry-[0-9A-Za-z][0-9A-Za-z.-]{0,40}$"
    ),
    source_repository_uri="https://github.com/OpenAdaptAI/OpenAdapt",
)


def _home() -> Path:
    return Path(os.environ.get("OPENADAPT_HOME", Path.home() / ".openadapt"))


def refreshed_trusted_root() -> Path:
    return _home() / "registry" / "trusted_root.json"


def trusted_root_path() -> Path:
    """The trusted root to verify with: the refreshed copy, else the pinned one."""

    refreshed = refreshed_trusted_root()
    return refreshed if refreshed.is_file() else TRUSTED_ROOT_FILE


def _sigstore() -> Any:
    try:
        import sigstore  # noqa: F401
    except ImportError as exc:
        raise AttestationError(
            "verifying the registry needs the sigstore package: "
            "pip install 'openadapt-flow[registry]'"
        ) from exc
    return sigstore


def refresh_trusted_root() -> Path:
    """Fetch Sigstore's current trusted root through its TUF repository.

    TUF checks the update against the root of trust in the ``sigstore``
    package. The result is written to ``$OPENADAPT_HOME`` and used from then
    on; the pinned copy in the package is left as it is.
    """

    _sigstore()
    from sigstore.models import DEFAULT_TUF_URL, TrustUpdater

    source = Path(TrustUpdater(DEFAULT_TUF_URL, offline=False).get_trusted_root_path())
    data = source.read_bytes()
    _load_trusted_root_bytes(data)  # refuse anything sigstore can't load
    out = refreshed_trusted_root()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)
    return out


def _load_trusted_root_bytes(data: bytes) -> Any:
    import tempfile

    from sigstore.models import TrustedRoot

    with tempfile.NamedTemporaryFile(suffix=".json") as handle:
        handle.write(data)
        handle.flush()
        return TrustedRoot.from_file(handle.name)


def statement_subjects(payload: bytes) -> dict[str, str]:
    """``{name: sha256}`` for every subject of an in-toto statement."""

    try:
        statement = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AttestationError(f"the attested statement is not JSON: {exc}") from exc
    if not isinstance(statement, dict) or statement.get("_type") not in (
        "https://in-toto.io/Statement/v1",
        "https://in-toto.io/Statement/v0.1",
    ):
        raise AttestationError("the attestation is not an in-toto statement")
    subjects: dict[str, str] = {}
    for subject in statement.get("subject") or []:
        name = subject.get("name") if isinstance(subject, dict) else None
        digest = (subject.get("digest") or {}).get("sha256") if name else None
        if not isinstance(name, str) or not isinstance(digest, str):
            raise AttestationError("an attested subject has no name or sha256")
        if subjects.get(name, digest) != digest:
            raise AttestationError(f"the attestation names {name!r} twice")
        subjects[name] = digest
    if not subjects:
        raise AttestationError("the attestation names no subject")
    return subjects


def _pinned_policy(identity: Identity) -> Any:
    from cryptography import x509
    from sigstore.errors import VerificationError
    from sigstore.verify import policy

    class PinnedIdentity(policy.VerificationPolicy):  # type: ignore[misc]
        def verify(self, cert: Any) -> None:
            policy.OIDCIssuerV2(identity.issuer).verify(cert)
            policy.OIDCSourceRepositoryURI(identity.source_repository_uri).verify(cert)
            try:
                san = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value
            except x509.ExtensionNotFound as exc:
                raise VerificationError("the certificate has no SAN") from exc
            uris = san.get_values_for_type(x509.UniformResourceIdentifier)
            if len(uris) != 1 or not identity.san.match(uris[0]):
                raise VerificationError(
                    f"the certificate's identity {uris} is not the pinned workflow"
                )

    return PinnedIdentity()


def verify_sigstore(
    bundle: Mapping[str, Any],
    *,
    identity: Identity = REGISTRY_IDENTITY,
    trusted_root: Optional[Path] = None,
) -> tuple[str, dict[str, str]]:
    """Verify a Sigstore bundle offline; returns (the SAN, its subjects)."""

    _sigstore()
    from cryptography import x509
    from sigstore.errors import Error as SigstoreError
    from sigstore.models import Bundle, TrustedRoot
    from sigstore.verify import Verifier

    try:
        parsed = Bundle.from_json(json.dumps(bundle))
        root = TrustedRoot.from_file(str(trusted_root or trusted_root_path()))
        payload_type, payload = Verifier(trusted_root=root).verify_dsse(
            parsed, _pinned_policy(identity)
        )
    except SigstoreError as exc:
        raise AttestationError(
            f"the Sigstore attestation does not hold: {exc}"
        ) from exc
    if payload_type != IN_TOTO_PAYLOAD_TYPE:
        raise AttestationError(f"the attestation's payload is {payload_type!r}")
    san = parsed.signing_certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)[0]
    return str(san), statement_subjects(payload)


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE's pre-authentication encoding."""

    kind = payload_type.encode("utf-8")
    return b"DSSEv1 %d %b %d %b" % (len(kind), kind, len(payload), payload)


def sign_test(
    subjects: Mapping[str, str], seed: bytes = TEST_KEY_SEED
) -> dict[str, Any]:
    """A test attestation naming ``subjects``, for a local index."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {"name": name, "digest": {"sha256": digest}}
            for name, digest in sorted(subjects.items())
        ],
        "predicateType": "https://openadapt.ai/registry-test/v1",
        "predicate": {},
    }
    payload = json.dumps(statement, sort_keys=True).encode("utf-8")
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(
        _pae(IN_TOTO_PAYLOAD_TYPE, payload)
    )
    return {
        "mediaType": TEST_MEDIA_TYPE,
        "dsseEnvelope": {
            "payloadType": IN_TOTO_PAYLOAD_TYPE,
            "payload": base64.b64encode(payload).decode("ascii"),
            "signatures": [
                {
                    "keyid": TEST_KEY_ID,
                    "sig": base64.b64encode(signature).decode("ascii"),
                }
            ],
        },
    }


def verify_test(
    envelope: Mapping[str, Any], seed: bytes = TEST_KEY_SEED
) -> dict[str, str]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    dsse = envelope.get("dsseEnvelope") or {}
    try:
        payload = base64.b64decode(str(dsse.get("payload", "")), validate=True)
        (signature,) = dsse.get("signatures") or [None]
        sig = base64.b64decode(str((signature or {}).get("sig", "")), validate=True)
        Ed25519PrivateKey.from_private_bytes(seed).public_key().verify(
            sig, _pae(str(dsse.get("payloadType")), payload)
        )
    except (ValueError, InvalidSignature) as exc:
        raise AttestationError(
            "the test attestation's signature does not hold"
        ) from exc
    if dsse.get("payloadType") != IN_TOTO_PAYLOAD_TYPE:
        raise AttestationError("the test attestation is not an in-toto statement")
    return statement_subjects(payload)


def verify(
    attestation: Mapping[str, Any],
    *,
    allow_test_key: bool = False,
    identity: Identity = REGISTRY_IDENTITY,
    trusted_root: Optional[Path] = None,
) -> tuple[str, dict[str, str]]:
    """(who attested, ``{name: sha256}``) for a published attestation."""

    if attestation.get("mediaType") == TEST_MEDIA_TYPE:
        if not allow_test_key:
            raise AttestationError(
                "the index is attested by the local test signer, not the release "
                "workflow"
            )
        return f"test:{TEST_KEY_ID}", verify_test(attestation)
    return verify_sigstore(attestation, identity=identity, trusted_root=trusted_root)
