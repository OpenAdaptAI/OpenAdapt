"""Built-in declarative UI mechanics profiles for ``form_tx``.

Profiles describe UI architecture classes (such as server-driven partial-page-reload
wizards or classic postback portals) with zero application or vendor names.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openadapt_flow.form_tx.types import UIProfileSpec

_PROFILES_DIR = Path(__file__).resolve().parent
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _unquote_scalar(token: str) -> Any:
    """Parse a scalar token for the minimal YAML subset fallback."""
    s = token.strip()
    if s in ("null", "~", ""):
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        quote = s[0]
        inner = s[1:-1]
        if quote == "'":
            return inner.replace("''", "'")
        return bytes(inner, "utf-8").decode("unicode_escape")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if s.startswith("[") and s.endswith("]"):
        body = s[1:-1].strip()
        if not body:
            return []
        return [_unquote_scalar(part) for part in body.split(",")]
    return s


def _parse_minimal_yaml_subset(raw_text: str) -> dict[str, Any]:
    """Safe fallback parser for flat key/scalar/list YAML profiles when PyYAML is absent."""
    result: dict[str, Any] = {}
    lines = raw_text.splitlines()
    idx = 0
    while idx < len(lines):
        raw_line = lines[idx]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            idx += 1
            continue
        if ":" not in raw_line or raw_line.startswith((" ", "\t", "-")):
            raise ValueError(
                f"Unsupported YAML construct on line {idx + 1}: {raw_line!r}"
            )
        key, rest = raw_line.split(":", 1)
        key = key.strip()
        rest_stripped = rest.strip()
        if rest_stripped in (">-", ">", "|", "|-"):
            folded_parts: list[str] = []
            idx += 1
            while idx < len(lines):
                cont = lines[idx]
                if not cont.strip():
                    idx += 1
                    continue
                if cont.startswith((" ", "\t")):
                    folded_parts.append(cont.strip())
                    idx += 1
                else:
                    break
            sep = " " if rest_stripped.startswith(">") else "\n"
            result[key] = sep.join(folded_parts)
            continue
        if rest_stripped == "":
            items: list[Any] = []
            idx += 1
            while idx < len(lines):
                item_line = lines[idx]
                item_stripped = item_line.strip()
                if not item_stripped or item_stripped.startswith("#"):
                    idx += 1
                    continue
                if item_line.startswith((" ", "\t")) and item_stripped.startswith("- "):
                    items.append(_unquote_scalar(item_stripped[2:]))
                    idx += 1
                else:
                    break
            result[key] = items
            continue
        result[key] = _unquote_scalar(rest_stripped)
        idx += 1
    return result


def _parse_profile_payload(raw_text: str, source_label: str) -> dict[str, Any]:
    stripped = raw_text.lstrip()
    if stripped.startswith("{"):
        parsed = json.loads(raw_text)
    else:
        try:
            import yaml  # type: ignore[import-untyped]
        except ModuleNotFoundError:
            parsed = _parse_minimal_yaml_subset(raw_text)
        else:
            parsed = yaml.safe_load(raw_text)
    if not isinstance(parsed, Mapping):
        raise ValueError(
            f"UI profile {source_label!r} must deserialize to a mapping, got {type(parsed).__name__}"
        )
    return dict(parsed)


def list_builtin_ui_profiles() -> tuple[str, ...]:
    """Return sorted identifiers of built-in ``UIProfileSpec`` YAML definitions."""
    ids = {
        path.stem
        for ext in ("*.yaml", "*.yml")
        for path in _PROFILES_DIR.glob(ext)
        if path.is_file()
    }
    return tuple(sorted(ids))


def _resolve_profile_path(profile_id_or_path: str | Path) -> Path:
    if isinstance(profile_id_or_path, Path):
        if not profile_id_or_path.is_file():
            raise FileNotFoundError(f"UI profile file not found: {profile_id_or_path}")
        return profile_id_or_path
    if isinstance(profile_id_or_path, str):
        token = profile_id_or_path.strip()
        if not token:
            raise ValueError("profile_id_or_path must be a non-empty string or Path")
        if _PROFILE_ID_RE.fullmatch(token):
            for suffix in (".yaml", ".yml", ".json"):
                builtin_candidate = _PROFILES_DIR / f"{token}{suffix}"
                if builtin_candidate.is_file():
                    return builtin_candidate
        path_candidate = Path(token)
        if path_candidate.is_file():
            return path_candidate
        available = ", ".join(list_builtin_ui_profiles())
        raise FileNotFoundError(
            f"Unknown UI profile {token!r} (not a file path and not in built-in "
            f"profiles: [{available}])"
        )
    raise TypeError(
        f"profile_id_or_path must be str or Path, got {type(profile_id_or_path).__name__}"
    )


def load_ui_profile(profile_id_or_path: str | Path) -> UIProfileSpec:
    """Load and validate a ``UIProfileSpec`` by built-in ``profile_id`` or file path."""
    target_path = _resolve_profile_path(profile_id_or_path)
    raw_text = target_path.read_text(encoding="utf-8")
    payload = _parse_profile_payload(raw_text, str(target_path))
    return UIProfileSpec.model_validate(payload)


__all__ = [
    "list_builtin_ui_profiles",
    "load_ui_profile",
]
