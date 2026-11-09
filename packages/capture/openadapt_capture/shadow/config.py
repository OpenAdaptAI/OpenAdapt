"""Configuration for shadow-mode pattern mining.

Shadow mining is off unless a caller passes ``ShadowConfig(enabled=True)``.
The configuration is a plain dataclass on purpose: it reads no environment
variable, settings file, or remote policy, so nothing outside the calling
process can turn it on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: The longest anything in the shadow buffer may live, and the longest a
#: shadow key may live before it's destroyed.
MAX_RETENTION_S = 24 * 3600.0

#: Applications treated as web browsers. A native event in one of them (an
#: accessibility click with no URL) is kept only while the browser's current
#: page is on an allowlisted work domain.
DEFAULT_BROWSER_APPS: frozenset[str] = frozenset(
    {
        "google chrome",
        "chrome",
        "chrome.exe",
        "chromium",
        "safari",
        "firefox",
        "firefox.exe",
        "microsoft edge",
        "msedge",
        "msedge.exe",
        "arc",
        "brave browser",
        "brave.exe",
        "opera",
        "opera.exe",
        "vivaldi",
        "vivaldi.exe",
        "browser",
    }
)

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9-]{1,63}$")


def normalize_domain(domain: str) -> str:
    """Return the canonical form of one allowlist entry, or raise ValueError.

    Accepts ``example.com``, ``.example.com``, and ``*.example.com``; all mean
    the domain and its subdomains. A scheme, path, port, or wildcard in the
    middle is refused, so an entry can't be broader than it looks.
    """

    text = str(domain).strip().casefold().rstrip(".")
    if text.startswith("*."):
        text = text[2:]
    text = text.lstrip(".")
    if not _DOMAIN.match(text) or "." not in text:
        raise ValueError(f"not a domain name: {domain!r}")
    return text


#: Roles whose label names a piece of UI chrome rather than a piece of data.
#: Only these labels may be kept in plain text for the review draft, and only
#: when :attr:`ShadowConfig.keep_chrome_labels` is on.
DEFAULT_CHROME_ROLES: frozenset[str] = frozenset(
    {"button", "menuitem", "menubaritem", "tab", "checkbox", "radiobutton"}
)

#: Roles whose label identifies the control (a field name or a button title),
#: so the label digest is part of the structural symbol. A label on any other
#: role (a table cell, a list row, a link) is treated as data: it becomes a
#: value digest and stays out of the symbol.
DEFAULT_STRUCTURAL_LABEL_ROLES: frozenset[str] = DEFAULT_CHROME_ROLES | frozenset(
    {"textfield", "textarea", "combobox", "searchfield"}
)


@dataclass(frozen=True)
class ShadowConfig:
    """Settings for :class:`~openadapt_capture.shadow.observer.ShadowObserver`.

    Attributes:
        enabled: Off by default. While off, the observer drops every event,
            opens no file, and never mines or prompts.
        max_symbols: Size cap of the ring buffer. The oldest symbol is
            overwritten first.
        max_age_s: Time cap of the ring buffer. Older symbols expire on every
            write and every read.
        store_path: Optional SQLite file for the ring buffer. ``None`` keeps
            the buffer in memory only.
        idle_gap_s: A gap longer than this starts a new session. A motif never
            spans two sessions.
        window_s: Occurrences are counted inside a sliding window of this
            length. The default is one day, which is what the prompt text
            ("today") assumes.
        min_occurrences: How many times a sequence must repeat inside
            ``window_s`` before it is offered.
        min_length: Shortest motif, in symbols.
        max_length: Longest motif, in symbols.
        min_apps: Distinct applications a motif must touch. The default of 2
            restricts mining to cross-application sequences.
        similarity_threshold: Structural similarity (0 to 1) an approximate
            occurrence needs before it joins a motif's episodes.
        significance: Family-wise false-alarm budget for the chance test that
            separates a real repeat from a coincidence in a busy stream.
        keep_chrome_labels: Keep short plain-text labels of chrome controls
            (for example "Save") so a person can read the review draft.
        chrome_roles: Roles eligible for a plain-text label.
        structural_label_roles: Roles whose label digest is part of a symbol.
        work_domains: The local allowlist of work domains (each entry also
            covers its subdomains). Browser activity on an allowlisted domain
            is recorded with its host and one scrubbed path word. Activity on
            any other site is reduced to a single "non-work browser" marker
            with no host, path, label, or value. Empty by default, so no site
            is recorded until the person adds one.
        browser_apps: Application names treated as web browsers.
        extra_path_words: Extra lowercase words a URL's first path segment may
            keep (on top of the built-in vocabulary).
        extra_label_words: Extra words a short chrome label may keep in plain
            text (on top of the built-in vocabulary).
        key_lifetime_s: How long one key epoch lasts. At the end the key is
            destroyed and the buffer purged. At most 24 hours.
    """

    enabled: bool = False
    max_symbols: int = 20_000
    max_age_s: float = 24 * 3600.0
    store_path: Path | None = None
    idle_gap_s: float = 300.0
    window_s: float = 24 * 3600.0
    min_occurrences: int = 3
    min_length: int = 4
    max_length: int = 40
    min_apps: int = 2
    similarity_threshold: float = 0.8
    significance: float = 1e-3
    keep_chrome_labels: bool = True
    chrome_roles: frozenset[str] = field(default=DEFAULT_CHROME_ROLES)
    structural_label_roles: frozenset[str] = field(default=DEFAULT_STRUCTURAL_LABEL_ROLES)
    work_domains: frozenset[str] = field(default_factory=frozenset)
    browser_apps: frozenset[str] = field(default=DEFAULT_BROWSER_APPS)
    extra_path_words: frozenset[str] = field(default_factory=frozenset)
    extra_label_words: frozenset[str] = field(default_factory=frozenset)
    key_lifetime_s: float = MAX_RETENTION_S

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool; shadow mining needs an explicit opt-in")
        if self.max_symbols < 1:
            raise ValueError("max_symbols must be at least 1")
        if not 0 < self.max_age_s <= MAX_RETENTION_S:
            raise ValueError("max_age_s must be positive and at most 24 hours")
        if not 0 < self.key_lifetime_s <= MAX_RETENTION_S:
            raise ValueError("key_lifetime_s must be positive and at most 24 hours")
        if isinstance(self.work_domains, str):
            raise TypeError("work_domains must be a collection of domain names")
        object.__setattr__(
            self, "work_domains", frozenset(normalize_domain(d) for d in self.work_domains)
        )
        object.__setattr__(
            self, "browser_apps", frozenset(a.strip().casefold() for a in self.browser_apps)
        )
        object.__setattr__(
            self, "extra_path_words", frozenset(w.strip().casefold() for w in self.extra_path_words)
        )
        object.__setattr__(
            self,
            "extra_label_words",
            frozenset(w.strip().casefold() for w in self.extra_label_words),
        )
        if self.min_occurrences < 2:
            raise ValueError("min_occurrences must be at least 2")
        if not 2 <= self.min_length <= self.max_length:
            raise ValueError("need 2 <= min_length <= max_length")
        if self.min_apps < 1:
            raise ValueError("min_apps must be at least 1")
        if not 0.0 < self.similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be in (0, 1]")
        if not 0.0 < self.significance < 1.0:
            raise ValueError("significance must be in (0, 1)")
