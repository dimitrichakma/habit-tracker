"""Password hashing, JWT issuance/verification, and the FastAPI dependency
that resolves the authenticated user's id from a bearer token.

No internal imports beyond stdlib/fastapi/bcrypt/jwt — this is a leaf module
(main -> auth), preserving the one-directional main -> agent -> tools ->
database import rule.
"""

import hmac
import os
import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Industry-standard-ish password rules. Mirrored (duplicated, not imported)
# in src/app.py for live client-side feedback, since app.py must stay
# HTTP-only with no backend imports — keep the two lists in sync by hand.
PASSWORD_REQUIREMENTS: list[tuple[str, "re.Pattern | None", int | None]] = [
    ("At least 8 characters", None, 8),
    ("One uppercase letter (A-Z)", re.compile(r"[A-Z]"), None),
    ("One lowercase letter (a-z)", re.compile(r"[a-z]"), None),
    ("One number (0-9)", re.compile(r"[0-9]"), None),
    ("One special character (e.g. ! @ # $ %)", re.compile(r"[^A-Za-z0-9]"), None),
]


def password_requirement_status(password: str) -> list[tuple[str, bool]]:
    """Each password requirement's label paired with whether it's met."""
    results = []
    for label, pattern, min_length in PASSWORD_REQUIREMENTS:
        met = len(password) >= min_length if min_length is not None else bool(pattern.search(password))
        results.append((label, met))
    return results


def is_strong_password(password: str) -> bool:
    """True only if every rule in PASSWORD_REQUIREMENTS is met."""
    return all(met for _, met in password_requirement_status(password))


# Fail fast at import time if unset — no insecure default. Generate once with:
#   python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
JWT_ALGORITHM = "HS256"
# 24h, and deliberately no refresh-token flow: the frontend only ever holds
# the token in st.session_state (lost on a hard refresh anyway), so a second
# refresh token would be stored the exact same way and lost at the exact
# same moment — real complexity (rotation, a second endpoint) for no actual
# gain at this app's scale. See src/app.py for the frontend side of this.
JWT_EXPIRY = timedelta(hours=24)

_bearer_scheme = HTTPBearer(auto_error=True)

# Phase 6.2 — gate the otherwise-open /auth/signup on the public backend.
# When SIGNUP_SECRET is set (Railway), a signup request must present the same
# value in the X-Signup-Secret header. This is a single-user app: the one
# account normally already exists, and the secret only matters if the DB is
# ever rebuilt.
SIGNUP_SECRET = os.environ.get("SIGNUP_SECRET")

# "Does this look like a real deployment?" — same signal as
# main._environment() / scheduler._environment(). Used to decide whether an
# unset SIGNUP_SECRET means "open (local dev)" or "misconfigured (fail closed)".
_DEPLOYED = bool(
    os.environ.get("APP_ENV")
    or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
    or os.environ.get("RAILWAY_ENVIRONMENT")
)


def require_signup_allowed(x_signup_secret: str | None = Header(default=None)) -> None:
    """FastAPI dependency for POST /auth/signup.

    - SIGNUP_SECRET set → the request must carry a matching X-Signup-Secret
      header (constant-time compared), else 403.
    - SIGNUP_SECRET unset + local dev → no-op, signup open (so you can create
      the first account).
    - SIGNUP_SECRET unset + looks deployed (Railway / APP_ENV env present) →
      403. A public backend where the gate was never configured must fail
      CLOSED, not sit open to the internet. Set SIGNUP_SECRET and retry with
      the header to recover.
    """
    if SIGNUP_SECRET is None:
        if _DEPLOYED:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Signup is disabled.")
        return
    if not x_signup_secret or not hmac.compare_digest(x_signup_secret, SIGNUP_SECRET):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Signup is disabled.")


def hash_password(password: str) -> str:
    """One-way hash for storage — bcrypt salts automatically, so the same
    password hashes differently every time; never compare hashes directly,
    always go through verify_password()."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    """Check a plaintext password against a hash produced by hash_password()."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(user_id: int, username: str) -> str:
    """Sign a JWT for this user. `sub` (subject) is the standard JWT claim
    for "who this token is about" — it's what get_current_user_id reads
    back out and the only thing any auth decision is based on. `username`
    just rides along in the payload for debuggability (e.g. reading a token
    at jwt.io); nothing in this app currently decodes it back out."""
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "username": username, "iat": now, "exp": now + JWT_EXPIRY}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme)) -> int:
    """FastAPI dependency: verify the bearer JWT, return the real user id.
    Every authenticated route depends on this — never trust an id from a
    request body or path param again.
    """
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")
    return int(payload["sub"])
