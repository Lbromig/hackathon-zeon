"""The driver registry can build a mock driver from a config dict."""
import drivers.mock  # noqa: F401  -- importing registers the mock driver types
from drivers import ConnectionState, Pose, available_types, build_driver
from drivers.capabilities.arm import ArmDriver


def test_mock_types_are_registered():
    types = available_types()
    for t in ("mock_arm", "mock_camera", "mock_liquid_handler"):
        assert t in types


def test_build_and_drive_mock_arm():
    arm = build_driver({"type": "mock_arm", "id": "left", "name": "Left arm"})
    assert isinstance(arm, ArmDriver)
    assert arm.device_id == "left"

    arm.connect()
    assert arm.state == ConnectionState.CONNECTED

    arm.move_to(Pose(x=10.0, y=20.0, z=30.0))
    assert arm.get_pose().x == 10.0
    assert arm.info.kind.value == "arm"
