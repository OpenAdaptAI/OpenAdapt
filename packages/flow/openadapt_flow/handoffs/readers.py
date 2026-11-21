"""Readers: look up one item in the other party's system.

A reader takes the item's key and answers with what the other system holds
for it, as flat ``field id -> value`` text:

- ``found``: exactly one record has the key;
- ``not_found``: none does (it never arrived, or not yet);
- ``ambiguous``: more than one does, so none of them confirms anything;
- ``unreachable``: the read failed (network, a 4xx or 5xx, an expired
  token, a body that isn't what the profile says). Never read as
  ``not_found``.

What to read is a profile (data), not code. :class:`FhirSearchProfile` reads
any FHIR R4 server: a search by one parameter and a path per field. A UI
read-back plugs in the same way, by returning a :class:`ReadResult`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.fhir_read import fhir_read_headers, next_page_url
from openadapt_flow.handoffs.ledger import ReadMethod
from openadapt_flow.runtime.effects.fhir import extract_path
from openadapt_flow.runtime.effects.readback_values import ValueKind

ReadStatus = Literal["found", "not_found", "ambiguous", "unreachable"]


@dataclass(frozen=True)
class ReadResult:
    status: ReadStatus
    #: For ``found``: each profile field's value as text, None when absent.
    fields: dict[str, Optional[str]] = field(default_factory=dict)
    #: SHA-256 of the body read, for the observation's evidence digest.
    evidence_sha256: Optional[str] = None
    #: The receiver's own date order per field (``dmy``/``mdy``), when its
    #: profile declares one. The sender's order never reads its values.
    date_orders: dict[str, Optional[str]] = field(default_factory=dict)


class Reader(Protocol):
    read_method: ReadMethod

    def read(self, key: str) -> ReadResult: ...


class FieldPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Dotted path in the resource (``birthDate``, ``name.family``,
    #: ``telecom[system=phone].value``).
    path: str
    kind: ValueKind = "text"
    date_order: Optional[Literal["dmy", "mdy"]] = None


class FhirSearchProfile(BaseModel):
    """How to find one item in a FHIR R4 server and read its fields."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(pattern=r"^[A-Z][A-Za-z]{1,63}$")
    #: The search parameter that finds the item by its key (``identifier``).
    key_param: str = Field(pattern=r"^[a-z][a-z0-9_:.-]{0,63}$")
    #: How the key is written into the search (``urn:x|{key}``).
    key_template: str = "{key}"
    #: Where each returned record holds the key. Every record the search
    #: returns is checked here too, so a server that ignores the search
    #: parameter (and returns everything) can't make a wrong record "found".
    key_path: str
    fields: dict[str, FieldPath] = Field(min_length=1, max_length=64)
    #: The other party, for people.
    counterparty: str = Field(default="the receiver", max_length=80)
    system_kind: Literal[
        "ehr",
        "payer_portal",
        "bank",
        "erp",
        "crm",
        "government_portal",
        "email",
        "other",
    ] = "other"

    @classmethod
    def load(cls, path: str | Path) -> "FhirSearchProfile":
        import yaml

        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


def _text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


class FhirSearchReader:
    """Read one item from a FHIR R4 server by a profile.

    ``base_url`` is the FHIR base; the token comes from ``token`` or the
    environment variable ``token_env`` and is never logged. Follows ``next``
    links on the same origin, so a second match on page two still counts.
    """

    read_method: ReadMethod = "api_readback"

    def __init__(
        self,
        profile: FhirSearchProfile,
        base_url: str,
        *,
        token: Optional[str] = None,
        token_env: Optional[str] = None,
        verify_tls: bool = True,
        timeout: float = 20.0,
        client: Any = None,
    ) -> None:
        self.profile = profile
        self.base_url = base_url.rstrip("/")
        self._token = (
            token
            if token is not None
            else (os.environ.get(token_env) if token_env else None)
        )
        self._verify = verify_tls
        self._timeout = timeout
        self._client = client

    def _get(self, url: str, params: Optional[Mapping[str, str]]) -> Any:
        headers = fhir_read_headers()
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._client is not None:
            return self._client.get(url, params=params, headers=headers)
        import httpx

        with httpx.Client(verify=self._verify, timeout=self._timeout) as client:
            return client.get(url, params=params, headers=headers)

    def read(self, key: str) -> ReadResult:
        from urllib.parse import urlsplit

        url: Optional[str] = f"{self.base_url}/{self.profile.resource}"
        params: Optional[dict[str, str]] = {
            self.profile.key_param: self.profile.key_template.format(key=key)
        }
        origin = urlsplit(self.base_url)[:2]
        matches: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        pages = 0
        seen = 0
        declared_total: Optional[int] = None
        while url is not None:
            pages += 1
            if pages > 20:
                return ReadResult("unreachable")
            try:
                response = self._get(url, params)
            except Exception:  # noqa: BLE001 - any transport failure
                return ReadResult("unreachable")
            if not 200 <= int(getattr(response, "status_code", 0)) < 300:
                return ReadResult("unreachable")
            body = response.content if hasattr(response, "content") else b""
            digest.update(body if isinstance(body, bytes) else str(body).encode())
            try:
                bundle = json.loads(body)
            except (TypeError, ValueError):
                return ReadResult("unreachable")
            if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
                return ReadResult("unreachable")
            entries = bundle.get("entry", [])
            if not isinstance(entries, list):
                return ReadResult("unreachable")
            for entry in entries:
                resource = entry.get("resource") if isinstance(entry, dict) else None
                if not isinstance(resource, dict):
                    return ReadResult("unreachable")
                if resource.get("resourceType") == "OperationOutcome":
                    # Missing-evidence class: an error outcome means the search
                    # is partial, so its matches prove nothing.
                    issues = resource.get("issue") or []
                    if not isinstance(issues, list) or any(
                        not isinstance(i, dict)
                        or i.get("severity") not in ("information", "warning")
                        for i in issues
                    ):
                        return ReadResult("unreachable")
                    continue
                if resource.get("resourceType") != self.profile.resource:
                    return ReadResult("unreachable")
                seen += 1
                if _text(extract_path(resource, self.profile.key_path)) == key:
                    matches.append(resource)
            total = bundle.get("total")
            if total is not None:
                if type(total) is not int or total < 0:
                    return ReadResult("unreachable")
                declared_total = total
            try:
                url = next_page_url(bundle)
            except ValueError:
                return ReadResult("unreachable")
            if url is not None and urlsplit(url)[:2] != origin:
                return ReadResult("unreachable")
            params = None
        evidence = digest.hexdigest()
        if declared_total is not None and declared_total != seen:
            # The server says it matched more (or fewer) than it returned.
            return ReadResult("unreachable", evidence_sha256=evidence)
        if not matches:
            return ReadResult("not_found", evidence_sha256=evidence)
        if len(matches) > 1:
            return ReadResult("ambiguous", evidence_sha256=evidence)
        record = matches[0]
        fields = {
            field_id: _text(extract_path(record, spec.path))
            for field_id, spec in self.profile.fields.items()
        }
        return ReadResult(
            "found",
            fields=fields,
            evidence_sha256=evidence,
            date_orders={
                field_id: spec.date_order
                for field_id, spec in self.profile.fields.items()
            },
        )


__all__ = [
    "FhirSearchProfile",
    "FhirSearchReader",
    "FieldPath",
    "ReadResult",
    "ReadStatus",
    "Reader",
]
