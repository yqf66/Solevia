from planner_api.work_item_domain import hybrid_rollout_enabled


def test_hybrid_rollout_modes_are_explicit_and_stable():
    assert not hybrid_rollout_enabled("goal-a", "OFF")
    assert hybrid_rollout_enabled("goal-a", "ALL")
    assert hybrid_rollout_enabled("goal-a", "NEW_PLANS")
    assert hybrid_rollout_enabled("goal-a", "PERCENTAGE", 0) is False
    assert hybrid_rollout_enabled("goal-a", "PERCENTAGE", 100) is True
    assert hybrid_rollout_enabled("goal-a", "PERCENTAGE", 50) == hybrid_rollout_enabled(
        "goal-a", "PERCENTAGE", 50
    )


def test_legacy_flag_remains_backward_compatible():
    assert hybrid_rollout_enabled("legacy-goal", "OFF", legacy_flag=True)
