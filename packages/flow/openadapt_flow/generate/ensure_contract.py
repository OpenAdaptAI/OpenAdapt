"""Generate an ``openadapt.ensure-record/v1`` contract for a FHIR write.

A person used to write about half of a FHIR ensure-record contract by hand
(``benchmark/fhir_medplum/readpath/MINUTES.md``): the ``telecom[system=...]``
paths, the comparison kinds, the state map, the identity rules, the search
parameter per identity value, the closed record and the change feed. This
module derives each of them from what a teach session already has:

- **two or more demonstrations**: the task's parameters, and the server's
  search results for the resource type before and after a correct write;
- **the server's CapabilityStatement** and its ``SearchParameter``
  definitions (which search parameters exist, their type and expression);
- **the server facts** a read-path probe measured once per server (does the
  audit trail name the writer, can the agent rewrite it, does ``_history``
  carry ``meta.author``, how long the trail takes to land);
- **deployment settings**: the base URL, the credential's environment
  variable and the agent's own actor reference.

Where a choice needs knowledge beyond the demonstrations, it comes from the
reviewed tables in :mod:`openadapt_flow.generate.fhir_tables`, never from a
model. Nothing here calls a model.

The gate refuses, with the reason, whenever it is unsure: a parameter that
no single element holds in every demonstration, an element the parameters
don't explain whose value changes between demonstrations, a list whose
elements can't be told apart, an identity value with no search parameter,
a missing server fact, or an audit trail the agent can rewrite. A type with
no reviewed identity template gets a proposed rule that a person confirms.
Before admitting, it checks the contract against every demonstration (each
reads back right, closed) and against one perturbation per field and one
extra element (each must be caught). Generation adds checks; it never
removes one: the record is always closed, comparisons are exact unless a
reviewed table names another, and an allowance is always one exact value.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ensure_record import (
    EnsureContract,
    FieldRule,
    extra_elements,
    is_person,
    normalize,
    wrong_fields,
)
from openadapt_flow.generate import fhir_tables as tables

SCHEMA = "openadapt.ensure-record/v1"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PATH_RE = re.compile(
    r"[A-Za-z]+(\[[A-Za-z]+=[^\]]*\])?(\.[A-Za-z]+(\[[A-Za-z]+=[^\]]*\])?)*"
)
#: Server facts the change feed needs, each measured by a read-path probe.
FACTS = (
    "audit_logs_writes",
    "audit_names_actor",
    "agent_cannot_write_audit",
    "history_lists_versions",
    "meta_author_returned",
    "audit_lag_max_s",
)


class GenerateError(ValueError):
    """The teach directory can't be read (never a refusal of the contract)."""


# -- inputs ----------------------------------------------------------------------


@dataclass
class Demo:
    name: str
    params: dict[str, Any]
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]


@dataclass
class Teach:
    resource_type: str
    demos: list[Demo]
    capability: Mapping[str, Any]
    search_parameters: Sequence[Mapping[str, Any]]
    facts: Mapping[str, Any]
    deployment: Mapping[str, Any]
    confirmed: set[str] = field(default_factory=set)


def _resources(doc: Any) -> list[dict[str, Any]]:
    """A Bundle's entries, or a list of resources, as resources."""

    if isinstance(doc, Mapping) and doc.get("resourceType") == "Bundle":
        return [
            dict(e["resource"])
            for e in doc.get("entry") or []
            if isinstance(e, Mapping) and isinstance(e.get("resource"), Mapping)
        ]
    if isinstance(doc, list):
        return [dict(r) for r in doc if isinstance(r, Mapping)]
    raise GenerateError("expected a Bundle or a list of resources")


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GenerateError(f"cannot read {path}: {exc}") from exc


