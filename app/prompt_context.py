"""Backward-compat shim: `from app.prompt_context import ...` still works."""
from app.services.prompt_context import *  # noqa: F401,F403
