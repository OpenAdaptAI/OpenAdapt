"""Who produced a file, PDF or email, and how strong a check of it can be.

A file check (:mod:`.file_arrival`, :mod:`.document_hash`,
:mod:`.document_arrival`) or a mail-store check (:mod:`.email_delivery`) reads
an artifact. How much that read proves depends on who produced the artifact,
not on the fact that it is a file:

- ``acting_session``: the session that made the change wrote or downloaded
  the artifact (an export saved by the acting app, a PDF the acting browser
  downloaded). The read can only repeat what the acting session already saw.
  At most check level 1, ``same_screen``.
- ``separate_session``: a separately signed-in, read-only session fetched the
  artifact from the application. At most level 3, ``separate_session``.
- ``system_of_record``: the system wrote it without the acting session, for
  example an export job dropping a file on an SFTP endpoint, or the mail
  server writing what it delivered to a capture store. At most level 4,
  ``system_of_record``.
- ``unknown``: nobody said. Graded like ``acting_session``.

The producer is declared in configuration. Two observations can lower it and
never raise it: a store root that overlaps a directory the acting session
writes to (``acting_roots``, the user's ``Downloads`` folder, or a Playwright
download folder) is graded ``acting_session`` whatever was declared.

The verifiers record the result in ``EffectVerdict.read_evidence["provenance"]``
and in the state ``detail``. The record names the producer and the level, not
paths or values.

This module imports only the standard library and other effects modules that
the verifiers already load. The verifiers import it inside ``__init__`` so the
import-time budgets of ``openadapt_flow.ir`` and ``openadapt_flow.receipt``
do not grow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from openadapt_flow.check_level import CheckLevel
from openadapt_flow.verification import VerificationTier

__all__ = [
    "ArtifactProducer",
    "ProvenanceGrade",
    "default_acting_roots",
    "grade_artifact",
    "parse_producer",
    "with_provenance",
]


class ArtifactProducer(str, Enum):
    """Who produced the artifact a file or mail check reads."""

    ACTING_SESSION = "acting_session"
    SEPARATE_SESSION = "separate_session"
    SYSTEM_OF_RECORD = "system_of_record"
    UNKNOWN = "unknown"


#: The strongest tier a check can carry for each producer.
_TIER_CAP: dict[ArtifactProducer, VerificationTier] = {
    ArtifactProducer.ACTING_SESSION: VerificationTier.IMMEDIATE_SCREEN,
    ArtifactProducer.UNKNOWN: VerificationTier.IMMEDIATE_SCREEN,
    ArtifactProducer.SEPARATE_SESSION: VerificationTier.INDEPENDENT_SESSION,
    ArtifactProducer.SYSTEM_OF_RECORD: VerificationTier.INDEPENDENT_SYSTEM,
}

#: Folder-name prefix Playwright gives the temporary folder it downloads into.
_PLAYWRIGHT_DOWNLOAD_PREFIX = "playwright-artifacts-"


def parse_producer(value: Any) -> ArtifactProducer:
    """The producer named by ``value``; ``None`` or empty is ``unknown``.

    Raises ``ValueError`` for any other name, so a typo cannot pass as a
    strong producer.
    """

    if value is None:
        return ArtifactProducer.UNKNOWN
    if isinstance(value, ArtifactProducer):
        return value
    if isinstance(value, str):
        key = value.strip().lower().replace("-", "_")
        if not key:
            return ArtifactProducer.UNKNOWN
        try:
            return ArtifactProducer(key)
        except ValueError:
            pass
    names = ", ".join(p.value for p in ArtifactProducer)
    raise ValueError(f"artifact producer must be one of {names}, not {value!r}")


def default_acting_roots() -> list[Path]:
    """Folders an acting browser downloads into by default.

    The user's ``Downloads`` folder. Playwright download folders are matched
    by name in :func:`grade_artifact`, since their location varies.
    """

    roots: list[Path] = []
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home:
        roots.append(Path(home) / "Downloads")
    return roots


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return path.expanduser().absolute()


def _overlaps(a: Path, b: Path) -> bool:
    """Whether one path is the other or contains it."""

    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


@dataclass(frozen=True)
class ProvenanceGrade:
    """The producer a check ends up graded by, and the tier it allows."""

    #: The producer the configuration declared (``unknown`` when none).
    declared: ArtifactProducer
    #: The producer the check is graded by, after observations.
    producer: ArtifactProducer
    #: Why ``producer`` differs from ``declared``; empty when it does not.
    reason: str = ""

    @property
    def verification_tier(self) -> VerificationTier:
        """The strongest tier a confirmed check of this artifact can carry."""

        return _TIER_CAP[self.producer]

    @property
    def check_level(self) -> CheckLevel:
        return self.verification_tier.check_level

    @property
    def independent(self) -> bool:
        """Whether the read is independent of the acting session."""

        return self.check_level >= CheckLevel.SEPARATE_SESSION

    def evidence(self) -> dict[str, Any]:
        """The ``provenance`` block for evidence: no paths, no values."""

        block: dict[str, Any] = {
            "producer": self.producer.value,
            "declared_producer": self.declared.value,
            "check_level": self.check_level.label,
        }
        if self.reason:
            block["lowered_because"] = self.reason
        return block


def grade_artifact(
    producer: Any,
    *,
    root: Optional[Path | str],
    acting_roots: Optional[Iterable[Path | str]] = None,
    observe_local: bool = True,
) -> ProvenanceGrade:
    """Grade the artifacts under ``root`` by who produced them.

    Args:
        producer: The declared producer (an :class:`ArtifactProducer` or its
            name; ``None`` is ``unknown``).
        root: The local folder the check reads. Ignored when
            ``observe_local`` is False (a remote SFTP folder).
        acting_roots: Folders the acting session writes or downloads into.
            The defaults from :func:`default_acting_roots` are always added.
        observe_local: Whether ``root`` is a local path that can be compared
            with the acting folders.
    """

    declared = parse_producer(producer)
    if declared in (ArtifactProducer.ACTING_SESSION, ArtifactProducer.UNKNOWN):
        return ProvenanceGrade(declared=declared, producer=declared)
    if not observe_local or root is None:
        return ProvenanceGrade(declared=declared, producer=declared)
    store = _resolved(Path(root))
    if any(part.startswith(_PLAYWRIGHT_DOWNLOAD_PREFIX) for part in store.parts):
        return ProvenanceGrade(
            declared=declared,
            producer=ArtifactProducer.ACTING_SESSION,
            reason="the store is a browser download folder",
        )
    candidates: Sequence[Path | str] = [
        *default_acting_roots(),
        *(acting_roots or ()),
    ]
    for candidate in candidates:
        if _overlaps(store, _resolved(Path(candidate))):
            return ProvenanceGrade(
                declared=declared,
                producer=ArtifactProducer.ACTING_SESSION,
                reason="the store overlaps a folder the acting session writes to",
            )
    return ProvenanceGrade(declared=declared, producer=declared)


def with_provenance(verdict: Any, grade: ProvenanceGrade) -> Any:
    """``verdict`` with the provenance block added to its ``read_evidence``."""

    evidence = dict(getattr(verdict, "read_evidence", None) or {})
    evidence["provenance"] = grade.evidence()
    return verdict.model_copy(update={"read_evidence": evidence})
