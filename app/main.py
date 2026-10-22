"""SupportIq API -- thin shell.

All feature code lives in focused packages; this file only:
  - creates the FastAPI `app`
  - wires the routers (URLs unchanged)
  - owns the queued-mode worker lifecycle
  - re-exports names existing tests/scripts import from app.main

Dependency direction (no cycles):
  api/v1/* -> core, schemas, models, services
  main      -> api/v1/*, services.worker
"""
import threading

from fastapi import FastAPI

from app.api.v1 import auth as routes_auth
from app.api.v1 import health as routes_health
from app.api.v1 import knowledge as routes_knowledge
from app.api.v1 import orders as routes_orders
from app.api.v1 import queries as routes_queries
from app.core.deps import _query_mode, _rate_hits, get_db  # noqa: F401 (re-exported for tests)
from app.core.logging import setup_logging
from app.core.middleware import CorrelationMiddleware, LoggingMiddleware
from app.services.prompt_context import build_prompt_context, _extract_order_number  # noqa: F401
from app.core.security import (  # noqa: F401 (re-exported for tests/scripts)
    _ACCESS_TOKEN_MINUTES,
    _DEFAULT_REFRESH_MINUTES,
    _expected_admin_key,
    _hash_refresh_token,
    _refresh_ttl_minutes,
    _TEST_ADMIN_KEY,
    _TEST_JWT_SECRET,
    bearer_scheme,
    current_identity,
    hash_password,
    issue_access_token,
    issue_refresh_token,
    require_admin_key,
    rotate_refresh_token,
    revoke_refresh_family,
    verify_access_token,
    verify_password,
)
# Live AI name: tests patch/monkeypatch `app.main.answer_customer_question`,
# so this attribute must exist here and routes_queries resolves it at call
# time via `import app.main` (no cycle: lookup happens inside the request).
from app.api.v1.queries import answer_customer_question  # noqa: F401

# Configure structured JSON logging before anything else logs.
setup_logging()

app = FastAPI(title="SupportIq")

# Correlation + security headers middleware (outermost: runs first on request,
# last on response -- sets context vars before LoggingMiddleware captures them).
app.add_middleware(LoggingMiddleware)
# Access logging middleware (captures correlation context after it's set).
app.add_middleware(CorrelationMiddleware)

app.include_router(routes_auth.router)
app.include_router(routes_health.router)
app.include_router(routes_orders.router)
app.include_router(routes_queries.router)
app.include_router(routes_knowledge.router)


_worker_stop = threading.Event()


@app.on_event("startup")
def _start_queue_worker() -> None:
    # In-process worker thread: no new deploy unit. Scaling later stays safe
    # because the claim is exactly-once (proven live, 6 concurrent claimers).
    if _query_mode() != "queued":
        return  # inline mode: the request itself does the AI work
    from app.services import worker as _worker
    threading.Thread(
        target=_worker.run_forever, kwargs={"stop_event": _worker_stop},
        daemon=True, name="supportiq-queue-worker",
    ).start()


@app.on_event("shutdown")
def _stop_queue_worker() -> None:
    _worker_stop.set()