def facts_from_probe(probe: Mapping[str, Any]) -> dict[str, Any]:
    """Server facts from ``benchmark/fhir_medplum/readpath/probe.py`` output.

    A probe that already reports the generic names passes through.
    """

    if all(k in probe for k in FACTS):
        return {k: probe[k] for k in FACTS}
    out: dict[str, Any] = {}
    logged = probe.get("bundle_and_graphql_writes_logged")
    if isinstance(logged, list):
        out["audit_logs_writes"] = len(logged) > 0
    for name, key in (
        ("audit_names_actor", "audit_names_the_user_profile"),
        ("agent_cannot_write_audit", "restricted_bot_audit_read_only"),
        ("history_lists_versions", "delete_in_history"),
        ("meta_author_returned", "meta_author_returned"),
    ):
        if key in probe:
            out[name] = probe[key]
    lags = probe.get("audit_lag_s")
    if isinstance(lags, Mapping) and lags:
        out["audit_lag_max_s"] = max(
            float(v.get("max", 0)) for v in lags.values() if isinstance(v, Mapping)
        )
    return out


def load_teach(root: str | Path, confirmed: Sequence[str] = ()) -> Teach:
    """Read a teach directory.

    ``teach.json`` (resource type and deployment settings),
    ``capability.json``, ``search_parameters.json`` (Bundle), ``facts.json``
    (generic facts or a probe's output) and ``demos/<name>/{params,before,
    after}.json``.
    """

    base = Path(root)
    meta = _load(base / "teach.json")
    demos = []
    for d in sorted(p for p in (base / "demos").iterdir() if p.is_dir()):
        demos.append(
            Demo(
                d.name,
                dict(_load(d / "params.json")),
                _resources(_load(d / "before.json")),
                _resources(_load(d / "after.json")),
            )
        )
    sp_path = base / "search_parameters.json"
    return Teach(
        resource_type=str(meta["resource_type"]),
        demos=demos,
        capability=_load(base / "capability.json"),
        search_parameters=_resources(_load(sp_path)) if sp_path.exists() else [],
        facts=facts_from_probe(_load(base / "facts.json")),
        deployment=dict(meta.get("deployment") or {}),
        # A confirmation is a person's, given to the command, never read
        # from the teach data.
        confirmed=set(confirmed),
    )


# -- the demonstrated record -----------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One scalar in a created record: where it is, and its value."""

    path: str  #: with list filters (``telecom[system=email].value``)
    plain: str  #: as ``ensure_record.leaves`` names it (``telecom.value``)
    value: Any
    ambiguous: bool = False  #: in a list whose elements can't be told apart
    group: Optional[str] = None  #: for a discriminator leaf: its element


def _exempt(plain: str) -> bool:
    top = plain.split(".")[0]
    if top == "meta":
        return not plain.startswith(("meta.security", "meta.tag"))
    return top in ("id", "resourceType")


