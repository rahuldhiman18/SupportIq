"""Backward-compat shim: `import app.worker` / `python -m app.worker` still work."""
from app.services.worker import *  # noqa: F401,F403
from app.services import worker as _worker
import sys as _sys

_sys.modules.setdefault("app.worker", _worker)
