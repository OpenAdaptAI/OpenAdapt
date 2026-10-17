"""The versioned SKILL LIBRARY: skills as ordered, provenance-carrying versions.

A skill is a :class:`~openadapt_flow.ir.ProgramGraph` (plus any subflows), and a
skill EVOLVES: each learn cycle may append a new version. The library stores, per
skill id, an ORDERED list of versions -- each with its provenance (parent
version, the trace ids that induced it, when), a validation score, and a status
(``active`` / ``candidate`` / ``rolled_back`` / ``superseded``). Exactly ONE
version is ``active`` at a time; promoting a candidate retires the prior active
to ``superseded`` and never deletes it, so the full lineage of a skill is
auditable (the governed-promotion posture of PR #70, lifted from a single heal to
a whole program revision).

Persistence is a single JSON file per library root (``skills.json``): the schema
is Pydantic, so a version round-trips a real ``ProgramGraph`` verbatim. No model
calls, no I/O beyond the JSON file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import ProgramGraph
from openadapt_flow.learning.trace import ExecutionTrace

VersionStatus = Literal["active", "candidate", "rolled_back", "superseded"]

_LIBRARY_FILE = "skills.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Provenance(BaseModel):
    """Where a skill version came from -- the audit trail for a revision."""

    parent_version: Optional[int] = None
    #: Trace ids the inducer generalised over to produce this version.
    trace_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    note: str = ""


class SkillVersion(BaseModel):
    """One versioned revision of a skill's program.

    ``validation_score`` is the fraction of the validation (held-out) successful
    traces this version reproduced at the time it was evaluated; ``reason``
    records why a candidate was quarantined / rolled back, when applicable.
    """

    version: int
    graph: ProgramGraph
    subflows: dict[str, ProgramGraph] = Field(default_factory=dict)
    status: VersionStatus = "candidate"
    provenance: Provenance = Field(default_factory=Provenance)
    validation_score: float = 0.0
    reason: str = ""


class Skill(BaseModel):
    """A skill and its full ordered version history + accumulated trace corpus.

    The ``corpus`` is the running set of executions observed for this skill; the
    learn loop splits it into a fit set (given to the inducer) and a held-out set
    (used only to validate a candidate), so a revision is always tested on
    executions it was NOT fitted to.
    """

    skill_id: str
    versions: list[SkillVersion] = Field(default_factory=list)
    corpus: list[ExecutionTrace] = Field(default_factory=list)

    def active(self) -> Optional[SkillVersion]:
        for v in self.versions:
            if v.status == "active":
                return v
        return None

    def by_version(self, version: int) -> Optional[SkillVersion]:
        for v in self.versions:
            if v.version == version:
                return v
        return None

    def next_version_number(self) -> int:
        return 1 + max((v.version for v in self.versions), default=0)


class SkillLibrary:
    """Persistent, versioned store of skills (one JSON file per root).

    Load-or-create semantics: constructing a library over a root reads any
    existing ``skills.json`` there; every mutating call re-writes it, so the
    library is durable across processes (a learn cycle in one run, a promotion in
    the next).
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, Skill] = {}
        self._load()

    # -- persistence --------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.root / _LIBRARY_FILE

    def _load(self) -> None:
        if not self.path.is_file():
            return
        raw = json.loads(self.path.read_text())
        for sid, data in raw.get("skills", {}).items():
            self._skills[sid] = Skill.model_validate(data)

    def save(self) -> Path:
        payload = {"skills": {sid: s.model_dump() for sid, s in self._skills.items()}}
        self.path.write_text(json.dumps(payload, indent=2, default=str))
        return self.path

    # -- read ---------------------------------------------------------------

    def skill_ids(self) -> list[str]:
        return sorted(self._skills)

    def get(self, skill_id: str) -> Skill:
        if skill_id not in self._skills:
            raise KeyError(f"no skill {skill_id!r} in library {self.root}")
        return self._skills[skill_id]

    def has(self, skill_id: str) -> bool:
        return skill_id in self._skills

    def active_version(self, skill_id: str) -> Optional[SkillVersion]:
        return self.get(skill_id).active()

    # -- write --------------------------------------------------------------

    def create_skill(
        self,
        skill_id: str,
        graph: ProgramGraph,
        *,
        subflows: Optional[dict[str, ProgramGraph]] = None,
        provenance: Optional[Provenance] = None,
        validation_score: float = 1.0,
    ) -> SkillVersion:
        """Register a NEW skill with its first, immediately-active version."""
        if skill_id in self._skills:
            raise ValueError(f"skill {skill_id!r} already exists")
        version = SkillVersion(
            version=1,
            graph=graph,
            subflows=subflows or {},
            status="active",
            provenance=provenance or Provenance(note="bootstrap version"),
            validation_score=validation_score,
        )
        self._skills[skill_id] = Skill(skill_id=skill_id, versions=[version])
        self.save()
        return version

    def add_candidate(
        self,
        skill_id: str,
        graph: ProgramGraph,
        *,
        subflows: Optional[dict[str, ProgramGraph]] = None,
        provenance: Optional[Provenance] = None,
        validation_score: float = 0.0,
    ) -> SkillVersion:
        """Append a CANDIDATE version (not yet active) to a skill's history."""
        skill = self.get(skill_id)
        version = SkillVersion(
            version=skill.next_version_number(),
            graph=graph,
            subflows=subflows or {},
            status="candidate",
            provenance=provenance or Provenance(),
            validation_score=validation_score,
        )
        skill.versions.append(version)
        self.save()
        return version

    def promote(self, skill_id: str, version: int) -> SkillVersion:
        """Promote a candidate to ACTIVE, retiring the prior active to
        ``superseded`` (never deleted -- the lineage stays auditable)."""
        skill = self.get(skill_id)
        target = skill.by_version(version)
        if target is None:
            raise KeyError(f"skill {skill_id!r} has no version {version}")
        if target.status not in ("candidate", "active"):
            raise ValueError(
                f"cannot promote version {version} in status {target.status!r}"
            )
        for v in skill.versions:
            if v.status == "active" and v.version != version:
                v.status = "superseded"
        target.status = "active"
        self.save()
        return target

    def quarantine(self, skill_id: str, version: int, reason: str) -> SkillVersion:
        """Mark a candidate ``rolled_back`` with the reason it was refused; the
        active version is untouched (the governed-rejection path)."""
        skill = self.get(skill_id)
        target = skill.by_version(version)
        if target is None:
            raise KeyError(f"skill {skill_id!r} has no version {version}")
        target.status = "rolled_back"
        target.reason = reason
        self.save()
        return target

    def extend_corpus(self, skill_id: str, traces: list[ExecutionTrace]) -> None:
        """Append newly observed executions to a skill's running corpus."""
        self.get(skill_id).corpus.extend(traces)
        self.save()
