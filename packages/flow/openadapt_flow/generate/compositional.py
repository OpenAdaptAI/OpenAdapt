"""Compositional field-set contract synthesis with planted-fault admission.

When ``verify_contract.choose()`` encounters a task whose field set is not
listed verbatim in ``manifest.yaml`` (for example after ``zero_touch.install``
strips runtime ``field_sets`` inputs, or when a combination exceeds
``MAX_VARIANTS``), this module can compose a candidate ``(write, found)``
contract pair strictly from per-field direct-column placements already
demonstrated and admitted in the manifest's listed variants.

Fail-closed invariants:
1. Never invents a placement for an undemonstrated field. Every field in
   ``given`` must appear as a direct ``{"param": p}`` match on the ``record``
   surface in at least one listed variant of the same kind, and ``given`` must
   fit the kind's declared ``fields`` specification when present.
2. Refuses conflicting placements, disagreeing invariant pins or side effects,
   non-record param placements, SQL-computed literal-``1`` placements, or
   mismatched SQL ``FROM`` tails (declining any shape that admission cannot
   directly exercise).
3. Composes update permissions strictly per surface from
   ``frame.target_fields_by_surface`` (never pulling another surface's columns
   onto ``record`` and never marking read-only identity selectors as writable).
4. Prepares phone-field matching once before admission and gates every composed
   contract pair through ``admit_composed_contract()``: SQL read-only guard,
   schema validation, 100% bound-field coverage, positive effect and frame
   controls, and planted wrong-value, null-vs-value, missing-row,
   duplicate-row, undeclared-column (update), and sibling-edit faults judged by
   the real effect and frame judges.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import EffectState, Verdict
from openadapt_flow.runtime.effects.frame import FrameSpec, judge_frame
from openadapt_flow.runtime.effects.sql import assert_read_only_sql


@dataclass(frozen=True)
class ComposedAdmissionResult:
    """Verdict of planted-fault admission on a composed contract pair."""

    admitted: bool
    reasons: tuple[str, ...] = ()
    faults_checked: int = 0
    faults_stopped: int = 0


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def apply_phone_rule(
    doc: Mapping[str, Any], params: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a copy of ``doc`` with non-phone-shaped phone params compared exactly.

    Matches :func:`openadapt_flow.generate.verify_contract._load_for` so a
    composed contract is prepared identically for both admission and runtime.
    """

    from openadapt_flow.generate.demo_analysis import phone_shaped

    copied = copy.deepcopy(dict(doc))
    for effect in copied.get("effects") or []:
        for value in (effect.get("match") or {}).values():
            if (
                isinstance(value, dict)
                and value.get("kind") == "phone"
                and not phone_shaped(params.get(str(value.get("param"))))
            ):
                value.pop("kind")
    return copied


def _split_top_level_select(sql: str) -> Optional[tuple[list[tuple[str, str]], str]]:
    """Split ``SELECT <items> FROM <tail>`` at paren depth 0.

    Returns ``([(alias, raw_item), ...], from_tail)`` or ``None`` if the query
    is not a plain top-level ``SELECT ... FROM ...``.
    """

    text = sql.strip().rstrip(";").strip()
    if not text.lower().startswith("select"):
        return None
    if len(text) <= 6 or not text[6].isspace():
        return None

    depth = 0
    quote: Optional[str] = None
    from_pos: Optional[int] = None
    i = 6
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth < 0:
                return None
            i += 1
            continue
        if (
            depth == 0
            and text[i : i + 4].lower() == "from"
            and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_"))
            and (i + 4 == n or not (text[i + 4].isalnum() or text[i + 4] == "_"))
        ):
            from_pos = i
            break
        i += 1

    if from_pos is None:
        return None

    select_part = text[6:from_pos].strip()
    from_tail = text[from_pos:].strip()
    if not select_part or not from_tail:
        return None

    items: list[str] = []
    start = 0
    depth = 0
    quote = None
    for idx, ch in enumerate(select_part):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(select_part[start:idx].strip())
            start = idx + 1
    items.append(select_part[start:].strip())

    parsed: list[tuple[str, str]] = []
    for raw in items:
        if not raw:
            return None
        alias = _projection_alias(raw)
        if not alias:
            return None
        parsed.append((alias, raw))
    return parsed, from_tail


def _unquote_ident(token: str) -> str:
    s = token.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "`"):
        return s[1:-1].replace(s[0] * 2, s[0])
    return s


