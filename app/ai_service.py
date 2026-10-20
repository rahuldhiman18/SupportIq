"""Backward-compat shim: `from app.ai_service import ...` still works."""
from app.services.ai_service import *  # noqa: F401,F403
