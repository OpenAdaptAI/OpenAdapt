"""Compositional field-set contract synthesis with planted-fault admission.

When ``verify_contract.choose()`` encounters a task whose field set is not
listed verbatim in ``manifest.yaml`` (for example after ``zero_touch.install``
strips runtime ``field_sets`` inputs, or when a combination exceeds
``MAX_VARIANTS``), this module can compose a candidate ``(write, found)``
contract pair strictly from per-field placements already demonstrated and
admitted in the manifest's listed variants.

Fail-closed invariants:
1. Never invents a placement for an undemonstrated field. Every field in
   ``given`` must appear in at least one listed variant of the same kind, and
   ``given`` must fit the kind's declared ``fields`` specification when
   present.
2. Refuses conflicting placements across variants, mismatched SQL base tables,
   or unpinned absent fields in ``create`` mode.
3. Gates every composed contract pair through ``admit_composed_contract()``:
   SQL read-only guard, contract schema validation, 100% bound-field coverage,
   a positive control, and planted wrong-value, null-vs-value, missing-row,
   duplicate-row, and sibling-edit faults judged by the real effect and frame
   judges. Any failure rejects composition (fail closed).
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


def _surface_name_from_probe(probe: Any) -> str:
    if not isinstance(probe, str):
        return ""
    head = probe.split("|", 1)[0].strip()
    if head.startswith("surface="):
        return head[len("surface=") :].strip()
    return ""


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


def _find_param_keys_in_doc(
    doc: Mapping[str, Any],
    param: str,
    projections: Mapping[str, str],
) -> set[str]:
    """Return all ``record`` effect ``match`` keys that bind ``param``."""

    effects = doc.get("effects") or []
    if not effects:
        return set()
    main_match = effects[0].get("match") or {}
    sql_params = ((doc.get("verifier") or {}).get("surfaces") or {}).get(
        "record", {}
    ).get("sql_query_params") or {}
    sql_param_keys = {
        k
        for k, v in sql_params.items()
        if isinstance(v, Mapping) and v.get("param") == param
    }
    keys: set[str] = set()
    for mk, mv in main_match.items():
        if _match_refs_param(mv, param):
            keys.add(str(mk))
            continue
        proj = projections.get(str(mk), "")
        if any(_expr_refs_param(proj, pk) for pk in sql_param_keys):
            keys.add(str(mk))
    return keys


def _compose_single_flavor(
    docs_by_variant: Sequence[tuple[frozenset[str], dict[str, Any]]],
    given: frozenset[str],
    *,
    mode: str,
    flavor: str,
) -> Optional[dict[str, Any]]:
    """Compose one contract document (``create``, ``update``, or ``found``)."""

    if not docs_by_variant:
        return None

    # Parse every source variant's ``record`` SQL query and verify structural compatibility.
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
        if base_effect_kind is None:
            base_effect_kind = str(e0.get("kind") or "")
            base_expected_count = e0.get("expected_count")
            base_count_new_only = e0.get("count_new_only")
        else:
            if (
                str(e0.get("kind") or "") != base_effect_kind
                or e0.get("expected_count") != base_expected_count
                or e0.get("count_new_only") != base_count_new_only
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

    # Choose the base variant with the highest field overlap with ``given``.
    parsed_variants.sort(
        key=lambda item: (len(item[0] & given), -len(item[0] - given)),
        reverse=True,
    )
    base_fields, base_doc, base_proj_list, base_proj_map, base_from_tail = (
        parsed_variants[0]
    )

    all_variant_fields: set[str] = set().union(*(v[0] for v in parsed_variants))
    if not given <= all_variant_fields:
        return None

    # Every field in ``given`` must resolve consistently across all variants that include it.
    merged_match: dict[str, Any] = {}
    merged_projs: dict[str, str] = dict(base_proj_map)
    merged_proj_order: list[str] = [alias for alias, _ in base_proj_list]
    merged_sql_params: dict[str, Any] = {}
    extra_surfaces: dict[str, dict[str, Any]] = {}
    extra_effects: list[dict[str, Any]] = []
    extra_target_by_surface: dict[str, set[str]] = {}
    extra_judged_by: dict[str, str] = {}

    bound_columns: set[str] = set()
    param_match_keys: dict[str, set[str]] = {}
    all_param_match_keys: set[str] = set()

    # Map every variant's param -> match keys so we know which match keys are param-owned.
    variant_param_keys: list[dict[str, set[str]]] = []
    for v_fields, doc, _, proj_map, _ in parsed_variants:
        pmap: dict[str, set[str]] = {}
        for p in v_fields:
            pkeys = _find_param_keys_in_doc(doc, p, proj_map)
            pmap[p] = pkeys
            all_param_match_keys.update(pkeys)
        variant_param_keys.append(pmap)

    for p in sorted(given):
        sources = [
            (idx, item) for idx, item in enumerate(parsed_variants) if p in item[0]
        ]
        if not sources:
            return None

        chosen_entries: Optional[dict[str, Any]] = None
        chosen_projs: Optional[dict[str, str]] = None
        chosen_sql_params: Optional[dict[str, Any]] = None
        chosen_extra_effects: Optional[
            list[tuple[str, dict[str, Any], dict[str, Any]]]
        ] = None

        for idx, (v_fields, doc, _, proj_map, _) in sources:
            e0_match = doc["effects"][0].get("match") or {}
            pkeys = variant_param_keys[idx].get(p, set())
            entries = {
                k: copy.deepcopy(e0_match[k]) for k in sorted(pkeys) if k in e0_match
            }
            projs = {k: proj_map[k] for k in sorted(entries) if k in proj_map}
            rec_params = (
                doc["verifier"]["surfaces"]["record"].get("sql_query_params") or {}
            )
            p_sql_params = {
                k: copy.deepcopy(v)
                for k, v in rec_params.items()
                if isinstance(v, Mapping) and v.get("param") == p
            }
            # Also check if ``p`` lives on a non-record child surface.
            p_extra: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            for eff in doc["effects"][1:]:
                if _match_refs_param(eff.get("match") or {}, p):
                    sname = _surface_name_from_probe(eff.get("probe"))
                    if sname and sname in doc["verifier"]["surfaces"]:
                        p_extra.append(
                            (
                                sname,
                                copy.deepcopy(doc["verifier"]["surfaces"][sname]),
                                copy.deepcopy(eff),
                            )
                        )

            if not entries and not p_extra:
                return None

            if chosen_entries is None:
                chosen_entries = entries
                chosen_projs = projs
                chosen_sql_params = p_sql_params
                chosen_extra_effects = p_extra
            else:
                if (
                    entries != chosen_entries
                    or projs != chosen_projs
                    or p_sql_params != chosen_sql_params
                    or p_extra != chosen_extra_effects
                ):
                    return None  # conflicting placements across variants -> fail closed

        assert chosen_entries is not None and chosen_projs is not None
        assert chosen_sql_params is not None and chosen_extra_effects is not None

        param_match_keys[p] = set(chosen_entries)
        for k, val in chosen_entries.items():
            if k in merged_match and merged_match[k] != val:
                return None
            merged_match[k] = val
            bound_columns.add(k)
        for alias, expr in chosen_projs.items():
            if alias in merged_projs and merged_projs[alias] != expr:
                return None
            if alias not in merged_projs:
                merged_proj_order.append(alias)
            merged_projs[alias] = expr
        for pk, pv in chosen_sql_params.items():
            if pk in merged_sql_params and merged_sql_params[pk] != pv:
                return None
            merged_sql_params[pk] = pv
        for sname, sdef, eff in chosen_extra_effects:
            if sname in extra_surfaces and extra_surfaces[sname] != sdef:
                return None
            extra_surfaces[sname] = sdef
            if eff not in extra_effects:
                extra_effects.append(eff)

    # Preserve non-param pins (constants, timestamps, copies, and absent-field pins).
    # First, keep pins that are invariant across all variants (and not overwritten by ``given``).
    omitted_fields = all_variant_fields - given
    if mode == "create" and flavor == "create":
        # Every omitted optional field must be omitted in at least one admitted variant
        # so we copy its demonstrated absent-field pin rather than guessing.
        for q in sorted(omitted_fields):
            omitting = [
                (idx, item)
                for idx, item in enumerate(parsed_variants)
                if q not in item[0]
            ]
            if not omitting:
                return None

    # Determine keys that appear in base_doc's main match that are NOT param-owned.
    # For each key in any variant's match that is not a param key:
    # - If it's an empty__<col> pin for a column now bound by ``given``, drop it.
    # - If it's an empty__<col> pin for an omitted field ``q``, take it from a variant omitting ``q``.
    # - Otherwise keep all invariant pins from base_doc.
    bound_base_cols = set(bound_columns)
    for k in list(bound_columns):
        for prefix in ("num__", "date__", "dt__", "min__", "fk__"):
            if k.startswith(prefix):
                bound_base_cols.add(k[len(prefix) :])

    base_e0_match = base_doc["effects"][0].get("match") or {}
    for mk, mv in base_e0_match.items():
        if mk in all_param_match_keys or mk in merged_match:
            continue
        if mk.startswith("empty__") and mk[len("empty__") :] in bound_base_cols:
            continue
        merged_match[mk] = copy.deepcopy(mv)

    # Also carry absent-field empty__ pins from variants that omit fields in ``omitted_fields``.
    if mode == "create":
        for q in sorted(omitted_fields):
            # Find columns that ``q`` writes in variants that include ``q``.
            q_cols: set[str] = set()
            for idx, (v_fields, _, _, _, _) in enumerate(parsed_variants):
                if q in v_fields:
                    q_cols.update(variant_param_keys[idx].get(q, set()))
            if not q_cols:
                continue
            for vf, doc, _, proj_map, _ in parsed_variants:
                if q in vf:
                    continue
                m = doc["effects"][0].get("match") or {}
                for qcol in sorted(q_cols):
                    ekey = f"empty__{qcol}"
                    if (
                        ekey in m
                        and qcol not in bound_base_cols
                        and ekey not in merged_match
                    ):
                        merged_match[ekey] = copy.deepcopy(m[ekey])
                        if ekey in proj_map:
                            if ekey not in merged_projs:
                                merged_proj_order.append(ekey)
                            merged_projs[ekey] = proj_map[ekey]
                break

    # Carry non-param sql_query_params (e.g. actor params) from base_doc if referenced in SQL.
    base_rec_params = (
        base_doc["verifier"]["surfaces"]["record"].get("sql_query_params") or {}
    )
    # Build final SELECT query for ``record`` surface.
    used_aliases = set(merged_match)
    final_projs: list[str] = []
    for alias in merged_proj_order:
        # Drop empty__ projections we removed because the field is now given.
        if (
            alias.startswith("empty__")
            and alias[len("empty__") :] in bound_base_cols
            and alias not in used_aliases
        ):
            continue
        if alias in merged_projs:
            final_projs.append(merged_projs[alias])
    final_record_sql = f"SELECT {', '.join(final_projs)} {base_from_tail}"

    for pk, pv in base_rec_params.items():
        if pk not in merged_sql_params and _expr_refs_param(final_record_sql, pk):
            merged_sql_params[pk] = copy.deepcopy(pv)

    # Assemble composed document from a deep copy of base_doc.
    composed = copy.deepcopy(base_doc)
    composed["verifier"]["surfaces"]["record"]["sql_query"] = final_record_sql
    if merged_sql_params:
        composed["verifier"]["surfaces"]["record"]["sql_query_params"] = (
            merged_sql_params
        )
    else:
        composed["verifier"]["surfaces"]["record"].pop("sql_query_params", None)

    for sname, sdef in extra_surfaces.items():
        composed["verifier"]["surfaces"][sname] = sdef

    composed["effects"][0]["match"] = merged_match

    # Keep non-param side effects from base_doc and replace param-specific child effects.
    base_non_param_effects = [
        eff
        for eff in base_doc["effects"][1:]
        if not any(
            _match_refs_param(eff.get("match") or {}, p) for p in all_variant_fields
        )
    ]
    composed["effects"] = [
        composed["effects"][0],
        *base_non_param_effects,
        *extra_effects,
    ]

    # Compose frame.target_fields / target_fields_by_surface safely for updates.
    frame = composed.get("frame")
    if isinstance(frame, dict) and mode == "update" and flavor == "update":
        # Shared bookkeeping target fields = intersection across all source variants
        # excluding any param-specific keys.
        all_tfs = [
            set((d.get("frame") or {}).get("target_fields") or [])
            for _, d, _, _, _ in parsed_variants
        ]
        shared_tf = (
            set.intersection(*all_tfs) - all_param_match_keys if all_tfs else set()
        )
        composed_tf = set(shared_tf)
        for p in given:
            composed_tf.update(param_match_keys.get(p, set()))
            # Also include any target_field that appears in EVERY variant containing ``p``
            # and in NO variant omitting ``p`` (e.g. FK column backing a joined lookup).
            with_p = [
                set((d.get("frame") or {}).get("target_fields") or [])
                for vf, d, _, _, _ in parsed_variants
                if p in vf
            ]
            without_p = [
                set((d.get("frame") or {}).get("target_fields") or [])
                for vf, d, _, _, _ in parsed_variants
                if p not in vf
            ]
            if with_p:
                only_with_p = set.intersection(*with_p) - (
                    set.union(*without_p) if without_p else set()
                )
                composed_tf.update(only_with_p)
        frame["target_fields"] = sorted(composed_tf)
        tfs_by_surf = frame.get("target_fields_by_surface")
        if isinstance(tfs_by_surf, dict):
            tfs_by_surf["record"] = sorted(composed_tf)
            for sname, cols in extra_target_by_surface.items():
                tfs_by_surf[sname] = sorted(cols)

    # Preserve database.judged_by for any extra surfaces.
    if isinstance(frame, dict) and isinstance(frame.get("database"), dict):
        judged_by = frame["database"].get("judged_by")
        if isinstance(judged_by, dict):
            for tbl, sname in extra_judged_by.items():
                judged_by.setdefault(tbl, sname)

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

    # If ``entry["fields"]`` was absent, enforce required fields as the intersection
    # of all listed variants so required/identity fields are never dropped.
    if not (isinstance(spec, Mapping) and spec):
        common_fields = set.intersection(*(set(vf) for vf, _ in write_sources))
        if not common_fields <= given:
            return None

    given_fs = frozenset(given)
    write_doc = _compose_single_flavor(write_sources, given_fs, mode=mode, flavor=mode)
    if write_doc is None:
        return None
    found_doc = _compose_single_flavor(
        found_sources, given_fs, mode=mode, flavor="found"
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
    """Gate a composed ``(write_doc, found_doc)`` pair through planted-fault admission.

    Verifies:
    1. Read-only SQL guard on every SQL surface in both documents.
    2. Schema parse via ``Contract.from_dict`` on both documents.
    3. Every field in ``given`` is bound in ``write_contract.params`` and
       ``found_contract.params``.
    4. Positive control: correct post-write state confirms on ``write`` and ``found``.
    5. Planted wrong-value fault on every field in ``given`` refutes.
    6. Planted null-vs-value fault on every non-null field in ``given`` refutes.
    7. Planted missing-write / no-op fault stops.
    8. Planted duplicate-row fault (``create`` mode) refutes.
    9. Planted sibling-edit / collateral fault refutes via ``judge_frame``.
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

    # 3. 100% bound-field coverage check.
    write_params = set(write_contract.params)
    found_params = set(found_contract.params)
    missing_write = sorted(given - write_params)
    missing_found = sorted(given - found_params)
    if missing_write:
        reasons.append(f"write contract dropped field(s): {', '.join(missing_write)}")
    if missing_found:
        reasons.append(f"found contract dropped field(s): {', '.join(missing_found)}")
    if reasons:
        return ComposedAdmissionResult(False, tuple(reasons))

    # Build complete sample params for every bound parameter in the composed contract.
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

    # Build synthetic baseline & correct post-write records for the main record surface.
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
        # Perturb target's changed columns in baseline so update actually changes them.
        target_fields = set((write_doc.get("frame") or {}).get("target_fields") or [])
        for col in target_fields:
            if col in target_before and col != identity_field:
                target_before[col] = f"__before_{col}__"
        before_rows = [copy.deepcopy(sibling_before), target_before]
        after_rows = [copy.deepcopy(sibling_before), copy.deepcopy(target_after)]

    before_state = EffectState(substrate="sql", reachable=True, records=before_rows)

    # 4. Positive control: correct write confirms on both write and found contracts.
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

    # Map each param in ``given`` to its matched column(s) in main_write.
    raw_match = write_doc["effects"][0].get("match") or {}
    param_to_cols: dict[str, list[str]] = {}
    for p in sorted(given):
        cols = [str(k) for k, v in raw_match.items() if _match_refs_param(v, p)]
        if cols:
            param_to_cols[p] = cols

    # 5. Planted wrong-value fault on EVERY field in ``given`` matched on main record.
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

    # 9. Planted sibling-edit / collateral fault judged by ``judge_frame``.
    frame_spec = write_contract.config.frame or FrameSpec(identity_field=identity_field)
    if not frame_spec.identity_field:
        frame_spec = frame_spec.model_copy(update={"identity_field": identity_field})
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
    if frame_verdict.status == "confirmed":
        reasons.append("planted sibling-edit collateral fault passed judge_frame")
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
    """Compose a novel field-set contract and gate it through planted-fault admission.

    Returns ``(write_doc, found_doc, admission_result)`` when composition
    succeeds and passes admission, or ``None`` if composition is not possible.
    Raises ``ValueError`` if a composed contract fails planted-fault admission.
    """

    docs = compose_field_set_docs(manifest_path, entry, given, lists, cleared)
    if docs is None:
        return None
    write_doc, found_doc = docs
    mode = str(entry.get("mode") or "")
    admission = admit_composed_contract(
        write_doc,
        found_doc,
        mode=mode,
        given=given,
        params=params,
    )
    if not admission.admitted:
        raise ValueError(
            "composed contract failed planted-fault admission: "
            + "; ".join(admission.reasons)
        )
    return write_doc, found_doc, admission
