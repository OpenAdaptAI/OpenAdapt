"""Seal a whole bundle or recording directory under one authenticated index.

A compiled bundle and a recording are both directories of files, and most of
those files can hold patient data: screen pixels, typed values, page text,
request and response bodies. This module encrypts all of them.

A *sealed set* is a directory with a plaintext header file,
``.openadapt-sealed.json``. A v1 header holds the scrypt salt and
parameters; a v2 header holds an authenticated wrapped data key and its fresh
set identity. Both carry an *index* sealed with AES-256-GCM under the data key. The index lists every
other file in the directory with the SHA-256 of its bytes on disk. Opening a
sealed set:

- derives the key once and authenticates the index, so a wrong key or an
  edited header fails here;
- refuses the directory if a file is present that the index does not list, or
  a listed file is missing, so nothing can be added or dropped;
- checks each file's on-disk SHA-256 against the index before decrypting it,
  so nothing can be swapped, including for an older ciphertext of the same
  file;
- decrypts in memory only. Each file is sealed with its set-relative path in
  the associated data, so a ciphertext cannot be renamed or moved between sets.

The ``set_digest`` is the SHA-256 of the canonical index. It names the exact
file set, and ``seal`` prints it.

Every file kind is classified in :data:`BUNDLE_FILES`,
:data:`RECORDING_FILES`, :data:`RUN_FILES`, :data:`LIBRARY_FILES` and
:data:`SANITIZED_FILES`. Writing into a sealed set refuses a file the
inventory does not know, rather than passing it through. ``structural`` files
hold only hashes, counts and format markers and stay readable without the key;
every other file is encrypted.

Callers read and write through the path-based helpers at the end of this
module (:func:`read_bytes`, :func:`write_bytes`, :func:`append_line` and so
on). Given an ordinary path they behave like :mod:`pathlib`; given a path
inside a sealed set, or inside a directory a :class:`SealedSetWriter` is
writing, they decrypt or encrypt in memory. That keeps the command code the
same for plain and sealed input.

Primitives come from :mod:`openadapt_flow.crypto` (scrypt and AES-256-GCM from
the ``cryptography`` package); nothing here is a new primitive.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Iterator, Literal, Optional

from openadapt_flow import crypto
from openadapt_flow.sealed_maintenance import mutation_lock

HEADER: Final[str] = ".openadapt-sealed.json"
FORMAT: Final[str] = "openadapt-flow-sealed-set"
FORMAT_VERSION: Final[int] = 1
SEALED_SUFFIX: Final[str] = ".enc"
#: Format version 3: a per-set data key wrapped to a bucket key
#: (:mod:`openadapt_flow.key_hierarchy`), Tink streaming payload.
ENVELOPE_VERSION: Final[int] = 3
_LEGACY_DERIVATION_FIELDS: Final[frozenset[str]] = frozenset(
    {"salt", "kdf", "kdf_n", "kdf_r", "kdf_p"}
)

Kind = Literal["bundle", "recording", "run", "library", "sanitized"]
Codec = Literal["set", "plain", "external"]
#: ``export`` is a plaintext file a person asked for (the receipt page),
#: kept beside an encrypted run and recorded in the plaintext ledger.
Classification = Literal["phi", "structural", "export"]

_INDEX_AAD: Final[bytes] = b"openadapt-flow/sealed-set/index/"
_FILE_AAD: Final[bytes] = b"openadapt-flow/sealed-set/file/"

#: Environment variable naming the passphrase for recordings. When it is not
#: set, :data:`openadapt_flow.crypto.ENV_KEY` is used, so one deployment key
#: covers recordings and bundles.
RECORDING_ENV_KEY: Final[str] = "OPENADAPT_RECORDING_KEY"


class SealedSetError(ValueError):
    """A sealed set is malformed, incomplete or was changed after sealing."""


class SealedSetKeyError(SealedSetError):
    """A sealed set was found but no key was configured to open it."""


class SealedSetRestoreRequired(SealedSetKeyError):
    """Only the recovery code opens this set; it opens only through an
    explicit restore (:func:`restore`)."""


@dataclass(frozen=True)
class FileKind:
    """One kind of file a bundle or recording can contain."""

    pattern: str
    classification: Classification
    holds: str


# The inventory (docs/phi_at_rest.md prints it). A set-relative POSIX path
# takes the first entry whose ``fnmatch`` pattern it fits; ``*`` also crosses
# ``/``. A path that fits no entry is unknown and is refused when sealing.
BUNDLE_FILES: Final[tuple[FileKind, ...]] = (
    FileKind(
        "workflow.json",
        "phi",
        "the compiled program: target labels, typed literals, landmark text, "
        "structural selectors, URLs, effect values, data-source rows, "
        "parameter defaults, window titles",
    ),
    FileKind(
        "templates/*",
        "phi",
        "pixel crops of the recorded screen (targets, expected regions, drag "
        "ends, identifier crops) and the layout fingerprint",
    ),
    FileKind(
        "manifest.json",
        "structural",
        "SHA-256 digests, compiler version and certification provenance",
    ),
    FileKind("workflow.py", "phi", "a readable rendering of workflow.json"),
    FileKind(
        "compile_notes.json", "phi", "gesture rewrites the compiler made, by step"
    ),
    FileKind("param_hygiene.json", "phi", "dropped checks with masked matched text"),
    FileKind(
        "sections.json",
        "phi",
        "named groups of steps: titles from interface text and field labels",
    ),
    FileKind(
        "param_proposals.json",
        "phi",
        "proposed parameters: field labels and partly masked examples",
    ),
    FileKind(
        "annotations.json",
        "phi",
        "model annotations, including raw example values and labels",
    ),
    FileKind(
        "api_proposal.json",
        "phi",
        "API-step proposal reasons, which can quote screen text and URL paths",
    ),
    FileKind(
        "effect_contract.yaml",
        "phi",
        "a scaffolded effect contract with observed and typed values",
    ),
    FileKind("undo-lineage.json", "phi", "prior content digests of undo bundles"),
    FileKind(
        "record-check-binding.json", "phi", "record-check coverage by step and field"
    ),
    FileKind(
        "qualification-proposal.json",
        "phi",
        "qualification pins (app origin), failure matrix and suggestions",
    ),
    FileKind(
        "qualification-admission.local-dev.json",
        "phi",
        "a local-development admission record",
    ),
    FileKind(
        "qualification-proposal.draft.json",
        "phi",
        "an admission's draft check proposal",
    ),
    FileKind(
        "admission-decision.json",
        "phi",
        "a signed admission decision with the decider's identity",
    ),
    FileKind(
        "admission.json",
        "phi",
        "a signed admission manifest, which can carry read-back values",
    ),
    FileKind("jit_draft.json", "phi", "draft state of a just-in-time program"),
    FileKind(
        "teach-authorization/*",
        "phi",
        "the person-signed teach authorization the program was taught under",
    ),
    FileKind("admit.json", "phi", "authoring admission with the local login name"),
    FileKind("openadapt-card.json", "phi", "operator-written name, purpose and inputs"),
    FileKind(
        "repair/*",
        "phi",
        "repair candidates: old and new anchors with their screen text",
    ),
    FileKind("qualification/*", "phi", "qualification cases, inputs and reports"),
    FileKind("evidence/*", "phi", "run evidence a command copied into the bundle"),
    FileKind("narration.json", "phi", "the recording's scrubbed narration"),
)

RECORDING_FILES: Final[tuple[FileKind, ...]] = (
    FileKind(
        "events.jsonl",
        "phi",
        "every action: typed text, field labels, structural locators, page "
        "URL and title, and system-of-record rows when a reader was attached",
    ),
    FileKind("frames/*", "phi", "before and after screenshots of every action"),
    FileKind(
        "meta.json",
        "phi",
        "app URL, parameter example values, window title, identifier region, "
        "task description",
    ),
    FileKind(
        "network.jsonl",
        "phi",
        "request URLs, form fields and bodies, response bodies",
    ),
    FileKind(
        "sor_snapshots.json",
        "phi",
        "system-of-record rows read before and after the recording",
    ),
    FileKind("verify.proposed.yaml", "phi", "effects proposed from the record diff"),
    FileKind("check.json", "phi", "a demonstrated check: marked fields and reset"),
    FileKind(
        "check.proposed.json",
        "phi",
        "a check proposed from a recorded look-up, with screen text and regions",
    ),
    FileKind(
        "readback.proposed.yaml",
        "phi",
        "a desktop read-back proposed from a recorded look-up",
    ),
    FileKind(
        "record_check.proposed.yaml", "phi", "a proposed record check with values"
    ),
    FileKind("record_check.proof.json", "phi", "record-check fault proof"),
    FileKind("identity.proposed.json", "phi", "proposed identity with record text"),
    FileKind(
        "effect_contract.yaml",
        "phi",
        "a scaffolded effect contract with observed values",
    ),
    FileKind("distill.json", "phi", "a distillation report"),
    FileKind("narration.json", "phi", "what the person said, scrubbed"),
    FileKind(
        ".openadapt-approval.json", "phi", "approval answers about recorded steps"
    ),
    FileKind(
        ".openadapt-sanitization.json", "phi", "redaction manifest of a derivative"
    ),
)

#: A run directory written while executing a sealed bundle. Every file is
#: encrypted; ``checkpoints/`` holds the durable store's own AES-GCM
#: containers, which the run set adopts as they are.
RUN_FILES: Final[tuple[FileKind, ...]] = (
    FileKind(".durable_run.claim", "structural", "run id and namespace claim"),
    FileKind(".durable_state.lock", "structural", "an empty lock file"),
    FileKind(
        ".attended_capability.key",
        "structural",
        "a random per-run capability secret (no recorded data)",
    ),
    FileKind(".attended_action.lease", "structural", "an action lease marker"),
    FileKind(
        ".report_run_id",
        "structural",
        "the random UUID that makes reporting this run to Cloud idempotent",
    ),
    FileKind("*.lock", "structural", "a lock file"),
    # The receipt files stay readable without the key: they hold signatures,
    # outcomes, counts and SHA-256 digests over the plaintext report.json,
    # never its contents (docs/phi_at_rest.md, "Receipts").
    FileKind("seal.json", "structural", "the signed run receipt over report.json"),
    FileKind("seal.anchor.json", "structural", "the receipt's transparency-log proof"),
    FileKind("seal.tsa.json", "structural", "the receipt's timestamp record"),
    FileKind("seal.tsr", "structural", "the receipt's RFC 3161 timestamp token"),
    FileKind("seal.publish.json", "structural", "where the public receipt was posted"),
    FileKind("public-seal.json", "structural", "the allow-listed public card"),
    FileKind("receipt.json", "structural", "the allow-listed run receipt"),
    FileKind("receipt.md", "structural", "the allow-listed run receipt as text"),
    FileKind("receipt.png", "structural", "the allow-listed run receipt as an image"),
    FileKind(
        "seal.html",
        "export",
        "the receipt page a person opens; it embeds report.json, so it is a "
        "plaintext copy of the run's inputs, named on stderr and in the ledger",
    ),
    FileKind("checkpoints/*", "phi", "durable checkpoints (already encrypted)"),
    FileKind(
        "*",
        "phi",
        "run report, step screenshots, identity and resolution crops, "
        "transition frames, read-back evidence, escalations",
    ),
)


#: A skill library written by ``teach`` (:mod:`openadapt_flow.learning.library`).
LIBRARY_FILES: Final[tuple[FileKind, ...]] = (
    FileKind(
        "skills.json",
        "phi",
        "every learned program version (target labels, typed literals, screen "
        "text, crop paths) and the execution traces it was learned from",
    ),
)

#: The sanitized review copy ``push`` makes of a sealed source
#: (:mod:`openadapt_flow.sanitized_artifact`). Scrubbing is best effort until a
#: person approves it, so every file stays encrypted.
SANITIZED_FILES: Final[tuple[FileKind, ...]] = (
    FileKind(
        ".openadapt-sanitization.json",
        "phi",
        "the redaction manifest: file names, hashes and redaction records",
    ),
    FileKind(".openadapt-approval.json", "phi", "the reviewer's approval"),
    FileKind(
        "*",
        "phi",
        "the scrubbed copy of each recording or bundle file, before review",
    ),
)

_INVENTORIES: Final[dict[str, tuple[FileKind, ...]]] = {
    "bundle": BUNDLE_FILES,
    "recording": RECORDING_FILES,
    "run": RUN_FILES,
    "library": LIBRARY_FILES,
    "sanitized": SANITIZED_FILES,
}


def inventory(kind: Kind) -> tuple[FileKind, ...]:
    return _INVENTORIES[kind]


def classify(kind: Kind, rel: str) -> Optional[FileKind]:
    """The inventory entry for ``rel``, or ``None`` for an unknown file."""
    for entry in inventory(kind):
        if fnmatch.fnmatchcase(rel, entry.pattern):
            return entry
    return None


# -- keys ---------------------------------------------------------------------


def recording_key(explicit: Optional[str] = None) -> Optional[str]:
    """The passphrase for recordings: explicit, then the recording variable,
    then the bundle variable, then the local key. ``None`` only under the
    plaintext opt-out."""
    if explicit:
        return explicit
    return os.environ.get(RECORDING_ENV_KEY) or crypto.resolve_key(None)


def passphrase_for(kind: str, explicit: Optional[str] = None) -> Optional[str]:
    if kind == "recording":
        return recording_key(explicit)
    if explicit:
        return explicit
    return output_bundle_key()


def output_bundle_key() -> Optional[str]:
    """The key for a bundle Flow writes: ``OPENADAPT_BUNDLE_KEY``, else
    ``OPENADAPT_RECORDING_KEY`` (a deployment that sets only the recording
    key keeps its bundles under it), else the local key."""
    return (
        os.environ.get(crypto.ENV_KEY)
        or os.environ.get(RECORDING_ENV_KEY)
        or crypto.resolve_key(None)
    )


def missing_key_message(root: Path | str, kind: str) -> str:
    variable = (
        f"{RECORDING_ENV_KEY} or {crypto.ENV_KEY}"
        if kind == "recording"
        else crypto.ENV_KEY
    )
    return (
        f"{root} is an encrypted {kind}; set {variable} to the key it was "
        "sealed with. Nothing was read."
    )


def _require_passphrase(root: Path, kind: str, key: Optional[str]) -> Optional[str]:
    """The passphrase to open ``root`` with. A version 3 set needs none (its
    recipients may be this computer's device identity), so None is allowed
    there; an older set without a passphrase refuses here."""
    resolved = passphrase_for(kind, key) or crypto.resolve_read_key(None)
    if resolved:
        return resolved
    try:
        if read_header(root).get("version") == ENVELOPE_VERSION:
            return None
    except SealedSetError:
        pass
    raise SealedSetKeyError(missing_key_message(root, kind))


def _file_aad(kind: str, rel: str) -> bytes:
    return _FILE_AAD + kind.encode("ascii") + b"/" + rel.encode("utf-8")


def _index_aad(kind: str) -> bytes:
    return _INDEX_AAD + kind.encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_rel(rel: str) -> str:
    path = PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts or rel in ("", "."):
        raise SealedSetError(f"refusing an unsafe path in a sealed set: {rel!r}")
    if path.name == HEADER:
        raise SealedSetError("the sealed-set header is not a data file")
    return path.as_posix()


def _physical(rel: str, codec: Codec) -> str:
    return rel + SEALED_SUFFIX if codec == "set" else rel


def _canonical(files: dict[str, dict[str, str]]) -> bytes:
    return json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` (ciphertext or structural only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _disk_files(root: Path) -> list[str]:
    """Every file under ``root`` except the header. A subdirectory that is
    itself a sealed set (an undo run inside a run directory) is a separate,
    separately verified set and is skipped."""
    out: list[str] = []

    def unreadable(exc: OSError) -> None:
        # A directory that can't be listed can't be checked against the
        # index, so the set can't be verified.
        raise SealedSetError(f"sealed set has an unreadable directory: {exc}")

    for current, dirnames, filenames in os.walk(
        root, onerror=unreadable, followlinks=False
    ):
        here = Path(current)
        for name in dirnames:
            if (here / name).is_symlink():
                raise SealedSetError(
                    "sealed set contains a symlink: "
                    f"{(here / name).relative_to(root).as_posix()}"
                )
        dirnames[:] = [name for name in dirnames if not is_sealed(here / name)]
        for name in filenames:
            path = here / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                raise SealedSetError(f"sealed set contains a non-file entry: {rel}")
            if rel != HEADER:
                out.append(rel)
    return sorted(out)


# -- header ------------------------------------------------------------------


def is_sealed(root: Path | str) -> bool:
    """Whether ``root`` is a sealed set (bundle or recording)."""
    return os.path.isfile(os.path.join(root, HEADER))


def read_header(root: Path | str) -> dict[str, object]:
    try:
        header = json.loads((Path(root) / HEADER).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SealedSetError(f"unreadable sealed-set header: {exc}") from exc
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise SealedSetError("not an openadapt-flow sealed set")
    if header.get("version") not in (FORMAT_VERSION, 2, ENVELOPE_VERSION):
        raise SealedSetError(
            f"sealed-set format version {header.get('version')!r} is not supported"
        )
    if header.get("kind") not in _INVENTORIES:
        raise SealedSetError("sealed-set header has no valid kind")
    return header


def header_kind(root: Path | str) -> Kind:
    return read_header(root)["kind"]  # type: ignore[return-value]


def _wrapping_aad(header: dict[str, object]) -> bytes:
    """Bind a v2 envelope to its fresh set identity and wrapping-key label."""
    names = ("format", "version", "kind", "cipher", "data_key_id", "key_id")
    return b"openadapt-flow/sealed-set/wrapped-key/v2\0" + json.dumps(
        {name: header[name] for name in names}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


_V3_WRAP: Final[bytes] = b"openadapt.ai/sealed-set/v3/wrap\0"
_V3_PAYLOAD: Final[bytes] = b"openadapt.ai/sealed-set/v3/payload\0"
_V3_HEADER: Final[bytes] = b"openadapt.ai/sealed-set/v3/header"
_V3_BUCKET_MAC: Final[bytes] = b"openadapt.ai/sealed-set/v3/bucket-mac"
_V3_STORE: Final[bytes] = b"openadapt.ai/sealed-set/v3/store\0"
PAYLOAD: Final[str] = "tink-aes256-gcm-hkdf-1mb"
_MAC_EXCLUDED: Final[frozenset[str]] = frozenset({"mac", "bucket_mac", "set_digest"})


def _hkdf(ikm: bytes, info: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(ikm)


def _v3_wrap_info(header: dict[str, object], fpr: str) -> bytes:
    return (
        _V3_WRAP
        + str(header["set_id"]).encode()
        + str(header["kind"]).encode()
        + fpr.encode()
    )


def _mac_body(header: dict[str, object]) -> bytes:
    body = {k: v for k, v in header.items() if k not in _MAC_EXCLUDED}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class V3Key:
    """A version 3 set's data key. Files are Tink AES-256-GCM-HKDF 1 MiB
    streams (:mod:`openadapt_flow.stream_aead`) under a payload key derived
    from the DEK and the set id, with the file's set path as associated
    data. ``bucket`` is the opened bucket, which MACs the header."""

    def __init__(self, dek: bytes, set_id: str, bucket: object = None) -> None:
        self._payload = _hkdf(dek, _V3_PAYLOAD + set_id.encode())
        self._header_key = _hkdf(dek, _V3_HEADER)
        self._store = _hkdf(dek, _V3_STORE + set_id.encode())
        self.bucket = bucket

    @property
    def store_passphrase(self) -> str:
        """The passphrase of the stores that keep their own containers inside
        this set (durable checkpoints, approvals, transition evidence). It
        comes from the set's data key, so shredding the bucket makes those
        containers unreadable too, in backups as well."""
        return base64.urlsafe_b64encode(self._store).decode("ascii")

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes:
        from openadapt_flow import stream_aead

        return crypto.SET3_FILE_MAGIC + stream_aead.encrypt(
            self._payload, plaintext, aad
        )

    def open(self, container: bytes, *, aad: bytes) -> bytes:
        from openadapt_flow import stream_aead

        if container[: len(crypto.SET3_FILE_MAGIC)] != crypto.SET3_FILE_MAGIC:
            raise SealedSetError("not a version 3 sealed-set file")
        try:
            return stream_aead.decrypt(
                self._payload, container[len(crypto.SET3_FILE_MAGIC) :], aad
            )
        except stream_aead.StreamError as exc:
            raise crypto.DecryptionError(str(exc)) from exc

    def _bucket_mac_key(self) -> Optional[bytes]:
        private = getattr(self.bucket, "private", None)
        if private is None:
            return None
        scalar = private.private_numbers().private_value.to_bytes(32, "big")
        return _hkdf(scalar, _V3_BUCKET_MAC)

    def stamp(self, header: dict[str, object]) -> None:
        """Write the header MACs (DEK and bucket key) into ``header``."""
        import hmac

        body = _mac_body(header)
        header["mac"] = hmac.new(self._header_key, body, hashlib.sha256).hexdigest()
        bucket_key = self._bucket_mac_key()
        if bucket_key is not None:
            header["bucket_mac"] = hmac.new(
                bucket_key, body, hashlib.sha256
            ).hexdigest()

    def check(self, header: dict[str, object]) -> None:
        import hmac

        body = _mac_body(header)
        want = hmac.new(self._header_key, body, hashlib.sha256).hexdigest()
        bucket_key = self._bucket_mac_key()
        bucket_want = (
            hmac.new(bucket_key, body, hashlib.sha256).hexdigest()
            if bucket_key
            else None
        )
        got, bucket_got = header.get("mac"), header.get("bucket_mac")
        if not (
            isinstance(got, str)
            and hmac.compare_digest(got, want)
            and isinstance(bucket_got, str)
            and bucket_want is not None
            and hmac.compare_digest(bucket_got, bucket_want)
        ):
            raise SealedSetError(
                "the encrypted set's header was changed after it was written, or "
                "was not written with its bucket key (header MAC mismatch)"
            )


def _open_v3(header: dict[str, object], *, restore: bool = False) -> V3Key:
    """Open a version 3 set: find its bucket record, unwrap the bucket key
    with a key this computer holds, unwrap the DEK, check the header MACs."""
    from openadapt_flow import key_hierarchy as kh

    if header.get("shredded"):
        raise SealedSetError(
            "this encrypted set was shredded: its bucket key was destroyed, so "
            "nobody can open it"
        )
    key_ref = header.get("key_ref")
    set_id = header.get("set_id")
    if (
        not isinstance(key_ref, dict)
        or not isinstance(set_id, str)
        or len(set_id) != 32
    ):
        raise SealedSetError("sealed-set version 3 header is malformed")
    inline = header.get("bucket_record")
    inline = inline if isinstance(inline, dict) else None
    try:
        record = kh.find_record(key_ref, inline)
        if record.get("bucket_id") != key_ref.get("bucket_id"):
            raise SealedSetError("the bucket record belongs to another set")
        # A record the header supplies proves nothing about who wrote the
        # set: only this computer's device keys or a recovery key open it.
        bucket = kh.open_bucket(record, from_header=record is inline, restore=restore)
    except kh.RecoveryRestoreRequired as exc:
        raise SealedSetRestoreRequired(str(exc)) from exc
    except kh.NoRecipientKey as exc:
        raise SealedSetKeyError(str(exc)) from exc
    except kh.KeyHierarchyError as exc:
        raise SealedSetError(str(exc)) from exc
    except RuntimeError as exc:  # the device key is locked, missing or replaced
        from openadapt_flow import at_rest

        if isinstance(exc, at_rest.LocalKeyError):
            raise SealedSetKeyError(str(exc)) from exc
        raise
    if bucket.fpr != key_ref.get("bucket_pub_fpr"):
        raise SealedSetError("the bucket record does not match the set's key reference")
    stanzas = header.get("stanzas")
    stanza = next(
        (
            s
            for s in (stanzas if isinstance(stanzas, list) else [])
            if isinstance(s, dict) and s.get("fpr") == bucket.fpr
        ),
        None,
    )
    if stanza is None:
        raise SealedSetError("the set has no data key wrapped to its bucket")
    try:
        dek = kh.hpke_open(
            bucket.private,  # type: ignore[arg-type]
            base64.b64decode(str(stanza["ct"]), validate=True),
            _v3_wrap_info(header, bucket.fpr),
        )
    except Exception as exc:  # noqa: BLE001 - wrong bucket or damaged stanza
        raise SealedSetError("the set's data key does not unwrap") from exc
    key = V3Key(dek, set_id, bucket)
    key.check(header)
    return key


def _data_key(header: dict[str, object], passphrase: Optional[str]) -> bytes:
    """Unwrapped data keys stay in memory; no fallback key resolution here."""
    if not passphrase:
        raise SealedSetKeyError("no key is configured to open this sealed set")
    try:
        if header.get("version") == 2:
            wrapped = header.get("wrapped_key")
            key_id = header.get("key_id")
            data_key_id = header.get("data_key_id")
            if (
                not isinstance(wrapped, str)
                or not isinstance(key_id, str)
                or not key_id
                or not isinstance(data_key_id, str)
                or len(data_key_id) != 32
                or any(char not in "0123456789abcdef" for char in data_key_id)
                or _LEGACY_DERIVATION_FIELDS.intersection(header)
            ):
                raise SealedSetError("sealed-set wrapping metadata is malformed")
            result = crypto.decrypt_bytes(
                base64.b64decode(wrapped, validate=True),
                passphrase,
                aad=_wrapping_aad(header),
            )
            if len(result) != 32:
                raise SealedSetError("sealed-set wrapped data key is malformed")
            return result
        salt = base64.b64decode(str(header["salt"]), validate=True)
        return crypto.derive_data_key(
            passphrase,
            salt,
            n=int(header["kdf_n"]),  # type: ignore[call-overload]
            r=int(header["kdf_r"]),  # type: ignore[call-overload]
            p=int(header["kdf_p"]),  # type: ignore[call-overload]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SealedSetError("sealed-set key header is malformed") from exc


def _derive(header: dict[str, object], passphrase: Optional[str]) -> Any:
    if header.get("version") == ENVELOPE_VERSION:
        return _open_v3(header)
    return crypto.SetKey(_data_key(header, passphrase))


def _open_index(
    root: Path, header: dict[str, object], key: crypto.SetKey
) -> dict[str, dict[str, str]]:
    sealed_index = header.get("index")
    if not isinstance(sealed_index, str):
        raise SealedSetError(
            f"{root} was never finalized (the recorder or sealer stopped before "
            "writing its index); refusing a partial set"
        )
    kind = str(header["kind"])
    try:
        raw = base64.b64decode(sealed_index, validate=True)
    except ValueError as exc:
        raise SealedSetError(f"sealed-set index is malformed: {exc}") from exc
    index = json.loads(key.open(raw, aad=_index_aad(kind)))
    files = index.get("files") if isinstance(index, dict) else None
    if not isinstance(files, dict) or index.get("kind") != kind:
        raise SealedSetError("sealed-set index is malformed")
    for rel, entry in files.items():
        _check_rel(rel)
        if not isinstance(entry, dict) or entry.get("codec") not in (
            "set",
            "plain",
            "external",
        ):
            raise SealedSetError(f"sealed-set index entry for {rel} is malformed")
    return files


# -- reading -----------------------------------------------------------------


class SealedSet:
    """A verified, caller-owned sealed-set snapshot.

    Keep this handle for an operation to reuse its derived key. Independent
    path-helper reads open a fresh handle; no reader or passphrase is retained
    by the module. Caller inputs and derived keys remain sensitive memory.
    """

    sealed = True

    def __init__(
        self,
        root: Path,
        kind: Kind,
        key: crypto.SetKey,
        files: dict[str, dict[str, str]],
        header: dict[str, object],
    ) -> None:
        self.root = root
        self.kind: Kind = kind
        self._key = key
        self._files = files
        self._header = header

    @property
    def set_digest(self) -> str:
        return _sha256(_canonical(self._files))

    @classmethod
    def open(
        cls,
        root: Path | str,
        key: Optional[str] = None,
        *,
        expect: Optional[Kind] = None,
    ) -> "SealedSet":
        base = Path(root)
        header = read_header(base)
        kind: Kind = header["kind"]  # type: ignore[assignment]
        if expect is not None and kind != expect:
            raise SealedSetError(f"{base} is a sealed {kind}, not a {expect}")
        passphrase = _require_passphrase(base, kind, key)
        set_key = _derive(header, passphrase)
        files = _open_index(base, header, set_key)
        present = _disk_files(base)
        if kind == "run":
            files = _reconcile_run_files(base, files, present)
        expected = sorted(_physical(rel, e["codec"]) for rel, e in files.items())  # type: ignore[arg-type]
        if present != expected:
            added = sorted(set(present) - set(expected))
            dropped = sorted(set(expected) - set(present))
            detail = []
            if added:
                detail.append("not in the sealed index: " + ", ".join(added))
            if dropped:
                detail.append("missing: " + ", ".join(dropped))
            raise SealedSetError(
                f"{base} does not match its sealed index ({'; '.join(detail)})"
            )
        return cls(base, kind, set_key, files, header)

    def is_file(self, rel: str) -> bool:
        return rel in self._files

    def codec(self, rel: str) -> Codec:
        entry = self._files.get(rel)
        if entry is None:
            raise FileNotFoundError(f"{rel} is not in sealed set {self.root}")
        return entry["codec"]  # type: ignore[return-value]

    def is_dir(self, rel: str) -> bool:
        prefix = rel.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in self._files)

    def files(self, prefix: str = "") -> list[str]:
        return sorted(rel for rel in self._files if rel.startswith(prefix))

    def read_bytes(self, rel: str) -> bytes:
        entry = self._files.get(rel)
        if entry is None:
            raise FileNotFoundError(f"{rel} is not in sealed set {self.root}")
        raw = (self.root / _physical(rel, entry["codec"])).read_bytes()  # type: ignore[arg-type]
        if _sha256(raw) != entry["sha256"]:
            raise SealedSetError(
                f"{rel} in {self.root} was changed after sealing (hash mismatch)"
            )
        if entry["codec"] != "set":
            # Structural plaintext, or ciphertext another keyed store owns
            # (durable checkpoints): returned as stored, hash-checked above.
            return raw
        return self._key.open(raw, aad=_file_aad(self.kind, rel))

    def verify_all(self) -> None:
        """Hash-check and decrypt every file once."""
        for rel in self._files:
            self.read_bytes(rel)


def _adoptable(root: Path, kind: str, rel: str) -> Optional[Codec]:
    """How a file another component wrote directly may join a run set:
    ``plain`` for a structural file the inventory names, ``external`` for
    ciphertext of another keyed store, ``None`` for plaintext (refused)."""
    entry = classify(kind, rel)  # type: ignore[arg-type]
    if entry is not None and entry.classification in ("structural", "export"):
        return "plain"
    if crypto.is_encrypted((root / rel).read_bytes()):
        return "external"
    return None


def _reconcile_run_files(
    root: Path, files: dict[str, dict[str, str]], present: list[str]
) -> dict[str, dict[str, str]]:
    """A run directory keeps changing after its index was sealed: the durable
    store adds, replaces and removes its own encrypted checkpoints, approvals
    and locks between legs of a run. Accept exactly those changes; any file
    the run's own writer sealed must still be present, and any other new file
    must be ciphertext or a structural lock, never plaintext."""
    out = dict(files)
    sealed_physical = {
        _physical(rel, e["codec"]): rel  # type: ignore[arg-type]
        for rel, e in files.items()
    }
    present_set = set(present)
    for physical, rel in sealed_physical.items():
        if physical not in present_set and files[rel]["codec"] != "set":
            del out[rel]
    for rel in present:
        if rel in sealed_physical:
            continue
        codec = _adoptable(root, "run", rel)
        if codec is None:
            raise SealedSetError(
                f"{root} holds {rel} in plaintext, outside its sealed index"
            )
        raw = (root / rel).read_bytes()
        out[rel] = {"codec": codec, "sha256": _sha256(raw)}
    for rel, entry in out.items():
        if entry["codec"] != "set":
            owned = root / rel
            if owned.is_file():
                # Owned and authenticated by its own store; track its bytes.
                entry["sha256"] = _sha256(owned.read_bytes())
    return out


# -- writing -----------------------------------------------------------------


def _explicit_passphrase(passphrase: Optional[str]) -> bool:
    """Whether ``passphrase`` is a configured key rather than this computer's
    local one (which the key hierarchy replaces for new sets)."""
    if not passphrase:
        return False
    from openadapt_flow import at_rest

    return at_rest.explicit_key() or passphrase != at_rest.existing_key()


def _new_v3(
    kind: str, passphrase: Optional[str]
) -> Optional[tuple[dict[str, object], V3Key]]:
    """A version 3 header for a new set and its key; None for a configured
    passphrase outside organization mode (the version 1 header Flow always
    wrote: explicit keys still win, deprecated)."""
    from openadapt_flow import key_hierarchy as kh

    try:
        policy = kh.load_policy()
        if _explicit_passphrase(passphrase) and policy.mode != "org":
            return None
        bucket = kh.new_bucket(kind, policy)
    except kh.KeyHierarchyError as exc:
        raise SealedSetError(f"Not writing an encrypted {kind}: {exc}") from exc
    set_id = secrets.token_hex(16)
    dek = secrets.token_bytes(32)
    header: dict[str, object] = {
        "format": FORMAT,
        "version": ENVELOPE_VERSION,
        "set_id": set_id,
        "kind": kind,
        "cipher": "AES-256-GCM",
        "payload": PAYLOAD,
        "key_ref": bucket.key_ref(),
        # Authenticated by the header MACs: shredding deletes the archive
        # copy from this archive, whatever the policy says by then.
        "records": policy.records,
    }
    if policy.records == "archive":
        header["key_archive"] = policy.key_archive
    header["stanzas"] = [
        {
            "type": kh.STANZA_TYPE,
            "fpr": bucket.fpr,
            "ct": base64.b64encode(
                kh.hpke_seal(bucket.public, dek, _v3_wrap_info(header, bucket.fpr))
            ).decode("ascii"),
        }
    ]
    if policy.records == "inline":
        header["bucket_record"] = bucket.record
    return header, V3Key(dek, set_id, bucket)


def preflight_new(kind: str, passphrase: Optional[str]) -> None:
    """Refuse, before a capture starts, a new set :func:`_new_v3` would
    refuse at sealing time (organization recipients, archive and backlog,
    hardware key). Makes no record."""
    from openadapt_flow import key_hierarchy as kh

    try:
        policy = kh.load_policy()
        if _explicit_passphrase(passphrase) and policy.mode != "org":
            return
        kh.preflight(policy)
    except kh.KeyHierarchyError as exc:
        raise SealedSetError(f"Not recording an encrypted {kind}: {exc}") from exc


class SealedSetWriter:
    """Write a sealed set file by file, ciphertext only, then finalize it.

    A new set writes its header (salt and parameters, no index) first, so a
    crash leaves a directory that :meth:`SealedSet.open` refuses as unfinished
    instead of a readable partial recording. ``resume=True`` reopens a
    finalized set (verifying it) to add or replace files; :meth:`finalize`
    then re-verifies every file this writer did not write.
    """

    def __init__(
        self,
        root: Path | str,
        kind: Kind,
        passphrase: Optional[str],
        *,
        resume: bool = False,
    ) -> None:
        self.root = Path(root)
        self.kind: Kind = kind
        self._codecs: dict[str, Codec] = {}
        self._new = False
        self._hashes: dict[str, str] = {}
        self._lines: dict[str, list[bytes]] = {}
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        with mutation_lock(self.root):
            self._initialize(passphrase, resume=resume)

    def _initialize(self, passphrase: Optional[str], *, resume: bool) -> None:
        kind = self.kind
        if resume and is_sealed(self.root):
            opened = SealedSet.open(self.root, passphrase, expect=kind)
            self._header: dict[str, object] = dict(opened._header)
            self._key = opened._key
            self._header_revision = _sha256((self.root / HEADER).read_bytes())
            for rel, entry in opened._files.items():
                self._codecs[rel] = entry["codec"]  # type: ignore[assignment]
                self._hashes[rel] = entry["sha256"]
            return
        if is_sealed(self.root):
            raise SealedSetError(f"{self.root} is already a sealed set")
        self.root.mkdir(parents=True, exist_ok=True)
        created = _new_v3(kind, passphrase)
        if created is not None:
            self._header, self._key = created[0], created[1]  # type: ignore[assignment]
            self._new = True
            self._write_header()
            return
        if not passphrase:
            raise SealedSetKeyError(missing_key_message(self.root, kind))
        salt = crypto.SetKey.new_salt()
        self._header = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "kind": kind,
            "cipher": "AES-256-GCM",
            "kdf": "scrypt",
            **crypto.kdf_parameters(),
            "salt": base64.b64encode(salt).decode("ascii"),
        }
        self._key = _derive(self._header, passphrase)
        self._write_header()

    def _write_header(self) -> None:
        if isinstance(self._key, V3Key):
            self._key.stamp(self._header)
        raw = json.dumps(self._header, indent=2, sort_keys=True).encode("utf-8")
        _atomic_write(self.root / HEADER, raw)
        self._header_revision = _sha256(raw)

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._lock, mutation_lock(self.root):
            try:
                current = _sha256((self.root / HEADER).read_bytes())
            except OSError as exc:
                raise SealedSetError(
                    "sealed-set header disappeared under writer"
                ) from exc
            if current != self._header_revision:
                raise SealedSetError(
                    "sealed-set header changed under writer; reopen before writing"
                )
            yield

    def _classified(self, rel: str) -> FileKind:
        rel = _check_rel(rel)
        entry = classify(self.kind, rel)
        if entry is None:
            raise SealedSetError(
                f"refusing to seal {rel}: it is not a known {self.kind} file, so "
                "Flow cannot say whether it holds patient data"
            )
        return entry

    def write(self, rel: str, data: bytes) -> None:
        """Seal ``data`` as ``rel``; a structural file stays plaintext."""
        entry = self._classified(rel)
        with self._mutation():
            old = self._codecs.get(rel)
            if entry.classification in ("structural", "export"):
                codec: Codec = "plain"
                blob = data
            else:
                codec = "set"
                blob = self._key.seal(data, aad=_file_aad(self.kind, rel))
            if old is not None and old != codec:
                os.unlink(self.root / _physical(rel, old))
            _atomic_write(self.root / _physical(rel, codec), blob)
            self._codecs[rel] = codec
            self._hashes[rel] = _sha256(blob)

    def append_line(self, rel: str, line: str) -> None:
        """Append one line to a sealed line log.

        The log is kept in memory and the whole file is resealed on each
        append, so the file on disk is always one complete ciphertext.
        """
        with self._lock:
            lines = self._lines.get(rel)
            if lines is None:
                lines = []
                if rel in self._codecs:
                    lines = self.read_bytes(rel).splitlines(keepends=True)
                self._lines[rel] = lines
            lines.append(line.rstrip("\n").encode("utf-8") + b"\n")
            self.write(rel, b"".join(lines))

    def unlink(self, rel: str, *, missing_ok: bool = False) -> None:
        with self._mutation():
            codec = self._codecs.pop(rel, None)
            self._hashes.pop(rel, None)
            self._lines.pop(rel, None)
            if codec is None:
                if missing_ok:
                    return
                raise FileNotFoundError(f"{rel} is not in {self.root}")
            os.unlink(self.root / _physical(rel, codec))

    def read_bytes(self, rel: str) -> bytes:
        """Decrypt a file in this set (for read-modify-write)."""
        with self._lock:
            codec = self._codecs.get(rel)
            if codec is None:
                raise FileNotFoundError(f"{rel} is not in {self.root}")
            raw = (self.root / _physical(rel, codec)).read_bytes()
            if _sha256(raw) != self._hashes[rel]:
                raise SealedSetError(f"{rel} in {self.root} changed under the writer")
        if codec != "set":
            return raw
        return self._key.open(raw, aad=_file_aad(self.kind, rel))

    def is_file(self, rel: str) -> bool:
        return rel in self._codecs

    def is_dir(self, rel: str) -> bool:
        prefix = rel.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in self._codecs)

    def files(self, prefix: str = "") -> list[str]:
        return sorted(rel for rel in self._codecs if rel.startswith(prefix))

    def _adopt_external(self) -> None:
        sealed_physical = {
            _physical(rel, c) for rel, c in self._codecs.items() if c == "set"
        }
        for rel in list(self._codecs):
            if self._codecs[rel] != "set" and not (self.root / rel).is_file():
                # A lock or checkpoint its owner removed.
                del self._codecs[rel]
                self._hashes.pop(rel, None)
        for rel in _disk_files(self.root):
            if rel in sealed_physical:
                continue
            codec = _adoptable(self.root, self.kind, rel)
            if codec is None:
                raise SealedSetError(
                    f"refusing to seal {self.root}: {rel} was written in "
                    "plaintext outside the sealed writer"
                )
            self._codecs[rel] = codec
            self._hashes[rel] = _sha256((self.root / rel).read_bytes())

    def finalize(self, *, adopt_external: bool = False) -> str:
        """Check the directory holds exactly what was written, seal the index,
        and return the set digest. Refuses any unexpected file.

        ``adopt_external`` (run directories) also accepts files another
        component wrote directly, but only two kinds: a structural file the
        inventory names (lock and claim files), and ciphertext from another
        keyed store (durable checkpoints, :func:`crypto.is_encrypted`). Any
        other file is plaintext that escaped the writer and is refused.
        """
        with self._mutation():
            if adopt_external:
                self._adopt_external()
            expected = {_physical(rel, c): rel for rel, c in self._codecs.items()}
            present = _disk_files(self.root)
            unexpected = sorted(set(present) - set(expected))
            if unexpected:
                raise SealedSetError(
                    f"refusing to seal {self.root}: file(s) were written "
                    "outside the sealed writer: " + ", ".join(unexpected)
                )
            missing = sorted(set(expected) - set(present))
            if missing:
                raise SealedSetError(
                    f"refusing to seal {self.root}: missing " + ", ".join(missing)
                )
            files: dict[str, dict[str, str]] = {}
            for physical, rel in sorted(expected.items()):
                actual = _sha256((self.root / physical).read_bytes())
                if actual != self._hashes[rel]:
                    raise SealedSetError(
                        f"refusing to seal {self.root}: {rel} changed outside "
                        "the sealed writer"
                    )
                files[rel] = {"codec": self._codecs[rel], "sha256": actual}
            index = json.dumps({"kind": self.kind, "files": files}, sort_keys=True)
            self._header["index"] = base64.b64encode(
                self._key.seal(index.encode("utf-8"), aad=_index_aad(self.kind))
            ).decode("ascii")
            digest = _sha256(_canonical(files))
            # Informational copy for inventories; the authenticated value is
            # the one recomputed from the sealed index on open.
            self._header["set_digest"] = digest
            if self._new and isinstance(self._key, V3Key) and self.kind != "bundle":
                self._close_bucket()
            self._write_header()
            return digest

    def _close_bucket(self) -> None:
        """The set is written: its bucket record loses the runtime stanza, so
        from now on only the viewer, organization or recovery key opens it.
        A bundle keeps it: programs replay unattended."""
        from openadapt_flow import key_hierarchy as kh

        bucket = getattr(self._key, "bucket", None)
        if bucket is None:
            return
        try:
            kh.close(bucket)
        except kh.KeyHierarchyError as exc:
            raise SealedSetError(f"refusing to seal {self.root}: {exc}") from exc
        if "bucket_record" in self._header:
            self._header["bucket_record"] = bucket.record
        self._new = False


# -- path-based helpers ------------------------------------------------------
#
# Command code keeps passing ordinary ``Path`` objects. These helpers find the
# sealed set (or the active writer) that contains a path and route the read or
# write through it.

_WRITERS: dict[str, SealedSetWriter] = {}
_STATE_LOCK = threading.RLock()


def _abs(path: Path | str) -> str:
    return os.path.abspath(os.fspath(path))


@contextmanager
def writing(
    root: Path | str,
    kind: Kind,
    passphrase: str,
    *,
    resume: bool = False,
) -> Iterator[SealedSetWriter]:
    """Route every helper write under ``root`` through one sealed writer and
    finalize it when the block succeeds. On error the set stays unfinalized,
    which every reader refuses."""
    writer = SealedSetWriter(root, kind, passphrase, resume=resume)
    register_writer(writer)
    try:
        yield writer
        writer.finalize()
    finally:
        unregister_writer(writer)


def register_writer(writer: SealedSetWriter) -> None:
    with _STATE_LOCK:
        _WRITERS[_abs(writer.root)] = writer


def unregister_writer(writer: SealedSetWriter) -> None:
    with _STATE_LOCK:
        if _WRITERS.get(_abs(writer.root)) is writer:
            _WRITERS.pop(_abs(writer.root), None)


def active_writer(root: Path | str) -> Optional[SealedSetWriter]:
    with _STATE_LOCK:
        return _WRITERS.get(_abs(root))


def _locate(path: Path | str) -> tuple[Optional[str], str]:
    """Return ``(root, rel)`` of the writer or sealed set holding ``path``."""
    full = _abs(path)
    with _STATE_LOCK:
        writers = sorted(_WRITERS, key=len, reverse=True)
    for root in writers:
        if full.startswith(root + os.sep):
            return root, full[len(root) + 1 :].replace(os.sep, "/")
    current = os.path.dirname(full)
    while True:
        if os.path.isfile(os.path.join(current, HEADER)):
            return current, full[len(current) + 1 :].replace(os.sep, "/")
        parent = os.path.dirname(current)
        if parent == current:
            return None, full
        current = parent


def open_set(root: Path | str, key: Optional[str] = None) -> SealedSet:
    """Open and verify a fresh caller-owned reader for this request.

    Reuse the returned handle explicitly within an operation, rather than
    retaining credentials or readers across unrelated calls.
    """
    base = _abs(root)
    header = read_header(base)
    passphrase = _require_passphrase(Path(base), str(header["kind"]), key)
    try:
        return SealedSet.open(base, passphrase)
    except crypto.CryptoError as exc:
        if key is None and not _configured(str(header["kind"])):
            # Opened with the local key because nothing was configured: the
            # set was most likely sealed with a deployment key that isn't set.
            raise SealedSetKeyError(
                missing_key_message(Path(base), str(header["kind"]))
                + " (The local key did not open it.)"
            ) from exc
        raise


def store_passphrase_of(writer: "SealedSetWriter") -> Optional[str]:
    """:func:`store_passphrase` of an open writer's set."""
    key = getattr(writer, "_key", None)
    return key.store_passphrase if isinstance(key, V3Key) else None


def store_passphrase(run_dir: Path | str) -> Optional[str]:
    """The passphrase for another store's containers inside a version 3 run
    (durable checkpoints, approvals, transition evidence), or None when
    ``run_dir`` is not one.

    It is derived from the set's data key, so it is the same for every
    process and every leg of a run, and a shred of the set's bucket makes
    those containers unreadable everywhere, backups included. Only the
    header is opened (and authenticated), so a run that has not been
    finalized yet, such as one paused mid-leg, still resolves it. A shredded
    set, or one no key here opens, refuses: nothing falls back to the
    long-lived local passphrase.
    """
    base = _abs(run_dir)
    writer = active_writer(base)
    if writer is not None:
        return store_passphrase_of(writer)
    if not is_sealed(base):
        return None
    header = read_header(base)
    if header.get("version") != ENVELOPE_VERSION:
        return None
    return _open_v3(header).store_passphrase


def _configured(kind: str) -> bool:
    return bool(os.environ.get(crypto.ENV_KEY) or os.environ.get(RECORDING_ENV_KEY))


def _reader(root: str) -> SealedSet | SealedSetWriter:
    with _STATE_LOCK:
        writer = _WRITERS.get(root)
    if writer is not None:
        return writer
    return open_set(root)


def sealed_root(path: Path | str) -> Optional[Path]:
    """The sealed set or active writer root that contains ``path``."""
    root, _rel = _locate(path)
    return Path(root) if root is not None else None


def is_sealed_path(path: Path | str) -> bool:
    """Whether ``path`` is, or is inside, a sealed set or active writer."""
    full = _abs(path)
    with _STATE_LOCK:
        if full in _WRITERS:
            return True
    return is_sealed(full) or sealed_root(full) is not None


def read_bytes(path: Path | str) -> bytes:
    root, rel = _locate(path)
    if root is None:
        return Path(path).read_bytes()
    return _reader(root).read_bytes(rel)


def read_text(path: Path | str, encoding: str = "utf-8") -> str:
    return read_bytes(path).decode(encoding)


def is_file(path: Path | str) -> bool:
    root, rel = _locate(path)
    if root is None:
        return Path(path).is_file()
    return _reader(root).is_file(rel)


def exists(path: Path | str) -> bool:
    full = _abs(path)
    root, rel = _locate(full)
    if root is None:
        return os.path.exists(full)
    reader = _reader(root)
    return reader.is_file(rel) or reader.is_dir(rel)


def is_dir(path: Path | str) -> bool:
    full = _abs(path)
    with _STATE_LOCK:
        if full in _WRITERS:
            return True
    if is_sealed(full):
        return True
    root, rel = _locate(full)
    if root is None:
        return os.path.isdir(full)
    return _reader(root).is_dir(rel)


def list_files(directory: Path | str, pattern: str = "*") -> list[Path]:
    """Files directly or recursively under ``directory`` matching ``pattern``
    (``fnmatch`` on the path relative to ``directory``)."""
    full = _abs(directory)
    with _STATE_LOCK:
        writer = _WRITERS.get(full)
    if writer is not None or is_sealed(full):
        root, prefix = full, ""
    else:
        root_opt, rel = _locate(full)
        if root_opt is None:
            base = Path(full)
            if not base.is_dir():
                return []
            return sorted(
                p
                for p in base.rglob("*")
                if p.is_file()
                and fnmatch.fnmatchcase(p.relative_to(base).as_posix(), pattern)
            )
        root, prefix = root_opt, rel.rstrip("/") + "/"
    reader = _reader(root)
    out = []
    for name in reader.files(prefix):
        tail = name[len(prefix) :]
        if fnmatch.fnmatchcase(tail, pattern):
            out.append(Path(root) / name)
    return out


def open_text(path: Path | str, encoding: str = "utf-8") -> io.StringIO:
    """A readable text stream over ``path`` (decrypted in memory if sealed)."""
    return io.StringIO(read_text(path, encoding))


def write_bytes(path: Path | str, data: bytes, *, key: Optional[str] = None) -> None:
    """Write ``path``; sealed if it lies in an active writer or a sealed set."""
    root, rel = _locate(path)
    if root is None:
        target = Path(path)
        target.write_bytes(data)
        return
    with _STATE_LOCK:
        writer = _WRITERS.get(root)
    if writer is not None:
        writer.write(rel, data)
        return
    kind = header_kind(root)
    passphrase = _require_passphrase(Path(root), kind, key)
    resumed = SealedSetWriter(root, kind, passphrase, resume=True)
    resumed.write(rel, data)
    resumed.finalize()


def write_text(
    path: Path | str, text: str, *, encoding: str = "utf-8", key: Optional[str] = None
) -> None:
    write_bytes(path, text.encode(encoding), key=key)


def append_line(path: Path | str, line: str) -> None:
    """Append one line to ``path`` (a sealed line log inside a writer)."""
    root, rel = _locate(path)
    if root is not None:
        with _STATE_LOCK:
            writer = _WRITERS.get(root)
        if writer is None:
            raise SealedSetError(
                f"cannot append to {rel} in finalized sealed set {root}"
            )
        writer.append_line(rel, line)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def unlink(
    path: Path | str, *, missing_ok: bool = False, key: Optional[str] = None
) -> None:
    root, rel = _locate(path)
    if root is None:
        Path(path).unlink(missing_ok=missing_ok)
        return
    with _STATE_LOCK:
        writer = _WRITERS.get(root)
    if writer is not None:
        writer.unlink(rel, missing_ok=missing_ok)
        return
    kind = header_kind(root)
    resumed = SealedSetWriter(
        root, kind, _require_passphrase(Path(root), kind, key), resume=True
    )
    resumed.unlink(rel, missing_ok=missing_ok)
    resumed.finalize()


def require_readable(path: Path | str, what: str) -> None:
    """Refuse a sealed directory in a command that cannot read one."""
    if is_sealed_path(path):
        root = sealed_root(path) or Path(path)
        kind = header_kind(root) if is_sealed(root) else "bundle"
        raise SealedSetError(
            f"{what} does not read encrypted {kind}s, and Flow will not decrypt "
            f"{root} to disk for it. Use an unencrypted copy inside your managed "
            "storage, or a command that reads sealed input."
        )


# -- recipients and shredding --------------------------------------------------


def _replace_header(root: Path, header: dict[str, object]) -> None:
    raw = json.dumps(header, indent=2, sort_keys=True).encode("utf-8")
    path = root / HEADER
    tmp = path.with_name(f".{HEADER}.{secrets.token_hex(6)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    if os.name == "posix":
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def restore(root: Path | str) -> str:
    """Restore a set that only the recovery code opens (a backup on a new or
    reset computer): open it with ``OPENADAPT_RECOVERY_CODE``, wrap its
    bucket to this computer's device key, and re-stamp the header MACs. From
    then on it opens here like a set written here. The person runs this on
    purpose (``at-rest restore DIR --yes``); a set from a header is never
    opened by the recovery code silently. Returns the device fingerprint."""
    from openadapt_flow import key_hierarchy as kh

    base = Path(_abs(root))
    with mutation_lock(base):
        header = read_header(base)
        if header.get("version") != ENVELOPE_VERSION or not isinstance(
            header.get("bucket_record"), dict
        ):
            raise SealedSetError(
                f"{base} keeps no key record in its header; only a solo-mode "
                "encrypted set is restored with the recovery code"
            )
        key = _open_v3(header, restore=True)
        bucket = key.bucket
        try:
            stanza = kh.device_stanza(bucket)  # type: ignore[arg-type]
        except kh.KeyHierarchyError as exc:
            raise SealedSetError(f"Not restoring {base}: {exc}") from exc
        raw_record = header["bucket_record"]
        assert isinstance(raw_record, dict)
        record = dict(raw_record)
        stanzas = [
            s for s in record.get("stanzas", []) if s.get("fpr") != stanza["fpr"]
        ]
        record["stanzas"] = [*stanzas, stanza]
        header["bucket_record"] = record
        key.stamp(header)
        _replace_header(base, header)
        return stanza["fpr"]


def key_record_of(root: Path | str) -> Optional[dict[str, object]]:
    """The bucket record of a version 3 set (no key material in the clear):
    inline in the header, or this computer's local copy. None otherwise."""
    from openadapt_flow import key_hierarchy as kh

    header = read_header(root)
    if header.get("version") != ENVELOPE_VERSION or header.get("shredded"):
        return None
    inline = header.get("bucket_record")
    try:
        return kh.find_record(
            header.get("key_ref") or {},  # type: ignore[arg-type]
            inline if isinstance(inline, dict) else None,
        )
    except kh.KeyHierarchyError:
        return None


_SHRED_HEADER_FIELDS: Final[tuple[str, ...]] = (
    "bucket_record",
    "stanzas",
    "wrapped_key",
    "salt",
    "kdf",
    "kdf_n",
    "kdf_r",
    "kdf_p",
    "mac",
    "bucket_mac",
)


def _delete_store_files(base: Path) -> list[str]:
    """Delete the other stores' ciphertext inside a shredded set (durable
    checkpoints, approvals). Idempotent; the set's own files stay, as
    ciphertext nobody can open."""
    removed: list[str] = []
    for rel in _disk_files(base):
        path = base / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data[: len(crypto.SET_FILE_MAGIC)] in (
            crypto.SET_FILE_MAGIC,
            crypto.SET3_FILE_MAGIC,
        ):
            continue  # the set's own ciphertext, now unopenable
        if crypto.is_encrypted(data):
            path.unlink()
            removed.append(rel)
    return removed


def _finish_shred(base: Path, header: dict[str, object]) -> dict[str, object]:
    """Carry an interrupted or incomplete shred forward: delete any store
    ciphertext still there, and retry the archive deletion this computer
    recorded as pending. The header of a shredded set has no MAC, so nothing
    here is chosen by it: the archive retry acts only on this computer's own
    pending marker."""
    from openadapt_flow import key_hierarchy as kh

    previous = header.get("shred_record")
    record: dict[str, object] = dict(previous) if isinstance(previous, dict) else {}
    deleted = [*record.get("store_files_deleted", []), *_delete_store_files(base)]  # type: ignore[misc]
    record["store_files_deleted"] = deleted
    record["stores_deleted"] = True
    bucket_id = record.get("bucket_id")
    archive_done = record.get("archive_complete", record.get("complete")) is not False
    if not archive_done and isinstance(bucket_id, str):
        try:
            archive_done = kh.retry_shred(bucket_id)
        except kh.KeyHierarchyError:
            archive_done = False
        if archive_done:
            record["copies_destroyed"] = [
                *record.get("copies_destroyed", []),  # type: ignore[misc]
                "archive",
            ]
    record["archive_complete"] = archive_done
    record["complete"] = archive_done
    header["shred_record"] = record
    _replace_header(base, header)
    if archive_done and isinstance(bucket_id, str):
        try:
            kh.clear_pending_shred(bucket_id)
        except kh.KeyHierarchyError:
            pass
    return record


def shred(root: Path | str) -> dict[str, object]:
    """Crypto-shred a sealed set.

    A version 3 set: authenticate its header with a key this computer holds
    (so a changed header can't point the deletion at another set's key),
    then destroy its bucket key's tracked copies (this computer's record,
    overwritten then deleted, and the copy in the archive the set was
    written to), drop the record and data-key stanza from the header, and
    delete the durable checkpoints and other stores' ciphertext inside it.
    The shred record says ``complete`` only after the last of these. In solo
    mode (record inline) this reaches only the copy on this disk: backups
    still hold a record the recovery key opens. An older set loses its salt
    (v1) or wrapped key (v2) instead. Returns the shred record. It can't be
    undone.
    """
    from openadapt_flow import key_hierarchy as kh

    base = Path(_abs(root))
    with mutation_lock(base):
        header = read_header(base)
        if header.get("shredded"):
            return _finish_shred(base, header)
        shred_record: dict[str, object] = {
            "schema": kh.SHRED_SCHEMA,
            "copies_destroyed": [],
            "complete": True,
        }
        if header.get("version") == ENVELOPE_VERSION:
            try:
                key = _open_v3(header)
            except SealedSetKeyError as exc:
                raise SealedSetKeyError(
                    f"Not shredding {base}: no key this computer holds "
                    f"authenticates its header ({exc}), so Flow can't tell which "
                    "key it may destroy. Nothing was destroyed. Shred it where "
                    "it was written, or as the organization (its archive record "
                    f"in {kh.buckets_dir()} and {kh.ORG_KEY_FILE_ENV})."
                ) from exc
            bucket = key.bucket
            records = header.get("records") or bucket.record.get("records")  # type: ignore[attr-defined]
            location = header.get("key_archive")
            try:
                shred_record = kh.shred(
                    bucket.bucket_id,  # type: ignore[attr-defined]
                    records=str(records) if records else None,
                    key_archive=str(location) if location else None,
                )
            except kh.KeyHierarchyError as exc:
                raise SealedSetError(f"Not shredding {base}: {exc}") from exc
        for name in _SHRED_HEADER_FIELDS:
            header.pop(name, None)
        header["shredded"] = True
        header["version"] = ENVELOPE_VERSION
        header.setdefault("set_id", secrets.token_hex(16))
        # Durable progress: not complete until the store files are gone too.
        shred_record["archive_complete"] = shred_record.get("complete") is not False
        shred_record["complete"] = False
        shred_record["stores_deleted"] = False
        shred_record["store_files_deleted"] = []
        header["shred_record"] = shred_record
        _replace_header(base, header)
        return _finish_shred(base, header)


__all__ = [
    "BUNDLE_FILES",
    "ENVELOPE_VERSION",
    "V3Key",
    "key_record_of",
    "shred",
    "store_passphrase",
    "HEADER",
    "LIBRARY_FILES",
    "RECORDING_ENV_KEY",
    "RECORDING_FILES",
    "SANITIZED_FILES",
    "SealedSet",
    "SealedSetError",
    "SealedSetKeyError",
    "SealedSetRestoreRequired",
    "restore",
    "SealedSetWriter",
    "append_line",
    "classify",
    "exists",
    "inventory",
    "is_dir",
    "is_file",
    "is_sealed",
    "is_sealed_path",
    "list_files",
    "missing_key_message",
    "open_set",
    "open_text",
    "passphrase_for",
    "read_bytes",
    "read_text",
    "recording_key",
    "require_readable",
    "sealed_root",
    "unlink",
    "write_bytes",
    "write_text",
    "writing",
]
