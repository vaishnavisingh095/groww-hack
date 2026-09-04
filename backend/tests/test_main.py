"""
Tests for the FastAPI app wiring itself.

Uses TestClient's context-manager form so the lifespan (which calls
ensure_indexes against a real MongoClient) actually runs -- this is
deliberately an integration-style test of app startup, not a pure unit
test, since app wiring is exactly the thing that needs an end-to-end
check.

NOTE: this test requires network access to whatever MONGODB_URI resolves
to (default: localhost:27017). See the implementation report for how
this was actually verified in an environment with no MongoDB server
available.
"""
from fastapi.testclient import TestClient

from app.main import app


def test_health_check_returns_ok():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
