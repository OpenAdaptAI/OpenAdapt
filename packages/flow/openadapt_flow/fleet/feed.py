"""Where patches travel between tenants.

:class:`PatchFeed` is the only interface the rest of the package uses.
:class:`FilePatchFeed` keeps patches as JSON files in one directory, for the
local simulator and for air-gapped transfer. :class:`CloudPatchFeed` marks
where an OpenAdapt Cloud distribution service would plug in; it is not
implemented.

A feed is transport only. Nothing read from a feed is trusted: every patch
is verified again on import (:func:`openadapt_flow.fleet.intake.import_patch`)
against the receiving tenant's own pinned roots.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from openadapt_flow.fleet.patch import SignedStructuralPatch


class PatchFeed(Protocol):
    def publish(self, patch: SignedStructuralPatch) -> str:
        """Store a patch; return its id. Idempotent by patch id."""
        ...

    def list_patches(
        self, *, app_family: str, app_fingerprint_sha256: Optional[str] = None
    ) -> list[SignedStructuralPatch]:
        """Patches for one application family (and post-change version)."""
        ...


class FilePatchFeed:
    """A directory of ``<patch_id>.json`` files."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish(self, patch: SignedStructuralPatch) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{patch.patch_id}.json"
        if path.exists():
            return patch.patch_id
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".patch-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(patch.to_json())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
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


class CloudPatchFeed:
    """Placeholder for a Cloud-hosted feed. Not implemented.

    A Cloud feed would hold signed patches and the signer certificates'
    revocation list, and would serve them by application family. It would
    never sign patches for a tenant, never see a tenant bundle, and never
    decide promotion.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def publish(self, patch: SignedStructuralPatch) -> str:
        raise NotImplementedError("the Cloud patch feed is not built yet")

    def list_patches(
        self, *, app_family: str, app_fingerprint_sha256: Optional[str] = None
    ) -> list[SignedStructuralPatch]:
        raise NotImplementedError("the Cloud patch feed is not built yet")


__all__ = ["CloudPatchFeed", "FilePatchFeed", "PatchFeed"]
