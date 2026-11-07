"""FHIR R4 discovery from actual capabilities, verified in a controlled context.

Protocol provenance: HL7 FHIR R4 4.0.1 CapabilityStatement REST interactions,
search.html, bundle-definitions.html and search-parameters.json Resource-id.
No vendor resource name, site, endpoint or field projection is built in.
Synthetic setup projections are supplied by the harness, never the acting arm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.rest_setup import (
    ReadProbe,
    RestReadContext,
    RestReadSetup,
    _ContextSession,
    _digest,
    _rows,
)
from openadapt_flow.runtime.effects.quiescence import QuiescenceAuthority

PROTOCOL_SOURCES = (
    "https://hl7.org/fhir/R4/capabilitystatement-definitions.html",
    "https://hl7.org/fhir/R4/search.html",
    "https://hl7.org/fhir/R4/bundle-definitions.html",
    "https://hl7.org/fhir/R4/search-parameters.json",
)


def search_resources(capability: Mapping[str, Any], base_url: str) -> list[str]:
    """Enumerate declarations, never infer an application endpoint from its name."""
    base = urlsplit(base_url)
    if (
        base.scheme not in ("http", "https")
        or not base.netloc
        or base.username
        or base.password
        or base.query
        or base.fragment
    ):
        raise ValueError("invalid authorized FHIR base")
    if (
        capability.get("resourceType") != "CapabilityStatement"
        or capability.get("fhirVersion") != "4.0.1"
        or capability.get("kind") != "instance"
    ):
        raise ValueError("unsupported FHIR capability version or kind")
    implementation = capability.get("implementation")
    if not isinstance(implementation, dict) or implementation.get("url", "").rstrip(
        "/"
    ) != base_url.rstrip("/"):
        raise ValueError("capability does not bind the authorized service base")
    rest = capability.get("rest")
    if not isinstance(rest, list) or len(rest) != 1 or rest[0].get("mode") != "server":
        raise ValueError("ambiguous FHIR REST capability")
    resources = rest[0].get("resource")
    if not isinstance(resources, list) or not 1 <= len(resources) <= 128:
        raise ValueError("bounded resource declarations required")
    names, selected = set(), []
    for resource in resources:
        name = resource.get("type")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Z][A-Za-z0-9]{0,63}", name) is None
            or name in names
        ):
            raise ValueError("ambiguous resource declaration")
        names.add(name)
        interactions = resource.get("interaction", [])
        if not isinstance(interactions, list) or any(
            not isinstance(i, dict) or not isinstance(i.get("code"), str)
            for i in interactions
        ):
            raise ValueError("invalid interaction declaration")
        codes = [i["code"] for i in interactions]
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate interaction declaration")
        if {"read", "search-type"} <= set(codes):
            selected.append(name)
    if not selected:
        raise ValueError("no declared readable search resource")
    return sorted(selected)


def learn_fhir_read(
    capability: Mapping[str, Any],
    *,
    base_url: str,
    field_paths: Mapping[str, str],
    probes: list[ReadProbe],
    session: Any,
    quiescence: QuiescenceAuthority | None,
) -> RestReadSetup:
    """Try every declared resource; retain only one complete independently matched read.

    Field projections are setup data and must match the independent intended
    store exactly. A patient-restricted token cannot qualify a population read.
    No historical absence, tenant identity or durable authorization is inferred.
    Without an actual writer-exclusion authority this mode is unavailable;
    two equal HTTP passes alone do not supply one.
    """
    result = RestReadSetup()
    try:
        names = search_resources(capability, base_url)
        if not isinstance(quiescence, QuiescenceAuthority) or session is None:
            raise ValueError("current authorized read and writer exclusion required")
        if (
            not probes
            or len(probes) > 32
            or any(p.identity is not None or p.snapshot_isolation for p in probes)
        ):
            raise ValueError("bounded whole-scope independent probes required")
        if not field_paths or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v
            for k, v in field_paths.items()
        ):
            raise ValueError("declared projection required")
        accepted, observations = [], []
        for name in names:
            cfg = {
                "kind": "fhir",
                "base_url": base_url.rstrip("/"),
                "resource_type": name,
                "field_paths": dict(field_paths),
                "records_consistency": "quiescent-two-pass",
            }
            matched, counts = True, []
            verifier = build_effect_verifier(
                EffectsConfig.model_validate(cfg),
                session=session,
                quiescence=quiescence,
            )
            if verifier is None:
                raise ValueError("the ordinary FHIR verifier is unavailable")
            for probe in probes:
                before = probe.snapshot()
                state = verifier.capture_pre_state()
                after = probe.snapshot()
                valid = state.reachable and _rows(before) == _rows(after) == _rows(
                    state.records
                )
                counts.append(
                    {
                        "expected": len(before),
                        "returned": len(state.records),
                        "matched": valid,
                    }
                )
                matched = matched and valid
            observations.append(
                {"resource_type": name, "matched": matched, "probes": counts}
            )
            if matched:
                accepted.append(cfg)
        result.evidence = {
            "protocol": "FHIR-R4-4.0.1",
            "protocol_sources": list(PROTOCOL_SOURCES),
            "specification_sha256": _digest(capability),
            "candidate_resources": names,
            "observations": observations,
            "status": "draft",
            "read_context": "revalidation-required",
            "consistency": "quiescent-two-pass",
            "scope": "learning-session-current-read",
        }
        if len(accepted) != 1:
            raise ValueError("no unique independently matched resource")
        result.config = accepted[0]
        result.evidence["config_sha256"] = _digest(result.config)
    except Exception:  # noqa: BLE001 - no credentials or response values in proof
        result.config = None
        result.problems.append(
            "FHIR setup is unavailable, ambiguous, or lacks complete independent read evidence"
        )
    return result


@dataclass
class FhirReadContext(RestReadContext):
    """Harness-owned current context; no database oracle is exported to an arm."""

    field_paths: Mapping[str, str] = field(default_factory=dict)

    def build(self, cfg: EffectsConfig, params: Mapping[str, object] | None) -> Any:
        if (
            not self._active
            or cfg.records_read_context != "revalidate"
            or self.identity_parameter is not None
        ):
            raise ValueError("an open FHIR read context is required")
        candidate = cfg.model_copy(deep=True)
        client = self.session_factory()
        if client is None:
            raise ValueError("FHIR read context opened no client")
        self._clients.append(client)
        fresh = learn_fhir_read(
            self.specification,
            base_url=self.base_url,
            field_paths=self.field_paths,
            probes=self.probes(),
            session=client,
            quiescence=self.quiescence,
        )
        if not fresh.observations_match or fresh.config is None:
            raise ValueError("FHIR read context revalidation failed")
        if candidate.records_drop_fields not in ([], ["id"]):
            raise ValueError("FHIR read context has unsupported projection")
        expected = EffectsConfig.model_validate(fresh.config).model_copy(
            update={
                "records_read_context": "revalidate",
                "records_drop_fields": list(candidate.records_drop_fields),
            }
        )
        if candidate.model_dump(mode="json") != expected.model_dump(mode="json"):
            raise ValueError("FHIR candidate differs from the current read contract")
        return build_effect_verifier(
            candidate.model_copy(update={"records_read_context": None}),
            params,
            session=_ContextSession(self, client),
            quiescence=self.quiescence,
        )
