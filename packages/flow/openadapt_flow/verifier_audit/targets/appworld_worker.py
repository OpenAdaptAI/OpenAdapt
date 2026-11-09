"""AppWorld replay worker. Runs in AppWorld's own interpreter, not this one.

AppWorld (Apache-2.0) pins pydantic 1, so the adapter in ``appworld.py``
starts this file with ``python -I`` from an AppWorld environment and talks
to it over JSON lines. This file imports only the standard library and
``appworld``; nothing from OpenAdapt.

Request: ``{"task": id, "calls": [{"method", "url", "data"}], "claim_index":
int|null, "score": bool}``. Response: ``{"final_rows", "claim_rows",
"call_errors", "verdict", "detail"}``.

Effect rows come from reading the SQLite files of a full dump directly
(``sqlite3``), as a multiset diff against the task's seed dump, so the
effect never passes through AppWorld's evaluator or its model layer.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
import types
from pathlib import Path


def _shim_icecream() -> None:
    try:
        import icecream  # noqa: F401
    except ModuleNotFoundError:
        module = types.ModuleType("icecream")

        def ic(*a):
            return a[0] if len(a) == 1 else (a or None)

        ic.configureOutput = lambda *a, **k: None  # type: ignore[attr-defined]
        setattr(module, "ic", ic)
        sys.modules["icecream"] = module


def _dump(world, to_dir: Path) -> None:
    from appworld.apps.api_lib import save_local_dbs

    save_local_dbs(
        from_db_home_path=world.models_to_db_home_path,
        to_db_home_path=str(to_dir),
        format="full",
        app_names=[*world.task.allowed_apps, "admin", "supervisor"],
        delete_if_exists=True,
        save_model_hashes=False,
    )


def _tables(conn: sqlite3.Connection, schema: str) -> set[str]:
    query = f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"
    return {r[0] for r in conn.execute(query)}


def _select(conn: sqlite3.Connection, sql: str) -> list[str]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [
        json.dumps(dict(zip(cols, row)), sort_keys=True, default=str) for row in cur
    ]


def _delta(seed_dir: Path, end_dir: Path) -> list[dict]:
    """Rows added or removed between two full dumps, computed inside SQLite
    (``EXCEPT``) so a run never serializes the unchanged tables. Every
    AppWorld table carries a primary key, so set difference is the row
    multiset difference."""
    rows = []
    names = sorted(
        {p.name for p in seed_dir.glob("*.db")} | {p.name for p in end_dir.glob("*.db")}
    )
    for name in names:
        conn = sqlite3.connect(":memory:", uri=True)
        try:
            schemas = []
            for alias, directory in (("s", seed_dir), ("e", end_dir)):
                if (directory / name).is_file():
                    conn.execute(
                        f"ATTACH DATABASE 'file:{directory / name}?mode=ro' AS {alias}"
                    )
                    schemas.append(alias)
            seed = _tables(conn, "s") if "s" in schemas else set()
            end = _tables(conn, "e") if "e" in schemas else set()
            stem = name[:-3]
            for table in sorted(seed | end):
                q = f'"{table}"'
                if table in seed and table in end:
                    added = _select(
                        conn, f"SELECT * FROM e.{q} EXCEPT SELECT * FROM s.{q}"
                    )
                    removed = _select(
                        conn, f"SELECT * FROM s.{q} EXCEPT SELECT * FROM e.{q}"
                    )
                else:
                    added = (
                        _select(conn, f"SELECT * FROM e.{q}") if table in end else []
                    )
                    removed = (
                        _select(conn, f"SELECT * FROM s.{q}") if table in seed else []
                    )
                rows += [
                    {"table": f"{stem}.{table}", "op": "added", "row": r} for r in added
                ]
                rows += [
                    {"table": f"{stem}.{table}", "op": "removed", "row": r}
                    for r in removed
                ]
        finally:
            conn.close()
    return rows


class Worker:
    def __init__(self, appworld_root: str, cache: Path) -> None:
        from appworld import update_root

        _shim_icecream()
        update_root(appworld_root)
        self.root = appworld_root
        self.cache = cache
        self.n = 0

    def _seed_dir(self, world, task: str) -> Path:
        path = self.cache / task
        if not (path / ".complete").is_file():
            _dump(world, path)
            (path / ".complete").write_text("full seed dump\n")
        return path

    def handle(self, req: dict) -> dict:
        from appworld import AppWorld

        self.n += 1
        experiment = f"verifier_audit_{os.getpid()}_{self.n}"
        world = None
        tmp = Path(tempfile.mkdtemp(prefix="aw-audit-"))
        out: dict = {
            "call_errors": {},
            "claim_rows": None,
            "verdict": None,
            "detail": None,
        }
        try:
            world = AppWorld(
                task_id=req["task"], experiment_name=experiment, raise_on_failure=False
            )
            seed = self._seed_dir(world, req["task"])
            client = getattr(world.requester, "client", None)
            for i, call in enumerate(req["calls"]):
                if i == req.get("claim_index"):
                    _dump(world, tmp / "claim")
                    out["claim_rows"] = _delta(seed, tmp / "claim")
                url = call["url"] if call["url"].startswith("/") else "/" + call["url"]
                try:
                    getattr(world.requester, call["method"])(
                        url,
                        data=call.get("data") or {},
                        client=client,
                        raise_on_failure=True,
                    )
                except Exception as exc:  # noqa: BLE001 - a refused call is data
                    out["call_errors"][str(i)] = str(exc)[:300]
            _dump(world, tmp / "end")
            out["final_rows"] = _delta(seed, tmp / "end")
            if req.get("score"):
                world._save_state(world.output_db_home_path_on_disk)
                tracker = world.evaluate(suppress_errors=True).to_dict()
                out["verdict"] = "PASS" if tracker.get("success") else "FAIL"
                out["detail"] = {
                    "num_tests": tracker.get("num_tests"),
                    "failures": [
                        str(f.get("requirement", ""))[:200]
                        for f in tracker.get("failures") or []
                    ],
                }
        except Exception:  # noqa: BLE001
            out["verdict"] = "ERROR"
            out["detail"] = traceback.format_exc()[-1500:]
            out.setdefault("final_rows", [])
        finally:
            if world is not None:
                try:
                    world.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(
                Path(self.root) / "experiments" / "outputs" / experiment,
                ignore_errors=True,
            )
        out.setdefault("final_rows", [])
        return out


def main() -> int:
    appworld_root, cache = sys.argv[1], Path(sys.argv[2])
    cache.mkdir(parents=True, exist_ok=True)
    # Keep the protocol channel clean: responses go to a private copy of fd 1,
    # and fd 1 itself (Python and C-level prints alike) goes to stderr.
    real_stdout = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    worker = Worker(appworld_root, cache)
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        resp = worker.handle(req)
        resp["id"] = req.get("id")
        real_stdout.write(json.dumps(resp, default=str) + "\n")
        real_stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
