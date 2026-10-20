"""Backward-compat shim: `from app.deps import ...` still works."""
from app.core.deps import get_db, throttle_public_chat

__all__ = ["get_db", "throttle_public_chat"]
