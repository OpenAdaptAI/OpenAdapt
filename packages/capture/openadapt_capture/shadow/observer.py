"""Shadow mode: watch for a repeated cross-application task, offer a draft.

:class:`ShadowObserver` ties the pieces together::

    ShadowEvent -> work scope -> Tokenizer -> SymbolRing -> mine_motifs -> MotifPrompt
                                                                  |
                         [Review draft] -> episodes export (for Flow's induce)
                         [Ignore]       -> SuppressionList

The observer is inert unless its configuration says ``enabled=True`` and the
person has acknowledged the current notice (see
:mod:`openadapt_capture.shadow.consent`). While inert it drops events without
tokenizing them, opens no file, and never prompts. It opens no network
connection in any state, and it never runs anything: the most it produces is
a JSON export of value-free episodes that a person may hand to Flow.

Keys last one epoch of at most 24 hours (:mod:`openadapt_capture.shadow.keys`).
When an epoch ends, the observer destroys the key, purges the buffer, and
starts again with a new key, so no digest outlives the key that made it.

The export never carries a keyed digest. Each distinct label or value digest
becomes a reference local to that one export (``label-a``, ``value-b``), which
says only which steps share a value. The digests stay in the buffer and expire
with it.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Iterable
from typing import Any

from openadapt_capture.shadow.config import ShadowConfig
from openadapt_capture.shadow.consent import Acknowledgement, require_current
from openadapt_capture.shadow.events import ShadowEvent
from openadapt_capture.shadow.keys import (
    KeyStore,
    MemoryKeyStore,
    ShadowKeys,
    create_keys,
    destroy_keys,
    load_or_create_keys,
)
from openadapt_capture.shadow.motifs import Motif, mine_motifs
from openadapt_capture.shadow.notify import (
    MotifNotifier,
    MotifPrompt,
    PromptAction,
    SuppressionList,
)
from openadapt_capture.shadow.ring import Clock, RowCipher, SymbolRing
from openadapt_capture.shadow.scrub import NON_WORK_BROWSER, allowlisted_domain, url_host
from openadapt_capture.shadow.storage import require_unsynced
from openadapt_capture.shadow.symbols import ShadowSymbol, Tokenizer

EPISODES_SCHEMA = "openadapt.shadow.episodes/v2"

PRIVACY_POSTURE = {
    "typed_values": "never exported; secure fields dropped",
    "keyed_digests": "never exported; replaced by references local to this export",
    "control_labels": "short chrome labels of common words in plain text; others a placeholder",
    "window_titles": "never read",
    "urls": "work domains only: host and one scrubbed path word; other sites a marker",
    "clipboard_contents": "never read; copy and paste come from the key chord",
    "screenshots": "never taken",
    "network": "none",
}

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


class ShadowModeDisabled(RuntimeError):
    """Raised when a caller asks a disabled observer to mine or export."""


def _letters(index: int) -> str:
    """0 -> a, 25 -> z, 26 -> aa. Letters only, so Flow's intents keep it."""

    out = ""
    n = index + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out


