"""A self-contained payments target with a deliberately field-only scorer.

Used by the tests and as the smallest worked example of an adapter. The
``strict`` scorer checks the exact set of new payments and that they existed
when the agent claimed success; the default scorer checks only that a
matching payment exists, the pattern the AppWorld audit found.
"""

from __future__ import annotations

from typing import Any, Sequence

from openadapt_flow.verifier_audit.model import (
    Call,
    CallKind,
    Episode,
    Verdict,
    canonical_effect,
)

ACCOUNTS = ("alice", "bob", "carol", "dave")
TASKS = {
    "pay-bob": [("bob", 10, "lunch")],
    "pay-carol-and-dave": [("carol", 5, "coffee"), ("dave", 7, "taxi")],
    "pay-alice": [("alice", 3, "snack")],
}


class ToyPayments:
    name = "toy-payments"

    def __init__(self, strict: bool = False) -> None:
        self.strict = bool(strict) and str(strict).lower() not in ("0", "false", "no")

    def tasks(self) -> list[str]:
        return list(TASKS)

    def reference(self, task: str) -> list[Call]:
        calls = [Call("list_contacts", {}, CallKind.READ)]
        for to, amount, note in TASKS[task]:
            calls.append(
                Call(
                    "send",
                    {"recipient": to, "amount": amount, "note": note},
                    CallKind.WRITE,
                )
            )
        calls.append(Call("complete", {}, CallKind.CLAIM))
        return calls

    def run(self, task: str, calls: Sequence[Call], *, score: bool) -> Episode:
        payments: list[dict[str, Any]] = []
        claimed: list[dict[str, Any]] | None = None
        errors: dict[int, str] = {}
        for i, call in enumerate(calls):
            if call.name == "send":
                if call.args.get("recipient") not in ACCOUNTS:
                    errors[i] = "unknown recipient"
                    continue
                payments.append(dict(call.args))
            elif call.name == "complete":
                claimed = list(payments)
        episode = Episode(
            final_effect=canonical_effect(payments),
            claim_effect=canonical_effect(claimed)
            if claimed is not None
            else canonical_effect([]),
            call_errors=errors,
        )
        if score:
            episode.verdict = self._score(task, payments, claimed or [])
        return episode

    def _score(
        self, task: str, payments: list[dict[str, Any]], claimed: list[dict[str, Any]]
    ) -> Verdict:
        want = [{"recipient": t, "amount": a, "note": n} for t, a, n in TASKS[task]]
        if self.strict:
            ok = (
                canonical_effect(claimed)
                == canonical_effect(want)
                == canonical_effect(payments)
            )
        else:
            ok = all(
                any(
                    p["recipient"] == w["recipient"] and p["amount"] == w["amount"]
                    for p in payments
                )
                for w in want
            )
        return Verdict.PASS if ok else Verdict.FAIL


def make_target(**kwargs: Any) -> ToyPayments:
    return ToyPayments(**kwargs)
