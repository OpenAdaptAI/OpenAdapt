"""Resolve doc conflicts by keeping both sides: a line from ours whose first 40
chars match a line from theirs is replaced by theirs (the newer wording of the
same row); every other line of both sides is kept, ours order first."""
import re, sys
from pathlib import Path
p = Path(sys.argv[1]); text = p.read_text()
pat = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n", re.S | re.M)
def res(m):
    ours = m.group(1).splitlines(True); theirs = m.group(2).splitlines(True)
    used = set(); out = []
    for l in ours:
        hit = next((i for i, t in enumerate(theirs) if i not in used and t[:40] == l[:40]), None)
        if hit is None: out.append(l)
        else: out.append(theirs[hit]); used.add(hit)
    out += [t for i, t in enumerate(theirs) if i not in used]
    print(f"{p.name}: block ours={len(ours)} theirs={len(theirs)} -> {len(out)}", file=sys.stderr)
    return "".join(out)
new, n = pat.subn(res, text); p.write_text(new); print(f"{p}: {n} blocks", file=sys.stderr)
