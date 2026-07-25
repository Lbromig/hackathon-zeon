"""FastAPI smoke test with a mock fleet — no hardware, no vendor SDK."""
import drivers.mock  # noqa: F401  -- registers the mock driver types
from fastapi.testclient import TestClient


def test_health_and_instruments(monkeypatch):
    from core.config import settings

    mock_fleet = [
        {"type": "mock_arm", "id": "left", "name": "Left arm"},
        {"type": "mock_arm", "id": "right", "name": "Right arm"},
        {"type": "mock_liquid_handler", "id": "ot", "name": "Opentrons"},
        {"type": "mock_camera", "id": "on_arm", "name": "On-arm cam"},
    ]
    # device_manager reads settings.fleet in the lifespan startup; patch the shared instance.
    monkeypatch.setattr(settings, "fleet", mock_fleet)

    from backend.app.main import app

    with TestClient(app) as client:  # context triggers the lifespan (load_fleet)
        assert client.get("/api/health").json() == {"ok": True}

        resp = client.get("/api/instruments")
        assert resp.status_code == 200
        ids = {d["id"] for d in resp.json()}
        assert {"left", "right", "ot", "on_arm"} <= ids
