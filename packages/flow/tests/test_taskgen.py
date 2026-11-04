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
from typing import Optional
from urllib.parse import urlparse

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
    seeded = (
        "public" if app == "a" and schema == 1 and family != "interruption" else "salt"
    )
    return {
        "id": tid,
        "_meta": {
            "app": app,
            "kind": kind,
            "family": family,
            "schema": schema,
            "seeded": seeded,
        },
    }


def test_splits_hold_out_by_app_mechanism_and_schema() -> None:
    tasks = [_task(f"a{i}", "a", "create", "control") for i in range(30)]
    tasks += [_task(f"m{i}", "a", "update", "interruption") for i in range(5)]
    tasks += [_task(f"b{i}", "b", "create", "control") for i in range(5)]
    tasks += [_task(f"s{i}", "a", "create", "control", schema=2) for i in range(5)]
    salted = [_task(f"p{i}", "a", "update", "control") for i in range(8)]
    for t in salted:
        t["_meta"]["seeded"] = "salt"
    tasks += salted
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
    # Public-seeded leftovers never join a private split.
    assert {t["id"][0] for t in s["private_train"]} == {"p"}
    assert len(s["public_unused"]) == 20
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


@pytest.mark.skipif(
    not os.environ.get("TASKGEN_PG"),
    reason="needs TASKGEN_PG (a PostgreSQL admin conninfo)",
)
def test_workbench_guards_and_stale_form() -> None:
    """The reset and state routes need the admin token, sign-in never
    redirects into them, and a concurrent edit makes the agent's form stale."""

    import http.cookiejar
    import urllib.error
    import urllib.request

    import psycopg2

    from openadapt_flow.taskgen.workbench import serve

    pg = os.environ["TASKGEN_PG"]
    db = "wb_taskgen_guards"
    conn = psycopg2.connect(pg + " dbname=postgres")
    conn.autocommit = True
    conn.cursor().execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    conn.cursor().execute(f"CREATE DATABASE {db}")
    conn.close()
    prof = load_profile(PROFILES[0])
    d = F.build(prof, "update", "concurrent_edit", False, 7)
    url, wb, stop = serve(prof, f"{pg} dbname={db}")
    try:
        wb.reset({"rows": d.world.setup_rows(), **d.setup})
        for method, path in (("GET", "/__state"), ("POST", "/__reset")):
            req = urllib.request.Request(
                url + path, data=b"{}" if method == "POST" else None, method=method
            )
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            assert err.value.code == 404
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        a = prof.agent
        resp = op.open(
            url + "/login?next=/__state",
            data=f"username={a['user']}&password={a['password']}".encode(),
        )
        assert urlparse(resp.geturl()).path == "/"
        # Open the edit form: the second user saves right after, so this
        # form's version is stale and the save is refused.
        target = d.setup["concurrent"]
        pid = next(
            i
            for i, r in d.world.find(prof.person)
            if r.get("_as") == target["record"][1:]
        )
        page = op.open(f"{url}/e/{prof.person}/{pid}/edit").read().decode()
        version = re.search(r'name="__version" value="(\d+)"', page).group(1)
        P = prof.entity(prof.person)
        row = d.world.get(prof.person, pid)
        body = (
            "&".join(f"{f.name}={row.get(f.name) or ''}" for f in P.editable)
            + f"&__version={version}"
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            op.open(f"{url}/e/{prof.person}/{pid}/edit", data=body.encode())
        assert err.value.code == 409
    finally:
        stop()


def test_public_sample_matches_its_manifest() -> None:
    """The committed public sample is the released one: every task's digest
    matches the manifest, each names a contract that exists, and nothing
    private (validation plans, the fault family in tags) ships with it."""

    import hashlib

    env = Path(__file__).resolve().parents[1] / "environments" / "taskgen-mockmed"
    manifest = json.loads((env / "MANIFEST.json").read_text())
    lines = (env / "tasks" / "public.jsonl").read_bytes().splitlines()
    assert len(lines) == manifest["splits"]["public"]["tasks"] == 100
    body = b"\n".join(lines) + b"\n"
    assert (
        "sha256:" + hashlib.sha256(body).hexdigest()
        == manifest["splits"]["public"]["tasks_sha256"]
    )
    assert not (env / "validation").exists()
    for raw in lines:
        task = json.loads(raw)
        digest = manifest["public_tasks"][task["id"]]
        assert digest["task"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert (env / task["verify"]).is_file()
        assert task["split"] == "public"
        assert not any(t.startswith("family:") for t in task["tags"])
        assert "fault" not in task  # the family stays private
        assert task["solution"][-1]["kind"] == "done"
    # Only the public split is described; the rest is a salted commitment.
    assert set(manifest["splits"]) == {"public"}
    assert manifest["private_commitment"].startswith("sha256:")


def test_private_splits_need_the_salt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the private salt only the public split can be written, and a
    salted task shares neither its seed nor its id with any public guess."""

    from openadapt_flow.taskgen import secret
    from openadapt_flow.taskgen.generate import write_pack

    monkeypatch.delenv(secret.SALT_ENV, raising=False)
    prof = load_profile(PROFILES[0])
    with pytest.raises(secret.SaltMissing):
        write_pack(
            prof,
            tmp_path / "m.yaml",
            tmp_path / "out",
            per_cell=1,
            seed=1,
            split="heldout",
        )
    salt_file = tmp_path / "salt"
    salt_file.write_bytes(b"x" * 40)
    monkeypatch.setenv(secret.SALT_ENV, str(salt_file))
    secret.write_check(tmp_path / "check.json")
    monkeypatch.setenv(secret.SALT_CHECK_ENV, str(tmp_path / "check.json"))
    salt = secret.load_salt("heldout")
    name = "app-v1-create-control-adv-1001"
    assert secret.seed(salt, name, 1001) != 1001
    assert secret.task_id(salt, name, "app") != secret.task_id(None, name, "app")
    assert secret.load_salt("public") is None


def _throwaway_salt(tmp_path: Path, name: str, byte: bytes) -> Path:
    # A throwaway salt for tests only; the real salt never enters a test.
    path = tmp_path / name
    path.write_bytes(byte * 40)
    return path


def test_a_wrong_salt_is_refused_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A salt that doesn't match the private check file fails instead of
    silently seeding a different split."""

    from openadapt_flow.taskgen import secret
    from openadapt_flow.taskgen.generate import write_pack

    right = _throwaway_salt(tmp_path, "right", b"r")
    wrong = _throwaway_salt(tmp_path, "wrong", b"w")
    check = tmp_path / "check.json"
    monkeypatch.setenv(secret.SALT_ENV, str(right))
    assert secret.main(["--write-check", str(check)]) == 0
    written = check.read_text()
    assert "r" * 40 not in written  # only the HMAC is stored
    assert json.loads(written) == {
        "schema": secret.CHECK_SCHEMA,
        "hmac_sha256": secret.fingerprint(b"r" * 40),
    }
    monkeypatch.setenv(secret.SALT_CHECK_ENV, str(check))
    assert secret.load_salt("heldout") == b"r" * 40

    monkeypatch.setenv(secret.SALT_ENV, str(wrong))
    with pytest.raises(secret.SaltMismatch, match="does not match"):
        secret.load_salt("heldout")
    out = tmp_path / "out"
    with pytest.raises(secret.SaltMismatch):
        write_pack(
            load_profile(PROFILES[0]),
            tmp_path / "m.yaml",
            out,
            per_cell=1,
            seed=1,
            split="heldout",
        )
    assert not out.exists()  # nothing was written with the wrong salt
    assert secret.load_salt("public") is None  # the public split needs no salt


@pytest.mark.parametrize(
    "check",
    [
        None,
        "missing",
        "not json",
        '{"hmac_sha256": "%s"}' % ("0" * 64),
        '{"schema": "openadapt.taskgen-salt-check/v1", "hmac_sha256": "abc"}',
        '{"schema": "openadapt.taskgen-salt-check/v1", "hmac_sha256": 7}',
        "[]",
    ],
)
def test_an_unchecked_salt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: Optional[str]
) -> None:
    """With no usable check file a private split refuses (fail closed)."""

    from openadapt_flow.taskgen import secret

    monkeypatch.setenv(secret.SALT_ENV, str(_throwaway_salt(tmp_path, "s", b"s")))
    if check is None:
        monkeypatch.delenv(secret.SALT_CHECK_ENV, raising=False)
    else:
        path = tmp_path / "check.json"
        if check != "missing":
            path.write_text(check)
        monkeypatch.setenv(secret.SALT_CHECK_ENV, str(path))
    with pytest.raises(secret.SaltMismatch):
        secret.load_salt("heldout")


def test_public_split_refuses_unbundled_profiles(tmp_path: Path) -> None:
    from openadapt_flow.taskgen import secret
    from openadapt_flow.taskgen.generate import write_pack

    src = PROFILE_DIR / f"{PROFILES[0]}.yaml"
    copy = tmp_path / "other.yaml"
    copy.write_text(src.read_text())
    with pytest.raises(secret.SaltMissing):
        write_pack(
            load_profile(copy),
            tmp_path / "m.yaml",
            tmp_path / "out",
            per_cell=1,
            seed=1,
        )
