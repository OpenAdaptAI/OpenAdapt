"""One command for the whole economic comparison: every arm, one seeded stream, resumable.

    cd packages/flow/benchmark
    python -m lifecycle.arms.compare --out lifecycle/results/economics-<date> [--seed 7 --items 40 --days 10]

- Every arm runs the same seeded worklist and event ledger through the soak
  harness's ``run_arm`` (same reset, seeded charts, drift days, injected
  faults, oracle, scripted operator and cost model).
- Each arm writes its own ``<out>/<arm>/ledger.jsonl``. Rerunning the same
  command skips arms whose ledger ends with ``arm_end``. An arm that stopped
  part-way (the spend cap, a crash, Ctrl-C) is moved to
  ``<arm>.partial-<n>`` and run again from its start, because the app is reset
  per arm; the partial ledger is kept and its model spend stays counted.
- Model spend is capped across all arms at $30 (``LIFECYCLE_SPEND_CAP_USD``
  raises it only with the coordinator's go). The cap is checked before every
  model call; reaching it stops the run with a message to resume.
- The API key comes from ``ANTHROPIC_API_KEY`` or the keychain item named in
  ``LIFECYCLE_KEYCHAIN_SERVICE``; it is never printed.
- At the end, ``economics.json`` and ``REPORT.md`` are written from the
  ledgers alone (``python -m lifecycle.arms.economics <out>`` recomputes them).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import budget, economics, report

DEFAULT_ARMS = [
    # (name in the ledger, module:factory, options)
    # The soak's OpenAdapt arm: the certified referral program under the production
    # configuration. Its API tier is refused on this form (results/api_tier/).
    ("openadapt", "lifecycle.openadapt_arm:make", {}),
    (
        "cua_alone",
        "lifecycle.arms.cua:factory",
        {"model": "claude-opus-5-5", "item_budget_usd": 1.00},
    ),
    (
        "agent_tools",
        "lifecycle.arms.agent_tools:factory",
        {"model": "claude-opus-5-5", "item_budget_usd": 0.60},
    ),
    (
        "agent_script",
        "lifecycle.arms.agent_script:factory",
        {"model": "claude-opus-5-5"},
    ),
    ("human_script", "lifecycle.arms.human_script:factory", {}),
]


MIN_FREE_GIB = float(__import__("os").environ.get("LIFECYCLE_MIN_FREE_GIB", "10"))


class DiskLow(BaseException):
    """Free disk fell under the shared machine's floor; stop between items."""


def free_gib() -> float:
    return shutil.disk_usage("/System/Volumes/Data").free / 2**30


ITEM_TIMEOUT_S = float(__import__("os").environ.get("LIFECYCLE_ITEM_TIMEOUT_S", "900"))
#: After the soft timeout, how long to wait for the arm to return before the
#: whole process exits (so a hang can never hold the shared fixture for hours).
HARD_GRACE_S = 180.0


def _playwright_children() -> list[int]:
    import os
    import subprocess

    out = subprocess.run(
        ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True
    ).stdout.split()
    pids = []
    for pid in out:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", pid], capture_output=True, text=True
        ).stdout
        if "playwright" in cmd:
            pids.append(int(pid))
    return pids


