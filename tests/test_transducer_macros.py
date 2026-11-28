from __future__ import annotations

import threading

import pytest

from openadapt.transducer_macros import (
    CompiledCommitMacro,
    MacroStore,
    NoProgressDetector,
    distill_verified_trace,
    instantiate_macro,
)

PAYLOAD = {"title": "Fix leak", "body": "Pipe leaks in unit 4", "ref_code": "R-77"}


def _trace():
    return [
        {"action": "type", "selector": "#t", "value": "Fix leak"},
        {
            "action": "type",
            "selector": "#b",
            "value": "Details: Pipe leaks in unit 4 (R-77)",
        },
        {"action": "click", "selector": "#x", "value": "", "ok": False},
        {"action": "type", "selector": "#c", "value": "constant"},
        {"action": "click", "selector": "#noop", "value": "", "noop": True},
        {"action": "click", "selector": "#go", "value": "", "is_commit": True},
    ]


def _macro():
    m = distill_verified_trace(_trace(), PAYLOAD, "/items/new")
    assert m is not None
    return m


def test_distill_slots_and_strips_failures():
    m = _macro()
    assert [s.selector for s in m.steps] == ["#t", "#b", "#c", "#go"]
    assert m.steps[0].value_template == "{title}" and m.steps[0].slot_name == "title"
    assert m.steps[1].value_template == "Details: {body} ({ref_code})"
    assert m.steps[2].value_template == "constant" and m.steps[2].slot_name is None
    assert set(m.slot_names) == {"title", "body", "ref_code"}
    assert m.steps[-1].is_commit


def test_distill_rejects_unusable_traces():
    assert distill_verified_trace([], PAYLOAD) is None
    no_commit = [{"action": "type", "selector": "#t", "value": "Fix leak"}]
    assert distill_verified_trace(no_commit, PAYLOAD) is None
    no_slot = [{"action": "click", "selector": "#g", "is_commit": True}]
    assert distill_verified_trace(no_slot, PAYLOAD) is None


def test_distill_dedupes_repeats_and_escapes_braces():
    trace = [
        {"action": "type", "selector": "#t", "value": "{Fix leak}"},
        {"action": "type", "selector": "#t", "value": "{Fix leak}"},
        {"action": "click", "selector": "#g", "is_commit": True},
    ]
    m = distill_verified_trace(trace, PAYLOAD)
    assert m and len(m.steps) == 2
    assert m.steps[0].value_template == "{{{title}}}"
    out = instantiate_macro(m, {"title": "Other"})
    assert out[0]["value"] == "{Other}"


def test_instantiate_binds_and_marks_source():
    out = instantiate_macro(_macro(), {"title": "T2", "body": "B2", "ref_code": "R-1"})
    assert [s["value"] for s in out] == ["T2", "Details: B2 (R-1)", "constant", ""]
    assert all(s["source"] == "compiled_macro" for s in out)
    assert out[-1]["is_commit"]


def test_instantiate_missing_slot_returns_empty():
    assert instantiate_macro(_macro(), {"title": "T2", "body": "B2"}) == []
    assert (
        instantiate_macro(_macro(), {"title": "T2", "body": "B2", "ref_code": " "})
        == []
    )


def test_store_roundtrip_and_ephemeral_isolation(tmp_path):
    p = tmp_path / "m.json"
    s = MacroStore(p, "iid_ephemeral", run_id="r1")
    s.put(_macro(), task_id="a")
    assert s.get(task_id="a").macro_id == _macro().macro_id
    assert s.get(task_id="b") is None
    # starting task b wiped task a
    assert s.get(task_id="a") is None
    s.put(_macro(), task_id="b")
    s.reset()
    assert s.get(task_id="b") is None


def test_ephemeral_isolated_per_run(tmp_path):
    p = tmp_path / "m.json"
    MacroStore(p, "iid_ephemeral", run_id="r1").put(_macro(), task_id="a")
    assert MacroStore(p, "iid_ephemeral", run_id="r2").get(task_id="a") is None


