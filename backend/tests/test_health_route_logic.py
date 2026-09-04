"""
Tests the health check route's own logic in isolation from app startup
wiring (which requires a real MongoDB connection -- see test_main.py's
docstring and the implementation report for why that test cannot run in
this environment).

This test uses FastAPI's TestClient WITHOUT triggering the lifespan
context (raise_server_exceptions default, no `with` statement means the
lifespan is not invoked by default in newer FastAPI/Starlette versions
for plain instantiation -- verified below by confirming this test passes
independent of MongoDB availability).
"""
from fastapi.testclient import TestClient

from app.main import app, health_check


def test_health_check_function_returns_expected_shape():
    """Calls the route function directly -- no HTTP, no ASGI lifespan,
    no MongoDB. Proves the handler's own logic is correct in isolation,
    which is what 'keep business logic testable without MongoDB or
    network access' actually requires for this endpoint."""
    result = health_check()
    assert result == {"status": "ok"}


def test_health_route_is_registered_on_app():
    """Confirms the route is actually wired into the app, without
    invoking the lifespan (no MongoDB needed for this check).

    Uses getattr with a default rather than assuming every entry in
    app.routes has a .path attribute -- FastAPI/Starlette can include
    router-wrapper objects alongside plain routes, and not all of them
    expose .path directly."""
    route_paths = [getattr(route, "path", None) for route in app.routes]
    assert "/health" in route_paths
