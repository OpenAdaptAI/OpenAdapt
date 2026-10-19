"""``python -m openadapt_flow.interchange`` : check exchanges, run fixtures.

python -m openadapt_flow.interchange check FILE [FILE ...]
python -m openadapt_flow.interchange cases DIR
python -m openadapt_flow.interchange schema
"""

from __future__ import annotations

import argparse
import json
import sys

from openadapt_flow.interchange import check_file, json_schema, run_cases


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openadapt_flow.interchange")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check exchange files")
    c.add_argument("files", nargs="+")
    k = sub.add_parser("cases", help="run a fixture directory with cases.json")
    k.add_argument("dir")
    sub.add_parser("schema", help="print the JSON Schema")
    args = p.parse_args(argv)

    if args.cmd == "schema":
        print(json.dumps(json_schema(), indent=2))
        return 0
    if args.cmd == "check":
        bad = 0
        for f in args.files:
            vs = check_file(f)
            print(f"{'ok ' if not vs else 'FAIL'} {f}")
            for v in vs:
                print(f"     {v}")
            bad += bool(vs)
        return 1 if bad else 0
    results = run_cases(args.dir)
    for r in results:
        print(
            f"{'ok ' if r.ok else 'FAIL'} {r.file}  expect={list(r.expected)} got={list(r.got)}"
            + (f"  l1: {r.l1_mismatch}" if r.l1_mismatch else "")
        )
    failed = sum(not r.ok for r in results)
    print(f"{len(results) - failed}/{len(results)} cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
