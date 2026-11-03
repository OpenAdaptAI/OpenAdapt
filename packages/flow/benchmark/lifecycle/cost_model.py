"""The operator cost model: how many seconds each kind of human touch is charged.

Versioned. A change to any number is a new version with a new key, never an
edit of an old one, so every results ledger names the exact numbers it was
charged with. The report shows every result at x0.5 and x2 of these numbers,
because none of them is measured from real operators yet (see CALIBRATION).

What each charge covers:

- ``approval``: read an approval card (the item's values and the save it will
  make) and press Approve or Deny.
- ``stop``: read a stop's reason, look at the item in the app, and decide
  (resume, skip, or do it by hand). Doing it by hand is charged separately.
- ``reconcile``: the product could not tell whether a save landed. Open the
  app, find the record, compare it with the item, answer.
- ``repair_review``: read a proposed repair (the changed step, before and
  after screens, probe results) and approve or reject it.
- ``question``: answer a free-text question from an agent.
- ``manual_entry``: do one item entirely by hand in the app (the all-manual
  baseline, and the fallback for any item automation leaves undone).
- ``interrupt``: the resumption cost of any interruption, added once per
  operator request (switching from other work to the queue and back).
- Setup and maintenance phases are charged at the wall time a person is
  attending them, plus fixed preparation where a phase needs it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

CALIBRATION = (
    "No timings from real operators exist yet. Desktop and the console record "
    "when a request was raised and when it was answered, but every answer on "
    "record was given by a script, so those gaps measure the harness, not a "
    "person. Until Desktop has real operator telemetry, the per-touch numbers "
    "below are assumptions: keystroke-level estimates for reading a card and "
    "clicking (approval), plus looking a record up in the app (stop, "
    "reconcile), and a typed-entry estimate for a 12-field registration form "
    "(manual entry: about 120 characters at 40 words a minute, plus reading "
    "the referral and the duplicate search). The interrupt charge is the "
    "resumption lag reported in interruption studies (tens of seconds)."
)


@dataclass(frozen=True)
class CostModel:
    version: str
    approval_s: float
    stop_s: float
    reconcile_s: float
    repair_review_s: float
    question_s: float
    manual_entry_s: float
    interrupt_s: float
    #: Preparing a demonstration (pick the example, open the app) before the
    #: recorded wall time is charged.
    teach_prep_s: float
    #: Reviewing a compiled program's parameters and steps before admission.
    compile_review_s: float
    #: Signing an admission once qualification passed.
    admit_s: float

    def per_request(self, kind: str) -> float:
        """Charged seconds for one operator request of ``kind``."""

        base = {
            "approval": self.approval_s,
            "stop": self.stop_s,
            "reconcile": self.reconcile_s,
            "repair_review": self.repair_review_s,
            "question": self.question_s,
            "manual_entry": self.manual_entry_s,
        }[kind]
        return base + self.interrupt_s

    def scaled(self, factor: float) -> "CostModel":
        """Every charge times ``factor`` (the sensitivity bands)."""

        fields = {
            k: v * factor
            for k, v in asdict(self).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return replace(self, version=f"{self.version}x{factor:g}", **fields)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "calibration": CALIBRATION}


V1 = CostModel(
    version="lifecycle-cost/2026-10-20.1",
    approval_s=20.0,
    stop_s=90.0,
    reconcile_s=120.0,
    repair_review_s=120.0,
    question_s=60.0,
    manual_entry_s=180.0,
    interrupt_s=30.0,
    teach_prep_s=300.0,
    compile_review_s=600.0,
    admit_s=300.0,
)

MODELS = {V1.version: V1}
BANDS = (0.5, 1.0, 2.0)
