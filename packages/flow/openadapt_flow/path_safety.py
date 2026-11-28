"""Platform root symlink canonicalization for receipt and authority stores.

macOS exposes ``/tmp`` and ``/var`` as root-owned symlinks to ``/private/tmp``
and ``/private/var``. Receipt store, ledger, and authority path validators that
reject every symlink ancestor fail unnecessarily on macOS temp paths unless
those known platform roots are canonicalized first.

:func:`canonicalize_platform_root_symlinks` resolves ONLY the known OS platform
root prefix (``/tmp``, ``/var``, or ``tempfile.gettempdir()``) while keeping
every descendant path component lexical. Any symlink created by a user or
attacker at or inside the store root remains a symlink and is still rejected.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = [
    "canonicalize_platform_root_symlinks",
    "validate_no_store_symlinks",
]

_KNOWN_PLATFORM_ROOTS: tuple[Path, ...] = (
    Path("/tmp"),
    Path("/var"),
)


def _platform_root_mappings() -> list[tuple[Path, Path]]:
    """Return ``(lexical_root, resolved_root)`` pairs for OS-managed symlink roots."""
    mappings: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    candidates = list(_KNOWN_PLATFORM_ROOTS)
    temp_root = Path(tempfile.gettempdir()).absolute()
    candidates.append(temp_root)

    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        try:
            if not root.exists():
                continue
            resolved = root.resolve(strict=True)
        except OSError:
            continue
        if resolved != root:
            mappings.append((root, resolved))
    # Match longest prefix first so /var/folders beats /var if needed.
    mappings.sort(key=lambda pair: len(pair[0].parts), reverse=True)
    return mappings


def canonicalize_platform_root_symlinks(path: Path | str) -> Path:
    """Canonicalize only known OS platform root symlinks (``/tmp``, ``/var``).

    Every path component below the platform root stays lexical, so descendant
    symlinks are preserved for subsequent refusal checks.
    """
    raw = os.path.abspath(os.fspath(Path(path).expanduser()))
    candidate = Path(raw)
    for lexical_root, resolved_root in _platform_root_mappings():
        try:
            relative = candidate.relative_to(lexical_root)
        except ValueError:
            continue
        return resolved_root / relative
    return candidate


def validate_no_store_symlinks(
    root: Path | str,
    *,
    label: str = "receipt store",
    check_descendants: bool = True,
) -> Path:
    """Canonicalize platform roots and refuse any symlink at, above, or inside ``root``.

    Args:
        root: Candidate store root directory.
        label: Human-readable store label for error messages.
        check_descendants: When True (default), also refuse any symlink found
            inside ``root`` when ``root`` already exists.

    Returns:
        The canonicalized ``Path`` with platform root symlinks normalized.

    Raises:
        ValueError: If any non-platform ancestor, ``root`` itself, or any
            descendant inside ``root`` is a symlink.
    """
    canonical = canonicalize_platform_root_symlinks(root)
    for component in (canonical, *canonical.parents):
        if component.is_symlink():
            raise ValueError(f"{label} root must not traverse a symlink: {component}")
    if check_descendants and canonical.exists():
        if not canonical.is_dir():
            raise ValueError(f"{label} root is not a directory: {canonical}")
        for entry in canonical.rglob("*"):
            if entry.is_symlink():
                raise ValueError(f"{label} must not contain a symlink: {entry}")
    return canonical
