"""The task generator: profiles, families and splits (no browser, no database).

The end-to-end check (demonstrations, contract generation and validation
through a real browser on PostgreSQL) is ``test_taskgen_e2e``; it runs when
``TASKGEN_PG`` names an admin connection.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from openadapt_flow.taskgen import families as F
from openadapt_flow.taskgen import kinds as K
from openadapt_flow.taskgen.profile import PROFILE_DIR, load_profile
from openadapt_flow.taskgen.splits import assign, matches
from openadapt_flow.taskgen.workbench import ddl

PROFILES = sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))
ENGINE = Path(K.__file__).resolve().parent


def test_engine_names_no_app() -> None:
    """App knowledge lives in profiles: no engine module names an app or
    one of its record types outside comments and docstrings."""

    words = set()
    for name in PROFILES:
        prof = load_profile(name)
        words |= {prof.app, prof.title.lower()}
        for e in prof.entities.values():
            words |= {e.name, e.table}
            words |= {f.name for f in e.fields if f.name not in ("status", "type")}
    # The default person fields, and the field formats the engine knows.
    words -= {"first_name", "last_name", "dob", "id", "phone", "email"}
    pattern = re.compile(r"[\"']([a-z_]+)[\"']")
    for py in ENGINE.glob("*.py"):
        code = "\n".join(
            line
            for line in py.read_text().splitlines()
            if not line.strip().startswith("#")
        )
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        found = {m for m in pattern.findall(code) if m in words}
        assert not found, f"{py.name} names app fields {sorted(found)}"


@pytest.mark.parametrize("name", PROFILES)
def test_profile_schema(name: str) -> None:
    prof = load_profile(name)
    stmts = ddl(prof)
    assert any("audit_log" in s for s in stmts)
    for e in prof.entities.values():
        assert any(f'"{e.table}"' in s for s in stmts)
    assert set(prof.entity(prof.person).changes) <= {
        f.name for f in prof.entity(prof.person).editable
    }


@pytest.mark.parametrize("name", PROFILES)
def test_every_cell_builds(name: str) -> None:
    """Every kind x family x form builds a task with a UI solution that ends
    in done, at least one planted variant, and an instruction that names
    real values."""

    prof = load_profile(name)
    for kind in K.KINDS:
        for family in F.APPLIES[kind]:
            for positive in F.positive_forms(family):
                for seed in (1, 2, 3):
                    try:
                        d = F.build(prof, kind, family, positive, seed)
                    except F.NotApplicable:
                        continue
                    assert d.solution[-1] == {"kind": "done", "claim": "success"}
                    assert d.planted, (kind, family)
                    assert "None" not in d.instruction
                    assert all(
                        a["kind"] in ("click", "fill", "select", "goto", "wait", "done")
                        for a in d.solution
                    )
                    assert not any(
                        a.get("path", "").startswith("/__") for a in d.solution
                    )
                    for p in d.planted:
                        assert p.setup or p.solution, p.name
                        if p.solution:
                            assert p.solution != d.solution


def test_build_is_deterministic() -> None:
    prof = load_profile(PROFILES[0])
    a = F.build(prof, "update", "hidden_state", False, 42)
    b = F.build(prof, "update", "hidden_state", False, 42)
    assert a.instruction == b.instruction and a.solution == b.solution
    assert a.world.setup_rows() == b.world.setup_rows()


def test_lookalike_differs_by_a_swap() -> None:
    assert K.lookalike("104729") == "107429"
    assert sorted(K.lookalike("104729")) == sorted("104729")


def _task(tid: str, app: str, kind: str, family: str, schema: int = 1) -> dict:
    return {
        "id": tid,
        "tags": [f"app:{app}", f"kind:{kind}", f"family:{family}", f"schema:v{schema}"],
    }


def test_splits_hold_out_by_app_mechanism_and_schema() -> None:
    tasks = [_task(f"a{i}", "a", "create", "control") for i in range(30)]
    tasks += [_task(f"m{i}", "a", "update", "interruption") for i in range(5)]
    tasks += [_task(f"b{i}", "b", "create", "control") for i in range(5)]
    tasks += [_task(f"s{i}", "a", "create", "control", schema=2) for i in range(5)]
    rules = {
        "public": {
            "app": "a",
            "schema": 1,
            "exclude_families": ["interruption"],
            "size": 10,
            "seed": 3,
        },
        "heldout": {
            "app": {"app": "b"},
            "mechanism": {"families": ["interruption"]},
            "schema": {"schema": 2},
        },
        "rest": "private_train",
    }
    s = assign(tasks, rules)
    assert len(s["public"]) == 10
    assert {t["id"][0] for t in s["heldout_app"]} == {"b"}
    assert {t["id"][0] for t in s["heldout_mechanism"]} == {"m"}
    assert {t["id"][0] for t in s["heldout_schema"]} == {"s"}
    assert len(s["private_train"]) == 20
    ids = [t["id"] for v in s.values() for t in v]
    assert len(ids) == len(set(ids)) == len(tasks)
    assert not matches(_task("x", "a", "create", "interruption"), rules["public"])


@pytest.mark.skipif(
    not os.environ.get("TASKGEN_PG"),
    reason="needs TASKGEN_PG (a PostgreSQL admin conninfo)",
)
def test_taskgen_e2e(tmp_path: Path) -> None:
    """Demonstrate, generate contracts, write a small pack and validate it
    through a real browser: every task valid."""

    import psycopg2

    from openadapt_flow.taskgen.contracts import demonstrate_and_propose
    from openadapt_flow.taskgen.generate import write_pack
    from openadapt_flow.taskgen.validate import validate
    from openadapt_flow.taskgen.workbench import serve

    pg = os.environ["TASKGEN_PG"]
    prof = load_profile(PROFILES[0])
    db = "wb_taskgen_e2e"
    conn = psycopg2.connect(pg + " dbname=postgres")
    conn.autocommit = True
    conn.cursor().execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    conn.cursor().execute(f"CREATE DATABASE {db}")
    conn.close()
    host = dict(kv.split("=") for kv in pg.split())
    url, wb, stop = serve(prof, f"{pg} dbname={db}", reader="wb_reader")
    try:
        manifest = demonstrate_and_propose(
            prof,
            url,
            wb,
            {
                "host": host["host"],
                "port": host["port"],
                "reader": "wb_reader",
                "dbname": db,
            },
            tmp_path / "work",
        )
    finally:
        stop()
    pack = tmp_path / "pack"
    write_pack(
        prof,
        manifest,
        pack,
        per_cell=1,
        seed=500,
        kinds=("create", "update"),
        families=("control", "misleading_banner"),
    )
    summary = validate(pack, PROFILES[0], pg)
    assert summary["overall"]["rate"] == 1.0, json.dumps(summary["rejected"], indent=1)
