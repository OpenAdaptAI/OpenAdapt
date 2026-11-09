"""Check GitHub for a newer OpenAdapt Desktop release.

Desktop ships as immutable ``openadapt-desktop-vX.Y.Z`` releases in the
OpenAdapt monorepo (see RELEASES.md). GitHub's ``/releases/latest`` names the
launcher's ``vX.Y.Z`` release there, so this reads the release list and picks
the newest published, non-prerelease ``openadapt-desktop-v`` release.

The request is a plain anonymous GET to the GitHub API. It carries no install
id, version, account, or other identifier, so GitHub sees only what any HTTP
client shows it. It runs at most once a day, in the background, with a short
timeout, and any failure is silent. ``OPENADAPT_UPDATE_CHECK=false`` or the
Settings switch turns it off.

This only tells the person a release exists and links to it. Installing stays
manual until the signed updater feed exists (the Tauri updater plugin stays
unregistered, see ``src-tauri/src/main.rs``).
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable

from openadapt_desktop import __version__, privacy_settings

try:
    from loguru import logger
except ImportError:  # pragma: no cover - desktop runtime always has loguru
    import logging

    logger = logging.getLogger("openadapt_desktop.updates")

RELEASES_API = "https://api.github.com/repos/OpenAdaptAI/OpenAdapt/releases"
RELEASES_PAGE = "https://github.com/OpenAdaptAI/OpenAdapt/releases"
TAG_PREFIX = "openadapt-desktop-v"
CHECK_INTERVAL_S = 24 * 60 * 60
TIMEOUT_S = 5.0
#: The only headers sent. No version, install id, or token.
REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "openadapt-desktop-update-check",
}
REQUEST_PARAMS = {"per_page": "50"}

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Fetch = Callable[[], list[dict[str, Any]]]

_lock = threading.Lock()
_running = False


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.match((value or "").strip().lstrip("v"))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(candidate: str, current: str) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and cur is not None and new > cur


def newest_release(releases: list[dict[str, Any]]) -> dict[str, str] | None:
    """The newest published, non-prerelease Desktop release in ``releases``."""
    best: tuple[tuple[int, int, int], dict[str, str]] | None = None
    for release in releases or []:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith(TAG_PREFIX):
            continue
        version = tag[len(TAG_PREFIX) :]
        parsed = parse_version(version)
        if parsed is None:
            continue
        url = str(release.get("html_url") or "")
        if not url.startswith(RELEASES_PAGE + "/"):
            url = f"{RELEASES_PAGE}/tag/{tag}"
        if best is None or parsed > best[0]:
            best = (parsed, {"version": version, "url": url})
    return best[1] if best else None


def fetch_releases() -> list[dict[str, Any]]:
    import httpx

    response = httpx.get(
        RELEASES_API,
        params=REQUEST_PARAMS,
        headers=REQUEST_HEADERS,
        timeout=TIMEOUT_S,
        follow_redirects=True,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _cache_path():
    return privacy_settings.state_dir() / "update_check.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        logger.debug("Could not save the update check result")


def status(current: str = __version__) -> dict[str, Any]:
    """The last known result, without touching the network."""
    enabled = privacy_settings.update_check_enabled()
    cache = _read_cache() if enabled else {}
    latest = cache.get("latest_version")
    latest = latest if isinstance(latest, str) and parse_version(latest) else None
    url = cache.get("url")
    url = url if isinstance(url, str) and url.startswith(RELEASES_PAGE) else RELEASES_PAGE
    checked_at = cache.get("checked_at")
    return {
        "enabled": enabled,
        "managed_by_env": privacy_settings.update_check_env_disabled(),
        "current_version": current,
        "latest_version": latest,
        "available": bool(latest and is_newer(latest, current)),
        "url": url,
        "checked_at": checked_at if isinstance(checked_at, int | float) else None,
    }


def is_due(now: float | None = None) -> bool:
    checked_at = _read_cache().get("checked_at")
    if not isinstance(checked_at, int | float):
        return True
    now = time.time() if now is None else now
    return now - checked_at >= CHECK_INTERVAL_S or checked_at > now


def check_now(
    current: str = __version__,
    *,
    fetch: Fetch | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Ask GitHub once and save the answer. Returns :func:`status`.

    Records the attempt time even when GitHub can't be reached, so a failing
    network is retried the next day, not on every launch.
    """
    if not privacy_settings.update_check_enabled():
        return status(current)
    now = time.time() if now is None else now
    cache: dict[str, Any] = {"checked_at": now}
    try:
        newest = newest_release((fetch or fetch_releases)())
        if newest:
            cache["latest_version"] = newest["version"]
            cache["url"] = newest["url"]
    except Exception:
        logger.debug("Update check skipped (GitHub unreachable)")
        previous = _read_cache()
        for key in ("latest_version", "url"):
            if key in previous:
                cache[key] = previous[key]
        _write_cache(cache)
        return status(current)
    _write_cache(cache)
    result = status(current)
    from openadapt_desktop import telemetry

    telemetry.capture("update_check", {"update_available": result["available"]})
    return result


def check_in_background(
    current: str = __version__,
    *,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    force: bool = False,
    fetch: Fetch | None = None,
) -> bool:
    """Start a check on a daemon thread if one is due. Returns whether it started."""
    global _running
    if not privacy_settings.update_check_enabled():
        return False
    if not force and not is_due():
        return False
    with _lock:
        if _running:
            return False
        _running = True

    def _run() -> None:
        global _running
        try:
            result = check_now(current, fetch=fetch)
            if on_result is not None:
                on_result(result)
        except Exception:
            logger.debug("Update check failed quietly")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_run, name="oa-update-check", daemon=True).start()
    return True
