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

A remote index must carry a Sigstore attestation from the registry release
workflow (:mod:`openadapt_flow.registry_attest`) naming its exact sha256, and
every archive's. The attested index carries ``issued_at``, ``expires_at`` and
a ``serial``: ``pull`` refuses an expired index and one whose serial is lower
than the highest it has seen from the same attesting identity, at any address
(``$OPENADAPT_HOME/registry-state.json``). ``--allow-unsigned`` and
``--allow-test-key`` apply only to an index on the local disk.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/"
    "packages/flow/registry/index.json"
)
STATUSES = ("example", "certified", "planned")
MAX_FILE_BYTES = 20 * 1024 * 1024
#: An archive's members, all together, and how many there may be.
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2000
#: An index's attestation bundle, at most.
MAX_ATTESTATION_BYTES = 1024 * 1024
#: The private tier's record check, at most.
MAX_FULL_CONTRACT_BYTES = 1024 * 1024
#: Hosts the private tier is fetched from; the bearer token goes nowhere else.
PRIVATE_TIER_HOSTS = frozenset({"registry.openadapt.ai"})
#: How long an index is valid from when the release workflow stamps it.
INDEX_LIFETIME = timedelta(days=30)
#: Clock skew allowed for an index's ``issued_at``.
_CLOCK_SKEW = timedelta(minutes=5)


class RegistryError(RuntimeError):
    """The index or a bundle failed a check. Nothing was written to ``out``."""


#: Domain separation for an entry's digest (``RegistryOrigin.entry_sha256``).
_ENTRY_DOMAIN = b"openadapt.registry-entry/v1\n"
#: The index fields that make an attested index fresh: when it was stamped,
#: when it expires and its place in the sequence.
INDEX_FRESHNESS_FIELDS = ("issued_at", "expires_at", "serial")

#: The files a public bundle may hold. Anything else is refused, so a
#: private-tier file can't slip in under another spelling.
PUBLIC_FILE_PATTERNS = (
    "workflow.json",
    "workflow.py",
    "manifest.json",
    "param_hygiene.json",
    "benchmark-contract.json",
    "skill.json",
    "verify.yaml",
    "README.md",
    "templates/*.png",
)

#: Private-tier files (source-policy.yaml: per-system oracle recipes and
#: fault suites), named in the refusal. The allowlist above is what decides.
PRIVATE_FILE_STEMS = frozenset(
    {"verify.full", "check_coverage", "faults", "spec", "witness"}
)
PRIVATE_FILE_PREFIXES = ("heldout", "fault-brief", "rows")


def public_bundle_file(rel: str) -> bool:
    """Whether a bundle path is one a public bundle may hold."""

    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in PUBLIC_FILE_PATTERNS)


def private_tier_file(rel: str) -> bool:
    """Whether a bundle path looks like a private-tier file, in any spelling."""

    name = Path(rel).name.lower()
    return name.rsplit(".", 1)[0] in PRIVATE_FILE_STEMS or name.startswith(
        PRIVATE_FILE_PREFIXES
    )


def entry_statement(raw: dict[str, Any]) -> bytes:
    """The canonical bytes of an entry, the record its digest names."""

    body = {k: v for k, v in raw.items() if k != "signature"}
    return _ENTRY_DOMAIN + json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def entry_sha256(raw: dict[str, Any]) -> str:
    return hashlib.sha256(entry_statement(raw)).hexdigest()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_index(
    index: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    lifetime: timedelta = INDEX_LIFETIME,
) -> dict[str, Any]:
    """Stamp ``issued_at``, ``expires_at`` and ``serial``, before attesting."""

    now = now or datetime.now(timezone.utc)
    index.update(
        issued_at=_stamp(now),
        expires_at=_stamp(now + lifetime),
        # Seconds since the epoch: it only ever grows from one release to the
        # next.
        serial=int(now.timestamp()),
    )
    return index


