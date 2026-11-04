"""Auth + security: admin key, JWT access tokens, passwords, refresh helpers.

One-way dependency: routes import from here; this module imports only
models/database (never routes, never main) -- no import cycles.
"""
import base64
import hashlib
import hmac
import json as _json
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import RefreshToken, User
from app.core.deps import get_db


# ** Phase A auth: admin key guards the critical write endpoints,
#    public chat stays open but IP-throttled (Gemini calls cost money)
api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)

# Dev fallback ONLY: tests/seed scripts run without a provisioned key.
_TEST_ADMIN_KEY = "test-admin-key"


def _expected_admin_key() -> str | None:
    configured = os.getenv("ADMIN_API_KEY")
    if configured:
        return configured
    # Offline-test fallback ONLY: pytest sets PYTEST_CURRENT_TEST while a test
    # body runs. Outside tests an unset ADMIN_API_KEY now fails CLOSED
    # (require_admin_key 401s) instead of exposing the well-known test key.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return _TEST_ADMIN_KEY
    return None


def require_admin_key(key: str | None = Depends(api_key_header)) -> None:
    expected = _expected_admin_key()
    # secrets.compare_digest raises TypeError on non-ASCII str -> compare bytes.
    if (not key or not expected
            or not secrets.compare_digest(key.encode("utf-8"),
                                          expected.encode("utf-8"))):
        raise HTTPException(status_code=401,
                            detail="Invalid or missing admin key")


# ** JWT login (Phase B): stdlib-only HS256 + PBKDF2, no new dependencies.
#    Tokens are OPTIONAL on existing routes (valid token scopes data to the
#    logged-in email; missing token keeps the current email-param behavior).
_JWT_ALGO = "HS256"
_ACCESS_TOKEN_MINUTES = 15
_TEST_JWT_SECRET = "test-jwt-secret-for-offline-tests"

