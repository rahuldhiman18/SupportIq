# SupportIq

AI-powered customer-support query engine with exactly-once queue processing, JWT refresh-token rotation, idempotent email outbox, and hybrid inline/queued AI answering backed by Google Gemini.

> **Status:** Local development ready — Docker Compose stack (api + Postgres + Mailpit) runs locally. Not yet deployed to AWS.
> **Team:** Solo build (full stack: API, DB models, queue worker, auth, tests).
> **Docs:** This README.

## Table of Contents

1. [Overview](#overview)
2. [Roadmap — Now vs Later](#roadmap)
3. [System Architecture](#system-architecture)
4. [Folder Structure](#folder-structure)
5. [Scale — Endpoints, Tables, Migrations](#scale)
6. [Tests](#tests)
7. [Speed & Performance](#speed)
8. [RAG & Knowledge Base](#rag)
9. [AI Tool Calling & Human Handoff](#tool-calling)
10. [Redis & Caching](#redis)
11. [Gemini Integration](#gemini)
12. [Getting Started](#getting-started)
13. [API Reference](#api-reference)
14. [Environment Variables](#environment-variables)
15. [Deployment & Status](#deployment-status)
16. [License](#license)

## Overview

SupportIq is a FastAPI backend that lets customers submit support questions, gets AI-generated answers grounded in your knowledge base and live order data, and falls back to a human queue when the AI can't resolve an issue. Key design decisions:

| Decision | Rationale |
|----------|-----------|
| Exactly-once queue (FOR UPDATE SKIP LOCKED) | Prevents duplicate AI calls and double billing on Gemini |
| Idempotent email outbox | Same idempotency key never produces two emails |
| JWT refresh-token rotation | Strict family-kill on reuse (theft detection) |
| Inline vs Queued mode | Flip QUERY_MODE env var without code change |
| Stdlib-only auth | HS256 JWT + PBKDF2 passwords, no extra dependencies |
| SQL-based grounding (no vector DB yet) | Keeps Phase A dependency-free; pgvector is Phase B |

## Roadmap — Now vs Later

| Area | Now (this release) | Later (planned) |
|------|-------------------|-----------------|
| Query serving | inline (AI inside HTTP request, default) | queued production smoke test |
| Rate limiting | In-memory stdlib (30 req / IP / min) | Redis-backed distributed limiter |
| Caching | None | Redis query/response cache |
| RAG | SQL-based policy + live order facts | Embeddings + vector search (pgvector) |
| AI accuracy | Manual review + pytest suite | Automated RAG eval benchmark |
| Deployment | Docker Compose (local) | AWS ECS / Fargate + CI/CD |
| Auth | JWT access + refresh (stdlib HS256) | httpOnly cookies + CSRF (frontend work) |
| Email | Mailpit / SMTP outbox (idempotent) | Real SMTP credentials + delivery metrics |
| Knowledge versions | Draft → current (single FK per query) | Approval flow + multi-version publishing |

## System Architecture

```mermaid
graph TD
    Client[Customer / Dashboard] -->|HTTP| API[FastAPI App]
    API --> Auth[Auth Layer<br/>Admin key + JWT]
    API --> Q[Query Router]
    Q -->|inline mode| AI[Gemini AI Service]
    Q -->|queued mode| Queue[(support_queries table)]
    Queue --> Worker[Queue Worker<br/>run_forever loop]
    Worker --> AI
    Worker --> Mail[Email Outbox<br/>process_outbox]
    AI --> KB[Knowledge Base<br/>SQL-based]
    AI --> Orders[Orders Table<br/>live facts]
    Mail --> SMTP[SMTP / Mailpit]
    DB[(PostgreSQL)]:::db

    classDef db fill:#f9f,stroke:#333,stroke-width:2px;
```

**Request flow (queued mode):**

```mermaid
sequenceDiagram
    participant C as Customer
    participant API as FastAPI
    participant DB as PostgreSQL
    participant W as Worker
    participant G as Gemini
    participant M as Mailpit/SMTP

    C->>API: POST /queries (202 Accepted)
    API->>DB: INSERT support_queries (status=queued)
    API-->>C: query_id returned
    loop Poll every 1s
        W->>DB: CLAIM row FOR UPDATE SKIP LOCKED
        W->>G: answer_customer_question_once
        G-->>W: answer text
        W->>DB: UPDATE status=resolved
        W->>M: queue_email (idempotent)
        M->>C: Delivery email
    end

```

## Folder Structure

```
supportiq/
├── alembic/
│   └── env.py                      # Alembic migration environment
├── app/
│   ├── main.py                     # Thin shell: FastAPI app, routers, worker lifecycle
│   ├── core/
│   │   ├── __init__.py
│   │   ├── database.py             # SQLAlchemy engine, SessionLocal, Base
│   │   ├── deps.py                 # get_db, throttle, QUERY_MODE flag
│   │   └── security.py             # Admin key, JWT, PBKDF2, refresh rotation
│   │
│   ├── api/
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── auth.py             # login / refresh / logout
│   │       ├── health.py           # /healthz + /admin/queue-stats
│   │       ├── knowledge.py        # knowledge CRUD (admin)
│   │       ├── orders.py           # order create + track
│   │       └── queries.py          # create / read / answer / feedback / admin feed
│   │
│   ├── models/
│   │   └── tables.py               # 10 SQLAlchemy ORM tables
│   │
│   ├── schemas/
│   │   └── dto.py                  # Pydantic request/response schemas
│   │
│   └── services/
│       ├── ai_service.py           # Gemini client (typed errors, retry, 10s timeout)
│       ├── mail.py                 # Idempotent outbox + SMTP delivery
│       ├── prompt_context.py       # Grounds AI in KB + live order facts
│       └── worker.py               # Claim/drain loop, state machine, reaper
├── tests/
│   ├── conftest.py
│   ├── test_env_example.py
│   ├── test_health.py
│   └── test_queries.py

## Scale — Endpoints, Tables, Migrations

| Layer | Count | Details |
|-------|-------|---------|
| API routers | 5 | auth, health, knowledge, orders, queries |
| API endpoints | 9 | See [API Reference](#api-reference) |
| DB tables | 10 | users, refresh_tokens, support_queries, support_answers, knowledge_answers, knowledge_answer_versions, email_outbox, feedback, orders, order_events |
| Alembic migrations | 1 | Base schema (initial) |

## Tests

| Metric | Value |
|--------|-------|
| Test files | 9 (`conftest.py` + 8 test modules) |
| Passing | 100% |
| Runner | `pytest` |

## Speed & Performance

| Metric | Target | Measured |
|--------|--------|----------|
| POST /queries (inline, excl. AI) | <100ms | — |
| Gemini call (hard cap) | 10s timeout | — |
| Queue claim + process | ~11s worst case | — |
| Worker poll interval | 1s | — |
| Rate limit | 30 POST /queries per IP / min | In-memory (stdlib) |
| DB ping (`/healthz`) | 2s short timeout | — |
| Worker reaper threshold | 60s stale `processing` | — |

## RAG & Knowledge Base

| Component | Status | Detail |
|-----------|--------|--------|
| Knowledge tables | Implemented | `knowledge_answers` + `knowledge_answer_versions` (draft → current) |
| Grounding source | Live policy + order facts | `prompt_context.py` joins all *current* policy versions into one labeled block |
| Order auto-resolution | Implemented | Server matches open orders by email before the single Gemini call |
| Embeddings / vector search | Not used | SQL-based lookup (no pgvector dependency) |
| Multi-policy grounding | Implemented | All active knowledge versions are joined, not just `.first()` |
| RAG accuracy benchmark | Not yet measured | Run `pytest` + manual review of AI answers against KB |

### How grounding works (single AI call)

1. `build_prompt_context()` loads every `knowledge_answer_version` where `is_current = true`, joined to its parent `knowledge_answer` for the topic label.
2. Order facts are resolved server-side: if the customer is authenticated and has exactly one open order, its tracking facts (stage, location, ETA, stalled flag) are injected.
3. The combined policy + situation block is sent to Gemini with `temperature=0.2`, `max_output_tokens=300`.
4. The model must answer, list options, or ask for clarification — never invent policy details.

## AI Tool Calling & Human Handoff

### Capabilities (4 AI functions)

| Tool | Purpose |
|------|---------|
| Intent classification | Classifies question into 7 intent buckets (order tracking, refund, payment, etc.) |
| Answer generation | Grounded answer from KB + order facts |
| Order tracking | Live order facts (stage, location, ETA, stalled) |
| Human handoff | Escalation when AI is blocked or exhausts retries |

### Escalation state machine

```mermaid
stateDiagram-v2
    [*] --> Queued
    Queued --> Processing: claim (FOR UPDATE SKIP LOCKED)
    Processing --> Resolved: AI answers successfully
    Processing --> NeedsHuman: AIContentBlockedError (no retry)
    Processing --> Queued: retry (attempt < 3, backoff 2/4/8 min)
    Processing --> NeedsHuman: attempt 3 exhausted
    NeedsHuman --> Queued: admin requeue (POST /queries/{id}/requeue)
    NeedsHuman --> [*]: human agent answers
```

### Escalation rules

- **Max AI attempts:** 3 per query
- **Backoff:** 2 → 4 → 8 minutes (exponential between attempts)
- **AIContentBlockedError** → instant human handoff (never retried — saves Gemini quota)
- **Reaper:** reclaims stale `processing` rows (>60s) after worker crash/hang
- **Human feed:** `GET /admin/needs-human` lists queries needing agent attention
- **Requeue:** `POST /queries/{id}/requeue` returns a `processing` or `needs_human` row to `queued` for one fresh AI attempt

## Redis & Caching

| Item | Status | Detail |
|------|--------|--------|
| Redis | Not used | No Redis dependency in `requirements.txt` |
| Rate limiting | In-memory | `defaultdict` sliding window, 30 req/IP/min (stdlib-only) |
| Session store | DB | Refresh tokens in PostgreSQL (`refresh_tokens` table) |
| Query cache | None | Every AI call hits Gemini fresh |
| Planned (Phase C) | Redis | Swap in-memory rate limiter + add response cache when scaling beyond a single instance |

## Gemini Integration

| Item | Value |
|------|-------|
| Model | `gemini-3.5-flash-lite` |
| Timeout | 10s hard cap per attempt |
| Temperature | 0.2 (deterministic) |
| Max output tokens | 300 |
| Retry policy | 3 attempts, exponential jitter (0.5s–4s) |
| Retryable errors | 429, 5xx, timeouts, connection errors |
| Non-retryable | `AIContentBlockedError` (safety filter / empty answer) |
| Single-attempt variant | `answer_customer_question_once()` (queue workers) |
| JSON mode | Intent classification forces `application/json` MIME type |

## Getting Started

### Prerequisites

- Python 3.10+
- PostgreSQL (or set `DATABASE_URL` to an existing instance)
- Google Gemini API key
- Docker (optional — for the Compose stack)
- SMTP server (Mailpit works locally: `SMTP_HOST=localhost SMTP_PORT=1025`)

### Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in:

```powershell
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/supportiq
GEMINI_API_KEY=your-google-ai-studio-key
JWT_SECRET_KEY=change-this-in-production
ADMIN_API_KEY=change-this-in-production
SMTP_HOST=localhost
SMTP_PORT=1025
QUERY_MODE=inline   # or "queued" for background worker
REFRESH_TOKEN_MINUTES=30
```

### Run the API (local, no Docker)

```powershell
uvicorn app.main:app --reload
```

### Run with Docker Compose (recommended — includes Postgres + Mailpit)

```powershell
docker compose up --build
# App:        http://127.0.0.1:8765
# Mailpit UI: http://127.0.0.1:8025
# Postgres:   localhost:5433
docker compose down -v   # wipe containers + db volume (clean slate)
```

### Run the worker standalone (queued mode only)

```powershell
QUERY_MODE=queued python -m app.worker
```

### Run tests

```powershell
python -m pytest tests/ -q
```

## API Reference

### Queries

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/queries` | Create a support query (202 Accepted) | Optional JWT |
| GET | `/queries/{id}` | Get query status & answer | Optional JWT |
| GET | `/queries/{id}/answer` | Get the latest answer | Optional JWT |
| POST | `/queries/{id}/feedback` | Submit rating 1-5 + comment | Optional JWT |
| GET | `/admin/needs-human` | List queries needing human attention | Admin key |
| POST | `/queries/{id}/requeue` | Requeue a stuck/needs-human query | Admin key |

### Auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/login` | Issue access + refresh tokens |
| POST | `/auth/refresh` | Rotate refresh token |
| POST | `/auth/logout` | Revoke refresh token family |

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/healthz` | Service health (open, DB ping) |
| GET | `/admin/queue-stats` | Queue stats + worker heartbeat (admin key) |

### Knowledge

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/knowledge` | Create knowledge answer + version (draft status) |

### Orders

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/orders` | Create order + events (admin key) |
| GET | `/orders/{number}` | Order details + tracking facts |

## Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `DATABASE_URL` | — | Yes | PostgreSQL connection string (`+psycopg` driver) |
| `GEMINI_API_KEY` | — | Yes | Google AI Studio / Gemini API key |
| `JWT_SECRET_KEY` | — | Yes | HS256 signing key for access tokens |
| `ADMIN_API_KEY` | — | No (fail-closed) | Admin key for `/admin/*` endpoints |
| `SMTP_HOST` | `localhost` | No | SMTP server |
| `SMTP_PORT` | `1025` | No | SMTP port |
| `SMTP_USER` | `""` | No | SMTP username |
| `SMTP_PASSWORD` | `""` | No | SMTP password |
| `MAIL_FROM` | `support@supportiq.local` | No | From address |
| `MAIL_FROM_NAME` | `SupportIq` | No | From name |
| `QUERY_MODE` | `inline` | No | `inline` or `queued` |
| `REFRESH_TOKEN_MINUTES` | `30` | No | Refresh token lifetime (absolute, anchored to family root) |

## Deployment & Status

| Item | Status |
|------|--------|
| **Container** | Dockerfile (`python:3.12-slim`, non-root user, healthcheck on `/healthz`) |
| **Compose stack** | `docker-compose.yml`: api + `postgres:16-alpine` + Mailpit |
| **Local run** | `docker compose up --build` → app at `:8765`, Mailpit at `:8025` |
| **Healthcheck** | Container `/healthz` probes DB + worker state |
| **CI/CD** | None configured |
| **Cloud deploy** | Not yet deployed to AWS / any cloud |
| **Team size** | Solo developer (full stack) |
| **Production readiness** | Core logic complete; needs RAG eval, latency benchmark, deploy config |

## License

Private / internal use.

---

*Last updated: 2026-10-21*
