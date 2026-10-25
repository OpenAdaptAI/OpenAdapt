"""Back-test: a generated per-site mapping onto the vetted identity template.

    python -m generated_parts.identity_mapping_backtest --dup-check <checkout of #1516>

The template is #1516's record check (``patient-identity/1``), run from that
checkout. The people are #1516's own (the stream's existing charts and every
new chart it creates), rendered in three site shapes:

- ``rows``: #1516's own patient rows (``fname``, ``lname``, ``DOB``, ...);
- ``openemr_rest``: OpenEMR's REST patient rows (``pubpid``, ``birth_lname``);
- ``fhir``: FHIR R4 ``Patient`` resources, flattened the way a generated FHIR
  read channel flattens them (``generate/channel.py``: first list item).

For each shape the mapping is generated from the site's rows and three
demonstrations (three of the charts, with the values typed for them), then
compared with the reference mapping (#1516's own adapter for that shape) on:

- the demonstrations: each saved record must read back as typed;
- a perturbation suite built from the site's records (generated duplicates,
  and every record posed as a new patient);
- #1516's 676-item stream (223 true duplicates), rendered in that shape.

No app, no model calls. Runs in a subprocess against the #1516 checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]

_RUN = r"""
import dataclasses, importlib.util, json, sys
sys.path = [p for p in sys.path if "openadapt-evals" not in p]
spec = importlib.util.spec_from_file_location("gp_identity_template", sys.argv[1])
it = importlib.util.module_from_spec(spec); sys.modules[spec.name] = it; spec.loader.exec_module(it)
from benchmark.dup_check import measure
from openadapt_flow.runtime.checks import duplicate_record as dr

variants = json.load(open(sys.argv[2]))["variants"]
nicknames = {}
for name, vs in variants.items():
    for v in vs:
        nicknames.setdefault(name.casefold(), set()).add(v)
        nicknames.setdefault(v.casefold(), set()).add(name)
nicknames = {k: sorted(v) for k, v in nicknames.items()}

def iso(v):
    return measure.iso(v or "")

def person(p):
    return {
        "fname": p.get("fname", ""), "lname": p.get("lname", ""), "DOB": iso(p.get("DOB", "")),
        "phone_home": p.get("phone_home", ""), "email": p.get("email", ""),
        "street": p.get("street", ""), "prior_lname": p.get("prior_lname") or "",
    }

def rows_shape(i, p):
    return dict(p, id=str(i), DOB=iso(p.get("DOB", "")))

def rest_shape(i, p):
    row = {"id": str(i), "pid": str(i), "uuid": f"u-{i}", "fname": p.get("fname", ""),
           "lname": p.get("lname", ""), "DOB": iso(p.get("DOB", "")), "pubpid": f"MRN{i:05d}",
           "phone_home": p.get("phone_home", ""), "street": p.get("street", ""),
           "email": p.get("email", ""), "sex": p.get("sex", "")}
    if p.get("prior_lname"):
        row["birth_lname"] = p["prior_lname"]
    return row

def fhir_resource(i, p):
    names = [{"use": "official", "family": p.get("lname", ""), "given": [p.get("fname", "")]}]
    if p.get("prior_lname"):
        names.append({"use": "old", "family": p["prior_lname"], "given": [p.get("fname", "")]})
    return {
        "resourceType": "Patient", "id": str(i), "name": names, "birthDate": iso(p.get("DOB", "")),
        "identifier": [{"type": {"coding": [{"code": "MR"}]}, "value": f"MRN{i:05d}"}],
        "telecom": [{"system": "phone", "use": "home", "value": p.get("phone_home", "")},
                    {"system": "email", "value": p.get("email", "")}],
        "address": [{"line": [p.get("street", "")]}],
    }

def flatten(resource):
    out = {}
    def walk(node, path):
        if isinstance(node, list):
            if node: walk(node[0], path)
            return
        if isinstance(node, dict):
            for k in sorted(node): walk(node[k], f"{path}.{k}" if path else k)
            return
        if path: out[path] = node
    walk(resource, "")
    return out

class FakeResp:
    def __init__(self, body, url): self.status_code, self._body, self.url = 200, body, url
    def json(self): return self._body
class FakeSession:
    def __init__(self, rows): self.rows = rows
    def get(self, url, **kw): return FakeResp({"data": self.rows, "links": {}}, url)

def ref_rows(rows):
    return [measure.record_of(i, r) for i, r in enumerate(rows)]

def ref_rest(rows):
    src = dr.RestPatientSource("http://ref.invalid", session=FakeSession(rows), page_size=10**6)
    return src.search({})

def ref_fhir(resources):
    src = dr.FhirPatientSource("http://ref.invalid")
    return [src._record(r) for r in resources]

