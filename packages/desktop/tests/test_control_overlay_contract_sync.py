from scripts.sync_control_overlay_contract import assert_control_overlay_contract_synced


def test_generated_control_overlay_contract_matches_pinned_types() -> None:
    assert_control_overlay_contract_synced()
