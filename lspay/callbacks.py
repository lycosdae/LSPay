"""Outgoing order callbacks to merchants, in FuturePay's format."""

import logging
from datetime import timedelta
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .config import settings
from .signing import callback_sign, format_amount
from .timeutil import fmt, now_tw

log = logging.getLogger(__name__)

# Minutes to wait before attempt n+1.
BACKOFF_MINUTES = [1, 2, 5, 10, 30, 60]


def build_payload(order: m.Order) -> dict:
    merchant = order.merchant
    source_no = order.paid_from_bank_no if order.type == m.TYPE_RECEIVE else order.bank_no
    return {
        "order_id": order.order_id,
        "order_sid": order.order_sid,
        "type": str(order.type),
        "amount": format_amount(order.amount),
        "fee": format_amount(order.fee),
        "bank_no": (source_no or "")[-5:],
        "message": order.message,
        "status": str(order.status),
        "Is_finish": order.is_finish,
        "finish_time": fmt(order.finish_time),
        "sign": callback_sign(merchant.api_key, order.amount, order.order_id, merchant.sign_key),
    }


def send_callback(order: m.Order, client: httpx.Client) -> tuple[bool, str]:
    try:
        resp = client.post(
            order.callback,
            json=build_payload(order),
            headers={"ACCESSTOKEN": order.merchant.api_key},
            timeout=10,
            follow_redirects=False,
        )
        body = resp.text.strip()
        return body.lower() == "success", f"HTTP {resp.status_code}: {body[:500]}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]


def attempt(db: Session, order: m.Order, client: httpx.Client) -> bool:
    ok, response = send_callback(order, client)
    order.callback_attempts += 1
    order.callback_last_response = response
    if ok:
        order.callback_status = m.CALLBACK_OK
        order.callback_next_at = None
    elif order.callback_attempts >= settings.callback_max_attempts:
        order.callback_status = m.CALLBACK_FAILED
        order.callback_next_at = None
    else:
        delay = BACKOFF_MINUTES[min(order.callback_attempts - 1, len(BACKOFF_MINUTES) - 1)]
        order.callback_next_at = now_tw() + timedelta(minutes=delay)
    db.add(
        m.OrderEvent(
            order_id=order.id,
            source="callback",
            actor="system",
            action="callback_ok" if ok else "callback_fail",
            note=response,
        )
    )
    return ok


def process_due(db: Session, client: Optional[httpx.Client] = None) -> int:
    """Send every callback that is due. Commits after each one."""
    own = client is None
    client = client or httpx.Client()
    sent = 0
    try:
        ids = db.scalars(
            select(m.Order.id).where(
                m.Order.callback_status == m.CALLBACK_PENDING,
                m.Order.callback_next_at.is_not(None),
                m.Order.callback_next_at <= now_tw(),
            )
        ).all()
        for oid in ids:
            order = db.get(m.Order, oid)
            attempt(db, order, client)
            db.commit()
            sent += 1
    finally:
        if own:
            client.close()
    return sent


def resend(db: Session, order: m.Order) -> None:
    """Queue a callback again (admin button)."""
    if not order.is_final:
        raise ValueError("訂單尚未結束，不能補發回調")
    order.callback_status = m.CALLBACK_PENDING
    order.callback_attempts = 0
    order.callback_next_at = now_tw()
