"""SupportIq API -- thin shell (split task).

All feature code moved verbatim into focused modules; this file only:
  - creates the FastAPI `app`
  - wires the routers (URLs unchanged)
  - owns the queued-mode worker lifecycle
  - re-exports names existing tests/scripts import from app.main

Dependency direction (no cycles):
  routes_* -> deps, security, schemas, models, database, prompt_context
  main     -> routes_*, worker
"""
import threading

from fastapi import FastAPI

from . import routes_auth, routes_health, routes_knowledge, routes_orders, routes_queries
from .deps import _query_mode, _rate_hits, get_db  # noqa: F401 (re-exported for tests)
from .prompt_context import build_prompt_context, _extract_order_number  # noqa: F401
from .security import (  # noqa: F401 (re-exported for tests/scripts)
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
from .routes_queries import answer_customer_question  # noqa: F401

app = FastAPI(title="SupportIq")

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
    from . import worker as _worker
    threading.Thread(
        target=_worker.run_forever, kwargs={"stop_event": _worker_stop},
        daemon=True, name="supportiq-queue-worker",
    ).start()


@app.on_event("shutdown")
def _stop_queue_worker() -> None:
    _worker_stop.set()