class ItemWatchdog:
    """Bounds one item.

    At ``timeout_s`` it writes every thread's Python stack to ``stack_path``
    (to find what hung) and kills this process's Playwright drivers, so a
    blocked browser call raises and the runner records the item as an error.
    If the arm still has not returned ``HARD_GRACE_S`` later (a hang outside
    the browser), it exits the process with code 5; the comparison resumes
    from the arm's start and keeps the partial ledger.
    """

    def __init__(self, timeout_s: float, stack_path: Path) -> None:
        import threading

        self.fired = False
        self.stack_path = stack_path
        self._soft = threading.Timer(timeout_s, self._fire)
        self._hard = threading.Timer(timeout_s + HARD_GRACE_S, self._exit)
        for t in (self._soft, self._hard):
            t.daemon = True
            t.start()

    def _dump(self, why: str) -> None:
        import faulthandler

        with open(self.stack_path, "a") as fh:
            fh.write(f"--- {why} at {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)

    def _fire(self) -> None:
        import os
        import signal

        self.fired = True
        self._dump("item timeout")
        for pid in _playwright_children():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _exit(self) -> None:
        import os

        self._dump("hard timeout; exiting")
        os._exit(5)

    def cancel(self) -> None:
        self._soft.cancel()
        self._hard.cancel()


class Guarded:
    """Any arm, checked for free disk before every item and bounded in time.

    The check runs between items, never inside the harness's app reset, so a
    stop leaves the fixture whole. DiskLow is a BaseException so the runner's
    ``except Exception`` cannot turn it into an item result.
    """

    def __init__(self, options: dict[str, Any]) -> None:
        from ..arm import load_arm

        inner_opts = dict(options)
        spec = inner_opts.pop("_inner")
        self.inner = load_arm(spec, inner_opts)
        self.name = self.inner.name

    def describe(self) -> dict[str, Any]:
        return self.inner.describe()

    def setup(self, env: Any, operator: Any) -> list[dict[str, Any]]:
        return self.inner.setup(env, operator)

    def run_item(self, item: Any, env: Any, operator: Any) -> Any:
        free = free_gib()
        if free < MIN_FREE_GIB:
            raise DiskLow(
                f"{free:.1f} GiB free, under {MIN_FREE_GIB:.0f} GiB; stopped before {item.item_id}"
            )
        dog = ItemWatchdog(ITEM_TIMEOUT_S, Path(env.workdir) / "stacks.txt")
        try:
            result = self.inner.run_item(item, env, operator)
        except Exception:
            if dog.fired:
                self._fresh_browser()
            raise
        finally:
            dog.cancel()
        if dog.fired:
            self._fresh_browser()
            result.claimed = "error"
            result.product_status = (
                f"item timed out after {ITEM_TIMEOUT_S:.0f} s; browser driver stopped. "
                + str(result.product_status)
            )[:500]
        return result

    def _fresh_browser(self) -> None:
        """The browser driver was killed: the arm starts a new browser on its next item."""

        session = getattr(self.inner, "session", None)
        if session is not None and hasattr(session, "close"):
            session.close()

    def after_drift(
        self, event: dict[str, Any], env: Any, operator: Any
    ) -> list[dict[str, Any]]:
        return self.inner.after_drift(event, env, operator)

    def close(self) -> None:
        self.inner.close()


def guarded(options: dict[str, Any]) -> Guarded:
    return Guarded(options)


def _complete(ledger: Path) -> bool:
    if not ledger.is_file():
        return False
    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    return (
        bool(lines)
        and json.loads(lines[-1]).get("type") in ("arm_end", "run_end")
        and any(json.loads(ln).get("type") == "arm_end" for ln in lines[-3:])
    )


def _move_partial(d: Path) -> Optional[Path]:
    if not d.exists():
        return None
    n = 1
    while (d.parent / f"{d.name}.partial-{n}").exists():
        n += 1
    dest = d.parent / f"{d.name}.partial-{n}"
    d.rename(dest)
    return dest


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--items", type=int, default=40)
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--existing", type=int, default=12)
    p.add_argument("--only", action="append", help="run only these arms (by name)")
    p.add_argument(
        "--arms-json", type=Path, help="a JSON list of [name, module:factory, options]"
    )
    p.add_argument("--app", default="openemr")
    p.add_argument("--report-only", action="store_true")
    p.add_argument(
        "--drift",
        action="append",
        default=None,
        metavar="KIND@SEQ",
        help="replace the drift schedule, as lifecycle.run --drift (e.g. relabel_save@26)",
    )
    args = p.parse_args(argv)

    from ..cost_model import V1
    from ..run import PROTOCOL_VERSION, Ledger, engine_commit, run_arm
    from ..workload import WORKLOAD_VERSION, generate

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    arms = json.loads(args.arms_json.read_text()) if args.arms_json else DEFAULT_ARMS
    if args.only:
        arms = [a for a in arms if a[0] in args.only]

    plan = {
        "seed": args.seed,
        "items": args.items,
        "days": args.days,
        "existing": args.existing,
        "app": args.app,
        "drift": args.drift,
        "arms": arms,
    }
    plan_path = out / "plan.json"
    if plan_path.is_file():
        old = json.loads(plan_path.read_text())
        same = {
            k: old.get(k) for k in ("seed", "items", "days", "existing", "app", "drift")
        }
        if same != {k: plan[k] for k in same}:
            raise SystemExit(
                f"{out} was started with a different stream {same}; use a new --out"
            )
    else:
        plan_path.write_text(json.dumps(plan, indent=1) + "\n")

    if not args.report_only:
        workload = generate(args.seed, args.items, args.days, args.existing)
        if args.drift is not None:
            by_seq = {i.seq: i for i in workload.items}
            workload.drift = [
                {"day": by_seq[int(seq)].day, "kind": kind, "first_seq": int(seq)}
                for kind, _, seq in (spec.partition("@") for spec in args.drift)
            ]
        wl = out / "workload.jsonl"
        text = "".join(
            json.dumps(r, sort_keys=True) + "\n" for r in workload.ledger_rows()
        )
        if wl.is_file() and wl.read_text() != text:
            raise SystemExit(
                "the seeded workload differs from the one this run started with"
            )
        wl.write_text(text)
        from ..apps import make_app

        app = None
        for name, spec, options in arms:
            d = out / name
            if _complete(d / "ledger.jsonl"):
                print(f"[{name}] already complete; skipping")
                continue
            moved = _move_partial(d)
            if moved:
                print(
                    f"[{name}] earlier attempt stopped part-way; kept as {moved.name}"
                )
            d.mkdir(parents=True)
            if app is None:
                app = make_app(args.app)
            ledger = Ledger(d / "ledger.jsonl")
            ledger.write(
                {
                    "type": "run_start",
                    "protocol": PROTOCOL_VERSION,
                    "workload": WORKLOAD_VERSION,
                    "seed": args.seed,
                    "items": args.items,
                    "days": args.days,
                    "cost_model": V1.to_dict(),
                    "engine_commit": engine_commit(),
                    "prices_as_of": budget.prices()["as_of"],
                    "spend_before_usd": budget.spent(),
                    "spend_cap_usd": budget.cap_usd(),
                    "app": app.describe(),
                }
            )
            t0 = time.time()
            try:
                if free_gib() < MIN_FREE_GIB:
                    raise DiskLow(f"{free_gib():.1f} GiB free before {name}")
                run_arm(
                    arm_spec="lifecycle.arms.compare:guarded",
                    arm_options={**options, "_inner": spec},
                    app=app,
                    workload=workload,
                    ledger=ledger,
                    out=d,
                    cost_name=V1.version,
                    stop_after=options.get("stop_after"),
                )
                ledger.write(
                    {
                        "type": "run_end",
                        "wall_s": round(time.time() - t0, 1),
                        "spend_after_usd": budget.spent(),
                    }
                )
            except budget.SpendCapReached as exc:
                ledger.write(
                    {"type": "stopped", "why": "spend_cap", "detail": str(exc)}
                )
                ledger.close()
                print(
                    f"[{name}] stopped: {exc}\nResume with the same command once the cap is raised."
                )
                return 3
            except BaseException as exc:
                # Matched by name: the guarded arm is loaded through the
                # module path, so its DiskLow is not this __main__'s class.
                if type(exc).__name__ != "DiskLow":
                    raise
                ledger.write({"type": "stopped", "why": "disk_low", "detail": str(exc)})
                ledger.close()
                print(
                    f"[{name}] stopped: {exc}\nResume with the same command when there is room."
                )
                return 4
            finally:
                ledger.close()

    rows = economics.read_rows(sorted(out.glob("*/ledger.jsonl")))
    result = economics.compute(rows)
    (out / "economics.json").write_text(
        json.dumps(result, indent=1, default=str) + "\n"
    )
    (out / "REPORT.md").write_text(report.render(result, out, plan))
    print(economics.render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
