"""Structural checks on the PWA shell.

The acceptance requirement is that raw evidence "is never cached by the
PWA/service worker".  A comment cannot prove that, so these tests assert the
worker's *structure*: it has no write path other than one frozen literal list,
and its fetch handler is an allowlist that leaves everything else alone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from engine.portal.server import PROTECTED_PREFIX, SHELL_ASSETS

SHELL = Path(__file__).resolve().parents[2] / "engine" / "portal" / "shell"
SW = (SHELL / "sw.js").read_text()
APP = (SHELL / "app.js").read_text()
INDEX = (SHELL / "index.html").read_text()


def _precached_paths() -> list[str]:
    """The exact literal list the worker may store, parsed from the source."""
    match = re.search(r"const SHELL_ASSETS = Object\.freeze\((\[[^\]]*\])\)", SW)
    assert match, "SHELL_ASSETS must remain a frozen literal list"
    return json.loads(re.sub(r",\s*\]", "]", match.group(1)))


def test_the_worker_has_no_cache_write_path_other_than_the_frozen_list() -> None:
    # A `put` would let any fetched response -- including a protected crop --
    # be stored after the fact. There must be none.
    assert "cache.put" not in SW
    assert ".put(" not in SW
    # `addAll` is the single write, and its only argument is the constant.
    writes = re.findall(r"\.addAll\(([^)]*)\)", SW)
    assert writes == ["SHELL_ASSETS"]


def test_the_precache_list_contains_no_protected_route() -> None:
    for path in _precached_paths():
        assert not path.startswith(PROTECTED_PREFIX), path
        assert not path.startswith("/api/"), path


def test_the_precache_list_is_a_subset_of_the_servers_shell_assets() -> None:
    served = set(SHELL_ASSETS) | {"/"}
    assert set(_precached_paths()) <= served


def test_the_fetch_handler_is_an_allowlist_that_bails_before_responding() -> None:
    handler = SW[SW.index('addEventListener("fetch"') :]
    guard = handler.index("if (!SHELL_ASSETS.includes(url.pathname)) return;")
    responds = handler.index("event.respondWith")
    # The allowlist guard must come first, so a protected request is never
    # intercepted at all -- not intercepted and then passed through.
    assert guard < responds
    # A new protected route is excluded by default because the guard is an
    # allowlist rather than a list of things to skip.
    assert "startsWith(PROTECTED_PREFIX)" not in handler


def test_shell_assets_are_served_network_first_so_a_fix_can_reach_a_phone() -> None:
    """Cache-first would pin the shell installed the first time a phone paired.

    A browser only reinstalls a worker when ``sw.js`` itself changes bytes, so
    a cache-first handler would keep serving an old ``app.js`` forever. The
    cached copy must be the offline fallback, not the preferred answer.
    """
    handler = SW[SW.index('addEventListener("fetch"') :]
    network = handler.index("fetch(request)")
    cache = handler.index("caches.match(request)")
    assert network < cache
    assert ".catch(" in handler


def test_the_shell_never_persists_a_credential_beyond_the_tab() -> None:
    assert "localStorage" not in APP
    assert "window.sessionStorage" in APP
    # A pairing secret arrives in the fragment and is removed from history
    # before anything else runs.
    assert 'history.replaceState(null, "", "/")' in APP


def test_every_fetch_in_the_shell_opts_out_of_every_cache() -> None:
    """Belt and braces beside the ``no-store`` response headers."""
    # Split on call sites and check each one's option object, so a new fetch
    # that forgets the flag fails here.
    chunks = APP.split("fetch(")[1:]
    assert len(chunks) >= 3, "the shell must call the protected routes"
    for chunk in chunks:
        if chunk.lstrip().startswith("request)"):
            continue  # the service-worker-free passthrough in loadFrame's guard
        assert 'cache: "no-store"' in chunk[:700], chunk[:200]
    assert APP.count('cache: "no-store"') == len(
        [c for c in chunks if not c.lstrip().startswith("request)")]
    )


def test_the_shell_renders_the_engines_action_set_without_adding_to_it() -> None:
    """Desktop presents; it does not decide."""
    assert "task.allowed_actions" in APP
    # Buttons are filtered from the signed task's list, never unioned with a
    # local default set.
    assert re.search(
        r"task\.allowed_actions\s*\.filter\(\(action\) => ACTION_WIRE\[action\]\)",
        APP.replace("\n", "").replace("  ", ""),
    )
    # The mapping mirrors Flow's own action names and adds no others.
    assert set(re.findall(r'wire: "(\w+)"', APP)) == {
        "continue",
        "skip",
        "teach",
        "escalate",
    }


def test_an_accepted_tap_is_never_rendered_as_success() -> None:
    assert "Waiting for this computer to check the live screen" in APP
    assert "The result is uncertain. Do not answer again" in APP
    assert "VERIFIED" not in APP


def test_the_shell_is_a_responsive_pwa_and_not_a_native_project() -> None:
    assert 'name="viewport"' in INDEX and "viewport-fit=cover" in INDEX
    assert 'rel="manifest"' in INDEX
    manifest = json.loads((SHELL / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["scope"] == "/"
    # Safe-area insets keep the action bar above the browser chrome on a phone.
    assert "env(safe-area-inset-bottom)" in (SHELL / "styles.css").read_text()


def test_no_native_mobile_project_was_added() -> None:
    root = Path(__file__).resolve().parents[2]
    for forbidden in ("ios", "android", "Podfile", "build.gradle"):
        assert not (root / forbidden).exists(), forbidden
