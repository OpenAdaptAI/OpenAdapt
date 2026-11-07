"""File times of the fara run folders, for the shadow-column replay."""
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1]); out = {}
for sub in ("runs", "excluded-signed-out"):
    for row in sorted((root / "fara" / sub).glob("r*-*.json")):
        t = {"row": os.stat(row).st_mtime}
        for f in ("start.png", "end.png", "transcript.json"):
            p = row.with_suffix("") / f
            if p.exists():
                t[f] = os.stat(p).st_mtime
        out[f"fara/{sub}/{row.stem}"] = t
for m in sorted((root / "fara").glob("meta-*.json")):
    ts = m.stem.split("-", 1)[1]
    out[f"fara/meta/{ts}"] = {"row": float(ts)}
json.dump(out, sys.stdout, indent=1)
