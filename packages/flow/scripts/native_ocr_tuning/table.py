"""Collect harness JSONs under a results dir into one Markdown table."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1])
    rows = []
    for p in sorted(root.rglob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if "config" not in d or "latency" not in d:
            continue
        dn = d.get("dense", {})
        la = d.get("ladder", {})
        ho = d.get("homonyms", {})
        lat = d["latency"]
        rows.append(
            (
                dn.get("false_abort", 10**6),
                d["config"],
                p.parent.name,
                f"{dn.get('false_accept', '')}",
                f"{dn.get('false_abort', '')}/{dn.get('n', '')}",
                f"{dn.get('fa_mismatch', '')}",
                f"{dn.get('fa_abstain', '')}",
                f"{la.get('false_accept', '')}"
                + (f" ({la.get('over_halt')}/{la.get('n')} halt)" if la else ""),
                f"{ho.get('false_accept', '')}" + (f"/{ho.get('n')}" if ho else ""),
                f"{lat.get('full_frame', {}).get('mean_ms', '')}",
                f"{lat.get('crop', {}).get('mean_ms', '')}",
                d.get("finished", "")[:16],
                " ".join(f"{x:.1f}" for x in d.get("load", [])),
            )
        )
    rows.sort()
    out = [
        "| config | run | dense FAcc | dense FA | mismatch | abstain | ladder FAcc | homonym FAcc | full-frame ms | crop ms | finished (America/Toronto) | load |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r[1:]) + " |")
    (root / "TABLE.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