def _parse_stamp(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def index_freshness_problem(
    index: dict[str, Any], *, now: Optional[datetime] = None
) -> Optional[str]:
    """Why an attested index is stale or unstamped, or None."""

    issued = _parse_stamp(index.get("issued_at"))
    expires = _parse_stamp(index.get("expires_at"))
    serial = index.get("serial")
    if issued is None or expires is None or not isinstance(serial, int):
        return "the index has no valid issued_at, expires_at and serial"
    now = now or datetime.now(timezone.utc)
    if issued > now + _CLOCK_SKEW:
        return f"the index was issued in the future ({index['issued_at']})"
    if now >= expires:
        return f"the index expired at {index['expires_at']}"
    return None


def _state_path() -> Path:
    home = Path(os.environ.get("OPENADAPT_HOME", Path.home() / ".openadapt"))
    return home / "registry-state.json"


def _read_state() -> dict[str, Any]:
    try:
        state = json.loads(_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def identity_key(identity: Any, attested_by: Optional[str] = None) -> str:
    """The high-water key for whoever attested an index, whatever its address.

    Every genuine index comes from one release workflow on one serial
    sequence, so the mark belongs to that identity (issuer, repository and
    SAN pattern), not to the URL or path the copy was read from. A mirror or
    a local copy of an older release is then held to the same mark.
    """

    if attested_by and attested_by.startswith("test:"):
        return attested_by
    return "|".join(
        (identity.issuer, identity.source_repository_uri, identity.san.pattern)
    )


def serial_problem(key: str, serial: int) -> Optional[str]:
    """Why ``serial`` is older than the newest this identity has issued, or None."""

    seen = (_read_state().get("serials") or {}).get(key)
    if isinstance(seen, int) and serial < seen:
        return (
            f"the index's serial {serial} is older than {seen}, the newest seen "
            "from its publisher at any address: a stale or rolled-back copy"
        )
    return None


def record_serial(key: str, serial: int) -> None:
    """Keep the highest serial seen for this attesting identity."""

    state = _read_state()
    serials = state.setdefault("serials", {})
    if not isinstance(serials.get(key), int) or serial > serials[key]:
        serials[key] = serial
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)


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


def _version_key(version: Optional[str]) -> tuple[Any, ...]:
    """Semantic-version order: a pre-release sorts before its release.

    ``1.2.0-rc.1`` < ``1.2.0`` < ``1.10.0``; build metadata (``+...``) is
    ignored, as semver says.
    """

    core, _, pre = (version or "0").split("+", 1)[0].partition("-")
    numbers = []
    for piece in core.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while numbers and numbers[-1] == 0:
        numbers.pop()
    tags: tuple[tuple[int, Any], ...] = tuple(
        (0, int(tag)) if tag.isdigit() else (1, tag) for tag in pre.split(".") if tag
    )
    return (tuple(numbers), 0 if pre else 1, tags)


def _remote(location: str) -> bool:
    return location.startswith(("http://", "https://"))


def _read(
    location: str, *, limit: int = MAX_FILE_BYTES, local_root: Optional[Path] = None
) -> bytes:
    """Bytes at an https URL or a local path, at most ``limit`` of them.

    Plain http is refused. A local path is read only under ``local_root``
    when one is given (the directory of a local index).
    """

    if location.startswith("http://"):
        raise RegistryError(f"{location}: plain http is refused; use https")
    if location.startswith("https://"):
        import httpx

        with httpx.stream("GET", location, timeout=30.0, follow_redirects=True) as resp:
            if resp.url.scheme != "https":
                raise RegistryError(f"{location} redirected off https")
            if resp.status_code != 200:
                raise RegistryError(f"GET {location} returned {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise RegistryError(f"{location} is larger than {limit} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
    path = Path(location)
    if local_root is not None and not path.resolve().is_relative_to(
        local_root.resolve()
    ):
        raise RegistryError(f"{location} is outside the index's directory")
    if path.stat().st_size > limit:
        raise RegistryError(f"{location} is larger than {limit} bytes")
    return path.read_bytes()


def _join(base: str, rel: str) -> str:
    if _remote(base):
        return urljoin(base, rel)
    return str(Path(base).parent / rel)


_SAFE_REL = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/"
)


def _safe_rel(rel: Any) -> bool:
    """A relative path that stays put when joined to a URL or a directory."""

    return (
        isinstance(rel, str)
        and bool(rel)
        and set(rel) <= _SAFE_REL
        and not rel.startswith("/")
        and "//" not in rel
        and ".." not in rel.split("/")
    )


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
        if "path" in item and not _safe_rel(str(item["path"]).rstrip("/")):
            raise RegistryError(f"{wid}: unsafe path {item.get('path')!r}")
        for rel in item.get("files") or {}:
            if not _safe_rel(rel):
                raise RegistryError(f"{wid}: unsafe path {rel!r}")
            if private_tier_file(rel):
                raise RegistryError(
                    f"{wid}: {rel!r} belongs to the private tier, never a public bundle"
                )
            if not public_bundle_file(rel):
                raise RegistryError(
                    f"{wid}: {rel!r} is not a file a public bundle may hold "
                    f"({', '.join(PUBLIC_FILE_PATTERNS)})"
                )
        if status != "planned" and any(
            rel.startswith("templates/") for rel in item.get("files") or {}
        ):
            screens = item.get("screens")
            if not isinstance(screens, dict) or screens.get("data") != "synthetic":
                raise RegistryError(
                    f"{wid}: its screen templates need the attestation "
                    "screens.data = 'synthetic' (crops of a copy with made-up data)"
                )
        full = item.get("full_contract")
        if full is not None:
            if not isinstance(full, dict) or set(full) != {"url", "sha256"}:
                raise RegistryError(
                    f"{wid}: full_contract takes exactly a url and a sha256"
                )
            problem = private_tier_url_problem(str(full["url"]))
            if problem:
                raise RegistryError(f"{wid}: full_contract {problem}")
        archive = item.get("archive")
        if archive is not None and (
            not isinstance(archive, dict)
            or not archive.get("url")
            or not archive.get("sha256")
        ):
            raise RegistryError(f"{wid}: archive needs a url and a sha256")
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


def private_tier_url_problem(url: str) -> Optional[str]:
    """Why ``url`` may not receive the private tier's token, or None."""

    parts = urlsplit(url)
    if parts.scheme != "https":
        return f"must be https, not {url!r}"
    if parts.username or parts.password or parts.port not in (None, 443):
        return f"must be a plain https address, not {url!r}"
    if (parts.hostname or "") not in PRIVATE_TIER_HOSTS:
        return (
            f"host {parts.hostname!r} is not a private-tier host "
            f"({', '.join(sorted(PRIVATE_TIER_HOSTS))})"
        )
    return None


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


def _read_attestation(index_url: str) -> Optional[Any]:
    """The index's attestation; None only when a local index has none."""

    from openadapt_flow.registry_attest import ATTESTATION_NAME

    location = _join(index_url, ATTESTATION_NAME)
    if not _remote(index_url) and not Path(location).exists():
        return None
    try:
        return json.loads(_read(location, limit=MAX_ATTESTATION_BYTES))
    except (RegistryError, OSError, json.JSONDecodeError) as exc:
        raise RegistryError(
            f"the index's attestation ({ATTESTATION_NAME}) can't be read: {exc}"
        ) from exc


def pull(
    workflow_id: str,
    out: Optional[Path | str] = None,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    overwrite: bool = False,
    allow_unsigned: bool = False,
    allow_test_key: bool = False,
    identity: Optional[Any] = None,
    trusted_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Download and verify a bundle into ``out`` (default ``./<name>``).

    Before anything is downloaded:

    - the index's attestation (``index.sigstore.json``) must hold for the
      pinned release-workflow identity and name the exact sha256 of the index
      that was read;
    - the attested index must be unexpired, with a serial never lower than
      the highest seen from the same attesting identity, whether it was read
      from the default address, a mirror or the local disk.

    An archive must be one the attestation names too. ``allow_unsigned``
    takes an index with no attestation, and ``allow_test_key`` one attested by
    the local test signer; both only from the local disk (an index you are
    authoring). An attestation that is present must always hold.
    """
    from openadapt_flow import registry_attest

    remote = _remote(index_url)
    if remote and not index_url.startswith("https://"):
        raise RegistryError(f"{index_url}: plain http is refused; use https")
    for flag, used in (
        ("--allow-unsigned", allow_unsigned),
        ("--allow-test-key", allow_test_key),
    ):
        if used and remote:
            raise RegistryError(
                f"{flag} applies only to an index on the local disk, never to "
                f"{index_url}"
            )
    local_root = None if remote else Path(index_url).resolve().parent
    raw_index = _read(index_url)
    try:
        index_data = json.loads(raw_index)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"index is not JSON: {exc}") from exc
    entry = find(validate_index(index_data), workflow_id)
    if entry.status == "planned":
        raise RegistryError(f"{workflow_id} is planned and has no bundle yet")
    attestation = _read_attestation(index_url)
    attested_by: Optional[str] = None
    subjects: Optional[dict[str, str]] = None
    serial_key: Optional[str] = None
    if attestation is None:
        if remote or not allow_unsigned:
            raise RegistryError(
                f"the index has no attestation ({registry_attest.ATTESTATION_NAME})"
            )
    else:
        pinned = identity or registry_attest.REGISTRY_IDENTITY
        try:
            attested_by, subjects = registry_attest.verify(
                attestation,
                allow_test_key=allow_test_key and not remote,
                identity=pinned,
                trusted_root=trusted_root,
            )
        except registry_attest.AttestationError as exc:
            raise RegistryError(str(exc)) from exc
        if (
            subjects.get(registry_attest.INDEX_SUBJECT)
            != hashlib.sha256(raw_index).hexdigest()
        ):
            raise RegistryError("the attestation does not name this index")
        problem = index_freshness_problem(index_data, now=now)
        serial_key = identity_key(pinned, attested_by)
        if problem is None:
            problem = serial_problem(serial_key, index_data["serial"])
        if problem is not None:
            raise RegistryError(problem)
    dest = Path(out) if out else Path(entry.id.split("/", 1)[1])
    if dest.exists() and not overwrite:
        raise RegistryError(f"{dest} exists; pass --force to replace it")
    base = _join(index_url, entry.raw["path"].rstrip("/") + "/")
    archive = entry.raw.get("archive")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "bundle"
        if (
            archive
            and subjects is not None
            and archive.get("sha256") not in set(subjects.values())
        ):
            raise RegistryError("the attestation does not name this entry's archive")
        unpacked = (
            _unpack_archive(archive, expected=set(entry.files), local_root=local_root)
            if archive
            else None
        )
        for rel, expected in sorted(entry.files.items()):
            if unpacked is not None:
                if rel not in unpacked:
                    raise RegistryError(f"{rel} is missing from the archive")
                data = unpacked[rel]
            else:
                data = _read(
                    urljoin(base, rel) if remote else str(Path(base) / rel),
                    local_root=local_root,
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
        mark_pulled(
            stage,
            entry,
            index_url=index_url,
            attested_by=attested_by,
            index_serial=index_data.get("serial") if subjects is not None else None,
            index_issued_at=(
                index_data.get("issued_at") if subjects is not None else None
            ),
        )
        if unpacked is not None and NOTICE_NAME in unpacked:
            (stage / NOTICE_NAME).write_bytes(unpacked[NOTICE_NAME])
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, dest)
    if serial_key is not None:
        record_serial(serial_key, index_data["serial"])
    return dest


#: The license notice a registry archive carries beside the bundle's files.
NOTICE_NAME = "NOTICE"


def _unpack_archive(
    archive: Any, *, expected: set[str], local_root: Optional[Path] = None
) -> dict[str, bytes]:
    """The files of a bundle published as a release asset (``.tar.gz``).

    The signed entry pins the archive's sha256. Its URL must be https (or,
    for a local index, a path inside the index's directory). Members must be
    regular files at safe relative paths, each one the entry lists (or the
    license notice), with sizes checked before anything is read.
    """

    import io
    import tarfile

    if (
        not isinstance(archive, dict)
        or not archive.get("url")
        or not archive.get("sha256")
    ):
        raise RegistryError("archive needs a url and a sha256")
    url = str(archive["url"])
    if not url.startswith("https://") and local_root is None:
        raise RegistryError(f"the archive must be at an https address, not {url!r}")
    if not url.startswith("https://") and local_root is not None:
        url = str(local_root / url)  # an absolute path stays as it is
    data = _read(url, limit=MAX_ARCHIVE_BYTES, local_root=local_root)
    if hashlib.sha256(data).hexdigest() != archive["sha256"]:
        raise RegistryError("the archive's sha256 does not match the index")
    out: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for count, member in enumerate(tar, start=1):
            if count > MAX_ARCHIVE_MEMBERS:
                raise RegistryError(
                    f"the archive has more than {MAX_ARCHIVE_MEMBERS} members"
                )
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise RegistryError(f"the archive has an unsafe path {name!r}")
            if member.isdir():
                continue
            if not member.isfile():
                raise RegistryError(
                    f"the archive member {name!r} is not a regular file"
                )
            if name not in expected and name != NOTICE_NAME:
                raise RegistryError(f"the archive member {name!r} is not in the entry")
            if name in out:
                raise RegistryError(f"the archive has {name!r} twice")
            # Sizes are checked from the header, before anything is read.
            if member.size > MAX_FILE_BYTES:
                raise RegistryError(f"{name} is larger than {MAX_FILE_BYTES} bytes")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise RegistryError(
                    f"the archive unpacks to more than {MAX_ARCHIVE_BYTES} bytes"
                )
            handle = tar.extractfile(member)
            if handle is None:
                raise RegistryError(f"the archive member {name!r} can't be read")
            out[name] = handle.read(member.size)
    return out


def license_notice(entry: dict[str, Any]) -> str:
    """The notice a registry archive carries for the app the skill drives."""

    app = entry.get("app") or entry.get("id", "the app")
    license_name = entry.get("app_license") or "its own license"
    return (
        f"This bundle describes a task in {app}, which is distributed under "
        f"{license_name}. It contains none of that app's code. Its screen "
        "templates are crops of the app's rendered screens, taken on a copy "
        "holding made-up data.\n"
    )


def build_archive(bundle_dir: Path, out: Path, *, notice: Optional[str] = None) -> str:
    """Write ``bundle_dir`` as a reproducible ``.tar.gz``; returns its sha256."""

    import gzip
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        members = [
            (path.relative_to(bundle_dir).as_posix(), path.read_bytes())
            for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file())
        ]
        if notice is not None:
            members.append((NOTICE_NAME, notice.encode("utf-8")))
        for name, data in members:
            info = tarfile.TarInfo(name)
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
        if getattr(args, "refresh_trust", False):
            from openadapt_flow import registry_attest

            try:
                path = registry_attest.refresh_trusted_root()
            except Exception as exc:  # noqa: BLE001 - report any TUF failure
                print(
                    f"pull: refreshing the Sigstore trusted root failed: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(f"Sigstore trusted root refreshed: {path}")
            return 0
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
    from openadapt_flow.cli_hints import command

    print(
        "It won't run until it is qualified on your instance:\n"
        f"  {command(f'skill bind {dest} ...')}\n"
        f"  {command(f'skill qualify {dest} --certify clinical-write')}"
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
        "--allow-unsigned",
        action="store_true",
        help="Take an index with no attestation from the local disk (one you are "
        "authoring); refused for a remote index",
    )
    p.add_argument(
        "--allow-test-key",
        action="store_true",
        help="Take an index attested by the local test signer, from the local disk "
        "only; refused for a remote index",
    )
    p.add_argument(
        "--refresh-trust",
        action="store_true",
        help="Fetch Sigstore's current trusted root through its TUF repository "
        "(into $OPENADAPT_HOME) and exit",
    )
    p.set_defaults(func=cmd_pull)
