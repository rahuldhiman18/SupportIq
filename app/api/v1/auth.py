"""Auth routes: login / refresh / logout."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_db, throttle_public_chat
from app.models.tables import RefreshToken, User
from app.schemas.dto import (
    LoginRequest, LoginResponse, LogoutRequest, RefreshRequest,
    RefreshResponse,
)
from app.core.security import (
    _hash_refresh_token, _refresh_ttl_minutes, issue_access_token,
    issue_refresh_token, rotate_refresh_token, verify_password,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=LoginResponse)
def login(request: LoginRequest, http_request: Request,
          db: Session = Depends(get_db),
          _throttle: None = Depends(throttle_public_chat)):
    # Same throttle as public chat: brute-force protection, stdlib only.
    user = db.execute(
        select(User).where(
            (User.username == request.username_or_email)
            | (User.email == request.username_or_email)
        )
    ).scalars().first()
    if user is None or not user.is_active or not verify_password(
            request.password, user.password_salt, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    raw_refresh = issue_refresh_token(db, user)
    return LoginResponse(
        access_token=issue_access_token(user),
        username=user.username,
        email=user.email,
        role=user.role,
        refresh_token=raw_refresh,
        refresh_expires_in_minutes=_refresh_ttl_minutes(),
    )


@router.post("/auth/refresh", response_model=RefreshResponse)
def refresh_session(request: RefreshRequest, http_request: Request,
                    db: Session = Depends(get_db),
                    _throttle: None = Depends(throttle_public_chat)):
    # Same throttle as login: brute-forcing a refresh token costs the same.
    user, new_access, new_refresh = rotate_refresh_token(
        db, request.refresh_token)
    return RefreshResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        refresh_expires_in_minutes=_refresh_ttl_minutes(),
    )


@router.post("/auth/logout")
def logout_session(request: LogoutRequest, db: Session = Depends(get_db)):
    # Best-effort, idempotent: logging out twice still returns 200.
    row = db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == _hash_refresh_token(
                request.refresh_token))
    ).scalars().first()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return {"logged_out": True}
