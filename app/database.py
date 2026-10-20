"""Backward-compat shim: `from app.database import ...` still works."""
from app.core.database import Base, DATABASE_URL, SessionLocal, engine

__all__ = ["Base", "DATABASE_URL", "SessionLocal", "engine"]
