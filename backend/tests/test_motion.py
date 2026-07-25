"""Safe pick/place: waypoint ordering + the upright (anti-spill) guard."""
import numpy as np
import pytest

from core.motion import PickPlaceConfig, StepKind, UprightViolation, assert_upright, plan


def test_plan_waypoint_order_and_safe_height():
    cfg = PickPlaceConfig(safe_transit_z=0.3)
    program = plan(pick_xyz=[0.1, 0.1, 0.05], place_xyz=[0.4, 0.2, 0.06],
                   grasp_rpy=[np.pi, 0.0, 0.0], cfg=cfg)

    move_names = [s.waypoint.name for s in program if s.kind is StepKind.MOVE]
    assert move_names == [
        "safe_over_pick", "approach_above_pick", "grasp", "retreat_pick", "safe_after_pick",
        "safe_over_place", "approach_above_place", "place", "retreat_place", "safe_after_place",
    ]

    kinds = [s.kind for s in program]
    # grasp before it is registered as held; release before detach
    assert kinds.index(StepKind.GRIP_CLOSE) < kinds.index(StepKind.ATTACH)
    assert kinds.index(StepKind.GRIP_OPEN) < kinds.index(StepKind.DETACH)

    # all lateral-transit waypoints ride at the safe transit height (FR4)
    for s in program:
        if s.kind is StepKind.MOVE and s.waypoint.name.startswith("safe"):
            assert s.waypoint.xyz[2] == pytest.approx(cfg.safe_transit_z)


def test_assert_upright_allows_small_tilt():
    grasp = [np.pi, 0.0, 0.0]
    assert_upright([np.pi + np.deg2rad(5), 0.0, 0.0], grasp, max_tilt_deg=20.0)  # no raise


def test_assert_upright_rejects_flip():
    grasp = [np.pi, 0.0, 0.0]
    with pytest.raises(UprightViolation):
        assert_upright([0.0, 0.0, 0.0], grasp, max_tilt_deg=20.0)  # 180° flip
