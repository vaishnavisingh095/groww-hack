"""
FastAPI application entry point.

Phase 1 scope: app wiring + a health endpoint only. No watchlist/
checkpoint/change-event routes yet — those depend on business logic
(Phase 2+) that doesn't exist yet, and adding routes ahead of the logic
they'd call would mean either stub handlers or logic-in-routes, both of
which the engineering rules for this project rule out.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.connection import get_database, close_client
from app.db.indexes import ensure_indexes
from app.routes.watchlist import router as watchlist_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Index creation is idempotent (create_index on an existing index
    # with the same spec is a no-op), so running this on every startup
    # is safe and means indexes are never accidentally missing after a
    # fresh deploy.
    ensure_indexes(get_database())
    yield
    close_client()


app = FastAPI(title="Groww Smart Watchlist", lifespan=lifespan)

# CORS: the React dev server runs on a different origin (typically
# localhost:5173 for Vite) than the FastAPI backend (localhost:8000).
# Without this, the browser blocks the frontend's fetch() calls entirely.
# In production, the deployed frontend (Vercel) and backend (Render) are
# two different real origins for the same reason -- so the deployed
# frontend origin is added alongside the local dev ones, not in place of
# them, since both need to keep working.
#
# allow_credentials=True is required for the browser to send/accept the
# anonymous owner cookie (see app/services/identity.py) across this
# origin split. This must never be combined with a wildcard
# allow_origins=["*"] -- browsers reject that combination outright, and
# it would be a real misconfiguration if this list is ever widened
# further; keep it an explicit list of real origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://smart-market-watchlist-six.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(watchlist_router)


@app.get("/health")
def health_check() -> dict:
    """
    Basic liveness check. Deliberately does NOT check MongoDB
    connectivity here — a health check that depends on a database call
    can itself become a source of false failures (e.g., a slow query
    makes an otherwise-healthy app report unhealthy). If a DB-aware
    readiness check becomes necessary later, it should be a separate
    endpoint, not a change to this one's meaning.
    """
    return {"status": "ok"}
