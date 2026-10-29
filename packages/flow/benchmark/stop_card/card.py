"""A one-tap stop card for an ensure-record write the verifier stopped.

PREREG_REVIEW.md fixes what the card shows, when each action applies and how
its person time is modelled. This module builds the card from the verifier's
own reads (the change feed, the matches, the created record against the
same contract), chooses exactly one category, and runs Undo or Fix with a
read-back. It never changes a verifier outcome: the stop stays a stop.

Prototype for the measurement, kept out of the runtime: it lives with the
benchmark and reads through ``openadapt_flow.ensure_record`` unchanged.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from openadapt_flow.ensure_record import (
    Baseline,
    EnsureContract,
    FhirReader,
    Unreadable,
    _ref,
    extra_elements,
    extract,
    find_matches,
    leaves,
    read_changes,
    wrong_fields,
)

#: (method, url, body) -> response JSON, with the reviewer's credential.
Writer = Callable[[str, str, Optional[dict[str, Any]]], Any]

#: PREREG_REVIEW.md's operators, in seconds.
INTERRUPT_S = 30.0
POINT_CLICK_S = 1.2
MENTAL_S = 1.35
READ_WPM = 238.0
FALLBACK_S = 300.0
REDO_S = 180.0


@dataclass
class Row:
    kind: str  # wrong | extra | match | write | other
    text: str


@dataclass
class Card:
    rows: list[Row]
    category: str  # approve | undo | fix | open
    created: Optional[str]  # "Type/id" of the agent's created record
    own_writes: dict[str, set[str]]  # ref -> actions, the agent's writes
    keep: Optional[str] = None  # undo: the created record that stays
    before: dict[str, dict[str, Any]] = field(default_factory=dict)

    def render(self) -> str:
        head = {
            "approve": "Stopped. The record reads right: approve, or open in the app.",
            "undo": "Stopped. Undo reverses the agent's writes outside the task.",
            "fix": "Stopped. Fix writes the task's values into the agent's record.",
            "open": "Stopped. No one-tap action applies: open in the app.",
        }[self.category]
        return "\n".join([head] + [f"- {r.text}" for r in self.rows])

    def words(self) -> int:
        return len(self.render().split())

    def person_seconds(self, factor: float = 1.0, wait_s: float = 0.0) -> float:
        """PREREG_REVIEW.md's model; ``factor`` scales every person step but
        the interrupt."""

        read = self.words() / READ_WPM * 60
        steps = POINT_CLICK_S + read + MENTAL_S * len(self.rows) + MENTAL_S
        if self.category == "open":
            return INTERRUPT_S + factor * (steps + FALLBACK_S)
        return INTERRUPT_S + factor * (steps + POINT_CLICK_S) + wait_s


def _short(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _version_before(
    reader: FhirReader, ref: str, since: Any
) -> Optional[dict[str, Any]]:
    """The resource as it was before the window, from its ``_history``."""

    from openadapt_flow.ensure_record import _when

    best = None
    for entry in reader.bundle(f"{ref}/_history", {"_count": "200"}):
        version = entry.get("resource")
        if not isinstance(version, dict):
            continue
        when = _when((version.get("meta") or {}).get("lastUpdated"))
        if when is not None and when < since and (best is None or when > best[0]):
            best = (when, version)
    return best[1] if best else None


def record_ok(
    contract: EnsureContract,
    resource: Mapping[str, Any],
    params: Mapping[str, Any],
) -> bool:
    return not wrong_fields(contract, resource, params) and not extra_elements(
        contract, resource, params
    )


def build(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    reader: FhirReader,
) -> Card:
    selves = {_ref(a) for a in contract.self_actors}
    rtype = contract.resource_type
    changes = read_changes(contract, reader, baseline.started)
    own = {
        r: c
        for r, c in changes.items()
        if not c.attributed or not c.authors or c.authors & selves
    }
    foreign_on_own = {r for r, c in own.items() if c.authors - selves}
    rows: list[Row] = []
    for ref, c in sorted(changes.items()):
        who = ", ".join(sorted(c.authors)) or "no one the trail names"
        mark = " (the agent)" if ref in own and not (c.authors - selves) else ""
        rows.append(
            Row("write", f"{ref}: {'/'.join(sorted(c.actions))} by {who}{mark}")
        )
    creates = sorted(
        r for r, c in own.items() if r.startswith(rtype + "/") and "C" in c.actions
    )
    resources: dict[str, dict[str, Any]] = {}
    for ref in creates:
        try:
            _, resources[ref] = reader.fetch(f"{reader.base_url}/{ref}")
        except Unreadable:
            rows.append(Row("other", f"{ref} can't be read back"))
    findings = 0
    created = creates[0] if len(creates) == 1 else None
    for ref, res in resources.items():
        for name in wrong_fields(contract, res, params):
            findings += 1
            found = extract(res, contract.fields[name].path)
            rows.append(
                Row(
                    "wrong",
                    f"{name}: task {_short(params.get(name))}, record {_short(found)}",
                )
            )
        extra = set(extra_elements(contract, res, params))
        for path, value in leaves(dict(res)):
            if path in extra:
                findings += 1
                rows.append(Row("extra", f"{path} = {_short(value)}: not in the task"))
                extra.discard(path)
    after, _ = find_matches(contract, reader, params)
    mine_ids = {r.split("/", 1)[1] for r in creates}
    for rid in sorted(set(after) - mine_ids):
        ref = f"{rtype}/{rid}"
        who = (
            ", ".join(sorted(changes[ref].authors))
            if ref in changes
            else "before the run"
        )
        rows.append(
            Row("match", f"{ref} is also the one the task names (written {who})")
        )
    for ref in sorted(foreign_on_own):
        rows.append(Row("other", f"someone else also wrote {ref}"))

    clean = not foreign_on_own and all(c.attributed and c.authors for c in own.values())
    card = Card(rows, "open", created, {r: set(c.actions) for r, c in own.items()})
    only_record = set(own) == {created} if created else False
    if (
        clean
        and created
        and only_record
        and created in resources
        and not baseline.matches
        and set(after) <= mine_ids
    ):
        card.category = "fix" if findings else "approve"
        return card
    keepers = [
        r
        for r in creates
        if r in resources
        and record_ok(contract, resources[r], params)
        and not baseline.matches
        and set(after) - mine_ids == set()
    ]
    if clean and own:
        card.category = "undo"
        card.keep = keepers[0] if keepers else None
        for ref, c in own.items():
            if ref != card.keep and "C" not in c.actions:
                prior = _version_before(reader, ref, baseline.started)
                if prior is None:
                    card.category = "open"  # nothing to put back
                    return card
                card.before[ref] = prior
    return card


@dataclass
class ActionResult:
    ok: bool
    seconds: float
    task_done: bool
    problems: list[str]


def act(
    card: Card,
    contract: EnsureContract,
    params: Mapping[str, Any],
    reader: FhirReader,
    write: Writer,
) -> Optional[ActionResult]:
    """Run the card's action and read back. None for approve and open."""

    if card.category not in ("undo", "fix"):
        return None
    base = reader.base_url
    t0 = time.monotonic()
    problems: list[str] = []
    if card.category == "fix":
        assert card.created is not None
        _, res = reader.fetch(f"{base}/{card.created}")
        fixed = _fixed(contract, res, params)
        write("PUT", f"{base}/{card.created}", fixed)
        _, back = reader.fetch(f"{base}/{card.created}")
        if not record_ok(contract, back, params):
            problems.append("the fixed record still differs")
        done = not problems
    else:
        for ref, actions in sorted(card.own_writes.items()):
            if ref == card.keep:
                continue
            if "C" in actions:
                write("DELETE", f"{base}/{ref}", None)
            else:
                body = copy.deepcopy(card.before[ref])
                body.pop("meta", None)
                write("PUT", f"{base}/{ref}", body)
        for ref, actions in sorted(card.own_writes.items()):
            if ref == card.keep:
                continue
            try:
                _, now = reader.fetch(f"{base}/{ref}")
            except Unreadable:
                now = None
            if "C" in actions:
                if now is not None:
                    problems.append(f"{ref} is still there")
            elif now is None or _strip(now) != _strip(card.before[ref]):
                problems.append(f"{ref} is not back at its earlier version")
        done = card.keep is not None and not problems
    return ActionResult(not problems, round(time.monotonic() - t0, 3), done, problems)


