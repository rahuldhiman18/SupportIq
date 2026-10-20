"""Backward-compat shim: `from app.mail import ...` still works."""
from app.services.mail import *  # noqa: F401,F403
