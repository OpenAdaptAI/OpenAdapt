"""Pre-freeze task validation, without OpenAdapt.

For each workflow: on a fresh baseline with nothing done the oracle must say
MISSING; after a plain Playwright script performs one item it must say
VERIFIED. This checks the task spec and the oracle, not the product.

    PYTHONPATH=. python benchmark/onboarding_effort/validate.py tr-ticket-new ...
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

FLOW = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(FLOW))

from benchmark.onboarding_effort.hooks import open_page  # noqa: E402
from benchmark.onboarding_effort.pool.base import FIRST  # noqa: E402
from benchmark.onboarding_effort.registry import get  # noqa: E402


def validate(wf_id: str, installation: str = FIRST, shot_dir: Path | None = None) -> dict:
    wf = get(wf_id)
    items = wf.items("qual", (12 if wf.shape == "paginated" else 3) if wf.batch_run else 1, installation)
    if wf.shape == "conditional":
        seen: dict = {}
        for it in wf.items("qual", 20, installation):
            seen.setdefault(wf.branch_key(it), it)
        items = list(seen.values())
    wf.reset(installation)
    wf.prepare_items(items, installation)
    before = wf.snapshot()
    idle = [wf.judge(i, before, wf.snapshot()).verdict for i in items]
    page, close = open_page(wf, installation)
    t0 = time.time()
    err = None
    per_item: list = []
    try:
        if wf.batch_run:
            wf.start(page, installation)
            for _ in items:
                wf.perform(page, items[0])
        else:
            per_item = []
            for item in items:
                b = wf.snapshot()
                wf.start(page, installation)
                wf.perform(page, item)
                time.sleep(0.5)
                per_item.append(wf.judge(item, b, wf.snapshot()))
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:500]
        if shot_dir:
            shot_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot_dir / f"{wf_id}-{installation}.png"))
    finally:
        close()
    time.sleep(0.5)
    after = wf.snapshot()
    done = per_item if not wf.batch_run and err is None else [wf.judge(i, before, after) for i in items]
    keyed = None
    if hasattr(wf, "demonstrate_keyed"):
        kitems = wf.items("qual", 2, installation)[1:]
        wf.reset(installation)
        wf.prepare_items(kitems, installation)
        kb = wf.snapshot()
        page, close = open_page(wf, installation)
        try:
            wf.start(page, installation)
            from benchmark.onboarding_effort.pool.base import _PlainRecorder

            wf.demonstrate_keyed(_PlainRecorder(page), page, kitems[0])
            time.sleep(0.5)
            keyed = wf.judge(kitems[0], kb, wf.snapshot()).verdict
        except Exception as exc:  # noqa: BLE001
            keyed = f"error: {type(exc).__name__}: {exc}"[:300]
        finally:
            close()
    out = {
        "workflow": wf_id,
        "keyed": keyed,
        "installation": installation,
        "idle": idle,
        "performed": [v.verdict for v in done],
        "detail": [v.detail for v in done],
        "error": err,
        "seconds": round(time.time() - t0, 1),
        "ok": err is None and all(v == "MISSING" for v in idle)
        and all(v.verdict == "VERIFIED" for v in done)
        and keyed in (None, "VERIFIED"),
    }
    return out


if __name__ == "__main__":
    import os

    shots = Path(os.environ.get("OE_SCRATCH", "/tmp")) / "validate-shots"
    inst = os.environ.get("OE_INSTALLATION", FIRST)
    results = [validate(w, inst, shots) for w in sys.argv[1:]]
    for r in results:
        print(json.dumps(r, default=str)[:1500])
    raise SystemExit(0 if all(r["ok"] for r in results) else 1)
