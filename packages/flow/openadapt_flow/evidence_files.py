"""Filename-first exclusion of local authority from evidence derivatives.

This policy never opens a candidate file. Apply it before copying, hashing,
archiving or adding an artifact to a publication inventory. Public certificates
and verification registries need an explicitly reviewed path at a publishing
boundary; a generic key-store filename is not evidence of public contents.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterable

_KEY_SUFFIXES = (".key", ".p12", ".pfx", ".jks", ".keystore")
# Unknown input containers cannot be screened by member filenames without
# opening them. Publication refuses them; an exporter may still construct its
# own final archive from an already filtered, frozen source tree.
OPAQUE_CONTAINER_SUFFIXES = (
    ".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar",
    ".whl", ".egg", ".jar", ".war", ".cab", ".iso", ".deb", ".rpm",
)
_KEY_STORES = frozenset(
    {"keys.json", "seal-key.json", "seal-keys.json", "site-keys.json"}
)
_PRIVATE_NAMES = frozenset(
    {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "token"}
)
_PRIVATE_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg", ".private", ".git"})
_PRIVATE_AUTHORITY = frozenset(
    {"attended_capability.json", "attended_capability_history.json"}
)
_DATA_SUFFIX = re.compile(r"\.(?:json|yaml|yml|toml|txt)(?:$|[.~_-])")
_CREDENTIAL_DATA = re.compile(
    r"credential|secret|token|password|passphrase|passkey|"
    r"private[_-]?key|signing[_-]?key|hmac[_-]?key|api[_-]?key|key[_-]?store|"
    r"named[_-]?person[_-]?binding|(?:^|[._-])keys?(?:$|[._-])"
)


def _name_or_backup(name: str, protected: str) -> bool:
    return name == protected or any(
        name.startswith(protected + delimiter) for delimiter in (".", "~", "_", "-")
    )


def is_key_material_path(relative: str | os.PathLike[str]) -> bool:
    """Deny key/credential-shaped paths using names alone, including backups.

    Backslashes are treated as separators on every host. A protected directory
    name also protects its descendants. Source files such as ``seal_keys.py``
    are not key material. Public ``.crt``/``.cer`` files are not private keys;
    unclassified PEM containers are denied because they can hold either.
    """
    parts = PurePosixPath(os.fspath(relative).replace("\\", "/")).parts
    for part in parts:
        name = part.casefold()
        if name in _PRIVATE_DIRECTORIES or any(
            _name_or_backup(name, protected) for protected in _PRIVATE_NAMES
        ):
            return True
        if name == ".env" or name.startswith(".env."):
            return True
        if any(
            re.search(re.escape(suffix) + r"(?:$|[.~_-])", name)
            for suffix in (*_KEY_SUFFIXES, ".pem")
        ):
            return True
        if any(_name_or_backup(name, store) for store in _KEY_STORES):
            return True
        if _DATA_SUFFIX.search(name) and _CREDENTIAL_DATA.search(name):
            return True
    return False


def is_publication_input_path_excluded(relative: str | os.PathLike[str]) -> bool:
    """Exclude key-shaped names and unclassified containers before any read.

    Container names are not claims that their contents are secret. This is a
    publication boundary, distinct from CI's tracked-key filename check.
    """
    if is_key_material_path(relative):
        return True
    parts = PurePosixPath(os.fspath(relative).replace("\\", "/")).parts
    return any(
        any(_name_or_backup(part.casefold(), authority) for authority in _PRIVATE_AUTHORITY)
        or any(
            re.search(re.escape(suffix) + r"(?:$|[.~_-])", part.casefold())
            for suffix in OPAQUE_CONTAINER_SUFFIXES
        )
        for part in parts
    )


def evidence_copy_ignore(directory: str, names: Iterable[str]) -> set[str]:
    """``shutil.copytree`` callback: reject names before copying their bytes."""
    return {name for name in names if is_publication_input_path_excluded(Path(directory) / name)}


def copy_evidence_tree(source: Path, destination: Path) -> Path:
    """Copy a frozen derivative, excluding key names and existing symlinks.

    The caller must keep this source immutable until copying finishes. This
    check does not provide a filesystem snapshot against concurrent swaps.
    """
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Evidence source must be a real directory")
    # Walk names first: do not follow a link while classifying the tree.
    for directory, subdirs, files in os.walk(source, followlinks=False):
        subdirs[:] = [
            name for name in subdirs if not is_publication_input_path_excluded(Path(directory) / name)
        ]
        for name in [*subdirs, *files]:
            path = Path(directory) / name
            if not is_publication_input_path_excluded(path) and path.is_symlink():
                raise ValueError("Evidence cannot contain a symbolic link")
    return Path(shutil.copytree(source, destination, ignore=evidence_copy_ignore))
