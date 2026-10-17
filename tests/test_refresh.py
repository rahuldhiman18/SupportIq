"""Refresh tokens: opaque + DB, rotation, strict family revoke.

Proves the approved design:
- login issues an opaque refresh token alongside the 15-min access token
- refresh rotates (old dies, new issued), expiry absolute from family root
- reuse of a rotated token kills the whole family + 401 (theft signal)
- expired / revoked / unknown / inactive-user refresh all 401
- logout is idempotent; raw token is never stored (hash only)
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.main as main
from app.main import (
    issue_refresh_token,
    rotate_refresh_token,
    revoke_refresh_family,
    _hash_refresh_token,
)
from app.models import RefreshToken, User
from app.main import hash_password


def _make_user(db_session, username="ruser", password="s3cret!"):
    salt, digest = hash_password(password)
    u = User(username=username, email=f"{username}@example.com",
             password_hash=digest, password_salt=salt)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u, password


def test_login_returns_refresh_token(client, db_session):
    u, pw = _make_user(db_session)
    r = client.post("/auth/login", json={
        "username_or_email": u.username, "password": pw})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_expires_in_minutes"] >= 1
    # Raw token never stored: only its hash lives in the DB.
    rows = db_session.execute(select(RefreshToken)).scalars().all()
    assert len(rows) == 1
    assert rows[0].token_hash == _hash_refresh_token(body["refresh_token"])
    assert body["refresh_token"] not in rows[0].token_hash


def test_refresh_happy_path_rotates(client, db_session):
    u, pw = _make_user(db_session, "ruser2")
    login = client.post("/auth/login", json={
        "username_or_email": u.username, "password": pw}).json()
    r1 = client.post("/auth/refresh", json={
        "refresh_token": login["refresh_token"]})
    assert r1.status_code == 200
    body = r1.json()
    assert body["access_token"]
    assert body["refresh_token"] != login["refresh_token"]
    # Old token is dead: replaced_by points at the new row.
    old = db_session.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == _hash_refresh_token(
                login["refresh_token"]))
    ).scalars().one()
    assert old.replaced_by is not None
    # The new access token verifies.
    claims = main.verify_access_token(body["access_token"])
    assert claims["sub"] == u.username
def test_reuse_of_rotated_token_kills_family(client, db_session):
    u, pw = _make_user(db_session, "ruser3")
    login = client.post("/auth/login", json={
        "username_or_email": u.username, "password": pw}).json()
    t1 = login["refresh_token"]
    t2 = client.post("/auth/refresh", json={"refresh_token": t1}
                     ).json()["refresh_token"]
    # Attacker replays the already-rotated T1 -> family kill + warning 401.
    evil = client.post("/auth/refresh", json={"refresh_token": t1})
    assert evil.status_code == 401
    assert "reuse" in evil.json()["detail"].lower()
    assert "log in again" in evil.json()["detail"]
    # Even the FRESH T2 is now dead: strict mode logs out everywhere.
    dead = client.post("/auth/refresh", json={"refresh_token": t2})
    assert dead.status_code == 401
    assert dead.json()["detail"] == "Refresh token revoked"


def test_unknown_refresh_token_401(client):
    r = client.post("/auth/refresh", json={"refresh_token": "nope-not-real"})
    assert r.status_code == 401


def test_expired_refresh_token_401(client, db_session):
    u, pw = _make_user(db_session, "ruser4")
    raw = issue_refresh_token(db_session, u)
    row = db_session.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == _hash_refresh_token(raw))
    ).scalars().one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    r = client.post("/auth/refresh", json={"refresh_token": raw})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_inactive_user_cannot_refresh(client, db_session):
    u, pw = _make_user(db_session, "ruser5")
    login = client.post("/auth/login", json={
        "username_or_email": u.username, "password": pw}).json()
    u.is_active = False
    db_session.commit()
    r = client.post("/auth/refresh", json={
        "refresh_token": login["refresh_token"]})
    assert r.status_code == 401


def test_logout_revokes_and_is_idempotent(client, db_session):
    u, pw = _make_user(db_session, "ruser6")
    login = client.post("/auth/login", json={
        "username_or_email": u.username, "password": pw}).json()
    t = login["refresh_token"]
    assert client.post("/auth/logout", json={"refresh_token": t}
                       ).status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": t}
                       ).status_code == 401
    # Second logout still 200 (idempotent, best-effort).
    assert client.post("/auth/logout", json={"refresh_token": t}
                       ).status_code == 200
    # Unknown token logout is also 200, not 404 (no oracle).
    assert client.post("/auth/logout", json={"refresh_token": "ghost"}
                       ).status_code == 200


def test_expiry_is_absolute_not_sliding(client, db_session):
    """Two rotations later, expiry still equals the family root's."""
    u, pw = _make_user(db_session, "ruser7")
    login = client.post("/auth/login", json={
        "username_or_email": u.username, "password": pw}).json()
    t2 = client.post("/auth/refresh", json={"refresh_token": login["refresh_token"]}
                     ).json()["refresh_token"]
    t3 = client.post("/auth/refresh", json={"refresh_token": t2}
                     ).json()["refresh_token"]
    rows = {r.token_hash: r for r in
            db_session.execute(select(RefreshToken)).scalars().all()}
    root_exp = rows[_hash_refresh_token(login["refresh_token"])].expires_at
    assert rows[_hash_refresh_token(t3)].expires_at == root_exp


def test_refresh_endpoint_is_throttled(client, db_session):
    u, pw = _make_user(db_session, "ruser8")
    for _ in range(30):
        client.post("/auth/refresh", json={"refresh_token": "junk"})
    r = client.post("/auth/refresh", json={"refresh_token": "junk"})
    assert r.status_code == 429
