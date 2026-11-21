"""Lab proof for ``openadapt proxy`` on PostgreSQL 16 (zenbook WSL).

    remote-run.sh <wt> uv run --no-sync --with 'psycopg[binary]' --with mcp \\
        python benchmark/ext_tx/proxy_lab.py --out benchmark/ext_tx/results/proxy.json

A synthetic schema (users, orders with ON DELETE CASCADE, a table with no
primary key) in its own database. Each statement goes through the proxy's
tools: preview (dry run in a rolled-back transaction), approval by key
``y``, apply once, undo. After each undo the whole database is compared with
its state before the statement. An independent read-back
(``openadapt_flow.verifier.capture_database``/``diff_database``) checks the
dry run's per-table counts after each apply. One session runs over the real
MCP client SDK against the HTTP endpoint. Counts only in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DB = os.environ.get("LAB_PG_DB", "oa_ext_tx")
CONNINFO = f"dbname={DB} host=/var/run/postgresql"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def psql(sql: str, db: str = DB) -> str:
    return subprocess.run(
        ["psql", "-qAt", "-d", db, "-c", sql],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def reset() -> None:
    subprocess.run(["dropdb", "--if-exists", DB], check=True)
    subprocess.run(["createdb", DB], check=True)
    psql("""
    CREATE TABLE users (id serial PRIMARY KEY, name text NOT NULL, role text NOT NULL,
                        active boolean NOT NULL DEFAULT true, balance numeric(12,2) NOT NULL DEFAULT 0,
                        updated_at timestamptz NOT NULL DEFAULT now());
    CREATE TABLE orders (id serial PRIMARY KEY, user_id int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                         total numeric(12,2) NOT NULL, placed date NOT NULL DEFAULT '2026-11-01');
    CREATE TABLE events (user_id int, kind text);
    INSERT INTO users (name, role, balance)
      SELECT 'user ' || g, CASE WHEN g % 97 = 0 THEN 'admin' ELSE 'member' END, g * 1.5
      FROM generate_series(1, 600) g;
    INSERT INTO orders (user_id, total) SELECT 1 + (g % 600), (g % 50) + 0.99 FROM generate_series(1, 1500) g;
    INSERT INTO events SELECT g, 'seed' FROM generate_series(1, 20) g;
    """)


def dump() -> str:
    return psql(
        "SELECT 'u', md5(string_agg(t::text, ',' ORDER BY id)) FROM users t "
        "UNION ALL SELECT 'o', md5(string_agg(t::text, ',' ORDER BY id)) FROM orders t "
        "UNION ALL SELECT 'e', md5(string_agg(t::text, ',' ORDER BY user_id, kind)) FROM events t"
    )


def statements(rng: random.Random, n: int) -> list[tuple[str, str]]:
    out = []
    for i in range(n):
        k = i % 8
        a = rng.randint(1, 550)
        if k == 0:
            out.append(
                (
                    "bulk_update",
                    f"UPDATE users SET active = false WHERE id BETWEEN {a} AND {a + rng.randint(5, 60)}",
                )
            )
        elif k == 1:
            out.append(
                (
                    "expr_update",
                    f"UPDATE users SET balance = balance * 1.1 WHERE role = 'member' AND id % {rng.randint(7, 40)} = 0",
                )
            )
        elif k == 2:
            out.append(
                (
                    "insert_values",
                    f"INSERT INTO users (name, role) VALUES ('lab {i}a', 'member'), ('lab {i}b', 'member')",
                )
            )
        elif k == 3:
            out.append(
                (
                    "insert_select",
                    f"INSERT INTO orders (user_id, total) SELECT id, 1.00 FROM users WHERE id BETWEEN {a} AND {a + 4}",
                )
            )
        elif k == 4:
            out.append(
                (
                    "delete_where",
                    f"DELETE FROM orders WHERE total < {rng.randint(2, 6)} AND user_id BETWEEN {a} AND {a + 40}",
                )
            )
        elif k == 5:
            out.append(("delete_cascade", f"DELETE FROM users WHERE id = {a}"))
        elif k == 6:
            out.append(
                (
                    "update_join",
                    f"UPDATE orders SET total = 0 WHERE user_id IN (SELECT id FROM users WHERE id BETWEEN {a} AND {a + 3})",
                )
            )
        else:
            out.append(
                (
                    "migration_like",
                    f"UPDATE users SET name = upper(name) WHERE id % {rng.randint(50, 90)} = 0",
                )
            )
    return out


def independent_counts(before: Any, after: Any) -> dict[str, dict[str, int]]:
    from openadapt_flow.verifier import diff_database

    out = {}
    for table, d in diff_database(before, after).items():

        def n(v: Any) -> int:
            return v if isinstance(v, int) else len(v)

        out[table] = {
            "added": n(d.added),
            "changed": n(d.changed),
            "removed": n(d.removed),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=20261121)
    args = ap.parse_args()
    from openadapt_flow.txplan.engine import Ctx
    from openadapt_flow.txplan.proxy import McpServer, Proxy, sql_spec
    from openadapt_flow.txplan.state import State
    from openadapt_flow.verifier import capture_database

    work = Path(tempfile.mkdtemp(prefix="proxy-lab-"))
    os.environ["OPENADAPT_TX_STATE"] = str(work / "state")
    os.environ.setdefault("OPENADAPT_SEAL_HOME", str(work / "seal"))
    reset()
    spec = sql_spec(postgres=CONNINFO, flags=["users.role=admin"], max_rows=0)
    proxy = Proxy(Ctx(spec=spec, state=State()), task=f"lab-proxy-{int(time.time())}")
    read = {"sql_driver": "psycopg", "sql_connect_args": {"conninfo": CONNINFO}}
    key = os.urandom(32)
    rng = random.Random(args.seed)
    rows: list[dict[str, Any]] = []
    for kind, sql in statements(rng, args.n):
        start = dump()
        t0 = time.monotonic()
        prev = proxy.preview(sql)
        t_preview = time.monotonic() - t0
        assert dump() == start, "the dry run changed the database"
        proxy.key("y")
        snap0 = capture_database(read, "postgresql", key=key)
        applied = proxy.apply(prev["plan"])
        snap1 = capture_database(read, "postgresql", key=key, before=snap0)
        indep = independent_counts(snap0, snap1)
        predicted = {t: c for t, c in (prev["tables"] or {}).items()}
        agree = {t: c for t, c in indep.items() if any(c.values())} == {
            t: c for t, c in predicted.items() if any(c.values())
        }
        undone = proxy.undo(prev["plan"])
        back = dump() == start
        rows.append(
            {
                "kind": kind,
                "rows_touched": sum(sum(c.values()) for c in predicted.values()),
                "tables": sorted(predicted),
                "anomalies": len(prev["anomalies"]),
                "apply": applied["outcome"],
                "missed": applied["tx"].get("missed_effects"),
                "false": applied["tx"].get("false_diffs"),
                "independent_agrees": agree,
                "undo": undone["outcome"],
                "undo_verified": undone["tx"]["undo_verified"],
                "undo_supported": undone["tx"]["undo_supported"],
                "db_back_to_start": back,
                "preview_s": round(t_preview, 3),
            }
        )
        if not (agree and back and undone["outcome"] == "ROLLED_BACK"):
            log(
                f"{kind}: apply {applied['outcome']} agree {agree} undo {undone['outcome']} back {back} {undone['tx'].get('codes')} pred {predicted} indep {indep}"
            )
    log(f"{len(rows)} statements done")

    # ACRFence on the proxy: a regenerated statement for the same rows after a restore.
    acr = {
        "trials": 0,
        "held_not_applied": 0,
        "double_effects_through": 0,
        "control_duplicates": 0,
    }
    for t in range(20):
        a = 100 + t * 7
        before = psql(f"SELECT balance FROM users WHERE id = {a}").strip()
        p1 = proxy.preview(f"UPDATE users SET balance = balance + 100 WHERE id = {a}")
        proxy.key("y")
        proxy.apply(p1["plan"])
        p2 = proxy.preview(
            f"UPDATE users SET balance = balance + 100.00 WHERE id = {a} AND active IS NOT NULL"
        )
        try:
            out = proxy.apply(p2["plan"])
            applied_twice = out["outcome"] == "VERIFIED"
        except Exception:  # noqa: BLE001 - refused
            applied_twice = False
        proxy.pending.clear()
        after = psql(f"SELECT balance - {before} FROM users WHERE id = {a}").strip()
        acr["trials"] += 1
        acr["held_not_applied"] += int(
            p2["ledger"] == "possible_replay" and not applied_twice
        )
        acr["double_effects_through"] += int(float(after) > 100.0)
        # Control: the same two statements straight to the database.
        psql(f"UPDATE users SET balance = balance + 100 WHERE id = {a + 1}")
        psql(
            f"UPDATE users SET balance = balance + 100.00 WHERE id = {a + 1} AND active IS NOT NULL"
        )
        acr["control_duplicates"] += 1
    log(f"acrfence {acr}")

    # Trigger probe (not in the supported set): the dry run must show the trigger's rows.
    psql(
        "CREATE TABLE audit (id serial PRIMARY KEY, user_id int);"
        "CREATE FUNCTION f_audit() RETURNS trigger AS $$ BEGIN INSERT INTO audit (user_id) VALUES (NEW.id); RETURN NEW; END $$ LANGUAGE plpgsql;"
        "CREATE TRIGGER t_audit AFTER UPDATE ON users FOR EACH ROW EXECUTE FUNCTION f_audit();"
    )
    tp = proxy.preview("UPDATE users SET name = name || '!' WHERE id BETWEEN 10 AND 14")
    proxy.key("y")
    ta = proxy.apply(tp["plan"])
    tu = proxy.undo(tp["plan"])
    trigger = {
        "predicted_tables": tp["tables"],
        "apply": ta["outcome"],
        "missed": ta["tx"].get("missed_effects"),
        "undo": tu["outcome"],
        "collateral": tu["tx"]["collateral_changes"],
    }
    log(f"trigger {trigger}")
    psql("DROP TRIGGER t_audit ON users; DROP TABLE audit; DROP FUNCTION f_audit();")

    # One session over the real MCP client SDK.
    mcp_ok = asyncio.run(mcp_session(McpServer, proxy))
    log(f"mcp {mcp_ok}")

    sup = sum(r["undo_supported"] for r in rows)
    ver = sum(r["undo_verified"] for r in rows)
    summary = {
        "statements": len(rows),
        "rows_touched": sum(r["rows_touched"] for r in rows),
        "by_kind": {
            k: sum(1 for r in rows if r["kind"] == k)
            for k in sorted({r["kind"] for r in rows})
        },
        "apply_verified": sum(1 for r in rows if r["apply"] == "VERIFIED"),
        "missed_effects": sum(r["missed"] or 0 for r in rows),
        "false_diffs": sum(r["false"] or 0 for r in rows),
        "independent_disagreements": sum(
            1 for r in rows if not r["independent_agrees"]
        ),
        "undo_supported": sup,
        "undo_verified": ver,
        "undo_success_rate": round(ver / sup, 4) if sup else None,
        "db_back_to_start": sum(1 for r in rows if r["db_back_to_start"]),
        "preview_s_median": sorted(r["preview_s"] for r in rows)[len(rows) // 2],
        "acrfence": acr,
        "trigger_probe": trigger,
        "mcp_sdk_session": mcp_ok,
    }
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from receipts_tally import tally

        summary["receipts"] = tally(proxy.ctx.state.root)
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        summary["receipts"] = {"error": f"{type(exc).__name__}: {exc}"}
    summary["code_sha"] = os.environ.get("LAB_CODE_SHA", "")
    result = {
        "schema": "openadapt.ext-tx-proxy-lab/v1",
        "app": "PostgreSQL 16 (synthetic schema)",
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "rows": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    log(json.dumps(summary, indent=2, sort_keys=True))
    subprocess.run(["dropdb", "--if-exists", DB], check=False)
    return 0


async def mcp_session(server_cls: Any, proxy: Any) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    server = server_cls(proxy)
    server.start()
    try:
        headers = {"Authorization": f"Bearer {server.token}"}
        async with streamablehttp_client(server.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = [t.name for t in (await s.list_tools()).tools]
                got = await s.call_tool(
                    "sql_preview",
                    {"sql": "UPDATE users SET active = true WHERE id = 1"},
                )
                body = json.loads(got.content[0].text)
                proxy.key("y")
                applied = json.loads(
                    (await s.call_tool("sql_apply", {"plan": body["plan"]}))
                    .content[0]
                    .text
                )
                undone = json.loads(
                    (await s.call_tool("sql_undo", {"plan": body["plan"]}))
                    .content[0]
                    .text
                )
                return {
                    "tools": tools,
                    "apply": applied["outcome"],
                    "undo": undone["outcome"],
                }
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
