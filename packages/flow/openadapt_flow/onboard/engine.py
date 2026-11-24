"""Automated pack synthesis and planted-fault qualification engine.

Domain-agnostic: operates solely on declarative :class:`OnboardSpec` inputs,
synthesizes standard ``openadapt.verify/v1`` contracts and ``manifest.yaml``
compatible with ``verify_contract.choose()`` (including compositional field-set
synthesis and null-safe SQL equality binding), runs a deterministic
planted-fault mutation suite against an isolated sandbox copy, verifies clean
writes with signed ``record/run/v1`` receipts, and refuses certification unless
0% of planted faults pass and 0% of clean writes false-stop.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.onboard.types import (
    CleanWriteCase,
    CleanWriteOutcome,
    KindOnboardingSpec,
    OnboardSpec,
    PlantedFaultOutcome,
    QualificationResult,
    _validate_ident,
)
from openadapt_flow.runtime.effects.sql import assert_read_only_sql
from openadapt_flow.verifier import (
    Actor,
    MemoryLedger,
    Outcome,
    begin_write,
    verify_write,
)


def _slug(fields: Sequence[str]) -> str:
    return "__".join(sorted(fields)) or "empty"


def _sqlite_table_columns(db: Path, table: str) -> list[str]:
    _validate_ident(table, "sqlite table")
    con = sqlite3.connect(db)
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        cols: list[str] = []
        for r in rows:
            col = str(r[1])
            _validate_ident(col, f"{table} column")
            cols.append(col)
        return cols
    finally:
        con.close()


def _all_kind_columns(
    kind_spec: KindOnboardingSpec,
    read_cfg: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    cols: list[str] = [kind_spec.identity_field]
    if read_cfg and isinstance(read_cfg.get("sqlite_database"), str):
        db_path = Path(str(read_cfg["sqlite_database"]))
        if db_path.is_file():
            for c in _sqlite_table_columns(db_path, kind_spec.table):
                if c not in cols:
                    cols.append(c)
    ordered_params = (
        *kind_spec.required_fields,
        *kind_spec.optional_fields,
        *kind_spec.identity_params,
        *kind_spec.change_fields,
        *kind_spec.filter_params,
    )
    for p in ordered_params:
        c = kind_spec.column_for(p)
        if c not in cols:
            cols.append(c)
    for c in (*kind_spec.invariant_pins.keys(), *kind_spec.bookkeeping_fields):
        if c not in cols:
            cols.append(c)
    return cols


def _build_contract_doc(
    spec: OnboardSpec,
    kind_spec: KindOnboardingSpec,
    field_set: Sequence[str],
    *,
    flavor: str,
    read_override: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build one ``openadapt.verify/v1`` document for ``(kind_spec, field_set, flavor)``."""

    read_cfg = dict(read_override if read_override is not None else spec.read)
    fset = set(field_set)
    base_cols = _all_kind_columns(kind_spec, read_cfg)
    select_items = [f"t.{col} AS {col}" for col in base_cols]
    match: dict[str, Any] = {}

    for p in sorted(fset):
        col = kind_spec.column_for(p)
        match[col] = {"param": p}

    for col, lit in sorted(kind_spec.invariant_pins.items()):
        if col not in match:
            match[col] = {"literal": lit}

    if kind_spec.mode == "create":
        for opt in sorted(kind_spec.optional_fields):
            if opt in fset:
                continue
            col = kind_spec.column_for(opt)
            alias = f"empty__{col}"
            select_items.append(
                f"CASE WHEN t.{col} IS NULL OR t.{col} = '' THEN '1' ELSE '0' END AS {alias}"
            )
            match[alias] = {"literal": "1"}

    where_clauses: list[str] = []
    sql_query_params: dict[str, Any] = {}
    for fp in kind_spec.filter_params:
        if fp in fset:
            col = kind_spec.column_for(fp)
            where_clauses.append(f"t.{col} = :{fp}")
            sql_query_params[fp] = {"param": fp}

    sql = f"SELECT {', '.join(select_items)} FROM {kind_spec.table} t"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    assert_read_only_sql(sql, binds=bool(sql_query_params))

    surface: dict[str, Any] = {
        "kind": "sql",
        **read_cfg,
        "sql_query": sql,
    }
    if sql_query_params:
        surface["sql_query_params"] = sql_query_params

    target_fields: list[str] = []
    if kind_spec.mode == "update" and flavor == "update":
        ident_set = set(kind_spec.identity_params)
        tfs = set(kind_spec.bookkeeping_fields)
        for p in sorted(fset - ident_set):
            tfs.add(kind_spec.column_for(p))
        target_fields = sorted(tfs)

    count_new_only = kind_spec.mode == "create" and flavor == "create"
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "surfaces",
            "surfaces": {"record": surface},
        },
        "effects": [
            {
                "kind": "record_written",
                "probe": "surface=record",
                "match": match,
                "expected_count": 1,
                "count_new_only": count_new_only,
            }
        ],
        "frame": {
            "identity_field": kind_spec.identity_field,
            "target_fields": target_fields,
            "target_fields_by_surface": (
                {"record": target_fields} if target_fields else {}
            ),
        },
    }


