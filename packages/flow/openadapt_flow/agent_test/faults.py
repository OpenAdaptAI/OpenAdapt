"""A scenario's fault, compiled into what the front door does on the save."""

from __future__ import annotations

from typing import Any

from openadapt_flow.agent_test.packs import AppPack, Scenario
from openadapt_flow.agent_test.proxy import Armed
from openadapt_flow.agent_test.store import Store


def _new_keys(store: Store, before: list[dict[str, Any]]) -> list[str]:
    seen = {r["__key"] for r in before}
    return [r["__key"] for r in store.rows() if r["__key"] not in seen]


def compile_fault(
    app: AppPack, scenario: Scenario, store: Store, before: list[dict[str, Any]]
) -> Armed:
    f = scenario.fault
    kind = scenario.kind
    a = Armed(kind=kind)
    if kind == "partial":
        a.body_set = {app.fields[name].form: "" for name in f["fields"]}
    elif kind == "wrong_value":
        a.body_set = {app.fields[name].form: v for name, v in f["set"].items()}
    elif kind == "duplicate":
        a.replay_headers = {
            str(k): str(v) for k, v in (f.get("replay_headers") or {}).items()
        }
    elif kind == "no_persist":

        def remove_new() -> str:
            keys = _new_keys(store, before)
            if not keys:
                return "failed: no new record to remove"
            for k in keys:
                store.delete(k)
            return f"removed {len(keys)} new record(s)"

        a.after_save = remove_new
    elif kind in ("collateral_loss", "collateral_edit"):
        target = f["target"]
        change = dict(f.get("set") or {})

        def touch_other() -> str:
            keys = store.find(target, before)
            if not keys:
                return "failed: the target record is not in the seed"
            for k in keys:
                if kind == "collateral_loss":
                    store.delete(k)
                else:
                    store.update(k, change)
            return f"{'deleted' if kind == 'collateral_loss' else 'changed'} {len(keys)} other record(s)"

        a.after_save = touch_other
    elif kind == "late_revert":
        change = dict(f.get("set") or {})
        delete = bool(f.get("delete"))
        a.delay_s = float(f.get("after_s", 45))

        def revert() -> str:
            keys = _new_keys(store, before)
            if not keys:
                return "failed: no new record to revert"
            for k in keys:
                if delete:
                    store.delete(k)
                else:
                    store.update(k, change)
            return f"{'deleted' if delete else 'reverted'} {len(keys)} new record(s)"

        a.later = revert
    return a