def _strip(resource: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in resource.items() if k not in ("meta", "text")}


def _fixed(
    contract: EnsureContract, resource: Mapping[str, Any], params: Mapping[str, Any]
) -> dict[str, Any]:
    """The record with the task's values and nothing the task didn't give.

    Only fields whose path has no list filter are written directly; a
    filtered path (``telecom[system=email].value``) is written into the
    element the filter selects, created if missing.
    """

    from openadapt_flow.generate.ensure_contract import _set_first

    out = {k: copy.deepcopy(v) for k, v in resource.items() if k != "meta"}
    for name in wrong_fields(contract, out, params):
        value = params[name]
        rule = contract.fields[name]
        if rule.map:
            value = rule.map.get(str(value), value)
        if not _set_first(out, rule.path, lambda _old, v=value: v):
            _create_path(out, rule.path, value)
    extra = set(extra_elements(contract, out, params))
    for path in extra:
        _drop_leaf(out, path)
    return out


def _create_path(resource: dict[str, Any], path: str, value: Any) -> None:
    node: Any = resource
    segs = path.split(".")
    for n, seg in enumerate(segs):
        name, _, cond = seg.partition("[")
        last = n == len(segs) - 1
        if last:
            node[name] = value
            return
        if cond:
            key, _, want = cond[:-1].partition("=")
            items = node.setdefault(name, [])
            hit = next((i for i in items if str(i.get(key)) == want), None)
            if hit is None:
                hit = {key: want}
                items.append(hit)
            node = hit
        else:
            node = node.setdefault(name, {})
            if isinstance(node, list):
                node = node[0]


def _drop_leaf(node: Any, path: str) -> bool:
    head, _, rest = path.partition(".")
    if isinstance(node, list):
        return any(_drop_leaf(item, path) for item in node)
    if not isinstance(node, dict) or head not in node:
        return False
    if not rest:
        del node[head]
        return True
    dropped = _drop_leaf(node[head], rest)
    if dropped and node[head] in ({}, [], [{}]):
        del node[head]
    return dropped