def _multi_lists(resources: Sequence[Mapping[str, Any]]) -> dict[str, list[list[dict]]]:
    """Every list of objects, by plain path, in every resource."""

    found: dict[str, list[list[dict]]] = {}

    def walk(node: Any, plain: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{plain}.{k}" if plain else k)
        elif isinstance(node, list):
            items = [x for x in node if isinstance(x, dict)]
            if items:
                found.setdefault(plain, []).append(items)
            for x in node:
                walk(x, plain)

    for r in resources:
        walk(r, "")
    return found


def discriminators(
    resources: Sequence[Mapping[str, Any]],
) -> dict[str, Optional[str]]:
    """For each list of objects that holds more than one element in some
    record: the key that tells its elements apart, or None (ambiguous)."""

    out: dict[str, Optional[str]] = {}
    for plain, lists in _multi_lists(resources).items():
        if all(len(items) < 2 for items in lists):
            continue
        out[plain] = None
        for key in tables.DISCRIMINATORS:
            if all(
                all(isinstance(i.get(key), str) for i in items)
                and len({i[key] for i in items}) == len(items)
                for items in lists
            ):
                out[plain] = key
                break
        # Told apart by ``use`` while ``system`` still differs: a value could
        # move to the wrong system and keep its ``use``. Refuse.
        if out[plain] not in (None, "system") and any(
            len({i.get("system") for i in items}) > 1 for items in lists
        ):
            out[plain] = None
    return out


def slots(
    resource: Mapping[str, Any], discs: Mapping[str, Optional[str]]
) -> list[Slot]:
    out: list[Slot] = []

    def walk(node: Any, path: str, plain: str, ambiguous: bool) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(
                    v,
                    f"{path}.{k}" if path else k,
                    f"{plain}.{k}" if plain else k,
                    ambiguous,
                )
            return
        if isinstance(node, list):
            key = discs.get(plain)
            for item in node:
                if isinstance(item, dict) and plain in discs:
                    if key is None:
                        walk(item, path, plain, True)
                        continue
                    group = f"{path}[{key}={item[key]}]"
                    out.append(Slot(f"{group}.{key}", f"{plain}.{key}", item[key],
                                    ambiguous, group))  # fmt: skip
                    rest = {k: v for k, v in item.items() if k != key}
                    walk(rest, group, plain, ambiguous)
                else:
                    walk(item, path, plain, ambiguous)
            return
        if path and not _exempt(plain):
            out.append(Slot(path, plain, node, ambiguous))

    walk(dict(resource), "", "", False)
    return out


def created_resource(demo: Demo, rtype: str) -> tuple[Optional[dict], Optional[str]]:
    """The one resource the demonstration created, or why there isn't one."""

    def key(r: Mapping[str, Any]) -> str:
        return str(r.get("id"))

    before = {key(r): r for r in demo.before}
    made = [r for r in demo.after if key(r) not in before]
    changed = [
        key(r)
        for r in demo.after
        if key(r) in before
        and (r.get("meta") or {}).get("versionId")
        != (before[key(r)].get("meta") or {}).get("versionId")
    ]
    if changed:
        return None, f"{demo.name} changed {len(changed)} existing {rtype} record(s)"
    if len(made) != 1:
        return None, f"{demo.name} created {len(made)} {rtype} records, not one"
    if made[0].get("resourceType", rtype) != rtype:
        return None, f"{demo.name} created a {made[0].get('resourceType')}"
    return made[0], None


# -- comparisons -----------------------------------------------------------------


def compare_for(path: str, values: Sequence[Any]) -> tuple[str, str]:
    """The comparison for an element, and the reviewed reason for it."""

    m = re.search(r"\[system=([^\]]+)\]\.value$", path)
    if m and m.group(1) in tables.CONTACT_COMPARE:
        kind = tables.CONTACT_COMPARE[m.group(1)]
        return kind, f"ContactPoint system {m.group(1)}: {kind}"
    leaf = path.rsplit(".", 1)[-1].split("[")[0]
    dated = leaf == "date" or leaf.endswith(("Date", "DateTime"))
    if (
        dated
        and values
        and all(isinstance(v, str) and DATE_RE.match(v) for v in values)
    ):
        return "date", "a FHIR date element, and every stored value is a date"
    return "exact", "no reviewed table applies"


def map_for(plain: str, values: Sequence[Any]) -> Optional[dict[str, str]]:
    if not (plain == "address.state" or plain.endswith(".address.state")):
        return None
    codes = set(tables.US_STATES.values())
    if values and all(isinstance(v, str) and v in codes for v in values):
        return dict(tables.US_STATES)
    return None


def _matches(stored: Any, wanted: Any, rule: FieldRule) -> bool:
    a, b = normalize(stored, rule), normalize(wanted, rule, task=True)
    return a is not None and b is not None and a == b


# -- search parameters -----------------------------------------------------------


def expression_paths(expression: str, rtype: str) -> list[str]:
    """Element paths a FHIRPath search expression reads, for ``rtype``.

    Only plain paths and ``where(key='value')`` filters are understood; an
    expression with anything else is skipped, never guessed.
    """

    out = []
    for part in expression.split("|"):
        part = part.strip()
        if part.startswith("(") and part.endswith(")"):
            part = part[1:-1].strip()
        if not part.startswith(rtype + "."):
            continue
        rest = re.sub(
            r"\.where\(\s*(\w+)\s*=\s*'([^']*)'\s*\)",
            r"[\1=\2]",
            part[len(rtype) + 1 :],
        )
        if PATH_RE.fullmatch(rest):
            out.append(rest)
    return out


def _covers(kind: str, expr: str, path: str) -> bool:
    if kind == "string":
        return path == expr or path.startswith(expr + ".")
    if kind == "token":
        return path in (expr, f"{expr}.code", f"{expr}.coding.code", f"{expr}.value")
    if kind == "reference":
        return path == f"{expr}.reference"
    return path == expr


def search_index(teach: Teach) -> list[tuple[str, str, str]]:
    """(name, type, element path) for every search parameter the server
    lists for the type in its CapabilityStatement and defines with an
    expression this module understands."""

    listed: dict[str, str] = {}
    for rest in teach.capability.get("rest") or []:
        for res in rest.get("resource") or []:
            if res.get("type") == teach.resource_type:
                for sp in res.get("searchParam") or []:
                    if sp.get("name"):
                        listed[sp["name"]] = str(sp.get("type") or "")
    out = []
    for sp in teach.search_parameters:
        code, expr = sp.get("code"), sp.get("expression")
        bases = sp.get("base") or []
        if not code or not expr or teach.resource_type not in bases:
            continue
        if code not in listed:
            continue
        kind = str(sp.get("type") or listed[code])
        if kind not in tables.SEARCHABLE_TYPES:
            continue
        for path in expression_paths(str(expr), teach.resource_type):
            out.append((str(code), kind, path))
    return out


def search_for(
    index: Sequence[tuple[str, str, str]], path: str
) -> Optional[tuple[str, str]]:
    """The most specific search parameter that finds a value at ``path``."""

    best: Optional[tuple[str, str, str]] = None
    for name, kind, expr in index:
        if _covers(kind, expr, path) and (best is None or len(expr) > len(best[2])):
            best = (name, kind, expr)
    return (best[0], best[1]) if best else None


# -- generation ------------------------------------------------------------------


@dataclass
class Generated:
    contract: Optional[dict[str, Any]]
    refusals: list[str]
    to_confirm: list[str]
    provenance: dict[str, str]
    checks: list[str]

    @property
    def admitted(self) -> bool:
        return self.contract is not None and not self.refusals and not self.to_confirm

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "refusals": self.refusals,
            "to_confirm": self.to_confirm,
            "provenance": self.provenance,
            "checks": self.checks,
            "contract": self.contract,
        }


