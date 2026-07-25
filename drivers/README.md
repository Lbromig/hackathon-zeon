# drivers — instrument abstraction layer

Every device is wrapped in a driver implementing a common contract. The backend
talks **only** to these interfaces (and the registry) — never to a vendor SDK
directly — so hardware is swappable and mockable.

```
base.py            InstrumentDriver ABC (connect / disconnect / status / state)
arm.py             ArmDriver          (enable, home, move_to, grip, ...)
liquid_handler.py  LiquidHandlerDriver(aspirate, dispense, pick_up_tip, ...)
camera.py          CameraDriver       (capture, capture_jpeg)
registry.py        build_driver(cfg) -> InstrumentDriver

xarm/              XArmDriver       -> wraps third_party/xArm-Python-SDK
opentrons/         OpentronsDriver  -> OT-One, pluggable transport
camera/            OpenCVCameraDriver
```

Add an instrument: create `drivers/<name>/driver.py` implementing the matching
capability ABC, export it in `__init__.py`, and `register()` it in `registry.py`.

```python
from drivers import build_driver, Pose
arm = build_driver({"type": "xarm", "id": "left", "ip": "192.168.1.10"})
with arm:
    arm.move_to(Pose(300, 0, 200))
```