def test_continual_persists_across_tasks_and_reset(tmp_path):
    p = tmp_path / "m.json"
    s = MacroStore(p, "continual_persistent", run_id="r1")
    s.put(_macro(), task_id="a", origin_path_pattern="/items/*", task_kind="create")
    s.reset()
    s2 = MacroStore(p, "continual_persistent", run_id="r2")
    got = s2.get(task_id="zzz", origin_path_pattern="/items/*", task_kind="create")
    assert isinstance(got, CompiledCommitMacro)
    assert s2.get(origin_path_pattern="/items/*", task_kind="edit") is None
    assert s2.get(origin_path_pattern="/other", task_kind="create") is None


def test_put_same_macro_accumulates_verified_count(tmp_path):
    s = MacroStore(tmp_path / "m.json", "continual_persistent")
    kw = {"origin_path_pattern": "/p", "task_kind": "k"}
    s.put(_macro(), **kw)
    s.put(_macro(), **kw)
    assert s.get(**kw).verified_count == 2
    assert s.record_macro_outcome(_macro().macro_id, True)
    assert s.get(**kw).verified_count == 3


def test_drift_failure_invalidates_and_falls_back(tmp_path):
    s = MacroStore(tmp_path / "m.json", "continual_persistent")
    kw = {"origin_path_pattern": "/p", "task_kind": "k"}
    s.put(_macro(), **kw)
    assert s.record_macro_outcome(_macro().macro_id, False) is False
    assert s.get(**kw) is None
    # a fresh verified macro replaces the invalidated one
    s.put(_macro(), **kw)
    assert s.get(**kw) is not None
    assert s.record_macro_outcome("macro-unknown", False) is False


def test_invalid_isolation_mode(tmp_path):
    with pytest.raises(ValueError):
        MacroStore(tmp_path / "m.json", "bogus")


def test_corrupt_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not json")
    s = MacroStore(p, "continual_persistent")
    assert s.get(origin_path_pattern="/p", task_kind="k") is None
    s.put(_macro(), origin_path_pattern="/p", task_kind="k")
    assert s.get(origin_path_pattern="/p", task_kind="k") is not None


def test_concurrent_puts_keep_all_entries(tmp_path):
    p = tmp_path / "m.json"

    def work(i):
        MacroStore(p, "continual_persistent").put(
            _macro(), origin_path_pattern=f"/p{i}", task_kind="k"
        )

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = MacroStore(p, "continual_persistent")
    assert all(s.get(origin_path_pattern=f"/p{i}", task_kind="k") for i in range(8))


# -- NoProgressDetector --------------------------------------------------

DOM = [
    {"tag": "input", "type": "text", "selector": "#name"},
    {"tag": "textarea", "selector": "#notes", "below_fold": True},
    {"tag": "button", "type": "submit", "selector": "#save", "in_modal": True},
    {"tag": "a", "selector": "#link"},
    {"tag": "div", "selector": "#plain"},
]


def _click(sel):
    return {"action": "click", "selector": sel}


def test_zero_delta_stall_and_blacklist():
    d = NoProgressDetector(stall_threshold=2)
    assert d.observe_step("/a", DOM, b"x", _click("#name")) is None
    assert d.observe_step("/a", DOM, b"x", _click("#dead")) is None
    plan = d.observe_step("/a", DOM, b"x", _click("#dead2"))
    assert plan and plan.is_stalled and plan.stall_count == 2
    assert "#dead" in plan.blacklisted_selectors
    assert "#dead2" in plan.blacklisted_selectors


def test_progress_resets_stall_count():
    d = NoProgressDetector(stall_threshold=2)
    d.observe_step("/a", DOM, None, _click("#a"))
    d.observe_step("/a", DOM, None, _click("#b"))
    assert d.observe_step("/b", DOM, None, _click("#c")) is None
    assert d.observe_step("/b", DOM, None, _click("#d")) is None


