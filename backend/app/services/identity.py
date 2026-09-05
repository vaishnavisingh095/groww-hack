"""
Identity Service — anonymous, capability-based per-browser identity.

This is NOT authentication. There are no accounts, no passwords, no
login flow. A first-time visitor is issued a single, high-entropy,
server-generated opaque token, delivered via a persistent httpOnly
cookie. On every subsequent request, that same cookie value IS the
owner's user_id -- whoever holds the cookie holds the watchlist, the
same way a physical key or an unguessable capability URL works. There
is no separate "owners" collection and no signature/verification step:
the token's own unguessability is what makes it safe to use directly.

This mirrors the shape every existing user_id consumer already expects
(a plain opaque string) -- CheckpointService, ChangeEventService,
AttentionEngine, and watchlist_service all already take user_id as a
parameter and were never hardcoded internally. Only the VALUE fed into
those existing parameters changes; none of those modules needed to
change for this.
"""
import secrets

from fastapi import Request, Response

from app.config import settings

OWNER_COOKIE_NAME = "watchlist_owner"

# ~1 year, per the product requirement ("persistent cookie, approximately
# 1 year"). A flat expiry, not a sliding one -- the simplest thing that
# satisfies "same browser, days later" without extra renewal logic.
_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365


def generate_owner_id() -> str:
    """
    Cryptographically secure, high-entropy, unguessable opaque token.
    stdlib `secrets` (never `random`, never a timestamp or a sequential
    id) -- 32 bytes (256 bits) of randomness, URL-safe encoded so it's
    also a valid cookie value with no extra escaping needed.
    """
    return secrets.token_urlsafe(32)


def resolve_owner_id(request: Request, response: Response) -> str:
    """
    FastAPI dependency: the ONE place in the app that reads or creates
    the anonymous owner identity. Every route needing a user_id depends
    on this rather than re-deriving it, so there is exactly one place
    to get it right.

    - An existing, non-empty cookie value is trusted and returned as-is.
      There is no database lookup to "validate" it against -- the
      cookie value IS the user_id used directly wherever Checkpoint/
      ChangeEvent/Watchlist documents already key on user_id (see
      watchlist_service.get_or_create_watchlist). A garbage or
      previously-unissued value is not rejected -- it simply becomes
      its own (freshly-effective, currently-empty) owner identity on
      first use. This can never crash and can never attribute a
      request to a DIFFERENT existing owner's data, since an unissued
      value is (by construction, given the token's entropy) never
      going to collide with a real one.
    - A missing or empty cookie value is never trusted or reused -- a
      fresh token is generated and set on the response instead, with
      the full set of security attributes required for a persistent
      anonymous capability cookie (httpOnly, environment-dependent
      SameSite/Secure -- Lax/not-Secure for local HTTP dev, None/Secure
      for the deployed cross-site frontend/backend split, ~1 year,
      path=/).
    - The frontend never receives this value in a form JavaScript can
      read: httponly=True is set below, and no route in this app ever
      echoes the resolved owner_id back in a response body.
    """
    existing = request.cookies.get(OWNER_COOKIE_NAME)
    if existing:
        return existing

    owner_id = generate_owner_id()
    # Local dev: frontend and backend are both plain-HTTP localhost, so
    # SameSite=Lax + no Secure is what actually works there (Secure
    # cookies are refused outright over HTTP). Production: the deployed
    # frontend (Vercel) and backend (Render) are two different real
    # origins, so the cookie must be sent on a cross-site fetch --
    # SameSite=None is required for that, and browsers refuse to accept
    # SameSite=None at all unless Secure is also set, which is already
    # true here since both flip on together, gated by the same
    # environment check.
    is_production = settings.environment == "production"
    response.set_cookie(
        key=OWNER_COOKIE_NAME,
        value=owner_id,
        max_age=_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="none" if is_production else "lax",
        secure=is_production,
        path="/",
    )
    return owner_id
