"""Backward-compat shim: `from app.security import ...` still works."""
from app.core.security import *  # noqa: F401,F403
from app.core import security as _sec

__all__ = [n for n in dir(_sec) if not n.startswith("__")]