def _projection_alias(item: str) -> str:
    """Extract the result column name of one ``SELECT`` projection item."""

    depth = 0
    quote: Optional[str] = None
    as_pos: Optional[int] = None
    n = len(item)
    i = 0
    while i < n:
        ch = item[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and item[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif (
            depth == 0
            and item[i : i + 2].lower() == "as"
            and i > 0
            and item[i - 1].isspace()
            and i + 2 < n
            and item[i + 2].isspace()
        ):
            as_pos = i
        i += 1
    if as_pos is not None:
        return _unquote_ident(item[as_pos + 2 :].strip())
    tail = item.rsplit(".", 1)[-1].strip()
    return _unquote_ident(tail)


def _normalize_sql_ws(sql: str) -> str:
    return " ".join(sql.split())


def _match_refs_param(match_val: Any, param: str) -> bool:
    if isinstance(match_val, Mapping):
        if match_val.get("param") == param:
            return True
        return any(_match_refs_param(v, param) for v in match_val.values())
    if isinstance(match_val, (list, tuple)):
        return any(_match_refs_param(v, param) for v in match_val)
    return False


def _expr_refs_param(sql_expr: str, param: str) -> bool:
    return f"%({param})s" in sql_expr or f":{param}" in sql_expr


def _record_target_fields(doc: Mapping[str, Any]) -> Optional[set[str]]:
    """Return the writable columns owned by the ``record`` surface only."""

    frame = doc.get("frame")
    if not isinstance(frame, Mapping):
        return set()
    tfs_by_surf = frame.get("target_fields_by_surface")
    if isinstance(tfs_by_surf, Mapping) and tfs_by_surf:
        return set(str(c) for c in (tfs_by_surf.get("record") or []))
    surfaces = ((doc.get("verifier") or {}).get("surfaces")) or {}
    if isinstance(surfaces, Mapping) and set(surfaces) == {"record"}:
        return set(str(c) for c in (frame.get("target_fields") or []))
    if not frame.get("target_fields"):
        return set()
    return None  # ambiguous multi-surface permissions without per-surface map


def _non_record_target_fields(doc: Mapping[str, Any]) -> Optional[dict[str, list[str]]]:
    """Return sorted per-surface target fields for all non-``record`` surfaces."""

    frame = doc.get("frame")
    if not isinstance(frame, Mapping):
        return {}
    tfs_by_surf = frame.get("target_fields_by_surface")
    if not isinstance(tfs_by_surf, Mapping):
        return {}
    return {
        str(k): sorted(str(c) for c in (v or []))
        for k, v in sorted(tfs_by_surf.items())
        if k != "record"
    }


def _direct_param_keys(doc: Mapping[str, Any], param: str) -> set[str]:
    """Return ``record`` effect ``match`` keys that directly bind ``{"param": param}``."""

    effects = doc.get("effects") or []
    if not effects:
        return set()
    main_match = effects[0].get("match") or {}
    return {str(mk) for mk, mv in main_match.items() if _match_refs_param(mv, param)}


def _has_sql_param_binding(doc: Mapping[str, Any], param: str) -> bool:
    sql_params = ((doc.get("verifier") or {}).get("surfaces") or {}).get(
        "record", {}
    ).get("sql_query_params") or {}
    return any(
        isinstance(v, Mapping) and v.get("param") == param for v in sql_params.values()
    )


def _compose_single_flavor(
    docs_by_variant: Sequence[tuple[frozenset[str], dict[str, Any]]],
    given: frozenset[str],
    *,
    mode: str,
    flavor: str,
    identity_fields: frozenset[str],
) -> Optional[dict[str, Any]]:
    """Compose one contract document (``create``, ``update``, or ``found``)."""

    if not docs_by_variant:
        return None

    parsed_variants: list[
        tuple[
            frozenset[str], dict[str, Any], list[tuple[str, str]], dict[str, str], str
        ]
    ] = []
    base_from_norm: Optional[str] = None
    base_effect_kind: Optional[str] = None
    base_expected_count: Any = None
    base_count_new_only: Any = None
    base_dialect: Any = None
    base_extra_effects: Optional[list[dict[str, Any]]] = None
    base_non_record_surfaces: Optional[dict[str, Any]] = None
    base_non_record_tfs: Optional[dict[str, list[str]]] = None

    for v_fields, doc in docs_by_variant:
        if doc.get("schema_version") != "openadapt.verify/v1":
            return None
        verifier = doc.get("verifier")
        if not isinstance(verifier, Mapping) or verifier.get("kind") != "surfaces":
            return None
        surfaces = verifier.get("surfaces")
        if not isinstance(surfaces, Mapping) or "record" not in surfaces:
            return None
        rec_surf = surfaces["record"]
        if not isinstance(rec_surf, Mapping) or rec_surf.get("kind") != "sql":
            return None
        sql = rec_surf.get("sql_query")
        if not isinstance(sql, str):
            return None
        split = _split_top_level_select(sql)
        if split is None:
            return None
        proj_list, from_tail = split
        from_norm = _normalize_sql_ws(from_tail)
        if base_from_norm is None:
            base_from_norm = from_norm
        elif from_norm != base_from_norm:
            return None

        effects = doc.get("effects")
        if not isinstance(effects, list) or not effects:
            return None
        e0 = effects[0]
        if not isinstance(e0, Mapping):
            return None
        extra_effs = effects[1:]
        # Decline if any non-record side effect references a task param or differs across variants.
        if any(
            _match_refs_param(eff.get("match") or {}, p)
            for eff in extra_effs
            for p in v_fields
        ):
            return None
        non_rec_surfs = {k: v for k, v in surfaces.items() if k != "record"}
        non_rec_tfs = _non_record_target_fields(doc)
        if non_rec_tfs is None or _record_target_fields(doc) is None:
            return None

        if base_effect_kind is None:
            base_effect_kind = str(e0.get("kind") or "")
            base_expected_count = e0.get("expected_count")
            base_count_new_only = e0.get("count_new_only")
            base_extra_effects = extra_effs
            base_non_record_surfaces = non_rec_surfs
            base_non_record_tfs = non_rec_tfs
        else:
            if (
                str(e0.get("kind") or "") != base_effect_kind
                or e0.get("expected_count") != base_expected_count
                or e0.get("count_new_only") != base_count_new_only
                or extra_effs != base_extra_effects
                or non_rec_surfs != base_non_record_surfaces
                or non_rec_tfs != base_non_record_tfs
            ):
                return None

        frame = doc.get("frame") or {}
        db_cfg = frame.get("database") or {} if isinstance(frame, Mapping) else {}
        dialect = db_cfg.get("dialect")
        if base_dialect is None:
            base_dialect = dialect
        elif dialect != base_dialect:
            return None

        proj_map = {alias: raw for alias, raw in proj_list}
        parsed_variants.append((v_fields, doc, proj_list, proj_map, from_tail))

    # Choose base variant deterministically (highest overlap with ``given``).
    parsed_variants.sort(
        key=lambda item: (len(item[0] & given), -len(item[0] - given)),
        reverse=True,
    )
    _, base_doc, base_proj_list, base_proj_map, base_from_tail = parsed_variants[0]

    all_variant_fields: set[str] = set().union(*(v[0] for v in parsed_variants))
    if not given <= all_variant_fields:
        return None

    # Map every variant's direct param keys and classify invariant vs absent-field pins.
    variant_param_keys: list[dict[str, set[str]]] = []
    all_param_match_keys: set[str] = set()
    for v_fields, doc, _, _, _ in parsed_variants:
        pmap: dict[str, set[str]] = {}
        for p in v_fields:
            # Decline SQL-computed literal-1 placements that admission cannot directly perturb.
            if _has_sql_param_binding(doc, p):
                return None
            pkeys = _direct_param_keys(doc, p)
            if not pkeys:
                return None
            pmap[p] = pkeys
            all_param_match_keys.update(pkeys)
        variant_param_keys.append(pmap)

    # Identify ``empty__*`` keys owned by each optional field:
    # an ``empty__*`` key belongs to field ``f`` if it is present in every variant
    # omitting ``f`` and absent in every variant containing ``f``.
    all_empty_keys: set[str] = {
        str(mk)
        for _, doc, _, _, _ in parsed_variants
        for mk in (doc["effects"][0].get("match") or {})
        if str(mk).startswith("empty__")
    }
    empty_keys_by_field: dict[str, dict[str, tuple[Any, Optional[str]]]] = {}
    for f in sorted(all_variant_fields):
        with_f = [item for item in parsed_variants if f in item[0]]
        without_f = [item for item in parsed_variants if f not in item[0]]
        if not with_f or not without_f:
            continue
        owned: dict[str, tuple[Any, Optional[str]]] = {}
        for ek in sorted(all_empty_keys):
            in_all_without = all(
                ek in (d["effects"][0].get("match") or {})
                for _, d, _, _, _ in without_f
            )
            in_any_with = any(
                ek in (d["effects"][0].get("match") or {}) for _, d, _, _, _ in with_f
            )
            if in_all_without and not in_any_with:
                # Require all variants omitting ``f`` to agree on the empty pin & SQL expr.
                vals = [d["effects"][0]["match"][ek] for _, d, _, _, _ in without_f]
                exprs = [pm.get(ek) for _, _, _, pm, _ in without_f]
                if any(v != vals[0] for v in vals[1:]) or any(
                    e != exprs[0] for e in exprs[1:]
                ):
                    return None
                owned[ek] = (copy.deepcopy(vals[0]), exprs[0])
        if owned:
            empty_keys_by_field[f] = owned

    # Every non-param, non-empty__ match key (invariant literal/constant/timestamp pins)
    # must agree across ALL source variants (manifest order never resolves conflicts).
    invariant_pins: Optional[dict[str, tuple[Any, Optional[str]]]] = None
    for idx, (_, doc, _, proj_map, _) in enumerate(parsed_variants):
        v_pkeys = (
            set().union(*variant_param_keys[idx].values())
            if variant_param_keys[idx]
            else set()
        )
        pins = {
            str(mk): (copy.deepcopy(mv), proj_map.get(str(mk)))
            for mk, mv in (doc["effects"][0].get("match") or {}).items()
            if str(mk) not in v_pkeys and not str(mk).startswith("empty__")
        }
        if invariant_pins is None:
            invariant_pins = pins
        elif pins != invariant_pins:
            return None
    assert invariant_pins is not None

    # Every unowned ``empty__*`` key (present in all variants) must also agree across all variants.
    owned_empty_keys: set[str] = {
        ek for mapping in empty_keys_by_field.values() for ek in mapping
    }
    shared_empty_pins: Optional[dict[str, tuple[Any, Optional[str]]]] = None
    for _, doc, _, proj_map, _ in parsed_variants:
        se = {
            str(mk): (copy.deepcopy(mv), proj_map.get(str(mk)))
            for mk, mv in (doc["effects"][0].get("match") or {}).items()
            if str(mk).startswith("empty__") and str(mk) not in owned_empty_keys
        }
        if shared_empty_pins is None:
            shared_empty_pins = se
        elif se != shared_empty_pins:
            return None
    assert shared_empty_pins is not None

    # Assemble merged match and SQL projections.
    merged_match: dict[str, Any] = {}
    merged_projs: dict[str, str] = dict(base_proj_map)
    merged_proj_order: list[str] = [alias for alias, _ in base_proj_list]

    for mk, (mv, expr) in sorted(invariant_pins.items()):
        merged_match[mk] = copy.deepcopy(mv)
        if expr is not None:
            if mk not in merged_projs:
                merged_proj_order.append(mk)
            merged_projs[mk] = expr

    for mk, (mv, expr) in sorted(shared_empty_pins.items()):
        merged_match[mk] = copy.deepcopy(mv)
        if expr is not None:
            if mk not in merged_projs:
                merged_proj_order.append(mk)
            merged_projs[mk] = expr

    param_match_keys: dict[str, set[str]] = {}
    for p in sorted(given):
        sources = [
            (idx, item) for idx, item in enumerate(parsed_variants) if p in item[0]
        ]
        if not sources:
            return None

        chosen_entries: Optional[dict[str, Any]] = None
        chosen_projs: Optional[dict[str, str]] = None
        for idx, (_, doc, _, proj_map, _) in sources:
            e0_match = doc["effects"][0].get("match") or {}
            pkeys = variant_param_keys[idx].get(p, set())
            entries = {
                k: copy.deepcopy(e0_match[k]) for k in sorted(pkeys) if k in e0_match
            }
            projs = {k: proj_map[k] for k in sorted(entries) if k in proj_map}
            if not entries:
                return None
            if chosen_entries is None:
                chosen_entries = entries
                chosen_projs = projs
            elif entries != chosen_entries or projs != chosen_projs:
                return None  # conflicting placements across variants -> fail closed

        assert chosen_entries is not None and chosen_projs is not None
        param_match_keys[p] = set(chosen_entries)
        for k, val in chosen_entries.items():
            if k in merged_match and merged_match[k] != val:
                return None
            merged_match[k] = val
        for alias, expr in chosen_projs.items():
            if alias in merged_projs and merged_projs[alias] != expr:
                return None
            if alias not in merged_projs:
                merged_proj_order.append(alias)
            merged_projs[alias] = expr

    # For create mode: every omitted field must be omitted in at least one admitted variant,
    # and we carry its exact demonstrated ``empty__*`` pin(s).
    omitted_fields = all_variant_fields - given
    if mode == "create":
        for q in sorted(omitted_fields):
            if flavor == "create" and not any(
                q not in vf for vf, _, _, _, _ in parsed_variants
            ):
                return None
            for ek, (ev, expr) in sorted(empty_keys_by_field.get(q, {}).items()):
                if ek in merged_match and merged_match[ek] != ev:
                    return None
                merged_match[ek] = copy.deepcopy(ev)
                if expr is not None:
                    if ek not in merged_projs:
                        merged_proj_order.append(ek)
                    merged_projs[ek] = expr

    # Build final SELECT query, dropping any removed ``empty__*`` projections.
    used_aliases = set(merged_match)
    final_projs: list[str] = []
    for alias in merged_proj_order:
        if alias.startswith("empty__") and alias not in used_aliases:
            continue
        if alias in merged_projs:
            final_projs.append(merged_projs[alias])
    final_record_sql = f"SELECT {', '.join(final_projs)} {base_from_tail}"

    # Carry non-task sql_query_params (e.g. actor params) only if referenced in SQL.
    base_rec_params = (
        base_doc["verifier"]["surfaces"]["record"].get("sql_query_params") or {}
    )
    merged_sql_params = {
        pk: copy.deepcopy(pv)
        for pk, pv in base_rec_params.items()
        if _expr_refs_param(final_record_sql, pk)
    }

    composed = copy.deepcopy(base_doc)
    composed["verifier"]["surfaces"]["record"]["sql_query"] = final_record_sql
    if merged_sql_params:
        composed["verifier"]["surfaces"]["record"]["sql_query_params"] = (
            merged_sql_params
        )
    else:
        composed["verifier"]["surfaces"]["record"].pop("sql_query_params", None)
    composed["effects"][0]["match"] = merged_match

    # Compose update permissions strictly per surface from ``target_fields_by_surface``,
    # separating read-only identity selectors from changed fields.
    frame = composed.get("frame")
    if isinstance(frame, dict) and mode == "update" and flavor == "update":
        per_variant_rec_tf: list[tuple[frozenset[str], set[str]]] = []
        for vf, d, _, _, _ in parsed_variants:
            rec_tf = _record_target_fields(d)
            if rec_tf is None:
                return None
            per_variant_rec_tf.append((vf, rec_tf))

        # Shared bookkeeping on ``record`` = columns in EVERY variant's record target_fields
        # that are not owned by any task parameter.
        shared_rec_tf = (
            set.intersection(*(tf for _, tf in per_variant_rec_tf))
            - all_param_match_keys
            if per_variant_rec_tf
            else set()
        )
        composed_rec_tf = set(shared_rec_tf)
        changed_given = given - identity_fields
        for p in sorted(changed_given):
            with_p = [tf for vf, tf in per_variant_rec_tf if p in vf]
            without_p = [tf for vf, tf in per_variant_rec_tf if p not in vf]
            if not with_p:
                return None
            # Only grant write permission on columns that EVERY variant with ``p`` grants
            # on the ``record`` surface (never granting read-only identity columns).
            p_allowed = set.intersection(*with_p)
            p_cols = param_match_keys.get(p, set()) & p_allowed
            only_with_p = p_allowed - (
                set[str]().union(*without_p) if without_p else set()
            )
            composed_rec_tf.update(p_cols)
            composed_rec_tf.update(only_with_p)

        tfs_by_surf: dict[str, list[str]] = dict(base_non_record_tfs or {})
        if composed_rec_tf:
            tfs_by_surf["record"] = sorted(composed_rec_tf)
        else:
            tfs_by_surf.pop("record", None)
        frame["target_fields_by_surface"] = {
            k: sorted(v) for k, v in sorted(tfs_by_surf.items()) if v
        }
        frame["target_fields"] = sorted(
            set().union(*(set(v) for v in frame["target_fields_by_surface"].values()))
            if frame["target_fields_by_surface"]
            else set()
        )

    return composed


def compose_field_set_docs(
    manifest_path: Path,
    entry: Mapping[str, Any],
    given: set[str],
    lists: Mapping[str, int],
    cleared: Sequence[str] = (),
) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    """Compose ``(write_doc, found_doc)`` from already-admitted variants in ``entry``.

    Returns ``None`` (fail closed) whenever composition is ineligible, any
    field in ``given`` is undemonstrated, or source variants conflict.
    """

    from openadapt_flow.generate.verify_contract import _fits

    if entry.get("dispatch") or lists or cleared:
        return None
    mode = str(entry.get("mode") or "")
    if mode not in ("create", "update"):
        return None
    spec = entry.get("fields")
    if isinstance(spec, Mapping) and spec:
        if not _fits(spec, given, lists, cleared):
            return None

    base = Path(manifest_path).parent
    variants = entry.get("variants") or []
    write_sources: list[tuple[frozenset[str], dict[str, Any]]] = []
    found_sources: list[tuple[frozenset[str], dict[str, Any]]] = []

    for var in variants:
        if var.get("lists") or var.get("cleared"):
            continue
        w_rel = var.get(mode)
        f_rel = var.get("found")
        if not isinstance(w_rel, str) or not isinstance(f_rel, str):
            continue
        w_path = base / w_rel
        f_path = base / f_rel
        if not w_path.is_file() or not f_path.is_file():
            continue
        v_fields = frozenset(str(x) for x in (var.get("fields") or []))
        w_doc = _load_yaml(w_path)
        f_doc = _load_yaml(f_path)
        if not isinstance(w_doc, dict) or not isinstance(f_doc, dict):
            return None
        write_sources.append((v_fields, w_doc))
        found_sources.append((v_fields, f_doc))

    if not write_sources:
        return None

    # Determine identity fields for update mode (read-only selectors).
    if mode == "update":
        if isinstance(spec, Mapping) and "identity" in spec:
            identity_fields = frozenset(str(x) for x in (spec.get("identity") or []))
        else:
            identity_fields = frozenset(
                set.intersection(*(set(vf) for vf, _ in write_sources))
            )
        if not identity_fields <= given or not (given - identity_fields):
            return None
    else:
        identity_fields = frozenset()
        if not (isinstance(spec, Mapping) and spec):
            common_fields = set.intersection(*(set(vf) for vf, _ in write_sources))
            if not common_fields <= given:
                return None

    given_fs = frozenset(given)
    write_doc = _compose_single_flavor(
        write_sources,
        given_fs,
        mode=mode,
        flavor=mode,
        identity_fields=identity_fields,
    )
    if write_doc is None:
        return None
    found_doc = _compose_single_flavor(
        found_sources,
        given_fs,
        mode=mode,
        flavor="found",
        identity_fields=identity_fields,
    )
    if found_doc is None:
        return None
    return write_doc, found_doc


def _synthetic_sample_value(param_name: str, provided: Mapping[str, Any]) -> Any:
    val = provided.get(param_name)
    if val is not None and not isinstance(val, (dict, list)):
        return val
    return f"sample_{param_name}"


def _perturb_value(value: Any, field: str) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 999
    if isinstance(value, float):
        return value + 999.5
    text = str(value)
    return f"__wrong_{field}_{text}__"


def _build_synthetic_record(
    effect: Any,
    params: Mapping[str, Any],
    *,
    identity_field: str,
    row_id: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {identity_field: row_id}
    for col, expr in effect.match.items():
        if expr.null:
            record[col] = None
        elif expr.literal is not None:
            record[col] = expr.literal
        else:
            val = expr.resolve(params)
            record[col] = val
    return record


def admit_composed_contract(
    write_doc: Mapping[str, Any],
    found_doc: Mapping[str, Any],
    *,
    mode: str,
    given: set[str],
    params: Mapping[str, Any],
) -> ComposedAdmissionResult:
    """Gate a prepared composed ``(write_doc, found_doc)`` pair through planted-fault admission.

    Verifies:
    1. Read-only SQL guard on every SQL surface in both documents.
    2. Schema parse via ``Contract.from_dict`` on both documents.
    3. Every field in ``given`` is bound in ``write_contract.params`` and
       ``found_contract.params`` AND directly exercised in ``effects[0].match``.
    4. Positive controls: correct post-write state confirms on ``write``,
       ``found``, and ``judge_frame``.
    5. Planted wrong-value fault on every field in ``given`` refutes.
    6. Planted null-vs-value fault on every non-null field in ``given`` refutes.
    7. Planted missing-write / no-op fault stops.
    8. Planted duplicate-row fault (``create`` mode) refutes.
    9. Planted undeclared-column mutation on target (``update`` mode) refutes
       via ``judge_frame``.
    10. Planted sibling-edit / collateral fault refutes via ``judge_frame``.
    """

    from openadapt_flow.verifier import Contract

    reasons: list[str] = []
    faults_checked = 0
    faults_stopped = 0

    # 1. SQL guard check on all surfaces in both docs.
    for label, doc in (("write", write_doc), ("found", found_doc)):
        surfaces = ((doc.get("verifier") or {}).get("surfaces")) or {}
        if not isinstance(surfaces, Mapping) or "record" not in surfaces:
            return ComposedAdmissionResult(
                False, (f"{label} contract missing record surface",)
            )
        for sname, sdef in surfaces.items():
            if isinstance(sdef, Mapping) and sdef.get("kind") == "sql":
                try:
                    assert_read_only_sql(
                        str(sdef.get("sql_query") or ""),
                        binds=bool(sdef.get("sql_query_params")),
                    )
                except ValueError as exc:
                    return ComposedAdmissionResult(
                        False, (f"{label} surface {sname!r} failed SQL guard: {exc}",)
                    )

    # 2. Schema parse check.
    try:
        write_contract = Contract.from_dict(dict(write_doc), name="composed_write")
        found_contract = Contract.from_dict(dict(found_doc), name="composed_found")
    except Exception as exc:  # noqa: BLE001 - fail closed on any schema error
        return ComposedAdmissionResult(
            False, (f"composed contract failed schema validation: {exc}",)
        )

    # 3. 100% bound-field coverage + direct-placement check on every field in ``given``.
    write_params = set(write_contract.params)
    found_params = set(found_contract.params)
    missing_write = sorted(given - write_params)
    missing_found = sorted(given - found_params)
    if missing_write:
        reasons.append(f"write contract dropped field(s): {', '.join(missing_write)}")
    if missing_found:
        reasons.append(f"found contract dropped field(s): {', '.join(missing_found)}")

    raw_match = write_doc["effects"][0].get("match") or {}
    param_to_cols: dict[str, list[str]] = {}
    for p in sorted(given):
        cols = [str(k) for k, v in raw_match.items() if _match_refs_param(v, p)]
        if not cols:
            reasons.append(
                f"field {p!r} has no direct record-effect placement to exercise"
            )
        else:
            param_to_cols[p] = cols
    if reasons:
        return ComposedAdmissionResult(False, tuple(reasons))

    sample_params: dict[str, Any] = {}
    for pname in sorted(write_params | found_params):
        sample_params[pname] = _synthetic_sample_value(pname, params)

    identity_field = str((write_doc.get("frame") or {}).get("identity_field") or "id")
    write_effects = [
        eff.resolve(sample_params) for eff in write_contract.config.effects
    ]
    found_effects = [
        eff.resolve(sample_params) for eff in found_contract.config.effects
    ]
    if not write_effects or not found_effects:
        return ComposedAdmissionResult(False, ("composed contract has no effects",))

    main_write = write_effects[0]
    main_found = found_effects[0]

    sibling_before: dict[str, Any] = {
        identity_field: "sibling-1",
        "__unrelated_col__": "untouched",
    }
    for col in main_write.match:
        if col != identity_field:
            sibling_before[col] = f"__sibling_{col}__"

    target_after = _build_synthetic_record(
        main_write,
        sample_params,
        identity_field=identity_field,
        row_id="target-1",
    )
    target_after["__unrelated_col__"] = "untouched"

    if mode == "create":
        before_rows = [copy.deepcopy(sibling_before)]
        after_rows = [copy.deepcopy(sibling_before), copy.deepcopy(target_after)]
    else:
        target_before = copy.deepcopy(target_after)
        rec_tf = _record_target_fields(write_doc) or set()
        for col in rec_tf:
            if col in target_before and col != identity_field:
                target_before[col] = f"__before_{col}__"
        before_rows = [copy.deepcopy(sibling_before), target_before]
        after_rows = [copy.deepcopy(sibling_before), copy.deepcopy(target_after)]

    before_state = EffectState(substrate="sql", reachable=True, records=before_rows)

    # 4a. Positive effect controls.
    pos_write = judge_records(main_write, before_state, after_rows, substrate="sql")
    if pos_write.verdict is not Verdict.CONFIRMED:
        reasons.append(
            f"positive control failed on write contract: {pos_write.verdict.value} ({pos_write.reason})"
        )
    pos_found = judge_records(
        main_found,
        EffectState(substrate="sql", reachable=True, records=after_rows),
        after_rows,
        substrate="sql",
    )
    if pos_found.verdict is not Verdict.CONFIRMED:
        reasons.append(
            f"positive control failed on found contract: {pos_found.verdict.value} ({pos_found.reason})"
        )

    # 4b. Positive frame control on the clean post-write state.
    frame_spec = write_contract.config.frame or FrameSpec(identity_field=identity_field)
    if not frame_spec.identity_field:
        frame_spec = frame_spec.model_copy(update={"identity_field": identity_field})
    clean_frame = judge_frame(
        frame_spec,
        [main_write],
        {"record": (before_state, after_rows)},
        params=sample_params,
        routes={"0": ["record"]},
        identities={"record": identity_field},
    )
    if clean_frame.status != "held":
        reasons.append(
            f"positive frame control failed on clean write: {clean_frame.status} ({clean_frame.reason})"
        )

    # 5. Planted wrong-value fault on EVERY field in ``given``.
    for p, cols in sorted(param_to_cols.items()):
        for col in cols:
            faults_checked += 1
            mutated_target = copy.deepcopy(target_after)
            mutated_target[col] = _perturb_value(target_after.get(col), col)
            mutated_after = [copy.deepcopy(sibling_before), mutated_target]
            res = judge_records(
                main_write, before_state, mutated_after, substrate="sql"
            )
            if res.verdict is Verdict.CONFIRMED:
                reasons.append(
                    f"planted wrong-value fault on field {p!r} (col {col!r}) passed"
                )
            else:
                faults_stopped += 1

    # 6. Planted null-vs-value fault on EVERY non-null field in ``given``.
    for p, cols in sorted(param_to_cols.items()):
        if sample_params.get(p) is None:
            continue
        for col in cols:
            faults_checked += 1
            mutated_target = copy.deepcopy(target_after)
            mutated_target[col] = None
            mutated_after = [copy.deepcopy(sibling_before), mutated_target]
            res = judge_records(
                main_write, before_state, mutated_after, substrate="sql"
            )
            if res.verdict is Verdict.CONFIRMED:
                reasons.append(
                    f"planted null-vs-value fault on field {p!r} (col {col!r}) passed"
                )
            else:
                faults_stopped += 1

    # 7. Planted missing-write / no-op fault.
    faults_checked += 1
    noop_res = judge_records(main_write, before_state, before_rows, substrate="sql")
    if noop_res.verdict is Verdict.CONFIRMED:
        reasons.append("planted missing-write (no-op) fault passed")
    else:
        faults_stopped += 1

    # 8. Planted duplicate-row fault (create mode with expected_count == 1).
    if mode == "create" and main_write.expected_count == 1:
        faults_checked += 1
        dup_row = copy.deepcopy(target_after)
        dup_row[identity_field] = "target-2"
        dup_after = [
            copy.deepcopy(sibling_before),
            copy.deepcopy(target_after),
            dup_row,
        ]
        dup_res = judge_records(main_write, before_state, dup_after, substrate="sql")
        if dup_res.verdict is Verdict.CONFIRMED:
            reasons.append("planted duplicate-row fault passed")
        else:
            faults_stopped += 1

    # 9. Planted undeclared-column mutation on target (update mode).
    if mode == "update":
        faults_checked += 1
        mutated_target = copy.deepcopy(target_after)
        mutated_target["__unrelated_col__"] = "__undeclared_edit__"
        undeclared_after = [copy.deepcopy(sibling_before), mutated_target]
        undeclared_verdict = judge_frame(
            frame_spec,
            [main_write],
            {"record": (before_state, undeclared_after)},
            params=sample_params,
            routes={"0": ["record"]},
            identities={"record": identity_field},
        )
        if undeclared_verdict.status != "refuted":
            reasons.append(
                f"planted undeclared target-column edit was not refuted by judge_frame: {undeclared_verdict.status}"
            )
        else:
            faults_stopped += 1

    # 10. Planted sibling-edit / collateral fault judged by ``judge_frame``.
    faults_checked += 1
    mutated_sibling = copy.deepcopy(sibling_before)
    mutated_sibling["__unrelated_col__"] = "__collateral_edit__"
    collateral_after = [mutated_sibling, copy.deepcopy(target_after)]
    frame_verdict = judge_frame(
        frame_spec,
        [main_write],
        {"record": (before_state, collateral_after)},
        params=sample_params,
        routes={"0": ["record"]},
        identities={"record": identity_field},
    )
    if frame_verdict.status != "refuted":
        reasons.append(
            f"planted sibling-edit collateral fault was not refuted by judge_frame: {frame_verdict.status}"
        )
    else:
        faults_stopped += 1

    return ComposedAdmissionResult(
        admitted=not reasons,
        reasons=tuple(reasons),
        faults_checked=faults_checked,
        faults_stopped=faults_stopped,
    )


def compose_and_admit(
    manifest_path: Path,
    entry: Mapping[str, Any],
    given: set[str],
    lists: Mapping[str, int],
    cleared: Sequence[str],
    params: Mapping[str, Any],
) -> Optional[tuple[dict[str, Any], dict[str, Any], ComposedAdmissionResult]]:
    """Compose, prepare (phone rule), and gate a novel field-set contract pair.

    Returns ``(prepared_write_doc, prepared_found_doc, admission_result)`` when
    composition succeeds and passes admission, or ``None`` if composition is
    not eligible. Raises ``ValueError`` if a composed contract fails admission.
    """

    docs = compose_field_set_docs(manifest_path, entry, given, lists, cleared)
    if docs is None:
        return None
    write_doc, found_doc = docs
    # Prepare once (including phone-shape normalization) so admission and
    # runtime judge the exact same contract document.
    prepared_write = apply_phone_rule(write_doc, params)
    prepared_found = apply_phone_rule(found_doc, params)
    mode = str(entry.get("mode") or "")
    admission = admit_composed_contract(
        prepared_write,
        prepared_found,
        mode=mode,
        given=given,
        params=params,
    )
    if not admission.admitted:
        raise ValueError(
            "composed contract failed planted-fault admission: "
            + "; ".join(admission.reasons)
        )
    return prepared_write, prepared_found, admission
