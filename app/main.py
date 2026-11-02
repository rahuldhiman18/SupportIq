"""SupportIq API -- thin shell.

All feature code lives in focused packages; this file only:
  - creates the FastAPI `app`
  - wires the routers (URLs unchanged)
  - composes the HTTP application and routers
  - re-exports names existing tests/scripts import from app.main

Dependency direction (no cycles):
  api/v1/* -> core, schemas, models, services
  main      -> api/v1/*, services.worker
"""
from fastapi import FastAPI

from app.api.v1 import auth as routes_auth
from app.api.v1 import health as routes_health
from app.api.v1 import knowledge as routes_knowledge
from app.api.v1 import orders as routes_orders
from app.api.v1 import queries as routes_queries
from app.core.deps import _query_mode, _rate_hits, get_db  # noqa: F401 (re-exported for tests)
from app.core.logging import setup_logging
from app.core.middleware import (
    CorrelationMiddleware,
    LoggingMiddleware,
    TimeoutMiddleware,
)
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
# Timeout middleware: outermost, wraps everything with 30s hard cap -> 504.
app.add_middleware(TimeoutMiddleware)

app.include_router(routes_auth.router)
app.include_router(routes_health.router)
app.include_router(routes_orders.router)
app.include_router(routes_queries.router)
app.include_router(routes_knowledge.router)


# Queue execution intentionally does not live in the HTTP composition root.
# The standalone worker service owns app.services.worker.run_forever. This
# keeps API lifecycle, queue execution, and deployment scaling independent.

