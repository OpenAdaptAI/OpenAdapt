"""The automated onboarding engineer.

Plays the customer's engineer through the product's own teach surfaces
(record, compile, lint, qualify, induce, for-each, teach), with a fixed budget,
and logs every human-equivalent action to the ledger (see PROTOCOL.md).

    source ~/oa/scratch/onboarding-effort/env.sh
    PYTHONPATH=. python benchmark/onboarding_effort/implementer.py \
        --workflow oe-allergy-add --installation first

Exit 0 = endpoint reached; 1 = failed within budget (the ledger says where).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

FLOW = Path(__file__).resolve().parents[2]
if str(FLOW) not in sys.path:
    sys.path.insert(0, str(FLOW))

from benchmark.onboarding_effort.ledger import Ledger, Row, free_gib  # noqa: E402
from benchmark.onboarding_effort.pool.base import FIRST, SECOND  # noqa: E402

SCRATCH = Path(os.environ.get("OE_SCRATCH", Path.home() / "oa/scratch/onboarding-effort"))
HELDOUT_N = 25
MIN_FREE_GIB = 10.0


@dataclass
class Budget:
    demos: int = 5
    corrections: int = 4
    qualify_attempts: int = 3
    machine_s: float = 120 * 60
    model_usd: float = 3.0


@dataclass
class State:
    demos: int = 0
    corrections: int = 0
    qualify_attempts: int = 0
    machine_s: float = 0.0
    model_usd: float = 0.0
    bundle: Optional[Path] = None
    certified: Optional[Path] = None
    verify: Optional[Path] = None
    recordings: list[Path] = field(default_factory=list)
    stuck: Optional[dict[str, Any]] = None
    outcome: str = "running"


class OutOfBudget(Exception):
    pass


def engine_commit() -> str:
    return subprocess.run(
        ["git", "-C", str(FLOW), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def manifest_digest() -> str:
    path = Path(__file__).parent / "pool" / "MANIFEST.json"
    if not path.exists():
        return "unfrozen"
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class Implementer:
    def __init__(self, wf: Any, installation: str, out: Path, ledger: Ledger, budget: Budget):
        self.wf = wf
        self.inst = installation
        self.out = out
        self.ledger = ledger
        self.budget = budget
        self.s = State()
        out.mkdir(parents=True, exist_ok=True)
        self.identifiers: list[str] = []
        self.risk_answered: set[str] = set()
        self.last_lint = ""
        os.environ["OE_WORKFLOW"] = wf.id
        os.environ["OE_INSTALLATION"] = installation

    # -- ledger helpers ------------------------------------------------------
    def log(self, phase: str, action: str, **kw: Any) -> Row:
        row = Row(workflow=self.wf.id, installation=self.inst, phase=phase, action=action,
                  run=self.out.name, **kw)
        self.s.machine_s += row.machine_s
        self.s.model_usd += row.model_usd
        self.ledger.add(row)
        print(f"[{self.wf.id}/{self.inst}] {phase}:{action} ok={row.ok} "
              f"{round(row.machine_s)}s {row.detail.get('summary', '')}", flush=True)
        return row

    def check_budget(self) -> None:
        b, s = self.budget, self.s
        if s.demos > b.demos:
            raise OutOfBudget("demonstrations")
        if s.corrections > b.corrections:
            raise OutOfBudget("corrections")
        if s.qualify_attempts > b.qualify_attempts:
            raise OutOfBudget("qualify attempts")
        if s.machine_s > b.machine_s:
            raise OutOfBudget("machine time")
        if s.model_usd > b.model_usd:
            raise OutOfBudget("model spend")
        if free_gib() < MIN_FREE_GIB:
            raise OutOfBudget(f"disk below {MIN_FREE_GIB} GiB")

    def cli(self, phase: str, action: str, args: list[str], *, log_name: str,
            counts: Optional[dict[str, Any]] = None, timeout: float = 3600) -> tuple[int, str]:
        env = dict(os.environ, PYTHONPATH=str(FLOW), OPENADAPT_NO_LAUNCHER_CHECK="1")
        t0 = time.time()
        try:
            proc = subprocess.run(
                ["openadapt-flow", *args], cwd=FLOW, env=env, capture_output=True,
                text=True, timeout=timeout,
            )
            rc, text = proc.returncode, proc.stdout + "\n" + proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, text = 124, f"timeout after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}"
        dt = time.time() - t0
        (self.out / f"{log_name}.log").write_text(text)
        c = dict(counts or {})
        detail = dict(c.pop("detail", {}))
        detail.update({"argv": args, "rc": rc, "tail": text.strip().splitlines()[-12:],
                       "summary": f"rc={rc}"})
        self.log(phase, action, commands=1, failed_commands=0 if rc == 0 else 1,
                 machine_s=round(dt, 1), ok=rc == 0, detail=detail, **c)
        return rc, text

    # -- teach surfaces --------------------------------------------------------
    def demonstrate(self, item: dict[str, str], tag: str, *, keyed: bool = False,
                    many: Optional[list[dict[str, str]]] = None) -> Path:
        """One scripted demonstration through the product recorder.

        With ``self.identifiers`` set (the engineer marked the record's
        identifying field, as ``record --identifier FIELD`` does), clicks carry
        that field's rect.
        """
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
        from openadapt_flow.recorder import Recorder

        from benchmark.onboarding_effort.hooks import open_page

        self.s.demos += 1
        self.check_budget()
        rec_dir = self.out / f"recording-{tag}"
        if rec_dir.exists():
            shutil.rmtree(rec_dir)
        t0 = time.time()
        items = many or [item]
        self.wf.reset(self.inst)
        self.wf.prepare_items(items, self.inst)
        page, close = open_page(self.wf, self.inst)
        ok, err, events = False, "", 0
        try:
            self.wf.start(page, self.inst)
            if self.identifiers:
                from benchmark.onboarding_effort.recorder_ext import IdentifierRecorder

                rec = IdentifierRecorder(PlaywrightBackend(page), rec_dir, app_url=page.url,
                                         page=page, identifiers=self.identifiers)
            else:
                rec = Recorder(PlaywrightBackend(page), rec_dir, app_url=page.url)
            if keyed:
                self.wf.demonstrate_keyed(rec, page, item)
            else:
                for one in items:
                    self.wf.demonstrate(rec, page, one)
            rec.finish()
            events = sum(1 for _ in (rec_dir / "events.jsonl").read_text().splitlines())
            ok = True
        except Exception as exc:  # noqa: BLE001 - a failed demonstration is a result
            err = f"{type(exc).__name__}: {exc}"[:600]
        finally:
            close()
        params = len([p for p in self.wf.inputs if p in item])  # declared once per recording
        self.log("demonstrate", f"scripted demonstration {tag}", demos_scripted=1,
                 questions=params, machine_s=round(time.time() - t0, 1), ok=ok,
                 detail={"recorded_actions": events, "items": items, "keyed": keyed, "error": err,
                         "declared_params": params,
                         "summary": f"{events} actions" if ok else err[:120]})
        if not ok:
            raise RuntimeError(f"demonstration failed: {err}")
        self.s.recordings.append(rec_dir)
        return rec_dir

    def compile(self, rec: Path, tag: str) -> Optional[Path]:
        bundle = self.out / f"bundle-{tag}"
        shutil.rmtree(bundle, ignore_errors=True)
        rc, text = self.cli("compile", "compile", ["compile", str(rec), "--out", str(bundle),
                            "--name", self.wf.id, "--no-confirm-params"], log_name=f"compile-{tag}")
        if rc != 0:
            return None
        props_path = bundle / "param_proposals.json"
        if props_path.exists():
            props = json.loads(props_path.read_text()).get("proposals", [])
            # The engineer answers each flagged proposal. Declared inputs are
            # already parameters, so a proposal is a constant of the task.
            decisions = {}
            for p in props:
                name = str(p.get("name"))
                decisions[name] = {"action": "confirm"} if name in self.wf.inputs else {"action": "constant"}
            dec = self.out / f"param-decisions-{tag}.json"
            dec.write_text(json.dumps(decisions, indent=2))
            shutil.rmtree(bundle, ignore_errors=True)
            rc, _ = self.cli("compile", "answer parameter proposals",
                             ["compile", str(rec), "--out", str(bundle), "--name", self.wf.id,
                              "--params-from", str(dec)],
                             log_name=f"compile-answers-{tag}",
                             counts={"questions": len(decisions),
                                     "detail": {"decisions": decisions}})
            if rc != 0:
                return None
        return bundle

    def lint(self, bundle: Path, tag: str) -> str:
        _, text = self.cli("lint", "lint", ["lint", str(bundle)], log_name=f"lint-{tag}")
        self.last_lint = text
        return text

    def write_verify(self, previous: Optional[Path] = None) -> Path:
        """Write verify.yaml; on a second installation count only changed lines."""
        import difflib

        path = self.out / "verify.yaml"
        text = self.wf.verify_yaml(self.inst)
        lines = sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
        if previous is not None and previous.exists():
            diff = difflib.ndiff(previous.read_text().splitlines(), text.splitlines())
            lines = sum(1 for d in diff if d.startswith("+ ") and d[2:].strip())
        first_on_app = not any(
            r["phase"] == "config" and r["detail"].get("hooks_for_app") == self.wf.app
            for r in self.ledger.rows()
        )
        hook = self.wf.hook_lines() if first_on_app else 0
        path.write_text(text)
        self.log("config", "write verify.yaml", config_lines=lines + hook, ok=True,
                 detail={"verify_lines": lines, "hook_lines": hook,
                         "hooks_for_app": self.wf.app if first_on_app else None,
                         "summary": f"{lines} lines + {hook} hook lines"})
        self.s.verify = path
        return path

    def qualify(self, bundle: Path, tag: str) -> tuple[bool, str, Optional[Path]]:
        self.s.qualify_attempts += 1
        self.check_budget()
        qout = self.out / f"qualify-{tag}"
        shutil.rmtree(qout, ignore_errors=True)
        rc, text = self.cli("qualify", f"qualify --certify {self.wf.policy}",
                            ["qualify", str(bundle), "--verify", str(self.s.verify),
                             "--out", str(qout), "--certify", self.wf.policy],
                            log_name=f"qualify-{tag}", timeout=4 * 3600)
        certified = qout / "bundle"
        ok = rc == 0 and certified.exists()
        return ok, text, certified if ok else None

    # -- held-out batch ---------------------------------------------------------
    def heldout(self, certified: Path, tag: str) -> dict[str, Any]:
        """25 held-out items, each a certified run, each judged by the oracle."""
        from benchmark.onboarding_effort.heldout import run_batch

        items = self.wf.items("heldout", HELDOUT_N, self.inst)
        t0 = time.time()
        result = run_batch(self.wf, self.inst, certified, self.s.verify, items,
                           self.out / f"heldout-{tag}")
        self.log("heldout", f"held-out batch of {len(items)}", commands=1,
                 machine_s=round(time.time() - t0, 1), ok=result["all_verified"],
                 detail={k: v for k, v in result.items() if k != "items"} |
                 {"summary": f"{result['oracle_verified']}/{len(items)} oracle VERIFIED, "
                             f"{result['silent_wrong']} silent wrong"})
        return result

    # -- strategies: each returns a compiled bundle or None (stuck) -------------
    def build_single(self, tag: str, item: dict[str, str]) -> Optional[Path]:
        rec = self.demonstrate(item, tag)
        bundle = self.compile(rec, tag)
        if bundle is None:
            self.stuck("compile", self._last_tail(f"compile-{tag}"))
            return None
        self.lint(bundle, tag)
        return bundle

    def build_induce(self, tag: str, groups: list[list[dict[str, str]]], why: str) -> Optional[Path]:
        """Several demonstrations, then ``induce`` (loops or branches)."""
        recs = []
        for g, items in enumerate(groups):
            recs.append(self.demonstrate_many(items, f"{tag}-r{g + 1}"))
        prog = self.out / f"bundle-{tag}"
        rc, text = self.cli("induce", f"induce from {len(recs)} demonstrations ({why})",
                            ["induce", *map(str, recs), "--out", str(prog), "--name", self.wf.id,
                             "--held-out", "--report", str(self.out / f"induce-{tag}.json")],
                            log_name=f"induce-{tag}")
        if rc != 0 or not (prog / "workflow.json").exists():
            self.stuck("induce", text)
            return None
        self.lint(prog, tag)
        return prog

    def build_for_each(self, tag: str, items: list[dict[str, str]]) -> Optional[Path]:
        """A per-row demonstration by key, wrapped in ``for-each`` over an exported list."""
        if not hasattr(self.wf, "demonstrate_keyed"):
            self.stuck("for-each", "no keyed path to a single row in this workflow")
            return None
        rec = self.demonstrate(items[0], tag, keyed=True)
        body = self.compile(rec, tag + "-body")
        if body is None:
            self.stuck("compile", self._last_tail(f"compile-{tag}-body"))
            return None
        rows = [self.wf.worklist_row(i) for i in items]
        csv_path = self.out / f"worklist-{tag}.json"
        csv_path.write_text(json.dumps(rows, indent=2))
        self.log("config", "export the worklist from the app", commands=1, config_lines=0, ok=True,
                 detail={"rows": len(rows), "summary": f"{len(rows)} rows exported"})
        prog = self.out / f"bundle-{tag}"
        rc, text = self.cli("for-each", "for-each over the exported worklist",
                            ["for-each", str(body), "--records", str(csv_path), "--out", str(prog),
                             "--name", self.wf.id], log_name=f"for-each-{tag}")
        if rc != 0:
            self.stuck("for-each", text)
            return None
        self.lint(prog, tag)
        return prog

    def demonstrate_many(self, items: list[dict[str, str]], tag: str) -> Path:
        """One recording: a loop's iterations, or one branch's item."""
        return self.demonstrate(items[0], tag, many=items)

    def ladder(self) -> list[tuple[str, Any]]:
        demo = self.wf.items("demo", 6, self.inst)
        steps: list[tuple[str, Any]] = [
            ("one demonstration", lambda tag: self.build_single(tag, demo[0])),
        ]
        if self.wf.shape in ("loop_search", "paginated"):
            steps.append(("induce a loop from 2- and 3-iteration demonstrations",
                          lambda tag: self.build_induce(tag, [demo[0:2], demo[2:5]], "loop")))
            steps.append(("for-each over an exported worklist",
                          lambda tag: self.build_for_each(tag, demo[0:3])))
        elif self.wf.shape == "conditional":
            seen: dict[str, dict[str, str]] = {}
            for it in self.wf.items("demo", 20, self.inst):
                seen.setdefault(self.wf.branch_key(it), it)
            branches = [[it] for it in seen.values()]
            steps.append((f"induce branches from {len(branches)} demonstrations",
                          lambda tag: self.build_induce(tag, branches, "branches")))
        steps.append(("re-demonstrate another item", lambda tag: self.build_single(tag, demo[1])))
        return steps

    # -- the ladder -------------------------------------------------------------
    def run_first(self) -> str:
        try:
            for n, (name, build) in enumerate(self.ladder(), start=1):
                tag = f"a{n}"
                if n > 1:
                    self.correction(name)
                try:
                    bundle = build(tag)
                except RuntimeError as exc:
                    self.stuck("demonstrate", str(exc))
                    continue
                if bundle is None:
                    continue
                if self.s.verify is None:
                    self.write_verify()
                ok, text, certified = self.qualify_with_remedies(bundle, tag, build)
                if ok is None:
                    self.s.outcome = f"failed: blocked by the product ({text})"
                    return self.s.outcome
                if not ok:
                    self.stuck("qualify", text, remedy="next strategy")
                    continue
                result = self.heldout(certified, tag)
                if result["all_verified"]:
                    self.s.outcome = "endpoint"
                    return "endpoint"
                self.stuck("heldout", json.dumps(result.get("first_failure", {}), default=str)[:2000],
                           remedy="next strategy")
            self.s.outcome = "failed: ladder exhausted"
            return self.s.outcome
        except OutOfBudget as exc:
            self.s.outcome = f"failed: out of budget ({exc})"
            self.log("end", "out of budget", ok=False, detail={"budget": str(exc), "stuck": self.s.stuck,
                                                               "summary": str(exc)})
            return self.s.outcome

    def qualify_with_remedies(self, bundle: Path, tag: str, build: Any) -> tuple[Optional[bool], str, Optional[Path]]:
        """qualify, then apply the product's documented fix for each refusal.

        Returns (True, text, certified), (False, text, None) to move to the
        next strategy, or (None, reason, None) when the product itself blocks
        every strategy (no fix an engineer can apply).
        """
        n = 0
        while True:
            n += 1
            ok, text, certified = self.qualify(bundle, f"{tag}q{n}")
            if ok:
                return True, text, certified
            remedy = self.classify(text)
            self.stuck("qualify", text, remedy=remedy)
            if remedy == "blocked":
                return None, _blocker(text), None
            if remedy == "mark identifier":
                if self.identifiers:
                    return False, text, None
                fields = self.identifier_fields(bundle)
                if not fields:
                    return False, text, None
                self.correction("mark the record's identifying field (record --identifier)")
                self.identifiers = fields
                self.log("config", "answer: which field identifies the record", questions=1, ok=True,
                         detail={"identifiers": fields, "summary": ", ".join(fields)})
                rebuilt = build(f"{tag}i")
                if rebuilt is None:
                    return False, text, None
                bundle = rebuilt
                continue
            if remedy == "set risk":
                steps = sorted(set(re.findall(r"\[(step_\d{3})\]", self.last_lint)) - self.risk_answered)
                if not steps:
                    return False, text, None
                self.correction("classify the risk of the flagged key steps (qualify set-risk)")
                self.answer_risk(bundle, steps, tag)
                continue
            if remedy.startswith("name the save step"):
                self.correction(remedy)
                self.edit_verify(remedy, text)
                continue
            return False, text, None

    def identifier_fields(self, bundle: Path) -> list[str]:
        """The field the record's match key was typed into (from the recording)."""
        verify = self.s.verify.read_text() if self.s.verify else ""
        m = re.search(r"match:\s*\[([^\],]+)", verify) or re.search(r"match: \{(\w+):", verify)
        if not m:
            return []
        key = m.group(1).strip()
        rec = self.s.recordings[-1] if self.s.recordings else None
        if rec is None:
            return []
        events = [json.loads(x) for x in (rec / "events.jsonl").read_text().splitlines()]
        for i, e in enumerate(events):
            if e.get("kind") == "type" and e.get("param") == key:
                for prev in reversed(events[:i]):
                    sel = ((prev.get("structural") or {}).get("selector"))
                    if prev.get("kind") == "click" and sel:
                        return [sel]
        return []

    def answer_risk(self, bundle: Path, steps: list[str], tag: str) -> None:
        if not (bundle / "qualification").exists():
            self.cli("qualify", "qualify init", ["qualify", "init", str(bundle), "--target", "web"],
                     log_name=f"init-{tag}")
        for step in steps:
            self.cli("qualify", f"set-risk {step}",
                     ["qualify", "set-risk", str(bundle), "--step", step, "--classification", "read_only",
                      "--explanation", "Closes a dropdown list; changes nothing"],
                     log_name=f"set-risk-{tag}-{step}", counts={"questions": 1})
            self.risk_answered.add(step)

    def edit_verify(self, remedy: str, text: str) -> None:
        """Apply the one verify.yaml edit the product asked for."""
        if remedy.startswith("name the save step"):
            m = re.findall(r"\b(step_\d{3})\b", text)
            if m and self.s.verify is not None:
                body = self.s.verify.read_text().replace("save_step: auto", f"save_step: {m[-1]}")
                self.s.verify.write_text(body)
                self.log("config", "name the save step", config_lines=1, ok=True,
                         detail={"save_step": m[-1], "summary": m[-1]})

    def run_second(self, first_dir: Path) -> str:
        """Re-qualify the first installation's bundle on the second installation."""
        first_bundle = _latest(first_dir, "qualify-a*/bundle")
        src = _latest(first_dir, "bundle-a*")
        if first_bundle is None or src is None:
            self.s.outcome = "failed: no first-installation bundle"
            self.log("end", "no first bundle", ok=False)
            return self.s.outcome
        self.wf.reset(self.inst)  # brings the app up on the new installation
        self.write_verify(previous=first_dir / "verify.yaml")
        ok, text, certified = self.qualify(src, "zero-touch")
        if ok:
            result = self.heldout(certified, "zero-touch")
            if result["all_verified"]:
                self.s.outcome = "zero-touch"
                return "zero-touch"
            self.stuck("heldout", json.dumps(result.get("first_failure", {}))[:2000])
        else:
            self.stuck("qualify", text, remedy=self.classify(text))
        # Repair surfaces need a halted run and a fix demonstration; with no
        # repair the implementer re-teaches on the new installation.
        self.s.verify = self.out / "verify.yaml"
        outcome = self.run_first()
        self.s.outcome = "re-teach" if outcome == "endpoint" else outcome
        return self.s.outcome

    # -- failure handling ---------------------------------------------------------
    def correction(self, remedy: str) -> None:
        self.s.corrections += 1
        self.log("correct", remedy, corrections=1, ok=None, detail={"summary": remedy})
        self.check_budget()

    def stuck(self, where: str, text: str, remedy: str = "") -> None:
        reason = _reason(text)
        self.s.stuck = {"where": where, "reason": reason, "remedy": remedy}
        self.log("stuck", where, ok=False, detail={"reason": reason, "remedy": remedy,
                                                   "summary": reason[:160]})

    def classify(self, text: str) -> str:
        t = text.lower()
        # Fix what an engineer can fix first; stop only when what is left
        # needs a product change.
        if "has no armed identity check" in t and not self.identifiers:
            return "mark identifier"
        if "has no structural selector" in t:
            return "blocked"
        if "risk_review_required" in t or "qualification override" in t:
            return "set risk"
        if "save_step" in t or "no irreversible step" in t or "several irreversible" in t:
            return "name the save step in verify.yaml"
        return "re-demonstrate"

    def _last_tail(self, log_name: str) -> str:
        p = self.out / f"{log_name}.log"
        return p.read_text()[-4000:] if p.exists() else ""


