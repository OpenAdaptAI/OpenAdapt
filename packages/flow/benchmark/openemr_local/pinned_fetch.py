# SPDX-License-Identifier: MIT
"""Fetch pinned upstream files at run time and refuse any byte that differs.

Third-party configuration this benchmark builds on is not stored in this
repository. ``fetch_pinned`` downloads each file from the upstream repository
at an exact commit, checks its SHA-256 against the value recorded in
``environment.lock.json``, and caches it under ``out/`` (git-ignored). A hash
mismatch, a missing file, or an unreachable host is a ``FixtureError``-style
``UpstreamFetchError``; nothing unverified is ever written to the cache.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

RAW_URL = "https://raw.githubusercontent.com/{repo}/{commit}/{path}"
CACHE_ENV = "OPENADAPT_UPSTREAM_CACHE"


class UpstreamFetchError(RuntimeError):
    """An upstream file could not be fetched or did not match its pin."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_root(default: Path) -> Path:
    configured = os.environ.get(CACHE_ENV)
    return Path(configured) if configured else default


def fetch_pinned(
    repo: str,
    commit: str,
    files: Mapping[str, str],
    dest: Path,
    *,
    opener=urlopen,
    timeout_s: float = 30.0,
) -> Path:
    """Place every ``path -> sha256`` from ``repo@commit`` under ``dest/commit``.

    Cached files are re-hashed on every call, so a cache edited by hand is
    refused rather than used.
    """

    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise UpstreamFetchError(f"upstream commit must be a full SHA-1: {commit!r}")
    root = dest / commit
    for rel, expected in files.items():
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise UpstreamFetchError(f"refusing unsafe upstream path {rel!r}")
        target = root / rel
        if target.is_file():
            if sha256_bytes(target.read_bytes()) == expected:
                continue
            raise UpstreamFetchError(
                f"cached {rel} at {target} does not match its pinned sha256; "
                "delete the cache to fetch it again"
            )
        url = RAW_URL.format(repo=repo, commit=commit, path=rel)
        try:
            with opener(url, timeout=timeout_s) as response:
                data = response.read()
        except (URLError, OSError) as exc:
            raise UpstreamFetchError(f"could not fetch {url}: {exc}") from exc
        actual = sha256_bytes(data)
        if actual != expected:
            raise UpstreamFetchError(
                f"{url} has sha256 {actual}, expected {expected}; refusing it"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    return root
