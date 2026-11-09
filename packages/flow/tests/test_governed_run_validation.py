from openadapt_flow.validation.governed_run import run_probe


def test_governed_policy_handoff_probe_has_expected_safety_outcomes():
    report = run_probe(trials=3)

    assert report["trials_per_condition"] == 3
    assert report["summary"]["permissive_unreadable"] == {
        "correct_action": 0,
        "silent_wrong_action": 3,
        "safe_halt": 0,
        "over_halt": 0,
    }
    assert report["summary"]["governed_unreadable"] == {
        "correct_action": 0,
        "silent_wrong_action": 0,
        "safe_halt": 3,
        "over_halt": 0,
    }
    assert report["summary"]["governed_verified"] == {
        "correct_action": 3,
        "silent_wrong_action": 0,
        "safe_halt": 0,
        "over_halt": 0,
    }