def _change_feed(
    teach: Teach, refusals: list[str], prov: dict[str, str]
) -> dict[str, Any]:
    facts = teach.facts
    missing = [k for k in FACTS if k not in facts]
    if missing:
        refusals.append("server facts not measured: " + ", ".join(missing))
        return {}
    lag = facts["audit_lag_max_s"]
    mistyped = [k for k in FACTS[:-1] if not isinstance(facts[k], bool)] + (
        []
        if isinstance(lag, (int, float))
        and not isinstance(lag, bool)
        and math.isfinite(lag)
        and lag >= 0
        else ["audit_lag_max_s"]
    )
    if mistyped:
        refusals.append("server facts of the wrong type: " + ", ".join(mistyped))
        return {}
    if not facts["audit_logs_writes"] or not facts["audit_names_actor"]:
        refusals.append(
            "the audit trail does not log writes with who wrote them, so no "
            "source can attribute a change"
        )
    if not facts["agent_cannot_write_audit"]:
        refusals.append(
            "the agent's credential can change the audit trail: restrict it "
            "(an access policy) and probe again"
        )
    rtype = teach.resource_type
    prov["change_feed.audit"] = "facts: the audit trail logs writes and names the actor"
    prov["change_feed.audit.heartbeat"] = (
        "always on: an empty audit window proves nothing unless the verifier's "
        "own read shows up"
    )
    prov["change_feed.history"] = "facts: history_lists_versions"
    prov["change_feed.history_author"] = "facts: meta_author_returned"
    prov["change_feed.sweep_types"] = "the resource type"
    prov["settle_s"] = "facts: 3 x the slowest audit lag measured, at least 1 s"
    return {
        "change_feed": {
            "audit": {"heartbeat": True},
            "history": bool(facts["history_lists_versions"]),
            "history_author": bool(facts["meta_author_returned"]),
            "sweep_types": [rtype],
        },
        "settle_s": float(max(1, math.ceil(3 * float(facts["audit_lag_max_s"])))),
    }