def _blocker(text: str) -> str:
    for line in text.splitlines():
        if "has no structural selector" in line:
            return line.strip(" -")
    return _reason(text)


def _reason(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keys = ("refus", "halt", "error", "fail", "not ", "cannot", "can't", "missing", "no ")
    hits = [ln for ln in lines if any(k in ln.lower() for k in keys)]
    return " | ".join((hits or lines)[-4:])[:1200]


def _latest(root: Path, pattern: str) -> Optional[Path]:
    found = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def main(argv: Optional[list[str]] = None) -> int:
    from benchmark.onboarding_effort.registry import get

    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--installation", choices=[FIRST, SECOND], default=FIRST)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--ledger", type=Path, default=Path(__file__).parent / "results" / "ledger.jsonl")
    a = ap.parse_args(argv)
    wf = get(a.workflow)
    # A fresh directory every time: the engine refuses to reuse a run path
    # that once held durable authority.
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = a.out or SCRATCH / "runs" / wf.id / f"{a.installation}-{stamp}"
    ledger = Ledger(a.ledger, manifest_sha256=manifest_digest(), engine_commit=engine_commit())
    imp = Implementer(wf, a.installation, out, ledger, Budget())
    imp.log("start", "begin", ok=None, detail={"shape": wf.shape, "policy": wf.policy,
                                                "second": wf.second_installation})
    if a.installation == FIRST:
        outcome = imp.run_first()
    else:
        firsts = sorted((SCRATCH / "runs" / wf.id).glob(f"{FIRST}-*"))
        outcome = imp.run_second(firsts[-1]) if firsts else "failed: no first installation run"
    imp.log("end", outcome, ok=outcome in ("endpoint", "zero-touch", "repair-only", "re-teach"),
            detail={"stuck": imp.s.stuck, "summary": outcome})
    return 0 if outcome in ("endpoint", "zero-touch", "repair-only", "re-teach") else 1


if __name__ == "__main__":
    raise SystemExit(main())
