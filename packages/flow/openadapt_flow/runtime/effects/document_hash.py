"""Filesystem document-hash system-of-record :class:`EffectVerifier`.

A deliberately DIFFERENT verifier *type* from the HTTP substrates (:mod:`.rest`,
:mod:`.fhir`): here the system of record is a directory of written documents,
and the effect is checked with a content hash, not a network read. Many
consequential desktop workflows end in a document -- a generated PDF, an
exported report, a signed form dropped in an output folder -- and the truth of
"it was actually produced, once, with the right content" lives on the
filesystem, not on the screen that said "Export complete".

Records are the files under ``root`` matching ``glob``; each flattens to
``{"id", "name", "sha256", "size"}`` so the shared :func:`judge_records`
decides:

- ``record_written`` (``match={"name": ...}``, ``expected_count=1``): exactly
  one document exists for the key -- a duplicate export that wrote a second
  ``report (1).pdf`` is caught as ``observed_count > expected``;
- ``field_equals`` (``field="sha256"``, ``value=<expected digest>``): the
  document's bytes match the expected content -- a truncated / partial write
  is caught even though the file exists.

Fail-safe: a missing or unreadable ``root`` reads as *unreadable* ->
INDETERMINATE -> HALT (the export target being gone is never "no document
expected"). This substrate runs live in CI (no external service).

Strength follows who wrote the documents (:mod:`.artifact_provenance`): a store the
acting session wrote or downloaded into is at most ``same_screen``; only a
declared ``system_of_record`` producer whose store does not overlap the
acting session's folders reaches ``system_of_record``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Optional

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.adapter import VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier


def sha256_file(path: Path, *, chunk: int = 1 << 16) -> str:
    """Streaming SHA-256 hex digest of a file's bytes."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


class DocumentHashVerifier(VerifierAdapterBase):
    """Verify effects against a directory of written documents.

    Args:
        root: The document store directory (the system of record).
        glob: Glob (relative to ``root``, recursive patterns allowed) of the
            documents that count as records.
        session: Unused; present for protocol symmetry.
        producer: Who wrote the documents (an
            :class:`.artifact_provenance.ArtifactProducer` or its name). ``None`` is ``unknown``, graded like the acting
            session.
        acting_roots: Folders the acting session writes or downloads into; a
            store overlapping one is graded as the acting session's.
    """

    substrate = "fs"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        root: Path | str,
        *,
        glob: str = "*",
        session: Any = None,
        producer: Any = None,
        acting_roots: Optional[Iterable[Path | str]] = None,
    ) -> None:
        from openadapt_flow.runtime.effects.artifact_provenance import grade_artifact

        self.root = Path(root)
        self.glob = glob
        self.provenance = grade_artifact(
            producer, root=self.root, acting_roots=acting_roots
        )
        self.verification_tier = self.provenance.verification_tier
        self.independent_system_of_record = (
            self.verification_tier is VerificationTier.INDEPENDENT_SYSTEM
        )
        #: Why the last scan was refused (an acting-session artifact under a
        #: strong declared producer); empty when it was not.
        self._refused = ""

    def _scan(self) -> Optional[list[dict[str, Any]]]:
        """List the store's documents as flat records.

        Returns ``None`` (unreadable -> INDETERMINATE) when the store
        directory is absent or cannot be listed. An empty but present store
        returns ``[]`` (readable, no documents -- a real, judgeable state).
        """
        self._refused = ""
        if not self.root.is_dir():
            return None
        try:
            paths = sorted(p for p in self.root.glob(self.glob) if p.is_file())
        except OSError:
            return None
        self._refused = self.provenance.acting_artifact(paths)
        if self._refused:
            return None
        records: list[dict[str, Any]] = []
        for p in paths:
            try:
                digest = sha256_file(p)
                size = p.stat().st_size
            except OSError:
                return None  # a document we cannot read -> unreadable SoR
            records.append(
                {
                    "id": str(p.relative_to(self.root)),
                    "name": p.name,
                    "sha256": digest,
                    "size": size,
                }
            )
        return records

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._scan()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={
                "root": str(self.root),
                "glob": self.glob,
                "provenance": self.provenance.evidence(),
            },
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        from openadapt_flow.runtime.effects.artifact_provenance import with_provenance

        current = self._scan()
        verdict = judge_records(expected, before, current, substrate=self.substrate)
        return with_provenance(verdict, self.provenance, self._refused)
