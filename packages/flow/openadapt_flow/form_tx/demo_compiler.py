"""Pack-authoring report compiled from dev-case demonstrations, at setup time.

A demonstration is the JSONL a recorder writes while a careful person fills a
form: before each interaction, what ``PlaywrightFormSurface`` sees (controls
with labels, values, options and row cells; page-text pairs; banners). From
one or more demonstrations and their intakes this module derives the pack
data a person would otherwise have to guess:

- **defaults table**: the value each bound control shows before anyone
  touches it. A value is a *proven* portal default only when it is the same
  blank value in every demonstration and at least one intake asked for
  something else, so it cannot be that client's data. Everything else is
  reported, never asserted.
- **row key cells**: for identical buttons in table rows ("Select"), the
  intake keys whose value is a cell of exactly one row.
- **option codecs**: a select's option codes and their visible text, as an
  ``ENUM_MAP`` codec, so a code entered and a label shown read back the same.
- **unopened launchers**: buttons the demonstrations showed but never
  clicked and the pack does not cover, so the author covers or marks them.
- **error routes** (suggest-only): a validation message seen in a banner and
  the field whose label it names.

Only proven defaults, exact row cells and exact option maps are offered as
pack data. Inferred mappings (error routes, a control the demonstration
skipped) are suggestions: the compiler never writes them into a pack, and
they never count toward a verified read-back.

Zero model calls; no application names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.form_tx.codecs import collapse_whitespace
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    FormSnapshot,
    ObservedControl,
)

__all__ = [
    "DefaultFinding",
    "DemoReport",
    "compile_demo_report",
    "load_demo",
]


def _norm(text: Any) -> str:
    return collapse_whitespace(str(text)).casefold()


def load_demo(path: str | Path) -> list[dict[str, Any]]:
    """Rows of a recorded demonstration (``FORM_TX_DEMO`` lines or plain JSONL)."""
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("FORM_TX_DEMO "):
            line = line[len("FORM_TX_DEMO ") :]
        elif not line.startswith("{"):
            continue
        row = json.loads(line)
        if isinstance(row, dict) and "controls" in row:
            rows.append(row)
    return rows


def _snapshot(row: Mapping[str, Any]) -> FormSnapshot:
    controls = []
    for raw in row.get("controls") or ():
        data = {k: v for k, v in raw.items() if k in ObservedControl.model_fields}
        data.setdefault("control_id", data.get("dom_selector") or "ctrl")
        data.setdefault("dom_selector", data["control_id"])
        if "options" in data:
            data["options"] = tuple(data["options"])
        if "row_cells" in data:
            data["row_cells"] = tuple(data["row_cells"])
        controls.append(ObservedControl.model_validate(data))
    return FormSnapshot(
        url_origin_and_path=str(row.get("frame_url") or ""),
        controls=tuple(controls),
        banner_errors=tuple(row.get("banners") or ()),
    )


def _shown(ctrl: ObservedControl) -> str:
    if ctrl.input_type.casefold() in ("checkbox",) or ctrl.role == "checkbox":
        return "true" if ctrl.checked else "false"
    if ctrl.input_type.casefold() == "radio" or ctrl.role == "radio":
        return ctrl.current_value if ctrl.checked else ""
    return ctrl.current_value


class DefaultFinding(BaseModel):
    """What one bound control showed before anyone touched it, per demo."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str
    dossier_key: str
    blank_values: tuple[str, ...]
    intake_values: tuple[str, ...]
    status: Literal[
        "proven_default", "unproven_same_as_intake", "differs_between_demos", "blank"
    ]


class RowKeySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    button_label: str
    dossier_keys: tuple[str, ...]
    rows_seen: int


class DemoReport(BaseModel):
    """Everything the compiler found, and what it offers as pack data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    demos_used: int
    defaults: tuple[DefaultFinding, ...] = ()
    row_keys: tuple[RowKeySuggestion, ...] = ()
    option_codecs: dict[str, FieldCodecSpec] = Field(default_factory=dict)
    unopened_launchers: tuple[str, ...] = ()
    suggested_error_routes: tuple[tuple[str, str], ...] = ()
    unseen_bindings: tuple[str, ...] = ()

    def proven_defaults(self) -> dict[str, tuple[str, ...]]:
        """binding_id -> values safe to put in ``portal_default_values``."""
        return {
            f.binding_id: tuple(sorted(set(f.blank_values)))
            for f in self.defaults
            if f.status == "proven_default"
        }


def _first_sightings(
    rows: Sequence[Mapping[str, Any]], bindings: Sequence[FieldBindingSpec]
) -> dict[str, ObservedControl]:
    """The first time each binding's control is seen in a demonstration."""
    seen: dict[str, ObservedControl] = {}
    for row in rows:
        snap = _snapshot(row)
        for b in bindings:
            if b.binding_id in seen:
                continue
            try:
                ctrl = match_anchor_in_snapshot(b.anchor, snap, include_readonly=False)
            except AmbiguousAnchorError:
                continue
            if ctrl is not None:
                seen[b.binding_id] = ctrl
    return seen


def _clicked_text(rows: Sequence[Mapping[str, Any]]) -> str:
    return " ".join(_norm(r.get("why") or "") for r in rows)