class ShadowObserver:
    """Passive pattern miner. Off unless ``config.enabled`` is True."""

    def __init__(
        self,
        config: ShadowConfig | None = None,
        *,
        notifier: MotifNotifier | None = None,
        suppression: SuppressionList | None = None,
        acknowledgement: Acknowledgement | None = None,
        digest_key: bytes | None = None,
        key_store: KeyStore | None = None,
        clock: Clock = time.time,
    ) -> None:
        """Create an observer.

        Args:
            config: Settings. Shadow mining stays off unless ``enabled=True``.
            notifier: Surface that shows prompts.
            suppression: Ignored motifs. Defaults to an in-memory list.
            acknowledgement: The person's acknowledgement of the current
                notice. Required when ``config.enabled`` is True.
            digest_key: An explicit key for the first epoch, for tests and
                fixtures. It is held in memory only, and is replaced by a
                random in-memory key when its epoch ends.
            key_store: Where the key lives. ``None`` means the OS credential
                store, with an in-memory fallback when there is none.
            clock: Time source, for tests.
        """
        self.config = config if config is not None else ShadowConfig()
        self._clock = clock
        self._lock = threading.RLock()
        self._notifier = notifier
        self._suppression = suppression if suppression is not None else SuppressionList()
        self._offered: dict[str, int] = {}
        self._motifs: dict[str, Motif] = {}
        self._symbols_cache: list[ShadowSymbol] = []
        self._tokenizer: Tokenizer | None = None
        self._ring: SymbolRing | None = None
        self._keys: ShadowKeys | None = None
        self._store: KeyStore | None = None
        self._shut = False
        self._paused = False
        self._browser_on_work_site = False
        self._last_was_marker = False
        self.acknowledgement: Acknowledgement | None = None
        if not self.config.enabled:
            return
        self.acknowledgement = require_current(acknowledgement)
        if self.config.store_path is not None:
            require_unsynced(self.config.store_path)
        now = clock()
        if digest_key is not None:
            if len(digest_key) < 16:
                raise ValueError("digest_key must be at least 16 bytes")
            self._store = MemoryKeyStore()
            key_id = hashlib.sha256(b"explicit\x00" + digest_key).hexdigest()[:16]
            self._keys = ShadowKeys(key_id, digest_key, now, self.config.key_lifetime_s, False)
        else:
            self._store = key_store
            self._keys = load_or_create_keys(
                key_store, lifetime_s=self.config.key_lifetime_s, now=now
            )
        self._tokenizer = Tokenizer(self.config, digest_key=self._keys.digest_key)
        self._ring = SymbolRing(
            max_symbols=self.config.max_symbols,
            max_age_s=self.config.max_age_s,
            path=self.config.store_path,
            cipher=self._cipher() if self.config.store_path is not None else None,
            clock=clock,
        )

    # ------------------------------------------------------------- state

    @property
    def enabled(self) -> bool:
        return self.config.enabled and not self._shut

    @property
    def keys_persisted(self) -> bool:
        return bool(self._keys is not None and self._keys.persisted)

    @property
    def epoch_ends_at(self) -> float | None:
        return self._keys.expires_at if self._keys is not None else None

    def _cipher(self) -> RowCipher:
        assert self._keys is not None
        return RowCipher(self._keys.store_key, key_id=self._keys.key_id, check=self._keys.check)

    def _rotate_if_due(self) -> None:
        """Destroy the key and purge the buffer once the epoch has ended."""

        assert self._keys is not None and self._ring is not None
        now = self._clock()
        if not self._keys.expired(now):
            return
        store = self._store if self._store is not None else None
        destroy_keys(store, also=(self._keys.key_id,))
        self._keys = create_keys(store, lifetime_s=self.config.key_lifetime_s, now=now)
        self._tokenizer = Tokenizer(self.config, digest_key=self._keys.digest_key)
        if self.config.store_path is not None:
            self._ring.rekey(self._cipher())
        else:
            self._ring.purge()
        self._motifs.clear()
        self._offered.clear()
        self._symbols_cache = []

    # ------------------------------------------------------------- input

    def _scoped(self, event: ShadowEvent) -> ShadowEvent | None:
        """Apply the work-domain allowlist. Returns the event to tokenize.

        A browser event on a site outside the allowlist becomes one
        ``non-work-browser`` marker (repeats collapse into one). A native
        event in a browser app is treated the same way until a browser event
        shows the current page is on a work domain. Switching to a browser
        keeps only the app name.
        """

        cfg = self.config
        in_browser = (event.app or "").strip().casefold() in cfg.browser_apps
        if event.kind == "focus" and not event.url:
            # After a switch, the browser may show any page: wait for a browser
            # event to say it's a work site again.
            self._browser_on_work_site = False
        if event.url:
            on_work = allowlisted_domain(url_host(event.url), cfg.work_domains) is not None
            if in_browser or event.kind in ("navigate", "submit"):
                self._browser_on_work_site = on_work
            if not on_work:
                return self._marker(event)
        elif in_browser and event.kind != "focus" and not self._browser_on_work_site:
            return self._marker(event)
        self._last_was_marker = False
        return event

    def _marker(self, event: ShadowEvent) -> ShadowEvent | None:
        if self._last_was_marker:
            return None
        self._last_was_marker = True
        return ShadowEvent(event.timestamp, "focus", app=NON_WORK_BROWSER)

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop recording until :meth:`unpause`. Typing in progress is dropped.

        What's already in the buffer stays, and expires as usual. Events that
        arrive while paused are dropped untouched, so nothing about what the
        person views during the pause reaches the buffer.
        """

        with self._lock:
            self._paused = True
            if self._tokenizer is not None:
                self._tokenizer.flush()
            self._browser_on_work_site = False
            self._last_was_marker = False

    def unpause(self) -> None:
        with self._lock:
            self._paused = False

    def observe(self, event: ShadowEvent | None) -> None:
        """Record one event. A disabled or paused observer drops it untouched."""

        if not self.enabled or self._paused or event is None:
            return
        with self._lock:
            if not self.enabled or self._paused:
                return
            self._rotate_if_due()
            assert self._tokenizer is not None and self._ring is not None
            scoped = self._scoped(event)
            if scoped is not None:
                self._ring.extend(self._tokenizer.feed(scoped))

    def observe_all(self, events: Iterable[ShadowEvent | None]) -> None:
        for event in events:
            self.observe(event)

    def symbols(self) -> list[ShadowSymbol]:
        """The live ring buffer contents, oldest first."""

        if not self.enabled:
            return []
        with self._lock:
            self._rotate_if_due()
            assert self._tokenizer is not None and self._ring is not None
            self._ring.extend(self._tokenizer.flush())
            return self._ring.snapshot()

    # ------------------------------------------------------------- mining

    def scan(self) -> list[MotifPrompt]:
        """Mine the buffer and offer each new, unsuppressed motif once.

        A motif is offered again only when its count has grown since the last
        offer. Returns the prompts offered by this call.
        """

        if not self.enabled:
            return []
        with self._lock:
            symbols = self.symbols()
            prompts: list[MotifPrompt] = []
            self._symbols_cache = symbols
            for motif in mine_motifs(symbols, self.config, now=self._clock()):
                if self._suppression.is_suppressed(motif.motif_id, motif.tokens):
                    continue
                self._motifs[motif.motif_id] = motif
                if self._offered.get(motif.motif_id, 0) >= motif.count_in_window:
                    continue
                self._offered[motif.motif_id] = motif.count_in_window
                prompts.append(MotifPrompt(motif_id=motif.motif_id, count=motif.count_in_window))
        for prompt in prompts:
            if self._notifier is not None:
                self._notifier.offer(prompt)
        return prompts

    def decide(self, motif_id: str, action: PromptAction | str) -> dict[str, Any] | None:
        """Apply a person's answer to a prompt.

        ``ignore`` suppresses the motif (and close variants) and returns None.
        ``review_draft`` returns the episodes export for Flow. Neither answer
        runs anything.
        """

        if not self.enabled:
            raise ShadowModeDisabled("shadow mining is off")
        action = PromptAction(action)
        with self._lock:
            motif = self._motifs.get(motif_id)
            if motif is None:
                raise KeyError(f"unknown or expired motif {motif_id}")
            if action is PromptAction.IGNORE:
                self._suppression.add(motif.motif_id, motif.tokens)
                self._motifs.pop(motif_id, None)
                return None
            return self.export(motif_id)

    def export(self, motif_id: str) -> dict[str, Any]:
        """Episodes of one motif, in the ``openadapt.shadow.episodes/v2`` form."""

        if not self.enabled:
            raise ShadowModeDisabled("shadow mining is off")
        with self._lock:
            motif = self._motifs[motif_id]
            return export_episodes(motif, self._symbols_cache, self.config)

    # ------------------------------------------------------------- off

    def purge(self) -> None:
        """Delete every stored symbol and forget every motif."""

        with self._lock:
            self._motifs.clear()
            self._offered.clear()
            self._symbols_cache = []
            if self._tokenizer is not None:
                self._tokenizer.flush()
            if self._ring is not None:
                self._ring.purge()

    def purge_and_disable(self) -> bool:
        """Delete the buffer file, the key, and the ignore list; then stay off.

        This is the one-click "Delete everything and turn off". The observer
        can't be turned back on; a new opt-in builds a new one. Returns False
        when the credential store refused to delete the key (it is retried by
        the next destroy), so a caller can say so instead of claiming success.
        """

        with self._lock:
            self._shut = True
            self.purge()
            self._suppression.clear()
            if self._ring is not None:
                self._ring.destroy()
            key_deleted = True
            if self._keys is not None:
                key_deleted = destroy_keys(
                    self._store if self._store is not None else None,
                    also=(self._keys.key_id,),
                )
            self._keys = None
            self._tokenizer = None
            return key_deleted

    def shutdown(self) -> bool:
        """Clear the buffer and destroy its key, then stop. For a clean exit.

        Unlike :meth:`purge_and_disable`, the ignore list stays, so a task
        the person ignored isn't offered again after a restart. Returns False
        when the credential store refused to delete the key.
        """

        with self._lock:
            self._shut = True
            self.purge()
            if self._ring is not None:
                self._ring.destroy()
            key_deleted = True
            if self._keys is not None:
                key_deleted = destroy_keys(self._store, also=(self._keys.key_id,))
            self._keys = None
            self._tokenizer = None
            return key_deleted

    def close(self) -> None:
        """Close the buffer file without clearing it. Prefer :meth:`shutdown`."""

        with self._lock:
            if self._ring is not None:
                self._ring.close()


def export_episodes(
    motif: Motif, symbols: list[ShadowSymbol], config: ShadowConfig
) -> dict[str, Any]:
    """Build the JSON-ready export Flow's ``induce`` bridge reads.

    No keyed digest leaves this function. Each distinct label digest becomes
    ``label-<letters>`` and each distinct value digest ``value-<letters>``,
    numbered in order of first appearance in this export. Two steps that share
    a reference shared a value; that is all the export says about values.
    """

    labels: dict[str, str] = {}
    values: dict[str, str] = {}
    prefixes: dict[str, str] = {}

    def label_ref(digest: str | None) -> str | None:
        if not digest:
            return None
        if digest not in labels:
            labels[digest] = f"label-{_letters(len(labels))}"
            prefixes.setdefault(digest[:8], labels[digest])
        return labels[digest]

    def value_ref(digest: str | None) -> str | None:
        if not digest:
            return None
        if digest not in values:
            values[digest] = f"value-{_letters(len(values))}"
        return values[digest]

    def scrub_token(token: str) -> str:
        return "|".join(
            prefixes.get(part, "label-?") if _HEX8.match(part) else part
            for part in token.split("|")
        )

    episodes = []
    for occ in motif.occurrences:
        steps = []
        for i in range(occ.start, occ.end):
            sym = symbols[i]
            steps.append(
                {
                    "t": sym.t,
                    "token": sym.token,
                    "kind": sym.kind,
                    "app": sym.app,
                    "window_class": sym.window_class,
                    "role": sym.role,
                    "label_ref": label_ref(sym.label_digest),
                    "display_label": sym.display_label,
                    "value_ref": value_ref(sym.value_digest),
                    "value_shape": sym.value_shape,
                    "url_pattern": sym.url_pattern,
                    "key": sym.key,
                    "modifiers": list(sym.modifiers),
                    "secure": sym.secure,
                }
            )
        episodes.append(
            {
                "t_start": occ.t_start,
                "t_end": occ.t_end,
                "exact": occ.exact,
                "similarity": occ.similarity,
                "steps": steps,
            }
        )
    for episode in episodes:
        for step in episode["steps"]:
            step["token"] = scrub_token(step["token"])
    tokens = [scrub_token(t) for t in motif.tokens]
    export_id = hashlib.sha256("\x1f".join(tokens).encode()).hexdigest()[:16]
    return {
        "schema": EPISODES_SCHEMA,
        "motif_id": export_id,
        "tokens": tokens,
        "apps": list(motif.apps),
        "count_in_window": motif.count_in_window,
        "window_s": config.window_s,
        "exact_count": motif.exact_count,
        "expected_by_chance": motif.expected_by_chance,
        "p_value": motif.p_value,
        "status": "candidate",
        "privacy": dict(PRIVACY_POSTURE),
        "episodes": episodes,
    }