def generate(teach: Teach) -> Generated:
    rtype = teach.resource_type
    refusals: list[str] = []
    to_confirm: list[str] = []
    prov: dict[str, str] = {}

    made: list[tuple[Demo, dict[str, Any]]] = []
    for demo in teach.demos:
        resource, why = created_resource(demo, rtype)
        if why or resource is None:
            refusals.append(why or f"{demo.name}: no created resource")
        else:
            made.append((demo, resource))
    if len(made) < 2:
        refusals.append(f"{len(made)} usable demonstration(s); at least 2 are needed")
        return Generated(None, refusals, to_confirm, prov, [])

    for key in ("base_url", "self"):
        if not teach.deployment.get(key):
            refusals.append(f"deployment setting missing: {key}")

    discs = discriminators([r for _, r in made])
    per_demo = [slots(r, discs) for _, r in made]
    values_at: dict[str, list[Any]] = {}
    plain_of: dict[str, str] = {}
    for ss in per_demo:
        for s in ss:
            values_at.setdefault(s.path, []).append(s.value)
            plain_of[s.path] = s.plain

    # Bind each task parameter to the one element that holds it everywhere.
    fields: dict[str, dict[str, Any]] = {}
    names = sorted({p for d, _ in made for p in d.params})
    for p in names:
        present = [
            (i, d.params[p])
            for i, (d, _) in enumerate(made)
            if d.params.get(p) not in (None, "")
        ]
        if not present:
            continue
        if len(present) < 2:
            refusals.append(f"{p}: given in only one demonstration")
            continue
        common: Optional[set[str]] = None
        ambiguous_hit = False
        for i, wanted in present:
            hits = set()
            for s in per_demo[i]:
                kind, _ = compare_for(s.path, values_at[s.path])
                rule = FieldRule(
                    path=s.path,
                    compare=kind,  # type: ignore[arg-type]
                    map=map_for(s.plain, values_at[s.path]) or {},
                )
                if _matches(s.value, wanted, rule):
                    if s.ambiguous:
                        ambiguous_hit = True
                    elif s.group is None:
                        hits.add(s.path)
            common = hits if common is None else common & hits
        if not common:
            refusals.append(
                f"{p}: no single element holds it in every demonstration"
                + (
                    " (it is in a list whose elements can't be told apart)"
                    if ambiguous_hit
                    else ""
                )  # fmt: skip
            )
            continue
        if len(common) > 1:
            refusals.append(f"{p}: more than one element holds it: {sorted(common)}")
            continue
        (path,) = common
        kind, why = compare_for(path, values_at[path])
        entry: dict[str, Any] = {"path": path}
        prov[f"fields.{p}.path"] = (
            f"bound in {len(present)} of {len(made)} demonstrations"
        )
        if kind != "exact":
            entry["compare"] = kind
            prov[f"fields.{p}.compare"] = why
        mapping = map_for(plain_of[path], values_at[path])
        if mapping:
            entry["map"] = mapping
            prov[f"fields.{p}.map"] = (
                "USPS codes: every demonstrated record stores a state code"
            )
        fields[p] = entry

    # Everything else the record holds must be the same constant everywhere.
    bound = {f["path"] for f in fields.values()}
    allow: list[str] = []
    seen: set[str] = set()
    for path in sorted(values_at):
        sample = next(s for ss in per_demo for s in ss if s.path == path)
        if path in bound or (
            sample.group and any(b.startswith(sample.group + ".") for b in bound)
        ):
            continue
        vals = values_at[path]
        present_in = sum(1 for ss in per_demo if any(s.path == path for s in ss))
        if sample.ambiguous:
            refusals.append(
                f"{sample.plain}: in a list whose elements can't be told apart"
            )
        elif "[" in path:
            # An allowance names an unfiltered path, so it would hold for
            # every element of the list, not only this one.
            refusals.append(
                f"{path}: an element of a told-apart list no task parameter explains"
            )
        elif (
            present_in == len(per_demo)
            and len(vals) == len(per_demo)
            and all(v == vals[0] for v in vals)
        ):
            allowed = f"{sample.plain}={json.dumps(vals[0], ensure_ascii=False)}"
            if allowed not in seen:
                seen.add(allowed)
                allow.append(allowed)
                prov[f"allow.{allowed}"] = "the same value in every demonstration"
        else:
            refusals.append(
                f"{path}: the record holds values no task parameter explains "
                f"({', '.join(sorted({json.dumps(v) for v in vals}))})"
            )
    for name, value in tables.DEFAULT_MEANING.get(rtype, {}).items():
        allowed = f"{name}={json.dumps(value)}"
        if allowed not in seen:
            seen.add(allowed)
            allow.append(allowed)
            prov[f"allow.{allowed}"] = "FHIR R4: the value its absence means"

    # Identity: the reviewed template, or a proposed rule a person confirms.
    index = search_index(teach)
    param_at = {f["path"]: p for p, f in fields.items()}
    identity: list[list[str]] = []
    template = tables.IDENTITY_TEMPLATES.get(rtype)
    if template is not None:
        for paths in template:
            if all(path in param_at for path in paths):
                identity.append([param_at[path] for path in paths])
                prov[f"identity.{'+'.join(paths)}"] = f"reviewed template for {rtype}"
        if not identity:
            refusals.append(
                f"no rule of the {rtype} identity template is bound by the demonstrations"
            )
    else:
        proposed = sorted(
            p
            for p, f in fields.items()
            if (hit := search_for(index, f["path"]))
            and hit[1] in ("reference", "token")
        )
        kinds = {
            p: hit[1] for p in proposed if (hit := search_for(index, fields[p]["path"]))
        }
        if proposed and not (
            "reference" in kinds.values()
            and any(k != "reference" for k in kinds.values())
        ):
            refusals.append(
                f"identity: {rtype} has no reviewed template, and the proposal "
                f"{proposed} lacks a subject reference and a coded value"
            )
        elif proposed:
            identity.append(proposed)
            prov[f"identity.{'+'.join(proposed)}"] = (
                "proposed: every bound field a reference or token search finds"
            )
            item = f"identity: {rtype} has no reviewed template; proposed {proposed}"
            if "identity" not in teach.confirmed:
                to_confirm.append(item)
        else:
            refusals.append(f"identity: {rtype} has no reviewed template and no rule")

    search: dict[str, str] = {}
    for p in sorted({p for rule in identity for p in rule}):
        hit = search_for(index, fields[p]["path"])
        if hit is None:
            refusals.append(f"{p}: no search parameter finds {fields[p]['path']}")
            continue
        search[p] = hit[0]
        prov[f"search.{p}"] = (
            f"CapabilityStatement + SearchParameter: {hit[0]} ({hit[1]})"
        )

    contract: dict[str, Any] = {
        "schema_version": SCHEMA,
        "resource_type": rtype,
        "base_url": teach.deployment.get("base_url"),
    }
    if teach.deployment.get("access_token_env"):
        contract["access_token_env"] = teach.deployment["access_token_env"]
    contract["self"] = list(teach.deployment.get("self") or [])
    contract.update(
        {"fields": fields, "identity": identity, "search": search, "closed": True}
    )
    prov["closed"] = "always: generation never opens the record"
    if allow:
        contract["allow"] = allow
    contract.update(_change_feed(teach, refusals, prov))

    checks: list[str] = []
    if not refusals and fields and identity:
        try:
            model = EnsureContract.model_validate(contract)
        except Exception as exc:  # pragma: no cover - a generator bug
            refusals.append(f"the generated contract does not load: {exc}")
        else:
            refusals += self_check(model, made, checks)
    return Generated(contract, refusals, to_confirm, prov, checks)


