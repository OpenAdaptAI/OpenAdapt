"""Raise exception cards from the agent's browser, without the agent.

``raise_exception`` (:mod:`.mcp_server`) only works when the agent calls it.
An agent that never calls it never asks anyone. :class:`ExceptionWatch` asks
on its own: it attaches to the agent's Chromium over CDP (the same endpoint
``openadapt-flow guard`` attaches to), looks at the page every ``poll_s``,
runs the :mod:`.detectors`, and when one fires it opens a card in the
registry. While the card is open it holds the run at the browser: every save
the page sends is blocked (CDP ``Fetch``), whatever the agent does. A verified
``continue`` or ``done_by_person`` lifts the hold. Any other outcome (a stop
answer, an expiry, an answer off the roster) keeps every later save blocked
for the rest of the run.

    openadapt-flow exceptions watch --cdp http://127.0.0.1:9222 \\
        --config guard.yaml --run-dir runs/intake-0412

What is a save comes from the guard's own config and rules
(:func:`openadapt_flow.agent_guard.requests.classify`): a write to the app's
origin that ``not_saves`` doesn't list, and any write to another origin. A
write ``not_saves`` lists (a sign-in form, the code form a person submits
for a second factor) goes through, so a person can type the code in the live
browser. Reads always go through.

The watch and the guard each pause requests in their own DevTools session.
Chromium hands a paused request to the session that attached last first, so
start the watch after the guard: it then decides a save before the guard
sees it. (In the other order a blocked save is still never sent, but the
guard has already let it go and ends the run unconfirmed.)

Covered, from what the page shows: ``second_factor``, ``unexpected_dialog``,
``session_expired`` (a password field after the watch has seen a save) and,
with ``stall_s``, ``timeout_or_stall`` (no request and no screen change).
Not covered: ``possible_duplicate`` (it needs a read of the app's records),
``ambiguous_record`` and ``cannot_complete`` (only the agent knows),
non-browser agents, saves over a WebSocket (``Fetch`` doesn't see them),
tabs other than the one watched, a save sent before the next look at the page
(``poll_s``), and a run with no watch running. The watch is not a sandbox: it can't stop an
agent that has its own way to the app, or one that kills the watch. A watch
that loses its DevTools session exits 4 (holding) or 5 and blocks nothing
from then on; the guard remains the backstop.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.exceptions.detectors import DEFAULT_STALL_S, Observation, detect_all
from openadapt_flow.exceptions.registry import (
    DEFAULT_DEADLINE_S,
    AnswerVerifier,
    ExceptionRegistry,
    Outcome,
)

__all__ = [
    "BLOCK_HEADER",
    "SNAPSHOT_JS",
    "WATCH_EVENTS",
    "ExceptionWatch",
    "WatchAttachError",
    "drain",
    "run_loop",
    "watch_browser",
]

#: The watch's log in the run folder: what it raised and blocked, no values.
WATCH_EVENTS = "exceptions_watch.jsonl"
#: Set on every response the watch makes up for a blocked save.
BLOCK_HEADER = "X-OpenAdapt-Exception"
_PAUSED_TYPES = ("Document", "XHR", "Fetch", "Ping", "Other")

#: The page, as detector elements (see :mod:`.detectors`), with geometry.
SNAPSHOT_JS = r"""
() => {
  const sel = 'input, textarea, select, button, dialog, [role], [aria-modal]';
  const out = [];
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 500)) {
    if (el.closest('[data-openadapt-guard]')) continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden'
      && st.display !== 'none' && el.type !== 'hidden';
    let label = '';
    if (el.labels && el.labels.length) label = text(el.labels[0]);
    const prev = el.previousElementSibling;
    const parent = el.parentElement;
    out.push({
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || (el.tagName === 'INPUT' ? 'text' : '')).toLowerCase(),
      role: el.getAttribute('role') || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      label: label.slice(0, 160),
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      inputmode: el.getAttribute('inputmode') || '',
      pattern: el.getAttribute('pattern') || '',
      maxlength: el.getAttribute('maxlength') || '',
      context: (prev ? text(prev) : text(parent)).slice(-160),
      visible: visible,
      modal: el.getAttribute('aria-modal') === 'true'
        || (el.tagName === 'DIALOG' && el.matches(':modal')),
      open: el.tagName === 'DIALOG' ? el.open : false,
      bbox: [r.x, r.y, r.width, r.height],
      z: parseInt(st.zIndex, 10) || 0,
    });
  }
  return {text: text(document.body).slice(0, 20000), elements: out};
}
"""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ExceptionWatch:
    """The watch's decisions, without a browser (the loop is :func:`watch_browser`).

    ``observe`` takes one page snapshot (``{"text", "elements"}``) and opens
    a card when a detector fires and none is open. ``on_request`` says
    whether a paused request goes on (``None``) or is blocked (a code).
    ``poll`` settles an open card without blocking.
    """

    def __init__(
        self,
        registry: ExceptionRegistry,
        *,
        run_ref: str,
        verify: AnswerVerifier,
        app_origin: str,
        not_saves: Sequence[Any] = (),
        task_id: Optional[str] = None,
        deadline_s: float = DEFAULT_DEADLINE_S,
        stall_s: Optional[float] = None,
        events_path: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from openadapt_flow.agent_guard.requests import origin_of

        registry.run_dir(run_ref)  # refuse a bad name before anything runs
        self.registry = registry
        self.run_ref = run_ref
        self.verify = verify
        self.app_origin = origin_of(app_origin) or app_origin
        self.not_saves = tuple(not_saves)
        self.task_id = task_id
        self.deadline_s = deadline_s
        self.stall_s = stall_s
        self.events_path = events_path
        self._clock = clock
        self.card: Any = None
        #: Why every later save is blocked (a stop outcome), or None.
        self.stopped: Optional[str] = None
        self.outcomes: list[Outcome] = []
        #: Kinds answered "go on" that the page still shows: not asked again
        #: until the page stops showing them.
        self._settled: set[str] = set()
        self._save_seen = False
        #: Why the watch lost the tab (``session_lost``, ``browser_lost``,
        #: ``watch_failed``).
        self.lost: Optional[str] = None
        #: The watch's own error (detection, opening a card, polling): from
        #: then on every write is refused until the process exits.
        self.failed: Optional[str] = None
        self._screen: Optional[str] = None
        self._last_change: Optional[float] = None
        self._last_request: Optional[float] = None
        self._log("watch_started", run_ref=run_ref)

    # -- the log -------------------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        if self.events_path is None:
            return
        line = {"t": round(self._clock(), 3), "event": event, **fields}
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            pass  # the log is lost, never the hold

    # -- decisions -----------------------------------------------------------

    @property
    def holding(self) -> bool:
        """Saves are blocked right now."""

        return (
            self.card is not None or self.stopped is not None or self.failed is not None
        )

    def on_request(self, request: Any) -> Optional[str]:
        """``None``: let the paused request go on; else the block's code."""

        from openadapt_flow.agent_guard.requests import classify

        self._last_request = self._clock()
        kind = classify(request, app_origin=self.app_origin, not_saves=self.not_saves)
        if kind == "save":
            self._save_seen = True
        if self.failed is not None and kind != "read":
            code = "watch_failed"  # the watch broke: no write goes on
        elif kind not in ("save", "other_origin") or not self.holding:
            return None
        else:
            code = "exception_open" if self.card is not None else "exception_stopped"
        self._log(
            "request_blocked",
            code=code,
            kind=kind,
            method=request.method,
            resource_type=request.resource_type,
        )
        return code

    def observe(self, snapshot: Mapping[str, Any]) -> Optional[str]:
        """Look at one snapshot; open a card when it calls for a person.

        Returns the kind of the card opened, or None.
        """

        now = self._clock()
        text = str(snapshot.get("text") or "")
        raw = snapshot.get("elements")
        elements = (
            [e for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []
        )
        digest = hashlib.sha256(
            (text + json.dumps(elements, sort_keys=True, default=str)).encode("utf-8")
        ).hexdigest()
        if digest != self._screen:
            self._screen, self._last_change = digest, now
        kinds = detect_all(
            Observation(
                screen_text=text,
                elements=elements,
                save_eligible_seen=self._save_seen,
                now=now if self.stall_s else None,
                last_action_at=self._last_request,
                last_change_at=self._last_change,
                stall_s=self.stall_s or DEFAULT_STALL_S,
            )
        )
        self._settled &= set(kinds)  # a kind the page stopped showing may ask again
        if self.holding:
            return None
        fresh = [k for k in kinds if k not in self._settled]
        if not fresh:
            return None
        kind = fresh[0]
        self.card = self.registry.open(
            self.run_ref,
            kind,
            None,  # the kind's own sentence: nothing from the page is relayed
            task_id=self.task_id,
            deadline_s=self.deadline_s,
        )
        self._log("card_opened", kind=kind, card_id=self.card.card_id)
        return kind

    def poll(self) -> Optional[Outcome]:
        """Settle the open card if it was answered or expired."""

        if self.card is None:
            return None
        outcome = self.registry.poll(self.card, verify=self.verify)
        if outcome is None:
            return None
        self.outcomes.append(outcome)
        self._log(
            "answer",
            card_id=outcome.card_id,
            answer=outcome.answer,
            proceed=outcome.proceed,
            reason=outcome.reason,
        )
        if outcome.proceed:
            self._settled.add(outcome.kind)
        else:
            self.stopped = outcome.reason or outcome.answer
        self.card = None
        return outcome

    def note_decision_failed(self, code: Optional[str], error: str) -> None:
        """The browser refused a continue or a block (logged, not fatal)."""

        self._log("request_decision_failed", code=code, error=error[:64])

    def mark_lost(self, reason: str) -> None:
        """The watch can no longer see or hold the tab. From here it blocks
        nothing: the guard (``openadapt-flow guard``) is the backstop."""

        self.lost = reason
        self._log(
            "watch_lost",
            reason=reason,
            card_open=self.card is not None,
            stopped=self.stopped,
        )
        import sys

        what = (
            "every write is refused until the watch exits; after that the "
            "watch holds nothing"
            if self.failed is not None
            else "saves are no longer held by the watch"
        )
        held = (
            "; a card was open"
            if self.card is not None
            else "; the run was stopped"
            if self.stopped is not None
            else ""
        )
        print(
            f"exceptions: WATCH LOST ({reason}): {what}{held}",
            file=sys.stderr,
            flush=True,
        )

    def fail(self, error: BaseException) -> None:
        """The watch's own code failed: refuse every write from here on."""

        if self.failed is not None:
            return
        self.failed = type(error).__name__
        self.mark_lost("watch_failed")

    def stop_code(self) -> int:
        """The watch's exit code:

        * 0: the tab closed or the watch was stopped with nothing open;
        * 3: an answer stopped the run;
        * 4: the watch failed or lost the tab's session while holding saves
          (a card open, a stop, or its own error);
        * 5: it lost the session with nothing held;
        * 6: it ended (``max_s``, a signal) with a card still unanswered.
        """

        if self.lost is not None:
            return 4 if self.holding else 5
        if self.stopped is not None:
            return 3
        return 6 if self.card is not None else 0


# -- the browser loop ------------------------------------------------------------


class WatchAttachError(RuntimeError):
    """The watch found no tab to watch (nothing was held)."""


def _blocked_body(code: str) -> str:
    import base64

    message = (
        "OpenAdapt is holding this run: a person is answering an exception card. "
        "Nothing was sent. Wait, then save again."
        if code == "exception_open"
        else "A person stopped this run. Nothing was sent; write nothing more."
        if code == "exception_stopped"
        else "OpenAdapt can't check this request. Nothing was sent; stop and "
        "tell a person."
    )
    payload = json.dumps({"error": code, "message": message})
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _decide(cdp: Any, watch: ExceptionWatch, params: Mapping[str, Any]) -> bool:
    """Continue or block one paused request. False when the browser refused
    the decision (the page aborted the request, or the session is gone): the
    caller checks the session. A request the page aborted was never sent."""

    from openadapt_flow.agent_guard.requests import HeldRequest

    request_id = str(params.get("requestId") or "")
    try:
        code = watch.on_request(HeldRequest.from_cdp(params))
    except Exception:  # noqa: BLE001 - can't tell what it is: refuse it
        code = "request_unclassified"
    try:
        if code is None:
            cdp.send("Fetch.continueRequest", {"requestId": request_id})
        else:
            cdp.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": 409,
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": BLOCK_HEADER, "value": f"blocked; code={code}"},
                    ],
                    "body": _blocked_body(code),
                },
            )
    except Exception as exc:  # noqa: BLE001 - one request must not end the watch
        watch.note_decision_failed(code, type(exc).__name__)
        return False
    return True


