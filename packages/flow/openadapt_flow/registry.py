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
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

DEFAULT_INDEX_URL = "https://raw.githubusercontent.com/OpenAdaptAI/openadapt-flow/main/registry/index.json"
STATUSES = ("example", "certified", "planned")
MAX_FILE_BYTES = 20 * 1024 * 1024


class RegistryError(RuntimeError):
    """The index or a bundle failed a check. Nothing was written to ``out``."""


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    status: str
    raw: dict[str, Any]

    @property
    def files(self) -> dict[str, str]:
        return dict(self.raw.get("files") or {})


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
        if "/" not in wid or wid in seen:
            raise RegistryError(f"bad or duplicate workflow id {wid!r}")
        seen.add(wid)
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
        entries.append(Entry(wid, item.get("title", wid), status, item))
    return entries


def load_index(index_url: str = DEFAULT_INDEX_URL) -> list[Entry]:
    try:
        data = json.loads(_read(index_url))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"index is not JSON: {exc}") from exc
    return validate_index(data)


def find(entries: list[Entry], workflow_id: str) -> Entry:
    for entry in entries:
        if entry.id == workflow_id:
            return entry
    known = ", ".join(e.id for e in entries)
    raise RegistryError(f"no workflow {workflow_id!r} in the registry (known: {known})")


def pull(
    workflow_id: str,
    out: Optional[Path | str] = None,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    overwrite: bool = False,
) -> Path:
    """Download and verify a bundle into ``out`` (default ``./<name>``)."""
    entry = find(load_index(index_url), workflow_id)
    if entry.status == "planned":
        raise RegistryError(f"{workflow_id} is planned and has no bundle yet")
    dest = Path(out) if out else Path(workflow_id.split("/", 1)[1])
    if dest.exists() and not overwrite:
        raise RegistryError(f"{dest} exists; pass --force to replace it")
    base = _join(index_url, entry.raw["path"].rstrip("/") + "/")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "bundle"
        for rel, expected in sorted(entry.files.items()):
            data = _read(
                urljoin(base, rel) if base.startswith("http") else str(Path(base) / rel)
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
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, dest)
    return dest


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
    return f"{entry.id:28} {labels[entry.status]:24} {entry.title}"


def cmd_pull(args: Any) -> int:
    import sys

    try:
        if args.list or not args.workflow:
            for entry in load_index(args.index):
                print(_describe(entry))
            return 0
        entry = find(load_index(args.index), args.workflow)
        dest = pull(args.workflow, args.out, index_url=args.index, overwrite=args.force)
    except RegistryError as exc:
        print(f"pull: {exc}", file=sys.stderr)
        return 1
    print(f"Pulled {entry.id} into {dest} ({len(entry.files)} files, sha256 checked)")
    if entry.status != "certified":
        print(
            "Status: example, not certified. Rehearse it on a test copy of the app first."
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
    p.set_defaults(func=cmd_pull)