# -- the admission check ---------------------------------------------------------


def _perturb(value: Any, rule: FieldRule) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    text = str(value)
    if rule.compare == "digits" or rule.compare == "date":
        for i in range(len(text) - 1, -1, -1):
            if text[i].isdigit():
                return text[:i] + str((int(text[i]) + 1) % 10) + text[i + 1 :]
    if rule.map:
        return "ZZ"
    return text + "x"


def _set_first(resource: dict[str, Any], path: str, make: Any) -> bool:
    """Replace the value ``extract_path`` reads at ``path`` (``make(old)``)."""

    node: Any = resource
    segs = path.split(".")
    for n, seg in enumerate(segs):
        name, _, cond = seg.partition("[")
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict) or name not in node:
            return False
        if n == len(segs) - 1 and not cond:
            child = node[name]
            if isinstance(child, list):
                if not child:
                    return False
                child[0] = make(child[0])
            else:
                node[name] = make(child)
            return True
        child = node[name]
        if cond:
            key, _, want = cond[:-1].partition("=")
            items = child if isinstance(child, list) else [child]
            child = next(
                (i for i in items if isinstance(i, dict) and str(i.get(key)) == want),
                None,
            )
        node = child
    return False


def self_check(
    contract: EnsureContract,
    made: Sequence[tuple[Demo, dict[str, Any]]],
    checks: list[str],
) -> list[str]:
    """The contract passes every demonstration and catches every perturbation."""

    problems: list[str] = []
    for demo, resource in made:
        params = {k: v for k, v in demo.params.items() if k in contract.fields}
        wrong = wrong_fields(contract, resource, params)
        if wrong:
            problems.append(f"{demo.name} reads as wrong: {wrong}")
        if not is_person(contract, resource, params):
            problems.append(f"{demo.name} is not the one its identity rules name")
        extra = extra_elements(contract, resource, params)
        if extra:
            problems.append(f"{demo.name} holds elements the contract doesn't: {extra}")
        for name, rule in contract.fields.items():
            if params.get(name) in (None, ""):
                continue
            bent = copy.deepcopy(resource)
            if not _set_first(bent, rule.path, lambda v, r=rule: _perturb(v, r)):
                problems.append(f"{demo.name}: can't perturb {name}")
            elif name not in wrong_fields(contract, bent, params):
                problems.append(f"{demo.name}: a changed {name} reads as right")
        bent = copy.deepcopy(resource)
        bent["extension"] = [{"url": "urn:openadapt:probe", "valueString": "x"}]
        if not extra_elements(contract, bent, params):
            problems.append(f"{demo.name}: an extra element reads as right")
    if not problems:
        checks.append(
            f"{len(made)} demonstrations read back right and closed; "
            f"{len(contract.fields)} field perturbations and one extra element "
            "caught in each"
        )
    return problems