def drain(cdp: Any, watch: ExceptionWatch, paused: list[Mapping[str, Any]]) -> None:
    """Decide every paused request before the session detaches. A decision
    the browser refuses is skipped; the rest are still decided."""

    while paused:
        try:
            _decide(cdp, watch, paused.pop(0))
        except Exception:  # noqa: BLE001 - one request never stops the rest
            continue


def _alive(cdp: Any) -> bool:
    """The watch's own DevTools session still answers."""

    try:
        cdp.send("Runtime.evaluate", {"expression": "1", "returnByValue": True})
        return True
    except Exception:  # noqa: BLE001 - detached or the browser is gone
        return False


def run_loop(
    watch: ExceptionWatch,
    page: Any,
    cdp: Any,
    paused: list[Mapping[str, Any]],
    *,
    poll_s: float = 0.5,
    max_s: Optional[float] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> None:
    """The watch's loop over an attached tab and its own Fetch session.

    Ends when the tab closes, ``stop()`` or ``max_s``, or when the session is
    lost while the tab is still open (:meth:`ExceptionWatch.mark_lost`).
    """

    started = time.monotonic()
    next_look = 0.0
    while not page.is_closed():
        while paused:
            if not _decide(cdp, watch, paused.pop(0)) and not _alive(cdp):
                watch.mark_lost("session_lost")
                return
        if time.monotonic() >= next_look:
            if not _alive(cdp):
                if page.is_closed():
                    return
                watch.mark_lost("session_lost")
                return
            snapshot = None
            if watch.failed is None:
                try:
                    snapshot = page.evaluate(SNAPSHOT_JS)
                except Exception:  # noqa: BLE001 - navigating: look again
                    snapshot = None
            if watch.failed is None:
                try:
                    if isinstance(snapshot, Mapping):
                        watch.observe(snapshot)
                    watch.poll()
                except Exception as exc:  # noqa: BLE001 - fail closed
                    # Detection, the card, or its answer broke (a full disk,
                    # an unwritable registry). The loop goes on, refusing
                    # every write, until the tab closes, the session is
                    # lost, a signal or max_s ends it.
                    watch.fail(exc)
            next_look = time.monotonic() + poll_s
        if stop is not None and stop():
            return
        if max_s is not None and time.monotonic() - started >= max_s:
            return
        try:
            page.wait_for_timeout(50)  # delivers CDP events
        except Exception:  # noqa: BLE001 - the browser went away
            if not page.is_closed():
                watch.mark_lost("browser_lost")
            return


def _page(browser: Any, app_origin: str, page_url: Optional[str]) -> Any:
    from openadapt_flow.agent_guard.requests import origin_of

    for context in browser.contexts:
        for page in context.pages:
            url = page.url or ""
            if page_url and url.startswith(page_url):
                return page
            if not page_url and origin_of(url) == app_origin:
                return page
    return None


def watch_browser(
    watch: ExceptionWatch,
    *,
    cdp_endpoint: str,
    page_url: Optional[str] = None,
    poll_s: float = 0.5,
    attach_wait_s: float = 30.0,
    max_s: Optional[float] = None,
    on_attached: Optional[Callable[[], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Attach to the agent's Chromium and run the watch until the tab or the
    browser closes, ``max_s`` passes or ``stop()`` says so. Returns the exit
    code (:meth:`ExceptionWatch.stop_code`)."""

    from playwright.sync_api import sync_playwright

    from openadapt_flow._browser_readiness import require_rendering_ready

    pw = sync_playwright().start()
    try:
        deadline = time.monotonic() + attach_wait_s
        browser = page = None
        while page is None:
            try:
                browser = pw.chromium.connect_over_cdp(cdp_endpoint)
                page = _page(browser, watch.app_origin, page_url)
                if page is not None:
                    # The tab must render before its screen is trusted, as
                    # for every other CDP attach (the guard's included).
                    require_rendering_ready(browser, attached_page=page)
            except Exception:  # noqa: BLE001 - not up or not rendering yet
                page = None
            if page is None:
                if browser is not None:
                    browser.close()
                    browser = None
                if time.monotonic() >= deadline:
                    raise WatchAttachError("no tab on the app's origin to watch")
                time.sleep(0.25)
        cdp = page.context.new_cdp_session(page)
        paused: list[Mapping[str, Any]] = []
        cdp.on("Fetch.requestPaused", lambda params: paused.append(params))
        cdp.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "*", "resourceType": t, "requestStage": "Request"}
                    for t in _PAUSED_TYPES
                ]
            },
        )
        if on_attached is not None:
            on_attached()
        started = time.monotonic()
        try:
            try:
                run_loop(
                    watch, page, cdp, paused, poll_s=poll_s, max_s=max_s, stop=stop
                )
            except Exception as exc:  # noqa: BLE001 - fail closed, then exit 4
                watch.fail(exc)
                # Keep refusing writes in the failed state until the run ends.
                left = (
                    None
                    if max_s is None
                    else max(0.0, max_s - (time.monotonic() - started))
                )
                try:
                    run_loop(
                        watch, page, cdp, paused, poll_s=poll_s, max_s=left, stop=stop
                    )
                except Exception:  # noqa: BLE001 - the tab or session is gone
                    pass
        finally:
            # Paused requests go on when the session detaches: decide them first.
            drain(cdp, watch, paused)
            try:
                cdp.detach()
            except Exception:  # noqa: BLE001 - the tab is gone
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
    finally:
        pw.stop()
    return watch.stop_code()
