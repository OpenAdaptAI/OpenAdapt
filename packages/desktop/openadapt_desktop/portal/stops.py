"""The stop-review queue on the paired phone.

The portal serves Desktop's stop cards (:mod:`openadapt_desktop.stop_queue`)
to a paired phone and passes the phone's answers back. Like the rest of the
portal it decides nothing: the dispatcher rebuilds every card before it acts,
and a card that changed, expired or is gone, or a choice the card doesn't
offer, changes nothing.

The phone can't reach Touch ID on this computer, so its answers are signed
with the decision portal's approver key (route ``portal``), and the receipt
names that key and the device session. Only this adapter, in this process,
can ask the dispatcher for that signature; no command on any wire can.

Two kinds of card are view-only here:

* ``approve_save``: a waiting save. The phone answers it through the portal's
  existing save approval, which the Flow console signs with the same key.
* ``rearm_problems_only``: turning "only ask about problems" back on changes
  how every later save is approved, so it is answered on the computer.
"""

from __future__ import annotations

from typing import Any, Protocol

#: How long a phone's answer waits for a command the window started (a run,
#: for instance) before it gives up and changes nothing.
ANSWER_LOCK_TIMEOUT_S = 2.0


class StopQueueBackend(Protocol):
    """What the portal needs from the stop queue (faked in tests)."""

    def list_cards(self) -> dict[str, Any]: ...

    def _runs_again(self, card_ids: list[str], choice: str) -> bool:
        from openadapt_desktop import stop_queue

        cards = {c["card_id"]: c for c in self._dispatcher.list_stop_cards().get("cards", [])}
        for card_id in card_ids:
            card = cards.get(card_id)
            found = stop_queue.choice_for(card, choice) if card else None
            # Unknown card or choice: take the lock; the dispatcher refuses it.
            if found is None or found.action in {"retry", "retry_after_check"}:
                return True
        return False

    def screenshot(self, card_id: str) -> bytes | None: ...

    def answer(
        self,
        card_ids: list[str],
        choice: str,
        *,
        shown_ms: Any,
        device: dict[str, str],
    ) -> dict[str, Any]: ...


class DispatcherStopQueue:
    """The stop queue backed by the engine dispatcher in this process."""

    def __init__(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def list_cards(self) -> dict[str, Any]:
        from openadapt_desktop.approver_key import approver_key_environment

        listed = self._dispatcher.list_stop_cards()
        if not listed.get("ok"):
            return {"ok": False, "error": listed.get("error") or "No stops to show."}
        return {
            "ok": True,
            "cards": listed["cards"],
            # Whether the phone's answers can be signed. Never the key.
            "portal_key": {"ready": approver_key_environment("portal") is not None},
        }

    def _runs_again(self, card_ids: list[str], choice: str) -> bool:
        from openadapt_desktop import stop_queue

        cards = {c["card_id"]: c for c in self._dispatcher.list_stop_cards().get("cards", [])}
        for card_id in card_ids:
            card = cards.get(card_id)
            found = stop_queue.choice_for(card, choice) if card else None
            # Unknown card or choice: take the lock; the dispatcher refuses it.
            if found is None or found.action in {"retry", "retry_after_check"}:
                return True
        return False

    def screenshot(self, card_id: str) -> bytes | None:
        return self._dispatcher.stop_card_screenshot(card_id)

    def answer(
        self,
        card_ids: list[str],
        choice: str,
        *,
        shown_ms: Any,
        device: dict[str, str],
    ) -> dict[str, Any]:
        # Only an answer that runs an item again can overlap a run the window
        # started, so only that waits for the window's lock. Closing a stop
        # (skip, done by hand, saved) goes through while a run waits.
        lock = (
            getattr(self._dispatcher, "serial_lock", None)
            if self._runs_again(card_ids, choice)
            else None
        )
        if lock is not None and not lock.acquire(timeout=ANSWER_LOCK_TIMEOUT_S):
            return {
                "ok": False,
                "code": "busy",
                "error": "OpenAdapt is busy on the computer. Try again in a moment. "
                "Nothing changed.",
            }
        try:
            return self._dispatcher.answer_stop_cards_from_portal(
                card_ids=card_ids, choice=choice, shown_ms=shown_ms, device=device
            )
        finally:
            if lock is not None:
                lock.release()


__all__ = ["ANSWER_LOCK_TIMEOUT_S", "DispatcherStopQueue", "StopQueueBackend"]