def compile_demo_report(
    pack: FormPack,
    demos: Sequence[tuple[Sequence[Mapping[str, Any]], Mapping[str, Any]]],
) -> DemoReport:
    """Compile a report from ``(demo rows, intake facts)`` pairs."""
    bindings = [
        b
        for b in (
            *pack.bindings,
            *(b for s in pack.popup_subroutines for b in s.bindings),
        )
        if b.control_kind
        not in (ControlKind.POPUP_LAUNCHER, ControlKind.READONLY_OUTPUT)
    ]
    sightings = [_first_sightings(rows, bindings) for rows, _ in demos]

    defaults: list[DefaultFinding] = []
    for b in bindings:
        firsts = [s.get(b.binding_id) for s in sightings]
        if any(c is None for c in firsts):
            continue
        blanks = tuple(_shown(c) for c in firsts if c is not None)
        intakes = tuple(
            "" if facts.get(b.dossier_key) is None else str(facts.get(b.dossier_key))
            for _, facts in demos
        )
        if not any(_norm(v) for v in blanks):
            status = "blank"
        elif len({_norm(v) for v in blanks}) > 1:
            status = "differs_between_demos"
        elif any(_norm(i) != _norm(blanks[0]) for i in intakes):
            status = "proven_default"
        else:
            status = "unproven_same_as_intake"
        defaults.append(
            DefaultFinding(
                binding_id=b.binding_id,
                dossier_key=b.dossier_key,
                blank_values=blanks,
                intake_values=intakes,
                status=status,  # type: ignore[arg-type]
            )
        )

    unseen = tuple(
        sorted(
            b.binding_id
            for b in bindings
            if any(b.binding_id not in s for s in sightings)
        )
    )

    # Option codecs: exact code -> text maps from the selects themselves.
    codecs: dict[str, FieldCodecSpec] = {}
    for b in bindings:
        if b.control_kind != ControlKind.SELECT:
            continue
        for s in sightings:
            ctrl = s.get(b.binding_id)
            if ctrl is None or not ctrl.options:
                continue
            aliases: dict[str, tuple[str, ...]] = {
                o.value: (o.label,)
                for o in ctrl.options
                if o.value and o.label and _norm(o.value) != _norm(o.label)
            }
            if aliases:
                codecs[b.binding_id] = FieldCodecSpec(
                    kind=FormCodecKind.ENUM_MAP, enum_aliases=aliases
                )
            break

    # Row key cells and unopened launchers, from every row of every demo.
    row_keys: dict[str, RowKeySuggestion] = {}
    buttons: dict[str, str] = {}
    routes: set[tuple[str, str]] = set()
    clicked = " ".join(_clicked_text(rows) for rows, _ in demos)
    covered_labels = _pack_button_labels(pack)
    for rows, facts in demos:
        values = {
            k: _norm(v) for k, v in facts.items() if v is not None and str(v).strip()
        }
        for row in rows:
            snap = _snapshot(row)
            by_label: dict[str, list[ObservedControl]] = {}
            for c in snap.controls:
                if c.tag == "button" or c.input_type in ("button", "submit"):
                    label = collapse_whitespace(c.label_text or c.current_value)
                    if label:
                        by_label.setdefault(label, []).append(c)
                        buttons.setdefault(_norm(label), label)
            for label, group in by_label.items():
                if len(group) < 2 or not all(c.row_cells for c in group):
                    continue
                keys = tuple(
                    sorted(
                        k
                        for k, v in values.items()
                        if sum(1 for c in group if v in {_norm(x) for x in c.row_cells})
                        == 1
                    )
                )
                if keys:
                    row_keys[label] = RowKeySuggestion(
                        button_label=label, dossier_keys=keys, rows_seen=len(group)
                    )
            for banner in snap.banner_errors:
                text = _norm(banner)
                for c in snap.controls:
                    label = collapse_whitespace(c.label_text)
                    if len(label) >= 4 and _norm(label) in text:
                        routes.add((collapse_whitespace(banner)[:120], label))
    unopened = tuple(
        sorted(
            label
            for key, label in buttons.items()
            if key not in clicked and key not in covered_labels
        )
    )
    return DemoReport(
        demos_used=len(demos),
        defaults=tuple(defaults),
        row_keys=tuple(row_keys.values()),
        option_codecs=codecs,
        unopened_launchers=unopened,
        suggested_error_routes=tuple(sorted(routes)),
        unseen_bindings=unseen,
    )


def _pack_button_labels(pack: FormPack) -> set[str]:
    """Normalized literal labels of every button the pack already handles."""
    anchors: list[Any] = []
    for s in pack.screens:
        anchors += [s.next_button_anchor, s.commit_button_anchor]
    for sub in pack.popup_subroutines:
        anchors += [sub.trigger_anchor, sub.commit_anchor, *sub.compute_anchors]
    anchors += [u.anchor for u in pack.unbound_controls]
    out: set[str] = set()
    for a in anchors:
        if a is None:
            continue
        for p in a.label_patterns:
            out.add(_norm(p.strip("^$").replace("\\", "")))
    return out


def _main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    import importlib

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pack_module", help="module exporting PACK")
    ap.add_argument(
        "--demo",
        nargs=2,
        action="append",
        metavar=("DEMO_JSONL", "INTAKE_JSON"),
        required=True,
    )
    args = ap.parse_args(list(argv) if argv is not None else None)
    pack = importlib.import_module(args.pack_module).PACK
    demos = []
    for demo_path, intake_path in args.demo:
        intake = json.loads(Path(intake_path).read_text())
        demos.append((load_demo(demo_path), intake.get("facts", intake)))
    print(compile_demo_report(pack, demos).model_dump_json(indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
