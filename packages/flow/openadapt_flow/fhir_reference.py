"""FHIR Reference typing and read-only target-resolution evidence.

StructureDefinition metadata identifies Reference elements. Display text never
identifies a target. Judging consumes a snapshot; only ReferenceReader performs
reads, and it never follows a reference outside the configured FHIR base.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

_RESOURCE = re.compile(r"[A-Z][A-Za-z0-9]*")
_ID = re.compile(r"[A-Za-z0-9\-.]{1,64}")
_PROFILE = "http://hl7.org/fhir/StructureDefinition/"


class ReferenceUnreadable(RuntimeError):
    """The supplied evidence cannot establish one reference target."""


def definition_digest(definition: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReferenceSchema(BaseModel):
    """A contract pins the exact StructureDefinition snapshot it trusts."""

    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any]
    sha256: str

    @model_validator(mode="after")
    def _snapshot(self) -> "ReferenceSchema":
        d = self.definition
        if self.sha256 != definition_digest(d):
            raise ValueError("the Reference schema digest does not match")
        if (
            d.get("resourceType") != "StructureDefinition"
            or d.get("kind") != "resource"
            or not isinstance(d.get("type"), str)
            or not _RESOURCE.fullmatch(d["type"])
            or not d.get("url")
            or not isinstance(d.get("snapshot"), dict)
            or not isinstance(d["snapshot"].get("element"), list)
        ):
            raise ValueError(
                "Reference typing needs a resource StructureDefinition snapshot"
            )
        seen: set[str] = set()
        for e in d["snapshot"]["element"]:
            if not isinstance(e, dict) or not isinstance(e.get("path"), str):
                raise ValueError("a schema element needs its path")
            path = e["path"]
            if path in seen:
                # Slices and choice types need more context than a plain path.
                raise ValueError("duplicate/sliced schema paths are unsupported")
            seen.add(path)
            if path != d["type"] and not path.startswith(d["type"] + "."):
                raise ValueError("a schema element belongs to another resource type")
        return self

    def paths(self) -> dict[str, tuple[str, ...]]:
        out = {}
        prefix = self.definition["type"] + "."
        for e in self.definition["snapshot"]["element"]:
            types = e.get("type") or []
            if not isinstance(types, list):
                raise ValueError("schema element types must be a list")
            if len(types) != 1 or not isinstance(types[0], dict):
                continue
            t = types[0]
            if t.get("code") != "Reference":
                continue
            path = e["path"][len(prefix) :]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(\.[A-Za-z][A-Za-z0-9]*)*", path):
                raise ValueError("choice or non-plain Reference paths are unsupported")
            profiles = t.get("targetProfile") or []
            if not isinstance(profiles, list) or any(
                not isinstance(x, str) for x in profiles
            ):
                raise ValueError("Reference target profiles must be URLs")
            targets = tuple(
                p[len(_PROFILE) :] for p in profiles if p.startswith(_PROFILE)
            )
            if len(targets) != len(profiles) or any(
                not _RESOURCE.fullmatch(t) for t in targets
            ):
                raise ValueError("custom target profiles need a profile resolver")
            out[path] = targets
        return out


def reference_value(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"reference": value}
    if not isinstance(value, Mapping):
        raise ReferenceUnreadable(
            "a Reference is neither an object nor a literal reference"
        )
    # Evidence contains identity material only. Unsupported Identifier
    # metadata must not be silently dropped or copied into a proof snapshot.
    out = {k: value[k] for k in ("reference", "type") if k in value}
    if any(not isinstance(v, str) for v in out.values()):
        raise ReferenceUnreadable("literal reference and type must be strings")
    if "reference" in out:
        try:
            parts = urlsplit(out["reference"])
        except ValueError as exc:
            raise ReferenceUnreadable("invalid literal reference URI") from exc
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ReferenceUnreadable(
                "reference URI credentials, queries and fragments are unsupported"
            )
    if "type" in out and not _RESOURCE.fullmatch(out["type"]):
        raise ReferenceUnreadable("unsupported Reference.type")
    if "identifier" in value:
        identifier = value["identifier"]
        if not isinstance(identifier, Mapping) or set(identifier) - {"system", "value"}:
            raise ReferenceUnreadable("unsupported Reference.identifier metadata")
        if any(not isinstance(v, str) for v in identifier.values()):
            raise ReferenceUnreadable("identifier system and value must be strings")
        out["identifier"] = dict(identifier)
    return out


def _key(value: Any, targets: Sequence[str]) -> str:
    try:
        return json.dumps([reference_value(value), sorted(targets)], sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ReferenceUnreadable(
            "a Reference contains non-JSON identity data"
        ) from exc


@dataclass(frozen=True)
class ReferenceEvidence:
    """Resolved identities and explicit failures, with no reader attached."""

    identities: Mapping[str, str] = field(default_factory=dict)
    problems: Mapping[str, str] = field(default_factory=dict)
    versions: Mapping[str, str] = field(default_factory=dict)

    def identity(self, value: Any, targets: Sequence[str]) -> str:
        key = _key(value, targets)
        if key not in self.identities:
            raise ReferenceUnreadable(
                self.problems.get(key, "reference target was not resolved")
            )
        return self.identities[key]

    def version(self, value: Any, targets: Sequence[str]) -> str | None:
        self.identity(value, targets)
        return self.versions.get(_key(value, targets))

    def search_value(self, value: Any, targets: Sequence[str], base_url: str) -> str:
        identity = self.identity(value, targets)
        prefix = base_url.rstrip("/") + "/"
        if not identity.startswith(prefix):
            raise ReferenceUnreadable("reference search identity belongs to another base")
        relative = identity[len(prefix) :]
        parts = relative.split("/")
        if (
            len(parts) != 2
            or not _RESOURCE.fullmatch(parts[0])
            or not _ID.fullmatch(parts[1])
        ):
            raise ReferenceUnreadable("reference search needs one logical Type/id")
        return relative


class ReferenceReader:
    """Collect target evidence through a FHIR reader's GET and full search."""

    def __init__(
        self,
        base_url: str,
        fetch: Callable[[str], Mapping[str, Any]],
        search: Callable[[str, Mapping[str, str]], Sequence[Mapping[str, Any]]],
    ) -> None:
        self.base = base_url.rstrip("/")
        self.fetch = fetch
        self.search = search
        self._identities: dict[str, str] = {}
        self._problems: dict[str, str] = {}
        self._versions: dict[str, str] = {}

    def snapshot(self) -> ReferenceEvidence:
        return ReferenceEvidence(
            dict(self._identities), dict(self._problems), dict(self._versions)
        )

    def collect(self, value: Any, targets: Sequence[str]) -> None:
        try:
            self.resolve(value, targets)
        except ReferenceUnreadable:
            pass  # retain the refusal; a pure comparison decides whether it needs it

    def _literal(self, value: Any) -> tuple[str, str, str]:
        if not isinstance(value, str) or value != value.strip():
            raise ReferenceUnreadable("a literal reference needs one exact URI")
        url = value if urlsplit(value).scheme else self.base + "/" + value
        p, b = urlsplit(url), urlsplit(self.base)
        if (
            p.scheme not in ("http", "https")
            or p.scheme != b.scheme
            or p.netloc != b.netloc
            or p.username
            or p.password
            or p.query
            or p.fragment
            or not p.path.startswith(b.path.rstrip("/") + "/")
        ):
            raise ReferenceUnreadable("a reference outside the FHIR base is unresolved")
        tail = p.path[len(b.path.rstrip("/")) + 1 :].split("/")
        if (
            len(tail) not in (2, 4)
            or not _RESOURCE.fullmatch(tail[0])
            or not _ID.fullmatch(tail[1])
        ):
            raise ReferenceUnreadable("unsupported literal reference identity")
        if len(tail) == 4 and (tail[2] != "_history" or not _ID.fullmatch(tail[3])):
            raise ReferenceUnreadable("unsupported versioned reference")
        # Logical identity names the resource. The requested version stays
        # in the read path and separate evidence; it never hides a duplicate.
        return self.base + "/" + "/".join(tail[:2]), tail[0], "/".join(tail)

    def resolve(self, value: Any, targets: Sequence[str]) -> str:
        key = _key(value, targets)
        if key in self._identities:
            return self._identities[key]
        if key in self._problems:
            raise ReferenceUnreadable(self._problems[key])
        try:
            identity, version = self._resolve(reference_value(value), targets)
        except Exception as exc:  # a transport failure cannot become a match
            problem = (
                str(exc)
                if isinstance(exc, ReferenceUnreadable)
                else "reference target read failed"
            )
            self._problems[key] = problem
            raise ReferenceUnreadable(problem) from exc
        self._identities[key] = identity
        if version is not None:
            self._versions[key] = version
        return identity

    def _resolve(
        self, ref: Mapping[str, Any], targets: Sequence[str]
    ) -> tuple[str, str | None]:
        literal = ref.get("reference")
        identifier = ref.get("identifier")
        declared = ref.get("type")
        if declared is not None and (
            not isinstance(declared, str) or not _RESOURCE.fullmatch(declared)
        ):
            raise ReferenceUnreadable("unsupported Reference.type")
        target = None
        identity = None
        version = None
        rtype = declared
        if literal is not None:
            identity, rtype, path = self._literal(literal)
            if declared is not None and declared != rtype:
                raise ReferenceUnreadable(
                    "Reference.type contradicts its literal target"
                )
            target = self.fetch(path)
            if (
                not isinstance(target, Mapping)
                or target.get("resourceType") != rtype
                or target.get("id") != path.split("/")[1]
            ):
                raise ReferenceUnreadable("the target read returned another identity")
            if (
                "/_history/" in path
                and (target.get("meta") or {}).get("versionId") != path.split("/")[-1]
            ):
                raise ReferenceUnreadable("the target read returned another version")
            if "/_history/" in path:
                version = path.split("/")[-1]
        permitted = tuple(t for t in targets if t != "Resource")
        if rtype is not None and permitted and rtype not in permitted:
            raise ReferenceUnreadable("the target type contradicts the schema")
        if identifier is not None:
            if not isinstance(identifier, Mapping):
                raise ReferenceUnreadable("Reference.identifier is not an Identifier")
            system, value = identifier.get("system"), identifier.get("value")
            if not all(
                isinstance(v, str) and v and v == v.strip() for v in (system, value)
            ):
                raise ReferenceUnreadable(
                    "an identifier needs its exact system and value"
                )
            if any(c in str(system) + str(value) for c in "|,$\\"):
                raise ReferenceUnreadable("identifier search escaping is unsupported")
            candidates = [rtype] if rtype is not None else list(permitted)
            if not candidates:
                raise ReferenceUnreadable(
                    "identifier resolution needs a declared target type"
                )
            matches: dict[str, Mapping[str, Any]] = {}
            for kind in candidates:
                for res in self.search(
                    kind, {"identifier": f"{system}|{value}", "_count": "200"}
                ):
                    if (
                        res.get("resourceType") != kind
                        or not isinstance(res.get("id"), str)
                        or not _ID.fullmatch(res["id"])
                    ):
                        raise ReferenceUnreadable(
                            "identifier search returned an invalid target identity"
                        )
                    ids = res.get("identifier")
                    if not isinstance(ids, list) or not any(
                        isinstance(i, Mapping)
                        and i.get("system") == system
                        and i.get("value") == value
                        for i in ids
                    ):
                        raise ReferenceUnreadable(
                            "identifier search returned an unverified identifier"
                        )
                    matches[f"{self.base}/{kind}/{res['id']}"] = res
            if len(matches) != 1:
                raise ReferenceUnreadable(
                    "identifier search did not resolve exactly one target"
                )
            found = next(iter(matches))
            if identity is not None:
                current = identity.split("/_history/", 1)[0]
                if current != found:
                    raise ReferenceUnreadable(
                        "Reference.reference and identifier contradict"
                    )
                ids = target.get("identifier") if target is not None else None
                if not isinstance(ids, list) or not any(
                    isinstance(i, Mapping)
                    and i.get("system") == system
                    and i.get("value") == value
                    for i in ids
                ):
                    raise ReferenceUnreadable(
                        "the literal target does not hold its claimed identifier"
                    )
            else:
                identity = found
        if identity is None:
            raise ReferenceUnreadable(
                "a display alone does not identify a reference target"
            )
        return identity, version
