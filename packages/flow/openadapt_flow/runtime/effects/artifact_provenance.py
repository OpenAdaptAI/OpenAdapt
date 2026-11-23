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

The producer is declared in configuration. Observations can lower it and
never raise it: a store root that overlaps a directory the acting session
writes to (``acting_roots``, the user's ``Downloads`` folder, or a Playwright
download folder) is graded ``acting_session`` whatever was declared.

The grade is also bound to each artifact a check reads. Under a declared
``separate_session`` or ``system_of_record`` producer, a scan that lists a
file inside a Playwright download folder beneath the store, or a file
whose real path leaves the store (a symlink), is refused (a declared acting
folder beneath the store already lowers the whole store when the verifier is
built):
the verifier reads the store as unreadable and the verdict is INDETERMINATE
with that reason (:meth:`ProvenanceGrade.acting_artifact`). The tier the
approval gate sees is fixed when the verifier is built, so a refused read is
the only honest outcome; it is never a confirmed read at a lower level.

A file the acting session saves straight into the store under an ordinary
name cannot be told apart this way. The evidence then says
``producer_source: declared``: the strong level rests on the configuration.

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
from dataclasses import dataclass, field
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
    #: The resolved local store, when it can be compared with acting folders.
    store: Optional[Path] = field(default=None, compare=False, repr=False)
    #: The resolved folders the acting session writes or downloads into.
    acting: tuple[Path, ...] = field(default=(), compare=False, repr=False)

    def acting_artifact(self, paths: Iterable[Path]) -> str:
        """Why one of ``paths`` cannot carry the declared producer, or ``""``.

        Applies only to a strong producer read from a local store: such a
        check must not rest on a file the acting session downloaded beneath
        the store, or on a file outside it. Weak producers are already graded
        ``same_screen`` and return ``""``.
        """

        if not self.independent or self.store is None:
            return ""
        for path in paths:
            real = _resolved(path)
            if any(p.startswith(_PLAYWRIGHT_DOWNLOAD_PREFIX) for p in real.parts):
                return "a file the check read is in a browser download folder"
            if not (real == self.store or real.is_relative_to(self.store)):
                return "a file the check read resolves outside the store"
            if any(real.is_relative_to(root) for root in self.acting):
                return (
                    "a file the check read is in a folder the acting session writes to"
                )
        return ""

    @property
    def source(self) -> str:
        """``observed`` when an observation lowered the declared producer,
        ``declared`` when the configuration's producer stands, ``none`` when
        nothing was declared. A declared producer is a claim by whoever wrote
        the configuration; nothing here confirms it."""

        if self.reason:
            return "observed"
        if self.declared is ArtifactProducer.UNKNOWN:
            return "none"
        return "declared"

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
            "producer_source": self.source,
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
    acting = tuple(_resolved(Path(candidate)) for candidate in candidates)
    for root_path in acting:
        if _overlaps(store, root_path):
            return ProvenanceGrade(
                declared=declared,
                producer=ArtifactProducer.ACTING_SESSION,
                reason="the store overlaps a folder the acting session writes to",
            )
    return ProvenanceGrade(
        declared=declared, producer=declared, store=store, acting=acting
    )


def with_provenance(verdict: Any, grade: ProvenanceGrade, refused: str = "") -> Any:
    """``verdict`` with the provenance block added to its ``read_evidence``.

    ``refused`` is the reason from :meth:`ProvenanceGrade.acting_artifact`
    when the last scan was refused. The verdict is then INDETERMINATE whatever
    it was, and the block records the producer as the acting session's.
    """

    evidence = dict(getattr(verdict, "read_evidence", None) or {})
    block = grade.evidence()
    update: dict[str, Any] = {}
    if refused:
        from openadapt_flow.runtime.effects.effect import Verdict

        block["producer"] = ArtifactProducer.ACTING_SESSION.value
        block["check_level"] = CheckLevel.SAME_SCREEN.label
        block["producer_source"] = "observed"
        block["lowered_because"] = refused
        update = {
            "verdict": Verdict.INDETERMINATE,
            "unavailable": False,
            "matched_records": [],
            "reason": (
                f"{refused}; the declared {grade.declared.value} producer cannot "
                "be confirmed for it, so the check is not counted (HALT)"
            ),
        }
    evidence["provenance"] = block
    update["read_evidence"] = evidence
    return verdict.model_copy(update=update)
