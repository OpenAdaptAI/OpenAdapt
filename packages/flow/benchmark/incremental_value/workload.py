"""The stream both arms run: ordinary referrals, blind fault cases, perturbations.

- Ordinary referrals come from the #1528 generator (``lifecycle.workload``),
  seed 2028, keeping only kinds that need no lifecycle injection: clean,
  namesakes, shared birthdays, a missing optional phone, names in capitals.
- Each blind fault case (``faultset6.yaml``, private) is one item about a new
  person from the same generator. The case's own ops run around it
  (``faults.py``).
- Each perturbation (:data:`PERTURBATIONS`) is 4 items.

The order is shuffled once with the seed and is the same for both arms.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from lifecycle.arm import WorkItem
from lifecycle.workload import _source_text, generate

SEED = 2028
N_ORDINARY = 50
ORDINARY_KINDS = {
    "clean",
    "ambiguous_new_person",
    "common_surname",
    "shared_dob",
    "missing_optional",
    "malformed_case",
}
#: Harness-written perturbations (not blind), 4 items each.
PERTURBATIONS = (
    "delayed_legit_write",
    "restart_after_save",
    "concurrent_staff_edit",
    "timeout_after_save",
)
PER_PERTURBATION = 4
#: Run 2 (PREREGISTRATION_2.md): run 1's mix at N = 300 with 80 fault cases.
RUN2 = {"seed": 2029, "n_ordinary": 168, "per_perturbation": 13}
#: Run 2's non-blocking slice: 30 items at run 1's density, 8 fault cases drawn
#: from the set by the seed, and 5 perturbations (the 4 kinds, then the first).
SLICE = {"seed": 2030, "n_ordinary": 17, "n_faults": 8, "n_perturbations": 5}


def build_slice(fault_ids: list[str], faultset_sha256: Optional[str] = None) -> "Stream":
    """The non-blocking slice's stream (PREREGISTRATION_2.md)."""

    rng = random.Random(SLICE["seed"])
    picked = rng.sample(sorted(fault_ids), SLICE["n_faults"])
    s = build(picked, faultset_sha256, SLICE["seed"], n_ordinary=SLICE["n_ordinary"], per_perturbation=2)
    pert = [i for i in s.items if i.kind == "perturbation"]
    keep = [next(i for i in pert if i.label == name and i.variant == 0) for name in PERTURBATIONS]
    keep.append(next(i for i in pert if i.label == PERTURBATIONS[0] and i.variant == 1))
    items = [i for i in s.items if i.kind != "perturbation" or i in keep]
    for n, it in enumerate(items, 1):
        it.seq = n
        it.item_id = f"sl-{n:03d}"
    return Stream(seed=SLICE["seed"], items=items, existing=s.existing, faultset_sha256=faultset_sha256)


@dataclass
class Item:
    seq: int
    item_id: str
    #: ordinary | fault | perturbation
    kind: str
    #: the ordinary generator kind, the fault case id, or the perturbation
    label: str
    params: dict[str, str]
    source_text: str
    person: dict[str, str]
    #: The chart the item should leave, or None. Fault cases may change it
    #: (``faults.py``: a case's prepare hook returns the new truth).
    intended: Optional[dict[str, str]]
    #: For perturbations: which occurrence (0-3), to vary timing.
    variant: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def work_item(self) -> WorkItem:
        return WorkItem(
            seq=self.seq,
            item_id=self.item_id,
            day=0,
            app="openemr",
            params=self.params,
            source_text=self.source_text,
        )


@dataclass
class Stream:
    seed: int
    items: list[Item]
    #: Charts that exist before item 1 (namesake targets and fault targets).
    existing: list[dict[str, str]]
    faultset_sha256: Optional[str]

    def sha256(self) -> str:
        payload = json.dumps(
            [asdict(i) for i in self.items] + self.existing, sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def build(
    fault_ids: list[str],
    faultset_sha256: Optional[str] = None,
    seed: int = SEED,
    *,
    n_ordinary: int = N_ORDINARY,
    per_perturbation: int = PER_PERTURBATION,
) -> Stream:
    wl = generate(seed, max(400, 4 * (n_ordinary + len(fault_ids) + 4 * per_perturbation)), 1)
    ordinary: list[Item] = []
    spare: list[dict[str, Any]] = []
    for w in wl.items:
        t = wl.truth[w.item_id]
        if t.exception in ORDINARY_KINDS and len(ordinary) < n_ordinary:
            ordinary.append(
                Item(0, "", "ordinary", t.exception, dict(w.params), w.source_text, dict(t.person), t.intended)
            )
        elif t.exception == "clean":
            spare.append({"person": dict(t.person)})
    need = len(fault_ids) + len(PERTURBATIONS) * per_perturbation
    if len(spare) < need:
        raise RuntimeError("not enough spare people for the stream")
    rest: list[Item] = []
    for case_id, s in zip(fault_ids, spare):
        p = s["person"]
        rest.append(Item(0, "", "fault", case_id, dict(p), _source_text(p), dict(p), dict(p)))
    k = len(fault_ids)
    for name in PERTURBATIONS:
        for v in range(per_perturbation):
            p = spare[k]["person"]
            k += 1
            rest.append(Item(0, "", "perturbation", name, dict(p), _source_text(p), dict(p), dict(p), variant=v))
    items = ordinary + rest
    random.Random(seed).shuffle(items)
    for n, it in enumerate(items, 1):
        it.seq = n
        it.item_id = f"iv-{n:03d}"
    return Stream(seed=seed, items=items, existing=list(wl.existing), faultset_sha256=faultset_sha256)


def save(stream: Stream, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "seed": stream.seed,
                "faultset_sha256": stream.faultset_sha256,
                "stream_sha256": stream.sha256(),
                "existing": stream.existing,
                "items": [asdict(i) for i in stream.items],
            },
            indent=1,
            sort_keys=True,
        )
    )


def load(path: Path) -> Stream:
    d = json.loads(path.read_text())
    return Stream(
        seed=d["seed"],
        items=[Item(**i) for i in d["items"]],
        existing=d["existing"],
        faultset_sha256=d.get("faultset_sha256"),
    )