# -- output ----------------------------------------------------------------------


def render_yaml(gen: Generated, *, generator: str = "") -> str:
    import yaml

    head = [
        "# Generated by openadapt_flow.generate.ensure_contract"
        + (f" ({generator})" if generator else "")
        + ".",
        "# The report beside it says where every line came from.",
    ]
    body = yaml.safe_dump(gen.contract, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(head) + "\n" + body


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("teach", help="teach directory")
    ap.add_argument("--out", help="write the contract YAML here when admitted")
    ap.add_argument("--report", help="write the JSON report here")
    ap.add_argument(
        "--confirm",
        action="append",
        default=[],
        help="a proposal a person confirmed (identity)",
    )
    ap.add_argument("--generator", default="", help="generator commit, for the header")
    args = ap.parse_args(argv)
    gen = generate(load_teach(args.teach, args.confirm))
    if args.report:
        Path(args.report).write_text(json.dumps(gen.as_dict(), indent=2) + "\n")
    for line in gen.refusals:
        print(f"refused: {line}", file=sys.stderr)
    for line in gen.to_confirm:
        print(f"confirm: {line}", file=sys.stderr)
    if not gen.admitted:
        return 2
    if args.out:
        Path(args.out).write_text(render_yaml(gen, generator=args.generator))
    print("admitted: " + "; ".join(gen.checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
