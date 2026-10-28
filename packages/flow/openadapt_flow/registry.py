"""Workflow registry: fetch a packaged, parameterized bundle by name.

``openadapt-flow pull openemr/new-referral`` reads ``registry/index.json`` from
this repository on GitHub, downloads each file of the named bundle, checks
every file against the sha256 recorded in the index, and then loads the bundle
so its own manifest digest is checked too. Nothing is installed or run.

``status`` in the index is one of:

* ``example``: a compiled bundle that ran on a synthetic copy of the app.
  It has not passed ``certify`` for production use.
* ``certified``: passed ``openadapt-flow certify`` against the policy named in
  ``certification``. The index validator refuses this status without one.
* ``planned``: listed so users can see it is coming; there are no files and
  ``pull`` refuses.

Schema 2 (docs/design/SKILLS_REGISTRY.md) adds, per entry, a ``version``
(``pull app/task@1.2.0`` pins it), the app's license, the bundled record-check
template (``contract``) with the variables it needs (``contract_vars``), its
fault ``coverage``, a blind ``verifier_score`` and the ``credentials`` it
needs. A pulled skill is marked (``skill.lock.json``) and any certification it
carries is expired: it runs only after ``openadapt-flow skill qualify`` on the
user's own instance (:mod:`openadapt_flow.skills`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/"
    "packages/flow/registry/index.json"
)
STATUSES = ("example", "certified", "planned")
MAX_FILE_BYTES = 20 * 1024 * 1024


class RegistryError(RuntimeError):
    """The index or a bundle failed a check. Nothing was written to ``out``."""


# -- signatures ---------------------------------------------------------------------

#: Domain separation for a signed entry (Ed25519ph over SHA-512, the form Rekor
#: checks for an Ed25519 key in a ``hashedrekord`` entry, so an entry can be
#: anchored as Seals are).
_ENTRY_DOMAIN = b"openadapt.registry-entry/v1\n"
KEYS_FILE = Path(__file__).with_name("registry_keys.json")
#: The TEST key's seed: public on purpose, for tests and local seeding. Its
#: public half is flagged ``test`` in ``registry_keys.json``, and ``pull``
#: accepts an entry it signed only with ``allow_test_key``.
TEST_KEY_ID = "openadapt-registry-test-1"
TEST_KEY_SEED = hashlib.sha256(
    b"openadapt-registry-TEST-key: not secret, accepted only with --allow-test-key"
).digest()

#: Files that belong to the private, credentialed tier and never to a public
#: bundle (source-policy.yaml: per-system oracle recipes and fault suites).
PRIVATE_FILE_NAMES = frozenset(
    {"verify.full.yaml", "check_coverage.json", "faults.yaml", "SPEC.md", "WITNESS.md"}
)
PRIVATE_FILE_PREFIXES = ("heldout", "fault-brief", "rows")


def private_tier_file(rel: str) -> bool:
    """Whether a bundle path names a private-tier file."""

    name = Path(rel).name
    return name in PRIVATE_FILE_NAMES or name.startswith(PRIVATE_FILE_PREFIXES)


def entry_statement(raw: dict[str, Any]) -> bytes:
    """The bytes a signature covers: every field of the entry but the signature."""

    body = {k: v for k, v in raw.items() if k != "signature"}
    return _ENTRY_DOMAIN + json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def entry_sha256(raw: dict[str, Any]) -> str:
    return hashlib.sha256(entry_statement(raw)).hexdigest()


def sign_entry(raw: dict[str, Any], seed: bytes, key_id: str) -> dict[str, str]:
    """A signature block for ``raw``, made with the 32-byte Ed25519 ``seed``."""

    from openadapt_flow.ed25519ph import sign_prehashed

    digest = hashlib.sha512(entry_statement(raw)).digest()
    signature = sign_prehashed(seed, digest)
    return {"key_id": key_id, "ed25519ph": base64.b64encode(signature).decode("ascii")}


def trusted_keys(*, allow_test_key: bool = False) -> dict[str, bytes]:
    """The registry's public keys, shipped with the package (never fetched).

    A key flagged ``test`` is left out unless ``allow_test_key``.
    """

    data = json.loads(KEYS_FILE.read_text())
    keys = {}
    for kid, spec in (data.get("keys") or {}).items():
        if isinstance(spec, str):
            spec = {"public": spec}
        if spec.get("test") and not allow_test_key:
            continue
        keys[kid] = base64.b64decode(spec["public"])
    return keys


def entry_signature_problem(
    raw: dict[str, Any], keys: dict[str, bytes]
) -> Optional[str]:
    """Why an entry's signature doesn't hold, or None when it does."""

    from openadapt_flow.ed25519ph import verify_prehashed

    signature = raw.get("signature")
    if not isinstance(signature, dict):
        return "the entry is not signed"
    key = keys.get(str(signature.get("key_id")))
    if key is None:
        hint = (
            " (the TEST key: pass --allow-test-key)"
            if signature.get("key_id") == TEST_KEY_ID
            else ""
        )
        return (
            f"the entry is signed by an unknown key {signature.get('key_id')!r}{hint}"
        )
    try:
        sig = base64.b64decode(str(signature.get("ed25519ph", "")), validate=True)
    except ValueError:
        return "the entry's signature is not base64"
    digest = hashlib.sha512(entry_statement(raw)).digest()
    if not verify_prehashed(key, digest, sig):
        return "the entry's signature does not match its content"
    return None


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    status: str
    raw: dict[str, Any]

    @property
    def files(self) -> dict[str, str]:
        return dict(self.raw.get("files") or {})

    @property
    def version(self) -> Optional[str]:
        version = self.raw.get("version")
        return str(version) if version else None

    @property
    def ref(self) -> str:
        """``id@version`` (or the bare id for a schema 1 entry)."""
        return f"{self.id}@{self.version}" if self.version else self.id


#: Contract variable kinds ``skill bind`` accepts; a credential is only ever the
#: name of the environment variable that holds it.
CONTRACT_VAR_KINDS = ("url", "env_var_name", "path", "command")


def _version_key(version: Optional[str]) -> tuple[int, ...]:
    parts = []
    for piece in (version or "0").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _read(location: str) -> bytes:
    if location.startswith(("http://", "https://")):
        import httpx

        resp = httpx.get(location, timeout=30.0, follow_redirects=True)
        if resp.status_code != 200:
            raise RegistryError(f"GET {location} returned {resp.status_code}")
        if len(resp.content) > MAX_FILE_BYTES:
            raise RegistryError(f"{location} is larger than {MAX_FILE_BYTES} bytes")
        return resp.content
    return Path(location).read_bytes()


def _join(base: str, rel: str) -> str:
    if base.startswith(("http://", "https://")):
        return urljoin(base, rel)
    return str(Path(base).parent / rel)


def validate_index(index: dict[str, Any]) -> list[Entry]:
    """Check the index shape and the status rules. Returns its entries."""
    entries = []
    seen = set()
    for item in index.get("workflows", []):
        wid = item.get("id", "")
        key = (wid, item.get("version"))
        if "/" not in wid or "@" in wid or key in seen:
            raise RegistryError(f"bad or duplicate workflow id {wid!r}")
        seen.add(key)
        status = item.get("status")
        if status not in STATUSES:
            raise RegistryError(f"{wid}: status must be one of {STATUSES}")
        if status == "certified" and not item.get("certification"):
            raise RegistryError(f"{wid}: 'certified' needs a certification record")
        if status != "planned" and not item.get("files"):
            raise RegistryError(f"{wid}: no files")
        for rel in item.get("files") or {}:
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise RegistryError(f"{wid}: unsafe path {rel!r}")
            if private_tier_file(rel):
                raise RegistryError(
                    f"{wid}: {rel!r} belongs to the private tier, never a public bundle"
                )
        full = item.get("full_contract")
        if full is not None:
            if (
                not isinstance(full, dict)
                or not full.get("url")
                or not full.get("sha256")
            ):
                raise RegistryError(f"{wid}: full_contract needs a url and a sha256")
        contract = item.get("contract")
        if contract is not None and contract not in (item.get("files") or {}):
            raise RegistryError(f"{wid}: contract {contract!r} is not one of its files")
        for var in item.get("contract_vars") or []:
            if not isinstance(var, dict) or var.get("kind") not in CONTRACT_VAR_KINDS:
                raise RegistryError(
                    f"{wid}: each contract variable needs a kind in {CONTRACT_VAR_KINDS}"
                )
        score = item.get("verifier_score")
        if score is not None:
            for field in (
                "suite_sha256",
                "caught",
                "missed",
                "clean_right",
                "false_alarm",
            ):
                if field not in score:
                    raise RegistryError(f"{wid}: verifier_score needs {field!r}")
        entries.append(Entry(wid, item.get("title", wid), status, item))
    return entries


def load_index(index_url: str = DEFAULT_INDEX_URL) -> list[Entry]:
    try:
        data = json.loads(_read(index_url))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"index is not JSON: {exc}") from exc
    return validate_index(data)


def find(entries: list[Entry], workflow_id: str) -> Entry:
    """The entry for ``app/task`` (its newest version) or ``app/task@version``."""

    wid, _, version = workflow_id.partition("@")
    matches = [
        e for e in entries if e.id == wid and (not version or e.version == version)
    ]
    if matches:
        return max(matches, key=lambda e: _version_key(e.version))
    known = ", ".join(sorted({e.ref for e in entries}))
    raise RegistryError(f"no workflow {workflow_id!r} in the registry (known: {known})")


def pull(
    workflow_id: str,
    out: Optional[Path | str] = None,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    overwrite: bool = False,
    keys: Optional[dict[str, bytes]] = None,
    allow_unsigned: bool = False,
    allow_test_key: bool = False,
) -> Path:
    """Download and verify a bundle into ``out`` (default ``./<name>``).

    The entry's signature is checked against the keys shipped with the
    package before anything is downloaded. ``allow_unsigned`` takes an
    unsigned entry (a local index while authoring); a signature that is
    present must always hold.
    """
    entry = find(load_index(index_url), workflow_id)
    if entry.status == "planned":
        raise RegistryError(f"{workflow_id} is planned and has no bundle yet")
    problem = entry_signature_problem(
        entry.raw,
        trusted_keys(allow_test_key=allow_test_key) if keys is None else keys,
    )
    if problem is not None and not (
        allow_unsigned and entry.raw.get("signature") is None
    ):
        hint = (
            "; pass --allow-unsigned for a local index you trust"
            if entry.raw.get("signature") is None
            else ""
        )
        raise RegistryError(f"{entry.ref}: {problem}{hint}")
    dest = Path(out) if out else Path(entry.id.split("/", 1)[1])
    if dest.exists() and not overwrite:
        raise RegistryError(f"{dest} exists; pass --force to replace it")
    base = _join(index_url, entry.raw["path"].rstrip("/") + "/")
    archive = entry.raw.get("archive")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "bundle"
        unpacked = _unpack_archive(archive, Path(tmp)) if archive else None
        for rel, expected in sorted(entry.files.items()):
            if unpacked is not None:
                if rel not in unpacked:
                    raise RegistryError(f"{rel} is missing from the archive")
                data = unpacked[rel]
            else:
                data = _read(
                    urljoin(base, rel)
                    if base.startswith("http")
                    else str(Path(base) / rel)
                )
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise RegistryError(f"{rel}: sha256 {actual} does not match the index")
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        from openadapt_flow.ir import Workflow

        try:
            Workflow.load(stage)
        except Exception as exc:  # noqa: BLE001 - report any load failure as one error
            raise RegistryError(f"bundle failed to load: {exc}") from exc
        from openadapt_flow.skills import mark_pulled

        # Mark it as pulled and expire any certification it carries: a
        # registry certification is about our test copy, never the user's.
        mark_pulled(stage, entry, index_url=index_url)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, dest)
    return dest


def _unpack_archive(archive: Any, tmp: Path) -> dict[str, bytes]:
    """The files of a bundle published as a release asset (``.tar.gz``).

    The signed entry pins the archive's sha256. Members must be regular files
    at safe relative paths; links, devices, absolute paths and ``..`` are
    refused.
    """

    import io
    import tarfile

    if (
        not isinstance(archive, dict)
        or not archive.get("url")
        or not archive.get("sha256")
    ):
        raise RegistryError("archive needs a url and a sha256")
    data = _read(str(archive["url"]))
    if hashlib.sha256(data).hexdigest() != archive["sha256"]:
        raise RegistryError("the archive's sha256 does not match the index")
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise RegistryError(f"the archive has an unsafe path {name!r}")
            if member.isdir():
                continue
            if not member.isfile():
                raise RegistryError(
                    f"the archive member {name!r} is not a regular file"
                )
            handle = tar.extractfile(member)
            if handle is None:
                raise RegistryError(f"the archive member {name!r} can't be read")
            out[name] = handle.read()
            if len(out[name]) > MAX_FILE_BYTES:
                raise RegistryError(f"{name} is larger than {MAX_FILE_BYTES} bytes")
    return out


def build_archive(bundle_dir: Path, out: Path) -> str:
    """Write ``bundle_dir`` as a reproducible ``.tar.gz``; returns its sha256."""

    import gzip
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(bundle_dir).as_posix())
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    packed = gzip.compress(buf.getvalue(), mtime=0)
    out.write_bytes(packed)
    return hashlib.sha256(packed).hexdigest()


def build_entry_files(bundle_dir: Path) -> dict[str, str]:
    """sha256 of every file in a bundle, for writing the index."""
    return {
        p.relative_to(bundle_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(bundle_dir.rglob("*"))
        if p.is_file()
    }


def _describe(entry: Entry) -> str:
    labels = {
        "example": "example, not certified",
        "certified": "certified",
        "planned": "planned, not available yet",
    }
    score = entry.raw.get("verifier_score")
    if score:
        cases = score["caught"] + score["missed"]
        clean = score["clean_right"] + score["false_alarm"]
        checked = f"blind: {score['caught']}/{cases} faults, {score['clean_right']}/{clean} clean"
    elif entry.status == "planned":
        checked = ""
    else:
        checked = "no blind score"
    return f"{entry.ref:34} {labels[entry.status]:24} {checked:32} {entry.title}"


def cmd_pull(args: Any) -> int:
    import sys

    try:
        if args.list or not args.workflow:
            for entry in load_index(args.index):
                print(_describe(entry))
            return 0
        entry = find(load_index(args.index), args.workflow)
        dest = pull(
            args.workflow,
            args.out,
            index_url=args.index,
            overwrite=args.force,
            allow_unsigned=getattr(args, "allow_unsigned", False),
            allow_test_key=getattr(args, "allow_test_key", False),
        )
    except RegistryError as exc:
        print(f"pull: {exc}", file=sys.stderr)
        return 1
    print(f"Pulled {entry.ref} into {dest} ({len(entry.files)} files, sha256 checked)")
    if entry.status != "certified":
        print(
            "Status: example, not certified. Rehearse it on a test copy of the app first."
        )
    print(
        "It won't run until it is qualified on your instance:\n"
        f"  openadapt-flow skill bind {dest} ...\n"
        f"  openadapt-flow skill qualify {dest} --certify clinical-write"
    )
    params = entry.raw.get("params") or []
    if params:
        print("Parameters: " + ", ".join(params))
    usage = entry.raw.get("usage")
    if usage:
        print(usage)
    return 0


def add_parser(sub: Any) -> None:
    p = sub.add_parser("pull", help="Download a packaged workflow bundle by name")
    p.add_argument(
        "workflow", nargs="?", help="Workflow id, for example openemr/new-referral"
    )
    p.add_argument(
        "--out", default=None, help="Destination directory (default: ./<name>)"
    )
    p.add_argument(
        "--index", default=DEFAULT_INDEX_URL, help="Registry index URL or path"
    )
    p.add_argument(
        "--force", action="store_true", help="Replace an existing destination"
    )
    p.add_argument("--list", action="store_true", help="List available workflows")
    p.add_argument(
        "--allow-test-key",
        action="store_true",
        help="Take an entry signed by the public TEST key (tests and local seeding)",
    )
    p.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="Take an unsigned entry (a local index you trust while authoring)",
    )
    p.set_defaults(func=cmd_pull)
