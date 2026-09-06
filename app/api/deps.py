import hmac

from fastapi import Header, HTTPException, status

from app.config.settings import settings


def require_internal_auth(x_internal_auth: str = Header(default="")) -> None:
    """Shared-secret auth for every route in this service - callers are
    Django/Nest, never end users directly (see ARCHITECTURE.md). Uses the
    same header name Nest already uses for its own internal-caller auth
    (X-Internal-Auth, see backend/Nestjs's internal-signing.ts) for
    naming consistency, but deliberately NOT that file's full HMAC +
    timestamp + nonce replay-protection scheme: that scheme hashes the
    entire request body to sign it, which doesn't fit a chunked binary
    upload PATCH (the whole point of tus is never needing to buffer/hash
    a large body up front). A bare shared secret is the right scope for
    "only Django/Nest may call this," matching what's actually scaffolded
    in settings.py (a single internal_token string, no signing keys).
    """
    if not settings.internal_token:
        # Unconfigured is a deploy-config error, not "auth disabled" - fail
        # closed rather than silently accepting every request.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is not configured with an internal token.",
        )
    if not x_internal_auth or not hmac.compare_digest(x_internal_auth, settings.internal_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing internal auth token.")