def _pack_digest(out_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(out_dir.rglob("*.yaml")):
        rel = p.relative_to(out_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def synthesize_pack(
    spec: OnboardSpec,
    out_dir: str | Path,
    *,
    read_override: Optional[Mapping[str, Any]] = None,
) -> tuple[Path, str]:
    """Synthesize declarative contracts and ``manifest.yaml`` into ``out_dir``."""

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    kinds_manifest: dict[str, Any] = {}

    for ks in spec.kinds:
        variants: list[dict[str, Any]] = []
        for raw_fs in ks.demonstrated_field_sets:
            fset = sorted(set(raw_fs))
            slug = _slug(fset)
            write_rel = f"contracts/{ks.kind}/{slug}.{ks.mode}.yaml"
            found_rel = f"contracts/{ks.kind}/{slug}.found.yaml"
            w_doc = _build_contract_doc(
                spec, ks, fset, flavor=ks.mode, read_override=read_override
            )
            f_doc = _build_contract_doc(
                spec, ks, fset, flavor="found", read_override=read_override
            )
            for rel, doc in ((write_rel, w_doc), (found_rel, f_doc)):
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            variants.append(
                {
                    "fields": fset,
                    ks.mode: write_rel,
                    "found": found_rel,
                }
            )

        fields_spec: dict[str, Any]
        if ks.mode == "create":
            fields_spec = {
                "required": sorted(ks.required_fields),
                "optional": sorted(ks.optional_fields),
                "lists": [],
            }
        else:
            fields_spec = {
                "identity": sorted(ks.identity_params),
                "changes": sorted(ks.change_fields),
                "lists": [],
            }
        kinds_manifest[ks.kind] = {
            "mode": ks.mode,
            "fields": fields_spec,
            "variants": variants,
        }

    manifest_doc = {
        "schema_version": gen.MANIFEST_SCHEMA,
        "pack_name": spec.name,
        "zero_touch": {"admitted": sorted(ks.kind for ks in spec.kinds)},
        "kinds": kinds_manifest,
    }
    manifest_path = root / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return manifest_path, _pack_digest(root)


def _perturb(value: Any, field: str) -> Any:
    if value is None:
        return f"__unexpected_{field}__"
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 999
    if isinstance(value, float):
        return value + 999.5
    return f"__fault_{field}_{value}__"


def _sqlite_insert(db: Path, table: str, row: Mapping[str, Any]) -> None:
    _validate_ident(table, "sqlite table")
    cols = [_validate_ident(str(c), f"{table} insert column") for c in row.keys()]
    placeholders = ", ".join("?" for _ in cols)
    con = sqlite3.connect(db)
    try:
        con.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )
        con.commit()
    finally:
        con.close()


def _sqlite_update(
    db: Path,
    table: str,
    updates: Mapping[str, Any],
    where: Mapping[str, Any],
) -> None:
    _validate_ident(table, "sqlite table")
    upd_cols = [_validate_ident(str(c), f"{table} update column") for c in updates]
    where_cols = [_validate_ident(str(c), f"{table} where column") for c in where]
    set_clause = ", ".join(f"{c} = ?" for c in upd_cols)
    where_clause = " AND ".join(f"{c} = ?" for c in where_cols)
    con = sqlite3.connect(db)
    try:
        con.execute(
            f"UPDATE {table} SET {set_clause} WHERE {where_clause}",
            [*updates.values(), *where.values()],
        )
        con.commit()
    finally:
        con.close()


def _sqlite_nullable_cols(db: Path, table: str) -> set[str]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        return {str(r[1]) for r in rows if int(r[3]) == 0}
    finally:
        con.close()


def _sqlite_table_rows(db: Path, table: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(f"SELECT * FROM {table}")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _run_planted_faults_sqlite(
    spec: OnboardSpec,
    source_db: Path,
    clean_cases: Sequence[CleanWriteCase],
    *,
    actor: Actor,
    workdir: Path,
) -> list[PlantedFaultOutcome]:
    """Run deterministic planted-fault mutations against isolated SQLite copies."""

    outcomes: list[PlantedFaultOutcome] = []
    case_by_kind: dict[str, CleanWriteCase] = {}
    for c in clean_cases:
        if not c.expect_found and c.kind not in case_by_kind:
            case_by_kind[c.kind] = c

    for ks in spec.kinds:
        rep_values: dict[str, Any] = {}
        if ks.kind in case_by_kind:
            rep_values = dict(case_by_kind[ks.kind].values)
        elif ks.sample_values:
            rep_values = dict(ks.sample_values)
        else:
            # Derive synthetic sample values from the first demonstrated field set.
            for f in ks.demonstrated_field_sets[0]:
                rep_values[f] = f"sample_{f}"

        def _exec_fault(
            fault_class: str,
            field_name: str,
            mutate: Any,
        ) -> PlantedFaultOutcome:
            with tempfile.TemporaryDirectory(dir=workdir) as td:
                td_path = Path(td)
                sb_db = td_path / "sandbox.sqlite"
                shutil.copy2(source_db, sb_db)
                sb_manifest, _ = synthesize_pack(
                    spec,
                    td_path / "pack",
                    read_override={**dict(spec.read), "sqlite_database": str(sb_db)},
                )
                contract, params = gen.choose(sb_manifest, ks.kind, rep_values)
                pending = begin_write(
                    contract,
                    params,
                    actor=actor,
                    ledger=MemoryLedger(),
                    workdir=td_path / "runs",
                )
                mutate(sb_db)
                res = verify_write(pending)
                caught = res.outcome is not Outcome.CONFIRMED
                return PlantedFaultOutcome(
                    kind=ks.kind,
                    fault_class=fault_class,
                    field=field_name,
                    outcome=res.outcome.value,
                    caught=caught,
                    reasons=tuple(res.reasons),
                )

        # Build the clean row payload for this kind.
        clean_row: dict[str, Any] = {}
        for p, v in rep_values.items():
            if v is not None:
                clean_row[ks.column_for(p)] = v
        for col, lit in ks.invariant_pins.items():
            clean_row.setdefault(col, lit)

        existing_rows = _sqlite_table_rows(source_db, ks.table)
        nullable_cols = _sqlite_nullable_cols(source_db, ks.table)

        if ks.mode == "create":
            testable_fields = [p for p, v in rep_values.items() if v is not None]
            # 1. wrong_value per field
            for p in testable_fields:
                col = ks.column_for(p)
                bad_row = {**clean_row, col: _perturb(rep_values[p], p)}
                outcomes.append(
                    _exec_fault(
                        "wrong_value",
                        p,
                        lambda db, r=bad_row: _sqlite_insert(db, ks.table, r),
                    )
                )
            # 2. null_vs_value per field (NULL on nullable columns, "" on NOT NULL columns)
            for p, v in rep_values.items():
                col = ks.column_for(p)
                if v is None:
                    bad_row = {**clean_row, col: f"__unexpected_{p}__"}
                else:
                    bad_row = {**clean_row, col: None if col in nullable_cols else ""}
                outcomes.append(
                    _exec_fault(
                        "null_vs_value",
                        p,
                        lambda db, r=bad_row: _sqlite_insert(db, ks.table, r),
                    )
                )
            # 3. missing_write (no-op)
            outcomes.append(_exec_fault("missing_write", "*", lambda _db: None))

            # 4. duplicate_row
            def _plant_dup(
                db: Path,
                tbl: str = ks.table,
                row: dict[str, Any] = clean_row,
            ) -> None:
                _sqlite_insert(db, tbl, row)
                _sqlite_insert(db, tbl, row)

            outcomes.append(_exec_fault("duplicate_row", "*", _plant_dup))
            # 5. sibling_edit (valid insert + tamper existing sibling row)
            if existing_rows:
                sib = existing_rows[0]
                sib_id = sib[ks.identity_field]
                tamper_col = next(
                    (
                        c
                        for c in sib
                        if c != ks.identity_field and c not in ks.bookkeeping_fields
                    ),
                    None,
                )
                if tamper_col is not None:

                    def _plant_create_sib(
                        db: Path,
                        tbl: str = ks.table,
                        row: dict[str, Any] = clean_row,
                        tc: str = tamper_col,
                        id_col: str = ks.identity_field,
                        sid: Any = sib_id,
                    ) -> None:
                        _sqlite_insert(db, tbl, row)
                        _sqlite_update(
                            db, tbl, {tc: "__tampered_sibling__"}, {id_col: sid}
                        )

                    outcomes.append(
                        _exec_fault("sibling_edit", tamper_col, _plant_create_sib)
                    )
        else:
            # Update mode
            ident_where = {
                ks.column_for(p): rep_values[p]
                for p in ks.identity_params
                if p in rep_values
            }
            change_updates = {
                ks.column_for(p): rep_values[p]
                for p in rep_values
                if p not in ks.identity_params and rep_values[p] is not None
            }
            # 1. wrong_value per changed field
            for p in sorted(
                k
                for k in rep_values
                if k not in ks.identity_params and rep_values[k] is not None
            ):
                col = ks.column_for(p)
                bad_upd = {**change_updates, col: _perturb(rep_values[p], p)}
                outcomes.append(
                    _exec_fault(
                        "wrong_value",
                        p,
                        lambda db, u=bad_upd, w=ident_where: _sqlite_update(
                            db, ks.table, u, w
                        ),
                    )
                )
            # 2. null_vs_value per changed field (NULL on nullable columns, "" on NOT NULL)
            for p in sorted(k for k in rep_values if k not in ks.identity_params):
                col = ks.column_for(p)
                if rep_values[p] is None:
                    bad_upd = {**change_updates, col: f"__unexpected_{p}__"}
                else:
                    bad_upd = {
                        **change_updates,
                        col: None if col in nullable_cols else "",
                    }
                outcomes.append(
                    _exec_fault(
                        "null_vs_value",
                        p,
                        lambda db, u=bad_upd, w=ident_where: _sqlite_update(
                            db, ks.table, u, w
                        ),
                    )
                )
            # 3. missing_write (no-op)
            outcomes.append(_exec_fault("missing_write", "*", lambda _db: None))
            # 4. wrong_target (apply update to a different row instead of target)
            other_rows = [
                r
                for r in existing_rows
                if all(str(r.get(wc)) != str(wv) for wc, wv in ident_where.items())
            ]
            if other_rows:
                other_id = other_rows[0][ks.identity_field]
                outcomes.append(
                    _exec_fault(
                        "wrong_target",
                        ks.identity_field,
                        lambda db, u=change_updates, oid=other_id: _sqlite_update(
                            db, ks.table, u, {ks.identity_field: oid}
                        ),
                    )
                )
                # 5. sibling_edit (valid update on target + tamper sibling row)
                tamper_col = next(
                    (
                        c
                        for c in other_rows[0]
                        if c != ks.identity_field and c not in ks.bookkeeping_fields
                    ),
                    None,
                )
                if tamper_col is not None:

                    def _plant_update_sib(
                        db: Path,
                        tbl: str = ks.table,
                        u: dict[str, Any] = change_updates,
                        w: dict[str, Any] = ident_where,
                        tc: str = tamper_col,
                        id_col: str = ks.identity_field,
                        oid: Any = other_id,
                    ) -> None:
                        _sqlite_update(db, tbl, u, w)
                        _sqlite_update(
                            db, tbl, {tc: "__tampered_sibling__"}, {id_col: oid}
                        )

                    outcomes.append(
                        _exec_fault("sibling_edit", tamper_col, _plant_update_sib)
                    )

    return outcomes


def _issue_signed_run_receipt(
    run_dir: Path,
    *,
    pack_name: str,
    kind: str,
    case_name: str,
    contract: Any,
    write_result: Any,
) -> Optional[str]:
    """Emit a signed ``record/run/v1`` DSSE receipt (``receipt.json``) in ``run_dir``."""

    from datetime import datetime, timezone

    from openadapt_types.receipt import OUTCOME_FROM_RUN

    from openadapt_flow.offline_seal import seal_home, signer_for
    from openadapt_flow.receipt_record import write_receipt_record

    run_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    verified = write_result.outcome is Outcome.CONFIRMED
    verified_outcome = next(iter(OUTCOME_FROM_RUN))
    run_outcome = verified_outcome if verified else "STOPPED"
    line_verdict = write_result.outcome.value.lower() if verified else "refuted"
    contract_hashes = [eff.contract_hash() for eff in (contract.config.effects or ())]
    evidence = [
        {
            "effect_contract_hash": ch,
            "substrate": "sql",
            "initial_verdict": line_verdict,
            "final_verdict": line_verdict,
            "observed_effect": "present" if verified else "absent",
            "verification_tier": 1,
        }
        for ch in contract_hashes
    ]
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "workflow_name": f"{pack_name}:{kind}"[:48],
        "run_id": write_result.write_id or case_name,
        "started_at": now,
        "finished_at": now,
        "execution_outcome": run_outcome,
        "outcome": run_outcome,
        "model_calls": 0,
        "verifier_outcome": write_result.outcome.value,
        "effect_receipt_sha256": write_result.receipt_sha256,
        "results": [
            {
                "step_id": case_name,
                "intent": f"onboard {kind}",
                "ok": verified,
                "actuation": "api",
                "effect_verified": verified,
                "effect_contract_hashes": contract_hashes,
                "effect_evidence": evidence,
            }
        ],
        "outcome_envelope": {
            "required_contracts": {"effect": 1, "authorization": 1},
            "passed_contracts": {"effect": 1 if verified else 0, "authorization": 1},
        },
    }
    if write_result.receipt:
        (run_dir / "effect-receipt.json").write_text(
            json.dumps(write_result.receipt, indent=2, default=str), encoding="utf-8"
        )
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    write_receipt_record(run_dir, signer=signer_for(seal_home()))
    receipt_path = run_dir / "receipt.json"
    return str(receipt_path) if receipt_path.is_file() else None


def qualify_pack(
    spec: OnboardSpec,
    manifest_path: str | Path,
    clean_cases: Sequence[CleanWriteCase] = (),
    *,
    pack_digest: Optional[str] = None,
    workdir: Optional[str | Path] = None,
) -> QualificationResult:
    """Run planted-fault mutation suite + clean-write verification and emit ``QUALIFICATION.json``."""

    mpath = Path(manifest_path)
    pack_dir = mpath.parent
    digest = pack_digest or _pack_digest(pack_dir)
    manifest_doc = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
    actor = Actor(kind="agent", id=spec.actor_id)
    run_workdir = Path(workdir) if workdir is not None else pack_dir / "runs"
    run_workdir.mkdir(parents=True, exist_ok=True)

    refusal_reasons: list[str] = []

    # Step 1: Deterministic planted-fault mutation suite against isolated sandbox copies.
    fault_outcomes: list[PlantedFaultOutcome] = []
    sqlite_db = spec.read.get("sqlite_database")
    if isinstance(sqlite_db, str) and Path(sqlite_db).is_file():
        fault_outcomes = _run_planted_faults_sqlite(
            spec,
            Path(sqlite_db),
            clean_cases,
            actor=actor,
            workdir=run_workdir,
        )
    else:
        refusal_reasons.append(
            "planted-fault sandbox requires a readable sqlite_database in spec.read"
        )

    faults_total = len(fault_outcomes)
    faults_caught = sum(1 for f in fault_outcomes if f.caught)
    if faults_total == 0:
        refusal_reasons.append("no planted faults were executed")
    elif faults_caught < faults_total:
        escaped = [
            f"{f.kind}:{f.fault_class}:{f.field}"
            for f in fault_outcomes
            if not f.caught
        ]
        refusal_reasons.append(
            f"{faults_total - faults_caught}/{faults_total} planted fault(s) passed: "
            + ", ".join(escaped)
        )

    # Require every mandatory fault class to be planted for every kind (fail closed
    # on sparse/empty databases where sibling_edit or wrong_target could not run).
    classes_by_kind: dict[str, set[str]] = {}
    for f in fault_outcomes:
        classes_by_kind.setdefault(f.kind, set()).add(f.fault_class)
    for ks in spec.kinds:
        required_classes = (
            {
                "wrong_value",
                "null_vs_value",
                "missing_write",
                "duplicate_row",
                "sibling_edit",
            }
            if ks.mode == "create"
            else {
                "wrong_value",
                "null_vs_value",
                "missing_write",
                "wrong_target",
                "sibling_edit",
            }
        )
        missing_classes = sorted(required_classes - classes_by_kind.get(ks.kind, set()))
        if missing_classes:
            refusal_reasons.append(
                f"kind {ks.kind!r} missing required planted fault class(es): "
                + ", ".join(missing_classes)
            )

    # Step 2: Clean-write verification (including novel composed field sets & explicit nulls).
    kind_specs = {ks.kind: ks for ks in spec.kinds}
    clean_outcomes: list[CleanWriteOutcome] = []
    receipt_paths: list[str] = []
    for idx, case in enumerate(clean_cases):
        kind_entry = (manifest_doc.get("kinds") or {}).get(case.kind) or {}
        listed_sets = {
            frozenset(v.get("fields") or []) for v in (kind_entry.get("variants") or [])
        }
        given_set = frozenset(
            k
            for k, v in case.values.items()
            if v is not None and not isinstance(v, list)
        )
        composed = given_set not in listed_sets
        case_name = case.name or f"{case.kind}#{idx + 1}"
        ks_obj = kind_specs.get(case.kind)
        explicit_nulls = (
            {
                k: None
                for k, v in case.values.items()
                if v is None and k in ks_obj.filter_params
            }
            if ks_obj is not None
            else {}
        )
        try:
            contract, params = gen.choose(
                mpath, case.kind, case.values, extra_params=explicit_nulls or None
            )
            pending = begin_write(
                contract,
                params,
                actor=actor,
                ledger=MemoryLedger(),
                workdir=run_workdir,
            )
            case.apply_write()
            res = verify_write(pending)
            confirmed = res.outcome is Outcome.CONFIRMED
            rpath = res.write_receipt_path
            if confirmed and not rpath:
                case_run_dir = run_workdir / f"clean-{idx + 1:03d}-{case.kind}"
                rpath = _issue_signed_run_receipt(
                    case_run_dir,
                    pack_name=spec.name,
                    kind=case.kind,
                    case_name=case_name,
                    contract=contract,
                    write_result=res,
                )
            if rpath:
                receipt_paths.append(rpath)
            clean_outcomes.append(
                CleanWriteOutcome(
                    kind=case.kind,
                    name=case_name,
                    fields=tuple(sorted(given_set)),
                    composed=composed,
                    outcome=res.outcome.value,
                    confirmed=confirmed,
                    receipt_path=rpath,
                    reasons=tuple(res.reasons),
                )
            )
            if not confirmed:
                refusal_reasons.append(
                    f"clean write {case_name!r} stopped with {res.outcome.value}: "
                    + "; ".join(res.reasons[:2])
                )
        except Exception as exc:  # noqa: BLE001 - fail closed on any runtime error
            from openadapt_flow.verdict import Errored

            not_checked = Errored(type(exc).__name__, str(exc))
            clean_outcomes.append(
                CleanWriteOutcome(
                    kind=case.kind,
                    name=case_name,
                    fields=tuple(sorted(given_set)),
                    composed=composed,
                    outcome="ERROR",
                    confirmed=False,
                    receipt_path=None,
                    reasons=(not_checked.reason,),
                )
            )
            refusal_reasons.append(
                f"clean write {case_name!r} raised: {not_checked.reason}"
            )

    clean_total = len(clean_outcomes)
    clean_verified = sum(1 for c in clean_outcomes if c.confirmed)
    if clean_total == 0:
        refusal_reasons.append(
            "no clean writes were verified (at least one verified clean write "
            "with a signed record/run/v1 receipt is required for certification)"
        )
    elif len(receipt_paths) < clean_verified:
        refusal_reasons.append(
            f"signed receipts issued ({len(receipt_paths)}) fewer than "
            f"verified clean writes ({clean_verified})"
        )
    false_stop_rate = (
        round((clean_total - clean_verified) / clean_total, 4)
        if clean_total > 0
        else 0.0
    )

    certified = (
        faults_total > 0
        and faults_caught == faults_total
        and clean_total > 0
        and clean_verified == clean_total
        and len(receipt_paths) == clean_verified
        and not refusal_reasons
    )

    result = QualificationResult(
        pack_name=spec.name,
        certified=certified,
        refusal_reasons=tuple(refusal_reasons),
        pack_dir=str(pack_dir),
        manifest_path=str(mpath),
        pack_digest=digest,
        planted_faults_caught=faults_caught,
        planted_faults_total=faults_total,
        clean_writes_verified=clean_verified,
        clean_writes_total=clean_total,
        false_stop_rate=false_stop_rate,
        receipts_issued=len(receipt_paths),
        receipt_paths=tuple(receipt_paths),
        fault_outcomes=tuple(fault_outcomes),
        clean_outcomes=tuple(clean_outcomes),
    )
    (pack_dir / "QUALIFICATION.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )
    return result


def onboard_pack(
    spec: OnboardSpec,
    out_dir: str | Path,
    clean_cases: Sequence[CleanWriteCase] = (),
    *,
    workdir: Optional[str | Path] = None,
) -> QualificationResult:
    """Single-call onboarding loop: synthesize pack -> planted-fault qualify -> certify."""

    manifest_path, digest = synthesize_pack(spec, out_dir)
    return qualify_pack(
        spec,
        manifest_path,
        clean_cases,
        pack_digest=digest,
        workdir=workdir,
    )