bearer_scheme = HTTPBearer(auto_error=False)


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY")
    if secret:
        production = os.getenv("APP_ENV", "development").lower() in {
            "prod", "production"
        }
        if production and len(secret.encode("utf-8")) < 32:
            raise HTTPException(
                status_code=503,
                detail="JWT signing key does not meet the minimum security requirement",
            )
        return secret
    # Offline-test fallback ONLY: same fail-closed rule as the admin key --
    # a predictable secret in prod would let anyone forge admin tokens.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return _TEST_JWT_SECRET
    raise HTTPException(status_code=503,
                        detail="JWT_SECRET_KEY is not configured")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_access_token(user: "User") -> str:
    header = _b64url(_json.dumps({"alg": _JWT_ALGO, "typ": "JWT"}).encode())
    payload_claims = {
        "sub": user.username,
        "email": user.email,
        "role": user.role,
        "tenant_id": getattr(user, "tenant_id", None),
        "token_version": getattr(user, "token_version", 0),
        "jti": str(uuid4()),
        "iat": int(time.time()),
        "exp": int(time.time()) + _ACCESS_TOKEN_MINUTES * 60,
    }
    issuer = os.getenv("JWT_ISSUER")
    audience = os.getenv("JWT_AUDIENCE")
    if issuer:
        payload_claims["iss"] = issuer
    if audience:
        payload_claims["aud"] = audience
    payload = _b64url(_json.dumps(payload_claims).encode())
    sig = _b64url(hmac.new(_jwt_secret().encode(), f"{header}.{payload}".encode(),
                           hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_access_token(token: str) -> dict:
    try:
        header, payload, sig = token.split(".")
        header_claims = _json.loads(_b64url_decode(header))
        if header_claims.get("alg") != _JWT_ALGO:
            raise HTTPException(status_code=401, detail="Invalid token")
        if header_claims.get("typ") != "JWT":
            raise HTTPException(status_code=401, detail="Invalid token")
        expected = _b64url(hmac.new(
            _jwt_secret().encode(), f"{header}.{payload}".encode(),
            hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid token")
        claims = _json.loads(_b64url_decode(payload))
    except (ValueError, HTTPException):
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    exp = claims.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise HTTPException(status_code=401, detail="Token expired")
    iat = claims.get("iat")
    if iat is not None and (not isinstance(iat, int) or iat > int(time.time()) + 30):
        raise HTTPException(status_code=401, detail="Invalid token")
    if not isinstance(claims.get("sub"), str):
        raise HTTPException(status_code=401, detail="Invalid token")
    if not isinstance(claims.get("email"), str):
        raise HTTPException(status_code=401, detail="Invalid token")
    if not isinstance(claims.get("role"), str):
        raise HTTPException(status_code=401, detail="Invalid token")
    if not isinstance(claims.get("jti"), str):
        raise HTTPException(status_code=401, detail="Invalid token")
    issuer = os.getenv("JWT_ISSUER")
    if issuer and claims.get("iss") != issuer:
        raise HTTPException(status_code=401, detail="Invalid token")
    audience = os.getenv("JWT_AUDIENCE")
    if audience:
        token_audience = claims.get("aud")
        if isinstance(token_audience, list):
            audience_valid = audience in token_audience
        else:
            audience_valid = token_audience == audience
        if not audience_valid:
            raise HTTPException(status_code=401, detail="Invalid token")
    return claims


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """PBKDF2-SHA256 (210k rounds) -> (salt_hex, hash_hex). No new dependency."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt), 210_000)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, digest_hex = hash_password(password, salt)
    return hmac.compare_digest(digest_hex, expected_hash)


def current_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> dict | None:
    """Logged-in claims, or None for anonymous callers (routes stay open)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    claims = verify_access_token(credentials.credentials)
    user = db.execute(
        select(User).where(
            (User.username == claims["sub"]) | (User.email == claims["email"])
        )
    ).scalars().first()
    if (
        user is None
        or not user.is_active
        or user.email.lower() != claims["email"].lower()
        or user.username != claims["sub"]
        or user.role != claims["role"]
        or getattr(user, "token_version", 0) != claims.get("token_version", 0)
        or getattr(user, "tenant_id", None) != claims.get("tenant_id")
    ):
        raise HTTPException(status_code=401, detail="Token is no longer valid")
    return {
        **claims,
        "tenant_id": getattr(user, "tenant_id", None),
        "email": user.email,
        "role": user.role,
    }


def require_customer_identity(
    identity: dict | None = Depends(current_identity),
) -> dict | None:
    """Require a JWT for customer data operations in production."""
    production = os.getenv("APP_ENV", "production").lower() in {
        "prod", "production"
    }
    if production and (not identity or not identity.get("email")):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity

# --- Refresh tokens: opaque + DB (helpers) ----------------------------------
# Approved design: 30-min testing TTL via REFRESH_TOKEN_MINUTES (prod default
# 720 = 12h absolute), rotation on every use, reuse of a rotated token kills
# the whole family (strict mode), expiry anchored to the family root.
_DEFAULT_REFRESH_MINUTES = 720


def _refresh_ttl_minutes() -> int:
    try:
        return max(1, int(os.getenv("REFRESH_TOKEN_MINUTES",
                                    str(_DEFAULT_REFRESH_MINUTES))))
    except ValueError:
        return _DEFAULT_REFRESH_MINUTES


def _hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refresh_prefix(raw: str) -> str:
    # Log-safe prefix only -- never the raw token.
    return _hash_refresh_token(raw)[:8]


def issue_refresh_token(db: Session, user: "User") -> str:
    """Create family root T1 for a fresh login. Returns the RAW token."""
    from app.models.tables import utcnow_unique
    raw = secrets.token_urlsafe(32)
    now = utcnow_unique()
    row = RefreshToken(
        user_id=user.id,
        token_hash=_hash_refresh_token(raw),
        family_id=uuid4(),
        created_at=now,
        expires_at=now + timedelta(minutes=_refresh_ttl_minutes()),
        revoked_at=None,
        replaced_by=None,
    )
    db.add(row)
    db.commit()
    return raw


def _family_is_revoked(db: Session, family_id: UUID) -> bool:
    # Any revoked row means the family was killed (logout-all/theft/deactivate).
    hit = db.execute(
        select(RefreshToken.id).where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_not(None),
        ).limit(1)
    ).first()
    return hit is not None


def revoke_refresh_family(db: Session, family_id: UUID) -> None:
    now = datetime.now(timezone.utc)
    for row in db.execute(
        select(RefreshToken).where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        ).with_for_update()
    ).scalars().all():
        row.revoked_at = now
    db.commit()
def rotate_refresh_token(db: Session, raw: str) -> tuple["User", str, str]:
    """Consume one refresh token -> (user, new_access, new_refresh_raw).

    Raises HTTPException 401 on unknown/expired/revoked/reused. Reuse of an
    already-rotated token revokes the whole family (theft signal, strict).
    """
    from app.models.tables import utcnow_unique
    row = db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == _hash_refresh_token(raw)
        ).with_for_update()
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if row.revoked_at is not None or _family_is_revoked(db, row.family_id):
        raise HTTPException(status_code=401, detail="Refresh token revoked")
    now = datetime.now(timezone.utc)
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if now > exp:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Refresh token revoked")
    if row.replaced_by is not None:
        # REUSE: already rotated -> theft signal -> kill family, warn user.
        revoke_refresh_family(db, row.family_id)
        raise HTTPException(
            status_code=401,
            detail=("Refresh token reuse detected -- all sessions revoked. "
                    "Please log in again; if this was not you, change your "
                    "password immediately."))
    # Fresh rotation: same family, expiry anchored to the family ROOT
    # (absolute, never sliding).
    root = db.execute(
        select(RefreshToken).where(
            RefreshToken.family_id == row.family_id)
        .order_by(RefreshToken.created_at.asc()).limit(1)
    ).scalars().first()
    root_expires = root.expires_at if root is not None else row.expires_at
    new_raw = secrets.token_urlsafe(32)
    new_row = RefreshToken(
        user_id=user.id,
        token_hash=_hash_refresh_token(new_raw),
        family_id=row.family_id,
        created_at=utcnow_unique(),
        expires_at=root_expires,
        revoked_at=None,
        replaced_by=None,
    )
    db.add(new_row)
    db.flush()
    row.replaced_by = new_row.id
    db.commit()
    return user, issue_access_token(user), new_raw
