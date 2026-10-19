"""Query routes: create / read / status / answers / admin feed / requeue."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

import app.ai_service as _ai_service
from app.ai_service import (
    AITimeoutError,
    AIRateLimitError,
    AIProviderError,
    AIContentBlockedError,
)
from .deps import _query_mode, get_db, throttle_public_chat
from .models import SupportQuery, Support_Answer, compute_base_priority
# Prompt-context builder shared by the inline and queued paths (Step 3b).
from .prompt_context import build_prompt_context
from .schemas import (
    AnswerResponse, HumanAnswerRequest, QueryResponse, QueryUpdate,
    SupportRequest,
)
from .security import current_identity, require_admin_key

router = APIRouter(tags=["queries"])


# Re-exported so existing `from app.main import answer_customer_question`
# patches keep hitting the live name (tests patch app.main, which re-exports
# this module attribute -- see app/main.py shims).
def answer_customer_question(*args, **kwargs):
    return _ai_service.answer_customer_question(*args, **kwargs)


# to create  unique query_id
@router.post("/queries", status_code = 202,response_model=QueryResponse)
def create_query(request: SupportRequest, http_request: Request,
                 db = Depends(get_db),
                 _throttle: None = Depends(throttle_public_chat),
                 identity: dict | None = Depends(current_identity)):
    # Persist the request extras (Step 3b): queued workers rebuild the prompt
    # from these columns after the HTTP request is long gone. Inline mode
    # reads them through the same context builder below, so both paths
    # ground on identical inputs.
    query = SupportQuery(question= request.question,
                         customer_email=request.customer_email,
                         order_number=request.order_number,
                         purchase_date=request.purchase_date,
                         order_status=request.order_status)
    db.add(query)
    db.commit()
    db.refresh(query)

    ctx = build_prompt_context(
        db,
        question=query.question,
        customer_email=query.customer_email,
        order_number=query.order_number,
        purchase_date=query.purchase_date,
        order_status=query.order_status,
        # Agent-style resolution (approved): auto-lookup runs ONLY when the
        # JWT-verified email matches the query's email -- body email alone is
        # spoofable, so anonymous callers get a polite ask instead.
        auth_email=(identity or {}).get("email"),
    )

    if ctx.resolved_order_number and not query.order_number:
        # Auto-matched the customer's only open order: persist it so the
        # queued worker rebuilds the same facts WITHOUT a JWT at claim time.
        query.order_number = ctx.resolved_order_number
        db.commit()
        db.refresh(query)

    if ctx.knowledge_version_id is not None:
        # schema allows one FK per query: store the first as the primary one
        query.knowledge_version_id = ctx.knowledge_version_id
        db.commit()
        db.refresh(query)

    if _query_mode() == "queued":
        # ** Step 3b enqueue: NO AI call inside the request. Priority is
        #    server-derived (never client-sent): stalled carriers, verified
        #    from live order events, jump straight to CRITICAL; everything
        #    else ages in from the base tier. The in-process worker claims by
        #    effective_score with FOR UPDATE SKIP LOCKED and owns retries.
        query.status = "queued"
        query.priority = compute_base_priority(None, stalled=ctx.stalled)
        db.commit()
        db.refresh(query)
        return {
            "query_id": query.id,
            "question": query.question,
            "status": query.status,
            "created_at": query.created_at,
            "intent": query.intent,
        }

    # ---- inline (Phase A behavior, unchanged; QUERY_MODE=inline default) ----
    # !Generate an intent-aware Ai response, grounded in the knowledge base
    # Map typed AI errors to proper HTTP statuses (instead of raw 500s)
    # NOTE (split task): resolve via app.main at CALL time so existing
    # `patch("app.main.answer_customer_question")` tests keep working --
    # patching app.main replaces the attribute this lookup reads.
    import app.main as _main_module
    try:
        result = _main_module.answer_customer_question(
            query.question,
            policy_context=ctx.policy_context,
            situation=ctx.situation,
        )
    except AIRateLimitError:
        raise HTTPException(status_code=429, detail="AI is busy, please try again shortly")
    except AITimeoutError:
        raise HTTPException(status_code=504, detail="AI timed out, please try again")
    except (AIContentBlockedError, AIProviderError):
        raise HTTPException(status_code=502, detail="Could not generate an answer right now")

    generate_text = result.answer_text
    query.intent = result.intent
    db.commit()
    db.refresh(query)

    # **create object of Suppoert_Answer to create an answer linked with saved query
    answer = Support_Answer(query_id = query.id, answer_text= generate_text)
    db.add(answer)
    db.commit()
    db.refresh(answer)

    return {
    "query_id": query.id,
    "question": query.question,
    "status": query.status,
    "created_at": query.created_at,
    "intent": query.intent
}

@router.get("/queries/{query_id}",response_model=QueryResponse)
def get_query(query_id:UUID, email: str | None = None, db = Depends(get_db),
              identity: dict | None = Depends(current_identity)):
    # READ-ONLY: select only, no writes, no AI calls -- safe to poll/refresh/cached.
    # Ownership: token email wins; else ?email= must match; else legacy open
    # (keeps existing offline tests passing; Phase C will require auth).
    effective_email = (identity or {}).get("email") or email
    stmt = select(SupportQuery).where(SupportQuery.id == query_id)
    if effective_email:
        stmt = stmt.where(SupportQuery.customer_email == effective_email)
    result = db.execute(stmt)
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404,detail="Query not found")
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at,
            "intent":query.intent
            }


@router.patch("/queries/{query_id}",response_model=QueryResponse,status_code=200)
def update_query(query_id:UUID, request:QueryUpdate, db = Depends(get_db),
                 _admin: None = Depends(require_admin_key)):
    # Status transitions are staff-only. Before this check anyone holding a
    # query UUID could flip any status (needs_human -> resolved, etc.).
    result = db.execute(select(SupportQuery).where(SupportQuery.id==query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code= 404,detail="Query Not found")
    query.status = request.status
    db.commit()
    db.refresh(query)
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at,
            "intent":query.intent
            }


@router.get("/queries/{query_id}/answer", response_model=AnswerResponse)
def get_latest_answer(query_id: UUID, email: str | None = None,
                      db: Session = Depends(get_db),
                      identity: dict | None = Depends(current_identity)):
    # READ-ONLY: select only, no writes, no AI calls -- safe to poll/refresh/cached.
    # Ownership scoped like get_query: token wins, else ?email=, else legacy open.
    effective_email = (identity or {}).get("email") or email
    owner_stmt = select(SupportQuery.id).where(SupportQuery.id == query_id)
    if effective_email:
        owner_stmt = owner_stmt.where(SupportQuery.customer_email == effective_email)
    owner = db.execute(owner_stmt).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status_code=404, detail="Query not found")
    # Find answers for this query, newest first
    stmt = (
        select(Support_Answer)
        .where(Support_Answer.query_id == query_id)
        .order_by(desc(Support_Answer.created_at))
    )
    result = db.execute(stmt)
    answer = result.scalars().first()

    if not answer:
        raise HTTPException(status_code=404, detail="No answer found for this query yet.")

    return answer

@router.patch("/queries/{query_id}/answer", response_model=AnswerResponse)
def human_answer_query(query_id: UUID, request: HumanAnswerRequest,
                       db: Session = Depends(get_db),
                       _admin: None = Depends(require_admin_key)):
    # Human-in-the-loop pickup: after Option A exhausts its 3 AI attempts the
    # query sits in needs_human; an agent (admin key) posts the authoritative
    # reply here. Answer + status flip happen in ONE transaction.
    result = db.execute(select(SupportQuery).where(SupportQuery.id == query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404, detail="Query not found")
    if query.status == "closed":
        raise HTTPException(status_code=409, detail="Query is closed")

    try:
        answer = Support_Answer(
            query_id=query.id,
            answer_text=request.answer_text,
            is_selected=True,  # human reply is the authoritative answer
        )
        db.add(answer)
        query.status = "resolved"
        db.commit()
        db.refresh(answer)
    except Exception:
        db.rollback()
        raise
    return answer


@router.get("/admin/needs-human")
def list_needs_human(db: Session = Depends(get_db),
                     _admin: None = Depends(require_admin_key)):
    # Notification channel for the human loop: a dashboard polls this every
    # 5-10s and pops up "AI failed -- you need to answer" per item. Items leave
    # the feed the moment an agent answers them (status leaves needs_human), so
    # no separate read/dismiss state is needed.
    rows = db.execute(
        select(SupportQuery)
        .where(SupportQuery.status == "needs_human")
        .order_by(SupportQuery.created_at.asc())
    ).scalars().all()
    items = [
        {
            "query_id": q.id,
            "question": q.question,
            "customer_email": q.customer_email,
            "attempts": q.attempts,
            "last_error": q.last_error,
            "message": (
                f"AI failed after {q.attempts} attempt(s) -- you need to answer "
                "this query."
            ),
            "created_at": q.created_at,
        }
        for q in rows
    ]
    return {"count": len(items), "items": items}


@router.post("/queries/{query_id}/requeue", response_model=QueryResponse)
def requeue_query(query_id: UUID, db = Depends(get_db),
                  _admin: None = Depends(require_admin_key)):
    # Narrow admin recovery path (P0 design, approved): ONLY `processing`
    # (stuck rows the reaper or an operator flags) and `needs_human` (agent
    # wants one fresh AI attempt after e.g. a policy fix) may re-enter the
    # queue. resolved/closed stay final -- reopening by accident is
    # impossible by construction, which is why the PATCH status enum stays
    # narrow. attempts is preserved as the audit trail; the next worker
    # claim gives exactly one fresh live attempt (a failure at 3+ returns
    # the row to needs_human via the normal budget rule).
    result = db.execute(select(SupportQuery).where(SupportQuery.id == query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404, detail="Query not found")
    if query.status not in ("processing", "needs_human"):
        raise HTTPException(status_code=409,
                            detail=f"Cannot requeue a '{query.status}' query")
    query.status = "queued"
    query.next_retry_at = None
    query.locked_at = None
    db.commit()
    db.refresh(query)
    return {"query_id": query.id,
            "question": query.question,
            "status": query.status,
            "created_at": query.created_at,
            "intent": query.intent}