stream = json.load(open(sys.argv[3]))["seed7-500"]
profile = stream["profile"]
items = stream["items"]
kinds = set(stream["duplicate_kinds"])
template = it.Template("patient-identity/1")
report = {}
shapes = {
    "rows": (rows_shape, None, ref_rows),
    "openemr_rest": (rest_shape, None, ref_rest),
    "fhir": (fhir_resource, None, ref_fhir),
    # The same resources read the way a generic flattening reads them (first
    # list item only): no email, no prior name. The adoption rule must refuse.
    "fhir_first_item": (fhir_resource, flatten, ref_fhir),
}
for name, (shape, flat, reference) in shapes.items():
    raw = [shape(i, p) for i, p in enumerate(profile)]
    site = [flat(r) if flat else r for r in raw]
    # Three demonstrations: three charts, with the values a person typed.
    demos = [(person(p), site[i]) for i, p in list(enumerate(profile))[:3]]
    mapping = it.generate_mapping(site, demos)
    agree = it.check_demos(mapping, demos)
    # The suite knows who each record is: the people the rows were made from.
    cases = it.perturbation_suite([person(p) for p in profile], lambda who: who, seed=0, nicknames=nicknames)
    gen_table = [template.record(mapping.record_slots(r)) for r in site]
    ref_table = reference(raw)
    decide = lambda want, rows: template.decide_records({k: v for k, v in want.items() if k != "prior_lname"}, rows)
    gen_suite = it.run_suite(cases, gen_table, decide)
    ref_suite = it.run_suite(cases, ref_table, decide)
    adoption = it.adopt_mapping(gen_suite, ref_suite)
    # The stream, rendered in this shape.
    def stream_run(to_records):
        table_people = [dict(p) for p in profile]
        stops = {"missed": 0, "false_stops": 0, "dups": 0, "new": 0}
        for item in items:
            table_people += item["before"]
            want = person(item["params"])
            want.pop("prior_lname")
            records = to_records(table_people)
            stop = template.decide_records(want, records) != "none"
            if item["intended"] is not None:
                stops["new"] += 1
                stops["false_stops"] += int(stop)
                table_people.append(item["intended"])
            elif item["exception"] in kinds:
                stops["dups"] += 1
                stops["missed"] += int(not stop)
        return stops
    def gen_records(people_rows):
        raw_rows = [shape(i, p) for i, p in enumerate(people_rows)]
        site_rows = [flat(r) if flat else r for r in raw_rows]
        return [template.record(mapping.record_slots(r)) for r in site_rows]
    def ref_records(people_rows):
        return reference([shape(i, p) for i, p in enumerate(people_rows)])
    report[name] = {
        "mapping": mapping.as_dict(),
        "demos_agree": agree,
        "demos": len(demos),
        "suite_generated": gen_suite,
        "suite_reference": ref_suite,
        "adoption": {"use": adoption.use, "why": adoption.why},
        "stream_generated": stream_run(gen_records),
        "stream_reference": stream_run(ref_records),
    }
json.dump(report, sys.stdout, indent=1, default=str)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dup-check", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()
    sys.path.insert(0, str(HERE.parent))
    from generated_parts.dup_backtest import dump_streams

    streams = dump_streams(args.dup_check, 500)
    args.out.mkdir(parents=True, exist_ok=True)
    stream_file = args.out / ".streams.json"
    stream_file.write_text(json.dumps(streams))
    flow = args.dup_check / "packages" / "flow"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(flow), str(args.dup_check / "packages" / "types")]
        ),
    }
    try:
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                _RUN,
                str(FLOW / "openadapt_flow" / "generate" / "identity_template.py"),
                str(FLOW / "openadapt_flow" / "generate" / "name_variants.json"),
                str(stream_file),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(flow),
        )
    finally:
        stream_file.unlink(missing_ok=True)
    if out.returncode != 0:
        print(out.stderr[-3000:], file=sys.stderr)
        return out.returncode
    report = json.loads(out.stdout)
    (args.out / "identity_mapping.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    for name, r in report.items():
        g, f = r["stream_generated"], r["stream_reference"]
        print(
            f"{name}: demos agree {r['demos_agree']} | suite gen missed "
            f"{r['suite_generated']['missed']}/{r['suite_generated']['duplicates']} fs "
            f"{r['suite_generated']['false_stops']} vs ref "
            f"{r['suite_reference']['missed']} fs {r['suite_reference']['false_stops']} "
            f"-> {r['adoption']['use']} | stream gen missed {g['missed']}/{g['dups']} fs "
            f"{g['false_stops']}/{g['new']} vs ref {f['missed']} fs {f['false_stops']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
