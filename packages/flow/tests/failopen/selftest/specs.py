"""Self-test: the harness flags each historical bug and passes its fix.

Each pair runs one spec, written against the bug class, on the vendored
pre-fix code (``_v_*_pre.py``, it must be flagged) and on the post-fix code
(``_v_*_post.py``, or main itself; it must not be). The vendored files carry
their commit and line range in their first lines. ``PAIRS`` maps a bug to
``(pre spec id, post spec id or None, the mutators expected to catch it)``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from tests.failopen.harness import (
    CodeCase,
    CorpusCase,
    Knob,
    Seed,
    SiteSpec,
    key,
    register,
)
from tests.failopen.mutators import load_corpus
from tests.failopen.selftest import (
    _v_s1_receipt_post,
    _v_s1_receipt_pre,
    _v_s2_audit_post,
    _v_s2_audit_pre,
    _v_s3_undo_post,
    _v_s3_undo_pre,
    _v_s4_form_judge_post,
    _v_s4_form_judge_pre,
    _v_s5_gate_head,
    _v_s5_gate_pre,
    _v_s6_typeahead_post,
    _v_s6_typeahead_pre,
    _v_s7_cancel_post,
    _v_s7_cancel_pre,
    _v_s8_governed_post,
    _v_s8_governed_pre,
    _v_s9_verify_run_pre,
    _v_wire_tap_post,
    _v_wire_tap_pre,
)
from tests.failopen.sites import governed_safety, verify_run_folds, wire_tap_relay

PAIRS: dict[str, tuple[str, Optional[str], frozenset[str]]] = {}


def pair(
    name: str,
    make: Callable[[Any, str], SiteSpec],
    pre: Any,
    post: Any,
    catches: set[str],
    post_id: Optional[str] = None,
) -> None:
    pre_id = f"selftest.{name}.pre"
    register(make(pre, pre_id))
    if post is not None:
        post_id = f"selftest.{name}.post"
        register(make(post, post_id))
    PAIRS[name] = (pre_id, post_id, frozenset(catches))


# -- S1 receipt tally (#2394, fixed by adef5a431) ----------------------------------


def receipt_spec(mod: Any, id: str) -> SiteSpec:
    ok, bad = {"effect_verified": True}, {"effect_verified": False}
    return SiteSpec(
        id=id,
        classes="C1",
        target="openadapt_flow.receipt_record:build_statement",
        kind="selftest",
        run=lambda ev: mod.outcome_of(ev),
        is_pass=lambda out: out == "VERIFIED",
        seeds=lambda: [
            Seed({"execution_outcome": "VERIFIED", "results": [ok, ok]}, True),
            Seed({"execution_outcome": "VERIFIED", "results": [ok, bad]}, False),
        ],
        items=key("results"),
        bad_item=lambda _r: dict(bad),
        inflate=lambda ev, n: {**ev, "results": [ok] * (n - 1) + [bad]},
        limit=256,
        vacuous="a run with no writes keeps its own outcome; the receipt never upgrades it",
    )


pair("S1_receipt_tally", receipt_spec, _v_s1_receipt_pre, _v_s1_receipt_post, {"M2"})


# -- S2 audit read stops early (#2437, fixed by de5ee3db2) -------------------------


def audit_spec(reader: Any, id: str) -> SiteSpec:
    def run(ev: dict[str, Any]) -> Any:
        log = [SimpleNamespace(**e) for e in ev["events"]]
        log.append(SimpleNamespace(id="c1", at=100, actor="me"))
        cursor = SimpleNamespace(ids={"c1"}, at=100)
        return reader(log, ev["page_size"]).after(cursor)

    own = {"id": "own1", "at": 110, "actor": "me"}
    stale = {"id": "s1", "at": 50, "actor": "me"}  # another node's clock
    return SiteSpec(
        id=id,
        classes="C1",
        target="openadapt_flow.txplan.audit:AuditReader.after",
        kind="selftest",
        run=run,
        is_pass=lambda w: len(w.events) == 1 and w.other_actors == 0,
        seeds=lambda: [
            Seed({"events": [own, stale], "page_size": 2}, True),
            Seed({"events": [own, {**own, "id": "own2"}], "page_size": 2}, False),
        ],
        items=key("events"),
        bad_item=lambda e: {**e, "id": e["id"] + "-dup"},
        inflate=lambda ev, n: {
            **ev,
            "events": [own, stale]
            + [{"id": f"x{i}", "at": 105, "actor": "other"} for i in range(n - 2)],
        },
    )


pair(
    "S2_audit_short_read",
    audit_spec,
    _v_s2_audit_pre.PreReader,
    _v_s2_audit_post.PostReader,
    {"M2", "M7"},
)


# -- S3 undo counted as rolled back (#2428, fixed by fda539c78) --------------------

UNDO_CODES = (
    "no_undo",
    "undo_refuted",
    "frame_differs",
    "changed_since_apply",
    "undo_failed",
    "unresolved",
)


def undo_spec(mod: Any, id: str, unlisted: bool = False) -> SiteSpec:
    def ev(*codes: str) -> dict[str, Any]:
        return {"per_write": {f"w{i}": c for i, c in enumerate(codes)}, "leftover": []}

    return SiteSpec(
        id=id,
        classes="C3",
        target="openadapt_flow.txplan.engine:undo_plan",
        kind="selftest",
        run=lambda e: mod.undo_outcome(
            dict(e["per_write"]), list(e["leftover"]), {}, [], {}
        ),
        is_pass=lambda out: out == "ROLLED_BACK",
        seeds=lambda: [
            Seed(ev("undone", "undone"), True),
            Seed(ev("undone", "undo_refuted"), False),
        ],
        codes=lambda: [
            CodeCase(
                ev(*prefix, code),
                True,
                lambda _o: False,
                f"{code} after {prefix}",
                unknown=True,
            )
            for code in (*UNDO_CODES, *(("a_code_no_list_names",) if unlisted else ()))
            for prefix in ((), ("undone",))
        ],
        notes={
            "codes": "every per-write code undo_plan emits that is not 'undone'; the "
            "neutral ones (cancelled, already_undone, not_this_plan, "
            "irreversible_released) are left out"
        },
    )


pair("S3_undo_no_undo", undo_spec, _v_s3_undo_pre, _v_s3_undo_post, {"M14"})
# The fix lists no_undo but keeps the deny-list: a per-write code nobody
# listed still settles as ROLLED_BACK. Pre-only until undo_plan adopts
# from_code (reported to the #2428 owner).
pair(
    "S3_undo_unlisted_code",
    lambda m, i: undo_spec(m, i, unlisted=True),
    _v_s3_undo_post,
    None,
    {"M14"},
)


# -- S4 written field with no verdict (#2429, fixed by fa26f69fc) ------------------


def form_judge_spec(mod: Any, id: str) -> SiteSpec:
    def run(ev: dict[str, Any]) -> Any:
        return mod.judge(ev["bindings"], dict(ev["facts"]), ev["screen"], ev["written"])

    good = {
        "bindings": [{"key": "name"}, {"key": "middle"}],
        "facts": [["name", "Ada"], ["middle", ""]],
        "screen": {"name": "Ada", "middle": ""},
        "written": ["name", "middle"],
    }
    return SiteSpec(
        id=id,
        classes="C1",
        target="openadapt_flow.form_tx.judge:SummaryJudge.evaluate",
        kind="selftest",
        run=run,
        is_pass=lambda out: out is True,
        seeds=lambda: [
            Seed(good, True),
            Seed({**good, "screen": {"name": "Ada", "middle": "Q"}}, False),
        ],
        items=key("facts"),
        plan=key("written"),
        bad_item=lambda f: [f[0], "Zed"],
    )


pair(
    "S4_form_written_no_fact",
    form_judge_spec,
    _v_s4_form_judge_pre,
    _v_s4_form_judge_post,
    {"M1"},
)


# -- S5 agent-test gate (#2419) -----------------------------------------------------

RATES = (
    "silent_corruption",
    "false_success",
    "completed_correctly",
    "safe_stop",
    "collateral_edit",
)


def _summary(scored: int) -> dict[str, Any]:
    rates = {k: {"count": 0, "of": scored, "rate": 0.0} for k in RATES}
    return {"scored": scored, "rates": rates}


def _meta(repetitions: int) -> dict[str, Any]:
    return {
        "app": "app",
        "app_pack": {"digest": "a"},
        "scenarios": {"digest": "s"},
        "images": ["img"],
        "agent": "agent",
        "repetitions": repetitions,
    }


def _gate_ev(scored: int, base_scored: int = 20) -> dict[str, Any]:
    """Twenty runs planned: two scenarios, ten repetitions."""
    base = {
        "schema": "openadapt.agent-test.baseline/v1",
        "app": "app",
        "app_pack_digest": "a",
        "scenarios_digest": "s",
        "images": ["img"],
        "agent": "agent",
        "repetitions": 10,
        "scored": base_scored,
        "rates": _summary(base_scored)["rates"],
    }
    return {
        "results": {"meta": _meta(10), "summary": _summary(scored)},
        "baseline": base,
    }


def gate_spec(mod: Any, id: str) -> SiteSpec:
    def call(ev: dict[str, Any], min_scored: Any = None) -> Any:
        with tempfile.TemporaryDirectory() as tmp:
            results, base = Path(tmp, "results.json"), Path(tmp, "baseline.json")
            results.write_text(json.dumps(ev["results"]))
            base.write_text(json.dumps(ev["baseline"]))
            kwargs = {} if min_scored is None else {"min_scored": min_scored}
            return mod.compare(results, base, **kwargs)

    return SiteSpec(
        id=id,
        classes="C4",
        target="openadapt_flow.agent_test.gate:compare",
        kind="selftest",
        run=call,
        is_pass=lambda out: out["verdict"] == "pass",
        seeds=lambda: [
            Seed(_gate_ev(20), True, "all 20 planned runs scored"),
            Seed(_gate_ev(1, 1), False, "1 of 20 planned runs scored"),
        ],
        knob=Knob("min_scored", None, (20,), call),
        relabel=lambda ev: _gate_ev(18),
    )


pair("S5_gate_min_scored", gate_spec, _v_s5_gate_pre, None, {"seed", "M9"})
pair("S5_gate_head", gate_spec, _v_s5_gate_head, None, {"seed"})


# -- S6 type-ahead leading part (#2448, fixed by c329c57dc) ------------------------

ADDRESS = "2210 Juniper Loop Apt 4, Brackenfield, OR"


def typeahead_spec(mod: Any, id: str, typed_kw: bool) -> SiteSpec:
    def run(ev: dict[str, Any]) -> Any:
        ctrl = SimpleNamespace(checked=None, current_value=ev["shown"], options=[])
        binding = SimpleNamespace(
            control_kind=mod.ControlKind.PORTAL_COMBOBOX,
            typeahead_query_chars=4,
            codec=None,
        )
        kwargs = {"typed_query": ev["typed"]} if typed_kw else {}
        return mod._control_matches_fact(ctrl, ev["intended"], binding, **kwargs)

    def near_misses() -> list[Any]:
        rows = load_corpus("values")["near_misses"]
        return [r for r in rows if isinstance(r["observed"], str) and r["expected"]]

    return SiteSpec(
        id=id,
        classes="C5",
        target="openadapt_flow.form_tx.reconciler:_control_matches_fact",
        kind="selftest",
        run=run,
        is_pass=lambda out: out is True,
        seeds=lambda: [
            Seed({"shown": ADDRESS, "intended": ADDRESS, "typed": None}, True),
            Seed(
                {"shown": "2210 Juniper Loop", "intended": ADDRESS, "typed": "2210"},
                True,
            ),
            Seed(
                {"shown": "2210 Juniper Loop", "intended": ADDRESS, "typed": None},
                False,
                "prefilled",
            ),
        ],
        corpus=CorpusCase(
            "value",
            apply=lambda ev, r: {
                **ev,
                "shown": r["observed"],
                "intended": r["expected"],
                "typed": None,
            },
        ),
        corpus_entries=near_misses,
    )


pair(
    "S6_typeahead_prefix",
    lambda m, i: typeahead_spec(m, i, m is _v_s6_typeahead_post),
    _v_s6_typeahead_pre,
    _v_s6_typeahead_post,
    {"seed", "M11"},
)


# -- S7 cancel after the child started (#2300) --------------------------------------


def cancel_spec(mod: Any, id: str) -> SiteSpec:
    def ev(status: str, report: dict[str, Any], cancelled: bool) -> dict[str, Any]:
        return {"status": status, "report": report, "cancelled": cancelled}

    return SiteSpec(
        id=id,
        classes="C7",
        target="openadapt_flow.connector.executor:FlowExecutor.execute",
        kind="selftest",
        run=lambda e: mod.child_status(e["status"], e["report"], e["cancelled"]),
        is_pass=lambda out: out == "success",
        seeds=lambda: [
            Seed(
                ev(
                    "success", {"execution_outcome": "VERIFIED", "success": True}, False
                ),
                True,
            )
        ],
        codes=lambda: [
            CodeCase(
                ev("failed", {}, True),
                True,
                lambda o: o == "failed",
                "cancel, no report",
            ),
            CodeCase(
                ev("halt", {"execution_outcome": "HALTED"}, True),
                True,
                lambda o: o == "failed",
                "cancel after a HALTED report",
            ),
        ],
    )


pair("S7_cancel_after_start", cancel_spec, _v_s7_cancel_pre, _v_s7_cancel_post, {"M12"})


# -- S8 governed '' vs NULL (#2258, fixed by 00a55cc16) -----------------------------


def _governed(check: Any, id: str) -> SiteSpec:
    spec = governed_safety.governed_spec(check, id, "selftest")
    return spec


pair(
    "S8_governed_note",
    _governed,
    _v_s8_governed_pre.check,
    _v_s8_governed_post.check,
    {"seed", "M10"},
)


# -- wire tap '///host' (#2470) ------------------------------------------------------


def _relay(fn: Any, id: str) -> SiteSpec:
    return wire_tap_relay.relay_spec(fn, id, "selftest")


pair(
    "wire_tap_triple_slash",
    _relay,
    _v_wire_tap_pre.relay_redirect_location,
    _v_wire_tap_post.relay_redirect_location,
    {"M11"},
)


# -- S9 verify_run folds before this change (latent C2) -----------------------------

from openadapt_flow import verify_run  # noqa: E402

for _name, _factory, _pre, _post in (
    (
        "S9_aggregate_empty",
        verify_run_folds.aggregate_spec,
        _v_s9_verify_run_pre._aggregate,
        verify_run._aggregate,
    ),
    (
        "S9_observed_empty",
        verify_run_folds.observed_spec,
        _v_s9_verify_run_pre._observed,
        verify_run._observed,
    ),
    (
        "S9_readback_class_empty",
        verify_run_folds.readback_spec,
        _v_s9_verify_run_pre.readback_class,
        verify_run.readback_class,
    ),
):
    pair(
        _name,
        lambda fn, i, f=_factory: f(fn, i, "selftest"),
        _pre,
        _post,
        {"M3"} if _name != "S9_readback_class_empty" else {"M1", "M3"},
    )
