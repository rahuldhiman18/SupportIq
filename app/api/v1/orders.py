"""Order routes: create + track."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models.tables import Order, OrderEvent
from app.schemas.dto import OrderCreate, OrderTrackResponse
from app.core.security import current_identity, require_admin_key

router = APIRouter(tags=["orders"])


@router.post("/orders", status_code=201)
def create_order(request: OrderCreate, db: Session = Depends(get_db),
                 _admin: None = Depends(require_admin_key)):
    order = Order(
        order_number=request.order_number.strip().upper(),
        customer_email=request.customer_email,
        delivery_method=request.delivery_method,
        destination_city=request.destination_city,
        carrier=request.carrier,
    )
    db.add(order)
    try:
        db.flush()
        for event in request.events:
            db.add(OrderEvent(order_id=order.id, stage=event.stage,
                              location=event.location,
                              occurred_at=event.occurred_at))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Order number already exists")
    return {"order_number": order.order_number, "status": "created",
            "events": len(request.events)}


@router.get("/orders/{order_number}", response_model=OrderTrackResponse)
def get_order(order_number: str, email: str | None = None,
              db: Session = Depends(get_db),
              identity: dict | None = Depends(current_identity)):
    # Token wins over ?email: a logged-in user can only see their own orders.
    # Anonymous callers must pass ?email= (existing behavior, still open).
    effective_email = (identity or {}).get("email") or email
    if not effective_email:
        raise HTTPException(status_code=401,
                            detail="Login or pass ?email= to view this order")
    # Scoped by email: wrong/missing email = 404, never leak other orders
    order = db.execute(
        select(Order).where(
            Order.order_number == order_number.strip().upper(),
            Order.customer_email == effective_email,
        )
    ).scalars().first()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    earliest = latest = None
    if order.current_stage != "delivered":
        earliest, latest = order.eta_window()
    return OrderTrackResponse(
        order_number=order.order_number,
        stage=order.current_stage,
        current_location=order.current_location,
        eta_earliest=earliest,
        eta_latest=latest,
        stalled=order.is_stalled(),
        delivery_method=order.delivery_method,
        destination_city=order.destination_city,
    )
