"""Where patches and fleet status travel between the hub and tenants.

:class:`PatchFeed` is the only interface tenants use. :class:`FilePatchFeed`
keeps documents as JSON files in one directory, for the local simulator and
for air-gapped transfer. :class:`CloudPatchFeed` marks where an OpenAdapt
Cloud distribution service would plug in; it is not implemented, and every
status read from it fails, so a tenant pointed at it never auto-promotes.

A feed is transport only. Nothing read from a feed is trusted: every patch
is verified again on import (:func:`openadapt_flow.fleet.intake.import_patch`)
and every status document by
:func:`openadapt_flow.fleet.status.read_fleet_status`, against the receiving
tenant's own pinned roots. Contributions never go through the feed; they go
to the hub.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from openadapt_flow.fleet.patch import SignedStructuralPatch
from openadapt_flow.fleet.status import (
    FleetStatusUnavailable,
    SignedKillSwitch,
    SignedRevocationList,
)


class PatchFeed(Protocol):
    def publish(self, patch: SignedStructuralPatch) -> str:
        """Store a patch; return its id. Idempotent by patch id."""
        ...

    def list_patches(
        self, *, app_family: str, app_fingerprint_sha256: Optional[str] = None
    ) -> list[SignedStructuralPatch]:
        """Patches for one application family (and post-change version)."""
        ...

    def revocation_list(self) -> SignedRevocationList: ...

    def kill_switches(self) -> list[SignedKillSwitch]: ...


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".feed-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class FilePatchFeed:
    """A directory: ``<patch_id>.json`` plus ``status/``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def status_dir(self) -> Path:
        return self.root / "status"

    def publish(self, patch: SignedStructuralPatch) -> str:
        path = self.root / f"{patch.patch_id}.json"
        if not path.exists():
            _write(path, patch.to_json())
        return patch.patch_id

    def list_patches(
        self, *, app_family: str, app_fingerprint_sha256: Optional[str] = None
    ) -> list[SignedStructuralPatch]:
        if not self.root.is_dir():
            return []
        found = []
        for path in sorted(self.root.glob("*.json")):
            try:
                patch = SignedStructuralPatch.from_json(path.read_text())
            except ValueError:
                # A malformed file is skipped here and could never verify.
                continue
            after = patch.payload.app_after
            if after.app_family != app_family:
                continue
            if (
                app_fingerprint_sha256 is not None
                and after.fingerprint_sha256() != app_fingerprint_sha256
            ):
                continue
            found.append(patch)
        return found

    # -- status ----------------------------------------------------------------

    def publish_revocation_list(self, doc: SignedRevocationList) -> None:
        _write(self.status_dir / "revocation-list.json", doc.model_dump_json(indent=2))

    def publish_kill_switch(self, doc: SignedKillSwitch) -> None:
        name = f"kill-switch-{doc.payload.sequence:08d}.json"
        _write(self.status_dir / name, doc.model_dump_json(indent=2))

    def revocation_list(self) -> SignedRevocationList:
        path = self.status_dir / "revocation-list.json"
        if not path.is_file():
            raise FleetStatusUnavailable("the feed has no revocation list")
        return SignedRevocationList.model_validate_json(path.read_text())

    def kill_switches(self) -> list[SignedKillSwitch]:
        if not self.status_dir.is_dir():
            return []
        # A malformed entry raises, and the status reader fails closed.
        return [
            SignedKillSwitch.model_validate_json(path.read_text())
            for path in sorted(self.status_dir.glob("kill-switch-*.json"))
        ]


class CloudPatchFeed:
    """Placeholder for a Cloud-hosted feed. Not implemented.

    A Cloud feed would serve hub-signed patches by application family, the
    current revocation list, and kill switches. It would never see a tenant
    bundle and never decide promotion.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def publish(self, patch: SignedStructuralPatch) -> str:
        raise NotImplementedError("the Cloud patch feed is not built yet")

    def list_patches(
        self, *, app_family: str, app_fingerprint_sha256: Optional[str] = None
    ) -> list[SignedStructuralPatch]:
        raise NotImplementedError("the Cloud patch feed is not built yet")

    def revocation_list(self) -> SignedRevocationList:
        raise FleetStatusUnavailable("the Cloud patch feed is not built yet")

    def kill_switches(self) -> list[SignedKillSwitch]:
        raise FleetStatusUnavailable("the Cloud patch feed is not built yet")


__all__ = ["CloudPatchFeed", "FilePatchFeed", "PatchFeed"]
