"""Seal a whole bundle or recording directory under one authenticated index.

A compiled bundle and a recording are both directories of files, and most of
those files can hold patient data: screen pixels, typed values, page text,
request and response bodies. This module encrypts all of them.

A *sealed set* is a directory with a plaintext header file,
``.openadapt-sealed.json``. The header holds the scrypt salt and parameters and
an *index* sealed with AES-256-GCM under the derived key. The index lists every
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

Every file kind is classified in :data:`BUNDLE_FILES` and
:data:`RECORDING_FILES`. Writing into a sealed set refuses a file the
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
import hmac
import io
import json
import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Iterator, Literal, Optional

from openadapt_flow import crypto

HEADER: Final[str] = ".openadapt-sealed.json"
FORMAT: Final[str] = "openadapt-flow-sealed-set"
FORMAT_VERSION: Final[int] = 1
SEALED_SUFFIX: Final[str] = ".enc"

Kind = Literal["bundle", "recording", "run"]
Codec = Literal["set", "plain", "external"]
Classification = Literal["phi", "structural"]

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
    FileKind("admit.json", "phi", "authoring admission with the local login name"),
    FileKind("openadapt-card.json", "phi", "operator-written name, purpose and inputs"),
    FileKind(
        "repair/*",
        "phi",
        "repair candidates: old and new anchors with their screen text",
    ),
    FileKind("qualification/*", "phi", "qualification cases, inputs and reports"),
    FileKind("evidence/*", "phi", "run evidence a command copied into the bundle"),
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
    FileKind("*.lock", "structural", "a lock file"),
    FileKind("checkpoints/*", "phi", "durable checkpoints (already encrypted)"),
    FileKind(
        "*",
        "phi",
        "run report, step screenshots, identity and resolution crops, "
        "transition frames, read-back evidence, escalations",
    ),
)


def inventory(kind: Kind) -> tuple[FileKind, ...]:
    if kind == "bundle":
        return BUNDLE_FILES
    if kind == "recording":
        return RECORDING_FILES
    return RUN_FILES


def classify(kind: Kind, rel: str) -> Optional[FileKind]:
    """The inventory entry for ``rel``, or ``None`` for an unknown file."""
    for entry in inventory(kind):
        if fnmatch.fnmatchcase(rel, entry.pattern):
            return entry
    return None


# -- keys ---------------------------------------------------------------------


def recording_key(explicit: Optional[str] = None) -> Optional[str]:
    """The passphrase for recordings: explicit, then the recording variable,
    then the bundle variable. ``None`` means no key is configured."""
    if explicit:
        return explicit
    return os.environ.get(RECORDING_ENV_KEY) or crypto.resolve_key(None)


def passphrase_for(kind: str, explicit: Optional[str] = None) -> Optional[str]:
    if kind == "recording":
        return recording_key(explicit)
    return crypto.resolve_key(explicit)


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


def _require_passphrase(root: Path, kind: str, key: Optional[str]) -> str:
    resolved = passphrase_for(kind, key)
    if not resolved:
        raise SealedSetKeyError(missing_key_message(root, kind))
    return resolved


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
    for current, dirnames, filenames in os.walk(root, followlinks=False):
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
    if header.get("version") != FORMAT_VERSION:
        raise SealedSetError(
            f"sealed-set format version {header.get('version')!r} is not supported"
        )
    if header.get("kind") not in ("bundle", "recording", "run"):
        raise SealedSetError("sealed-set header has no valid kind")
    return header


def header_kind(root: Path | str) -> Kind:
    return read_header(root)["kind"]  # type: ignore[return-value]


def _derive(header: dict[str, object], passphrase: str) -> crypto.SetKey:
    try:
        salt = base64.b64decode(str(header["salt"]), validate=True)
        return crypto.SetKey.derive(
            passphrase,
            salt,
            n=int(header["kdf_n"]),  # type: ignore[call-overload]
            r=int(header["kdf_r"]),  # type: ignore[call-overload]
            p=int(header["kdf_p"]),  # type: ignore[call-overload]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SealedSetError(f"sealed-set header is malformed: {exc}") from exc


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
    """A verified, opened sealed set. Plaintext is only ever held in memory."""

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
    if entry is not None and entry.classification == "structural":
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
        passphrase: str,
        *,
        resume: bool = False,
    ) -> None:
        self.root = Path(root)
        self.kind: Kind = kind
        self._codecs: dict[str, Codec] = {}
        self._hashes: dict[str, str] = {}
        self._lines: dict[str, list[bytes]] = {}
        self._lock = threading.RLock()
        if resume and is_sealed(self.root):
            opened = SealedSet.open(self.root, passphrase, expect=kind)
            self._header: dict[str, object] = dict(opened._header)
            self._key = opened._key
            for rel, entry in opened._files.items():
                self._codecs[rel] = entry["codec"]  # type: ignore[assignment]
                self._hashes[rel] = entry["sha256"]
            return
        if is_sealed(self.root):
            raise SealedSetError(f"{self.root} is already a sealed set")
        self.root.mkdir(parents=True, exist_ok=True)
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
        _atomic_write(
            self.root / HEADER,
            json.dumps(self._header, indent=2, sort_keys=True).encode("utf-8"),
        )

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
        with self._lock:
            old = self._codecs.get(rel)
            if entry.classification == "structural":
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
        with self._lock:
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
        with self._lock:
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
            self._write_header()
            _forget(self.root)
            return digest


# -- path-based helpers ------------------------------------------------------
#
# Command code keeps passing ordinary ``Path`` objects. These helpers find the
# sealed set (or the active writer) that contains a path and route the read or
# write through it.

_WRITERS: dict[str, SealedSetWriter] = {}
_OPENED: dict[tuple[str, str], tuple[str, SealedSet]] = {}
_STATE_LOCK = threading.RLock()


def _abs(path: Path | str) -> str:
    return os.path.abspath(os.fspath(path))


def _forget(root: Path) -> None:
    key = _abs(root)
    with _STATE_LOCK:
        for cached in [k for k in _OPENED if k[0] == key]:
            _OPENED.pop(cached, None)


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
    """Open and verify a sealed set once per process and key."""
    base = _abs(root)
    header = read_header(base)
    passphrase = _require_passphrase(Path(base), str(header["kind"]), key)
    # Keyed by the sealed index, never by any digest of the passphrase; a
    # cached set is reused only for the same passphrase (constant-time check).
    cache_key = (base, str(header.get("index")))
    with _STATE_LOCK:
        cached = _OPENED.get(cache_key)
    if cached is not None and hmac.compare_digest(
        cached[0].encode("utf-8"), passphrase.encode("utf-8")
    ):
        return cached[1]
    opened = SealedSet.open(base, passphrase)
    with _STATE_LOCK:
        _OPENED[cache_key] = (passphrase, opened)
    return opened


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


__all__ = [
    "BUNDLE_FILES",
    "HEADER",
    "RECORDING_ENV_KEY",
    "RECORDING_FILES",
    "SealedSet",
    "SealedSetError",
    "SealedSetKeyError",
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
