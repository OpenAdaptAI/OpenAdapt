"""Measure resource-scoped verification on real systems: planted faults and clean writes.

    python benchmark/api_scoped/run.py --system medplum --clean 300 --faults 10
    python benchmark/api_scoped/run.py --system gitea --clean 300 --faults 10

Every write goes through :func:`openadapt_flow.txplan.scoped.scoped_write`
with the system's tx file (``packs/<system>.tx.yaml``). A planted fault is a
real write made through a replaced ``send`` (the executor misbehaves); the
verifier sees only the record and the system's own log. Whether each fault
really landed is read from the oracle channel (Medplum's Postgres, a Gitea
administrator), which the verifier never uses.

Kill criterion (fixed before the run): any planted fault the system's log
exposes that the verifier misses, or more than 1% stops on clean writes.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lab import LABS, http  # noqa: E402

from openadapt_flow.txplan.engine import Ctx, TxRefused  # noqa: E402
from openadapt_flow.txplan.scoped import default_send, scoped_write  # noqa: E402
from openadapt_flow.txplan.spec import load_spec  # noqa: E402
from openadapt_flow.txplan.state import State  # noqa: E402

FAMILIES = ["Synthwood", "Testerly", "Fixtureson", "Mockford", "Sampleton", "Placeholm"]
GIVEN = ["Ada", "Bo", "Cy", "Di", "Ed", "Flo", "Gus", "Hal"]
#: Names carry the run, so a record from an earlier run never counts as this run's.
RUN = format(int(time.time()) % 16**5, "05x")


class Trial:
    def __init__(
        self,
        kind: str,
        fault: Optional[str],
        kwargs: dict[str, Any],
        send: Optional[Callable[..., Any]] = None,
        landed: Optional[Callable[[dict[str, Any]], bool]] = None,
        exposed: bool = True,
    ) -> None:
        self.kind, self.fault, self.kwargs, self.send = kind, fault, kwargs, send
        self.landed, self.exposed = landed, exposed


def _with(fields: dict[str, Any], **change: Any) -> Callable[..., Any]:
    def send(adapter: Any, coll: Any, **kw: Any) -> Any:
        return default_send(
            adapter, coll, **{**kw, "fields": {**kw["fields"], **change}}
        )

    return send


def _twice(strip_guard: bool = True) -> Callable[..., Any]:
    """A retry storm: the executor sends the write again, without the guard."""

    def send(adapter: Any, coll: Any, **kw: Any) -> Any:
        if kw["op"] == "create":
            saved = coll.scoped.condition_header
            coll.scoped.condition_header = None
            try:
                default_send(adapter, coll, **{**kw, "marker": None})
            finally:
                coll.scoped.condition_header = saved
        else:
            default_send(adapter, coll, **kw)
        return default_send(adapter, coll, **kw)

    return send


def _then(extra: Callable[[], None]) -> Callable[..., Any]:
    def send(adapter: Any, coll: Any, **kw: Any) -> Any:
        rid = default_send(adapter, coll, **kw)
        extra()
        return rid

    return send


def _only(keep: list[str]) -> Callable[..., Any]:
    def send(adapter: Any, coll: Any, **kw: Any) -> Any:
        return default_send(
            adapter,
            coll,
            **{**kw, "fields": {k: v for k, v in kw["fields"].items() if k in keep}},
        )

    return send


# -- Medplum ----------------------------------------------------------------------


class MedplumScenario:
    system = "medplum"

    def __init__(self, lab: Any, rng: random.Random) -> None:
        self.lab, self.rng = lab, rng
        self.fhir = lab.base + "/fhir/R4"
        self.seeds: list[str] = []

    def _h(self, role: str = "writer") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.lab.token(role)}",
            "Content-Type": "application/fhir+json",
        }

    def patient(self) -> dict[str, Any]:
        r = self.rng
        return {
            "name": [
                {
                    "family": f"{r.choice(FAMILIES)}-{RUN}-{r.randrange(16**6):06x}",
                    "given": [r.choice(GIVEN)],
                }
            ],
            "birthDate": f"{r.randint(1940, 2010)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
            "gender": r.choice(["female", "male", "other", "unknown"]),
            "address": [
                {
                    "city": r.choice(["Springfield", "Riverton", "Lakeside"]),
                    "country": "XX",
                }
            ],
        }

    def seed(self, n: int) -> None:
        for _ in range(n):
            c, got = http(
                "POST",
                self.fhir + "/Patient",
                body={"resourceType": "Patient", **self.patient()},
                headers=self._h("bystander"),
            )
            assert c == 201, (c, got)
            self.seeds.append(got["id"])

    def other(self, rid: str) -> str:
        return self.rng.choice([s for s in self.seeds if s != rid])

    def _edit(self, role: str, rid: str) -> None:
        c, cur = http("GET", f"{self.fhir}/Patient/{rid}", headers=self._h(role))
        cur["birthDate"] = f"19{self.rng.randint(10, 39)}-01-01"
        c, _ = http(
            "PUT", f"{self.fhir}/Patient/{rid}", body=cur, headers=self._h(role)
        )
        assert c == 200, c

    def clean(self, i: int) -> Trial:
        if i % 2 == 0:
            return Trial(
                "create",
                None,
                {"collection": "patient", "op": "create", "fields": self.patient()},
            )
        rid = self.rng.choice(self.seeds)
        p = self.patient()
        field = self.rng.choice(["birthDate", "gender", "address"])
        return Trial(
            "update",
            None,
            {
                "collection": "patient",
                "op": "update",
                "rid": rid,
                "fields": {field: p[field]},
            },
        )

    def faults(self, i: int) -> list[Trial]:
        rid = self.rng.choice(self.seeds)
        p = self.patient()
        oracle = self.lab.oracle_patient
        # The fault leaves gender unwritten, so the intent must change it.
        new_gender = next(
            g
            for g in ("other", "female", "male")
            if g != (oracle(rid) or {}).get("gender")
        )
        out = [
            Trial(
                "create",
                "wrong_value",
                {"collection": "patient", "op": "create", "fields": p},
                _with(p, birthDate="1901-01-01"),
                lambda r: (oracle(r["id"]) or {}).get("birthDate") == "1901-01-01",
            ),
            Trial(
                "update",
                "wrong_value",
                {
                    "collection": "patient",
                    "op": "update",
                    "rid": rid,
                    "fields": {"birthDate": "1903-03-03"},
                },
                _with({}, birthDate="1902-02-02"),
                lambda r, rid=rid: (oracle(rid) or {}).get("birthDate") == "1902-02-02",
            ),
            Trial(
                "create",
                "duplicate",
                {"collection": "patient", "op": "create", "fields": self.patient()},
                _twice(),
                None,
            ),
            Trial(
                "update",
                "duplicate",
                {
                    "collection": "patient",
                    "op": "update",
                    "rid": rid,
                    "fields": {"birthDate": p["birthDate"]},
                },
                _twice(),
                None,
            ),
            Trial(
                "update",
                "partial_write",
                {
                    "collection": "patient",
                    "op": "update",
                    "rid": rid,
                    "fields": {"birthDate": "1977-07-07", "gender": new_gender},
                },
                _only(["birthDate"]),
                lambda r, rid=rid, g=new_gender: (
                    (oracle(rid) or {}).get("birthDate") == "1977-07-07"
                    and (oracle(rid) or {}).get("gender") != g
                ),
            ),
        ]
        victim = self.other(rid)
        out.append(
            Trial(
                "update",
                "collateral_same_actor",
                {
                    "collection": "patient",
                    "op": "update",
                    "rid": rid,
                    "fields": {"birthDate": "1966-06-06"},
                },
                _then(lambda v=victim: self._edit("writer", v)),
                lambda r: True,
            )
        )
        victim2 = self.other(rid)
        out.append(
            Trial(
                "update",
                "collateral_other_actor",
                {
                    "collection": "patient",
                    "op": "update",
                    "rid": rid,
                    "fields": {"birthDate": "1955-05-05"},
                },
                _then(lambda v=victim2: self._edit("bystander", v)),
                lambda r: True,
                exposed=False,
            )
        )
        # Duplicate creates: count the records holding the same name in the oracle.
        fam = out[2].kwargs["fields"]["name"][0]["family"]
        out[2].landed = lambda r, fam=fam: self.lab.oracle_count(fam) == 2
        out[3].landed = lambda r: (
            True
        )  # two PUTs were sent; the log decides if the server kept both
        return out


# -- Gitea ------------------------------------------------------------------------


class GiteaScenario:
    system = "gitea"

    def __init__(self, lab: Any, rng: random.Random) -> None:
        self.lab, self.rng = lab, rng
        self.api = lab.base + "/api/v1/repos/oa-writer/lab"
        self.seeds: list[str] = []

    def _h(self, role: str = "writer") -> dict[str, str]:
        return {"Authorization": f"token {self.lab.token(role)}"}

    def title(self) -> str:
        return f"Synthetic task {RUN}-{self.rng.randrange(16**6):06x}"

    def seed(self, n: int) -> None:
        for _ in range(n):
            c, got = http(
                "POST",
                self.api + "/issues",
                body={"title": self.title(), "body": "seed"},
                headers=self._h("bystander"),
            )
            assert c == 201, (c, got)
            self.seeds.append(str(got["number"]))

    def other(self, rid: str) -> str:
        return self.rng.choice([s for s in self.seeds if s != rid])

    def clean(self, i: int) -> Trial:
        k = i % 4
        rid = self.rng.choice(self.seeds)
        if k == 0:
            return Trial(
                "create",
                None,
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": self.title(), "body": f"Synthetic body {i}"},
                },
            )
        if k == 1:
            return Trial(
                "comment",
                None,
                {
                    "collection": "comment",
                    "op": "create",
                    "path": {"issue": rid},
                    "slot": f"c{i}",
                    "fields": {"body": f"Synthetic comment {i}"},
                },
            )
        if k == 2:
            state = self.lab.oracle_issue("lab", rid)["state"]
            return Trial(
                "close",
                None,
                {
                    "collection": "issue",
                    "op": "update",
                    "rid": rid,
                    "fields": {"state": "open" if state == "closed" else "closed"},
                },
            )
        return Trial(
            "edit",
            None,
            {
                "collection": "issue",
                "op": "update",
                "rid": rid,
                "fields": {"title": self.title()},
            },
        )

    def faults(self, i: int) -> list[Trial]:
        rid = self.rng.choice(self.seeds)
        victim = self.other(rid)
        t = self.title()
        oracle = self.lab.oracle_issue
        w = self._h("writer")
        b = self._h("bystander")

        # A planted side write counts as landed only when the server accepted
        # it (status < 300): a rejected one is not evidence of a plant.
        planted: dict[str, bool] = {}

        def side(
            tag: str, method: str, url: str, body: dict[str, Any], h: dict[str, str]
        ) -> None:
            code, _ = http(method, url, body=body, headers=h)
            planted[tag] = code < 300

        def comment(h: dict[str, str], n: str, tag: str) -> Callable[[], None]:
            return lambda: side(
                tag,
                "POST",
                f"{self.api}/issues/{n}/comments",
                {"body": "collateral"},
                h,
            )

        def retitle(h: dict[str, str], n: str, tag: str) -> Callable[[], None]:
            return lambda: side(
                tag, "PATCH", f"{self.api}/issues/{n}", {"title": self.title()}, h
            )

        def toggle(h: dict[str, str], n: str, tag: str) -> Callable[[], None]:
            def go() -> None:
                st = oracle("lab", n)["state"]
                side(
                    tag,
                    "PATCH",
                    f"{self.api}/issues/{n}",
                    {"state": "open" if st == "closed" else "closed"},
                    h,
                )

            return go

        def landed(tag: str) -> Callable[[dict[str, Any]], bool]:
            return lambda r: planted.get(tag, False)

        return [
            Trial(
                "create",
                "wrong_value",
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": t, "body": "x"},
                },
                _with({}, title=t + " (wrong)"),
                lambda r: (
                    (oracle("lab", r["id"]) or {}).get("title", "").endswith("(wrong)")
                ),
            ),
            Trial(
                "edit",
                "wrong_value",
                {
                    "collection": "issue",
                    "op": "update",
                    "rid": rid,
                    "fields": {"title": t},
                },
                _with({}, title=t + " (wrong)"),
                lambda r, n=rid: (
                    (oracle("lab", n) or {}).get("title", "").endswith("(wrong)")
                ),
            ),
            Trial(
                "create",
                "duplicate",
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": t + " d", "body": "x"},
                },
                _twice(),
                None,
            ),
            Trial(
                "comment",
                "duplicate",
                {
                    "collection": "comment",
                    "op": "create",
                    "path": {"issue": rid},
                    "slot": f"d{i}",
                    "fields": {"body": f"dup {i}"},
                },
                _twice(),
                lambda r: True,
            ),
            Trial(
                "create",
                "partial_write",
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": t + " p", "body": "Synthetic body"},
                },
                _only(["title"]),
                lambda r: (oracle("lab", r["id"]) or {}).get("body") in ("", None),
            ),
            Trial(
                "comment",
                "collateral_same_actor",
                {
                    "collection": "comment",
                    "op": "create",
                    "path": {"issue": rid},
                    "slot": f"x{i}",
                    "fields": {"body": f"c {i}"},
                },
                _then(comment(w, victim, "c")),
                landed("c"),
            ),
            Trial(
                "create",
                "collateral_same_actor",
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": t + " c", "body": "x"},
                },
                _then(toggle(w, victim, "t")),
                landed("t"),
            ),
            Trial(
                "create",
                "collateral_same_actor_unlogged",
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": t + " u", "body": "x"},
                },
                _then(retitle(w, victim, "r")),
                landed("r"),
                exposed=False,
            ),
            Trial(
                "create",
                "collateral_other_actor",
                {
                    "collection": "issue",
                    "op": "create",
                    "fields": {"title": t + " o", "body": "x"},
                },
                _then(comment(b, victim, "o")),
                landed("o"),
                exposed=False,
            ),
        ]

    def dup_landed(self, trial: Trial) -> bool:
        if trial.kwargs["collection"] != "issue":
            return True
        title = trial.kwargs["fields"]["title"]
        # Newest issues first, no search index (the index trails the write).
        c, got = http(
            "GET",
            f"{self.api}/issues?state=all&type=issues&sort=newest&limit=20",
            headers=self._h("oracle"),
        )
        return sum(1 for x in got if x["title"] == title) == 2


SCENARIOS = {"medplum": MedplumScenario, "gitea": GiteaScenario}


def run_one(ctx: Ctx, system: str, trial: Trial, task: str) -> dict[str, Any]:
    t0 = time.monotonic()
    kw = dict(trial.kwargs)
    try:
        out = scoped_write(ctx, task=task, system=system, send=trial.send, **kw)
    except TxRefused as exc:
        out = {"outcome": "REFUSED", "codes": [exc.code], "notes": []}
    out["seconds"] = round(time.monotonic() - t0, 3)
    if out.get("run_dir"):
        tx = json.loads((Path(out["run_dir"]) / "report.json").read_text()).get(
            "tx", {}
        )
        out["audit"] = tx.get("audit")
        out["mismatched_fields"] = tx.get("mismatched_fields")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--system", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--clean", type=int, default=300)
    ap.add_argument("--faults", type=int, default=10, help="rounds of the fault set")
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    lab = LABS[args.system]()
    for k, v in lab.env().items():
        os.environ[k] = v
    os.environ.setdefault(
        "OPENADAPT_SEAL_HOME", tempfile.mkdtemp(prefix="api-scoped-seal-")
    )
    spec = load_spec(HERE / "packs" / f"{args.system}.tx.yaml")
    state = State(Path(tempfile.mkdtemp(prefix="api-scoped-state-")))
    ctx = Ctx(spec=spec, state=state)
    sc = SCENARIOS[args.system](lab, rng)
    sc.seed(12)
    run_id = f"{int(time.time())}"
    rows: list[dict[str, Any]] = []

    for i in range(args.clean):
        trial = sc.clean(i)
        out = run_one(ctx, args.system, trial, f"clean-{run_id}-{i}")
        rows.append({"phase": "clean", "kind": trial.kind, "fault": None, **_row(out)})
    for i in range(args.faults):
        for trial in sc.faults(i):
            out = run_one(
                ctx,
                args.system,
                trial,
                f"fault-{run_id}-{i}-{trial.fault}-{trial.kind}",
            )
            if trial.fault == "duplicate" and trial.landed is None:
                landed = sc.dup_landed(trial) if hasattr(sc, "dup_landed") else None
            else:
                landed = trial.landed(out) if trial.landed else None
            rows.append(
                {
                    "phase": "fault",
                    "kind": trial.kind,
                    "fault": trial.fault,
                    "exposed": trial.exposed,
                    "landed": landed,
                    **_row(out),
                }
            )
    # Idempotency: the same task retried, and retried with a regenerated payload.
    for i in range(args.faults):
        trial = sc.clean(0)
        task = f"retry-{run_id}-{i}"
        first = run_one(ctx, args.system, trial, task)
        again = run_one(ctx, args.system, trial, task)
        regen = dict(trial.kwargs)
        regen["fields"] = _regenerated(sc, regen["fields"])
        third = run_one(ctx, args.system, Trial(trial.kind, None, regen), task)
        rows.append(
            {
                "phase": "retry",
                "kind": trial.kind,
                "fault": "retry_same_payload",
                "first": first["outcome"],
                **_row(again),
            }
        )
        rows.append(
            {
                "phase": "retry",
                "kind": trial.kind,
                "fault": "retry_regenerated_payload",
                "first": first["outcome"],
                **_row(third),
            }
        )

    summary = summarize(rows)
    result = {
        "system": args.system,
        "seed": args.seed,
        "started": run_id,
        "summary": summary,
        "rows": rows,
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _regenerated(sc: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """The same record, written again with some value the model changed."""
    out = json.loads(json.dumps(fields))
    if "address" in out:
        out["address"] = [{"city": "Regenerated", "country": "XX"}]
    elif "body" in out:
        out["body"] = str(out["body"]) + " (regenerated)"
    return out


def _row(out: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome": out.get("outcome"),
        "codes": out.get("codes") or [],
        "notes": out.get("notes") or [],
        "replayed": bool(out.get("replayed")),
        "receipt": bool(out.get("receipt")),
        "seconds": out.get("seconds"),
        "audit": out.get("audit"),
        "mismatched_fields": out.get("mismatched_fields"),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clean = [r for r in rows if r["phase"] == "clean"]
    stops = [r for r in clean if r["outcome"] != "VERIFIED"]
    secs = [r["seconds"] for r in clean if r.get("seconds") is not None]
    faults: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["phase"] != "fault":
            continue
        k = f"{r['fault']}/{r['kind']}"
        f = faults.setdefault(
            k,
            {
                "n": 0,
                "landed": 0,
                "stopped": 0,
                "not_landed": 0,
                "exposed": r["exposed"],
                "codes": {},
            },
        )
        # A stop counts as a detection only for a plant that landed; a trial
        # whose plant didn't land is not evidence either way.
        if r["landed"] is False:
            f["not_landed"] += 1
            continue
        f["n"] += 1
        f["landed"] += 1 if r["landed"] else 0
        if r["outcome"] != "VERIFIED":
            f["stopped"] += 1
        for c in r["codes"]:
            f["codes"][c] = f["codes"].get(c, 0) + 1
    missed = [k for k, f in faults.items() if f["exposed"] and f["stopped"] < f["n"]]
    retry = [r for r in rows if r["phase"] == "retry"]
    return {
        "clean": {
            "n": len(clean),
            "verified": len(clean) - len(stops),
            "stops": len(stops),
            "stop_rate": round(len(stops) / len(clean), 4) if clean else None,
            "stop_codes": sorted({c for r in stops for c in r["codes"]}),
            "receipts": sum(1 for r in clean if r["receipt"]),
            "seconds_p50": statistics.median(secs) if secs else None,
            "seconds_p95": sorted(secs)[int(0.95 * (len(secs) - 1))] if secs else None,
        },
        "faults": faults,
        "exposed_faults_missed": missed,
        "retry": {
            "same_payload_replayed": sum(
                1 for r in retry if r["fault"] == "retry_same_payload" and r["replayed"]
            ),
            "regenerated_refused": sum(
                1
                for r in retry
                if r["fault"] == "retry_regenerated_payload"
                and "replay_mismatch" in r["codes"]
            ),
            "n": sum(1 for r in retry if r["fault"] == "retry_same_payload"),
        },
        "kill": bool(missed) or (bool(clean) and len(stops) / len(clean) > 0.01),
    }


if __name__ == "__main__":
    raise SystemExit(main())
