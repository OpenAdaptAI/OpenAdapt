"""Keep host paths out of committed evidence.

Ledgers, switch records and console rows are committed to a public
repository, so a path on the machine that ran the soak is written relative
to the run's root (its ``--out`` directory). A path under the working
directory is written relative to it. Any other path under the home
directory or a temporary directory keeps only its name. Paths inside the app
(a container's ``/var/www/...``) are left alone.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

OUTSIDE = "<outside-run>"

_TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


def _private_prefixes() -> list[str]:
    prefixes = {str(Path.home()), tempfile.gettempdir(), *(_TEMP_ROOTS)}
    for prefix in list(prefixes):
        prefixes.add(os.path.realpath(prefix))
    return sorted((p.rstrip("/") + "/" for p in prefixes if p), key=len, reverse=True)


def record_path(path: Any, root: Path | str) -> str:
    """``path`` as committed evidence records it."""

    text = str(path)
    if not os.path.isabs(text):
        return text
    resolved = os.path.realpath(text)
    home = os.path.realpath(str(Path.home()))
    bases = [root]
    cwd = os.path.realpath(os.getcwd())
    # A working directory at or above home would keep the user's name.
    if not (home == cwd or home.startswith(cwd.rstrip("/") + "/")):
        bases.append(cwd)
    for base in bases:
        base_real = os.path.realpath(str(base))
        for candidate in (text, resolved):
            if candidate == base_real or candidate.startswith(base_real + "/"):
                rel = os.path.relpath(candidate, base_real)
                return "." if rel == "." else Path(rel).as_posix()
        base_text = str(base).rstrip("/")
        if text.startswith(base_text + "/"):
            return Path(os.path.relpath(text, base_text)).as_posix()
    if any(text.startswith(prefix) for prefix in _private_prefixes()):
        return f"{OUTSIDE}/{Path(text).name}"
    return text


def _pattern() -> re.Pattern[str]:
    alternatives = "|".join(re.escape(p) for p in _private_prefixes())
    return re.compile(rf"(?:{alternatives})[^\s\"'`,;)\]}}]*")


def relative_paths(value: Any, root: Path | str) -> Any:
    """``value`` with every host path, alone or inside text, made relative."""

    if isinstance(value, Path):
        return record_path(value, root)
    if isinstance(value, str):
        if os.path.isabs(value) and not any(ch.isspace() for ch in value):
            return record_path(value, root)
        return _pattern().sub(lambda m: record_path(m.group(0), root), value)
    if isinstance(value, dict):
        return {k: relative_paths(v, root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [relative_paths(v, root) for v in value]
    return value
