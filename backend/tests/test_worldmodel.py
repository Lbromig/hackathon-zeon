"""World model: transform composition + reparent (pick/place attach-detach)."""
import numpy as np

from core.worldmodel import Entity, EntityKind, WorldModel, from_xyz_rpy


def test_world_pose_composes_up_the_tree():
    wm = WorldModel()
    wm.add(Entity("base", EntityKind.ARM_BASE, parent="world", local=from_xyz_rpy(x=1.0)))
    wm.add(Entity("tcp", EntityKind.TCP, parent="base", local=from_xyz_rpy(y=2.0)))
    assert np.allclose(wm.world_pose("tcp")[:3, 3], [1.0, 2.0, 0.0])


def test_reparent_keeps_world_pose_fixed():
    wm = WorldModel()
    wm.add(Entity("gripper", EntityKind.TOOL, parent="world", local=from_xyz_rpy(x=1.0, z=0.5)))
    wm.add(Entity("well", EntityKind.WELL, parent="world", local=from_xyz_rpy(x=0.3)))
    wm.add(Entity("tube", EntityKind.TUBE, parent="well", local=from_xyz_rpy(z=0.02)))

    before = wm.world_pose("tube").copy()
    wm.reparent("tube", "gripper", keep_world_pose=True)

    assert wm.get("tube").parent == "gripper"
    assert np.allclose(before, wm.world_pose("tube"))  # world pose unchanged on pick


def test_reparent_without_keep_follows_new_parent():
    wm = WorldModel()
    wm.add(Entity("gripper", EntityKind.TOOL, parent="world", local=from_xyz_rpy(x=1.0)))
    wm.add(Entity("tube", EntityKind.TUBE, parent="world", local=from_xyz_rpy(x=0.3)))

    wm.reparent("tube", "gripper", keep_world_pose=False)
    # local (x=0.3) is now measured from the gripper at x=1.0 -> world x = 1.3
    assert np.allclose(wm.world_pose("tube")[:3, 3], [1.3, 0.0, 0.0])