def test_dom_change_counts_as_progress():
    d = NoProgressDetector(stall_threshold=1)
    d.observe_step("/a", DOM, None, _click("#a"))
    changed = DOM + [{"tag": "input", "selector": "#new"}]
    assert d.observe_step("/a", changed, None, _click("#b")) is None


@pytest.mark.parametrize("observation", ["terminal_output", "text_digest"])
def test_text_only_progress_and_stall(observation):
    detector = NoProgressDetector(stall_threshold=1)
    for step in range(5):
        # Repeating commands can produce new observations on every step.
        action = {"action": "run", "value": "a" if step % 2 else "b"}
        assert (
            detector.observe_step("", [], None, action, **{observation: str(step)})
            is None
        )
    plan = detector.observe_step("", [], None, action, **{observation: "4"})
    assert plan and plan.is_stalled and plan.stall_count == 1


def test_terminal_states_can_alternate():
    detector = NoProgressDetector(stall_threshold=5)
    plan = None
    for output in ("a", "b", "a", "b"):
        plan = detector.observe_step("", [], None, None, terminal_output=output)
    assert plan and plan.is_stalled and "alternate" in plan.hint


def test_state_digest_uses_terminal_tail_alongside_browser_state():
    digest = NoProgressDetector.state_digest
    tail = "λ" * 2000
    base = digest("/a", DOM, b"x", terminal_output=tail, text_digest="first")
    assert (
        digest("/a", DOM, b"x", terminal_output="prefix" + tail, text_digest="first")
        == base
    )
    assert (
        digest("/a", DOM, b"x", terminal_output=tail + "!", text_digest="first") != base
    )
    assert digest("/a", DOM, b"x", terminal_output=tail, text_digest="second") != base
    assert digest("/a", DOM, b"y", terminal_output=tail, text_digest="first") != base
    assert digest("/b", DOM, b"x", terminal_output=tail, text_digest="first") != base
    assert digest("/a", [], b"x", terminal_output=tail, text_digest="first") != base
    assert digest("/a", DOM, b"x") == digest(
        "/a", DOM, b"x", terminal_output="", text_digest=""
    )


def test_surfaces_untried_controls_submit_first_including_below_fold():
    d = NoProgressDetector(stall_threshold=1)
    d.observe_step("/a", DOM, None, _click("#name"))
    plan = d.observe_step("/a", DOM, None, _click("#name"))
    sels = [c["selector"] for c in plan.surfaced_form_candidates]
    assert sels == ["#save", "#notes"]  # submit first; #name tried; div/link skipped
    assert not plan.switch_to_vision


def test_vision_switch_when_nothing_untried():
    d = NoProgressDetector(stall_threshold=1)
    d.observe_step("/a", [], b"s", _click("#a"))
    plan = d.observe_step("/a", [], b"s", _click("#a"))
    assert plan.surfaced_form_candidates == [] and plan.switch_to_vision


def test_cycle_detection_with_changing_state():
    d = NoProgressDetector(stall_threshold=5)
    out = None
    for i in range(4):
        url = "/a" if i % 2 == 0 else "/b"
        out = d.observe_step(url, DOM, None, _click("#toggle" + url))
    assert out and out.is_stalled and "alternate" in out.hint


def test_escalation_capped():
    d = NoProgressDetector(stall_threshold=1, max_escalations=2)
    d.observe_step("/a", DOM, None, _click("#a"))
    flags = [
        d.observe_step("/a", DOM, None, _click("#a")).escalate_effort for _ in range(4)
    ]
    assert flags == [True, True, False, False]
    d.reset()
    d.observe_step("/a", DOM, None, _click("#a"))
    assert d.observe_step("/a", DOM, None, _click("#a")).escalate_effort
