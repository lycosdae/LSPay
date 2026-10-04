"""Merchant API, wire-compatible with FuturePay v1s.

Auth headers: CLIENTSID, ACCESSTOKEN. Every call carries ``sign``
(see lspay.signing). Parameters may come as a JSON body, a form body or a
query string. Business errors are HTTP 200 with ``{"code": 400, "msg": ...}``
like FuturePay.
"""

import hmac
import json
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from .. import services, telegram
from ..config import settings
from ..db import get_db
from ..signing import verify_api_sign
from ..timeutil import fmt, parse

router = APIRouter(prefix="/v1s")

PAGE_SIZE = 100


def fail(msg: str, data: Optional[dict] = None) -> JSONResponse:
    body = {"code": 400, "msg": msg}
    if data is not None:
        body = {"data": data, **body}
    return JSONResponse(body)


async def read_params(request: Request) -> dict:
    params: dict = dict(request.query_params)
    raw = await request.body()
    if raw.strip():
        ctype = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
            form = await request.form()
            params.update({k: v for k, v in form.items() if isinstance(v, str)})
        else:
            try:
                body = json.loads(raw)
            except ValueError:
                raise services.ApiError("JSON Format Error.")
            if not isinstance(body, dict):
                raise services.ApiError("JSON Format Error.")
            params.update(body)
    return params


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def authenticate(request: Request, db: Session, params: dict) -> m.Merchant:
    sid = request.headers.get("CLIENTSID", "")
    token = request.headers.get("ACCESSTOKEN", "")
    merchant = db.scalar(select(m.Merchant).where(m.Merchant.client_sid == sid)) if sid else None
    if merchant is None or not merchant.enabled or not hmac.compare_digest(token, merchant.api_key):
        raise services.ApiError("Authentication Failed.")
    allowed = [ip.strip() for ip in (merchant.ip_whitelist or "").split(",") if ip.strip()]
    if allowed and client_ip(request) not in allowed:
        raise services.ApiError("IP Not Allowed.")
    if not verify_api_sign(params, merchant.sign_key):
        raise services.ApiError("Sign Error.")
    return merchant


@router.post("/order/receive")
async def order_receive(request: Request, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        params = await read_params(request)
        merchant = authenticate(request, db, params)
        order = services.create_receive(db, merchant, params)
        db.commit()
    except services.ApiError as exc:
        db.rollback()
        return fail(exc.msg, exc.data)
    tasks.add_task(telegram.notify_safe, order.id)
    data = {
        "order_sid": order.order_sid,
        "order_id": order.order_id,
        "amount": order.amount,
        "init_time": fmt(order.init_time),
        "fronttable_url": cashier_url(request, order),
    }
    if order.receiving_account_id:
        data.update(
            bank_code=order.bank_code,
            bank_title=order.bank_title,
            bank_name=order.bank_name,
            bank_no=order.bank_no,
        )
    return {"data": jsonable(data), "code": 200}


@router.post("/order/withdraw")
async def order_withdraw(request: Request, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        params = await read_params(request)
        merchant = authenticate(request, db, params)
        order = services.create_withdraw(db, merchant, params)
        db.commit()
    except services.ApiError as exc:
        db.rollback()
        return fail(exc.msg, exc.data)
    tasks.add_task(telegram.notify_safe, order.id)
    data = {
        "order_sid": order.order_sid,
        "order_id": order.order_id,
        "bank_title": order.bank_title,
        "bank_name": order.bank_name,
        "bank_branch": order.bank_branch,
        "bank_no": order.bank_no,
        "amount": order.amount,
        "init_time": fmt(order.init_time),
        "end_time": fmt(order.end_time),
    }
    return {"data": jsonable(data), "code": 200}


@router.api_route("/order/list", methods=["GET", "POST"])
async def order_list(request: Request, db: Session = Depends(get_db)):
    try:
        params = await read_params(request)
        merchant = authenticate(request, db, params)
        q = select(m.Order).where(m.Order.merchant_id == merchant.id)
        if params.get("order_id"):
            q = q.where(m.Order.order_id == str(params["order_id"]))
        if params.get("order_sid"):
            q = q.where(m.Order.order_sid == str(params["order_sid"]))
        try:
            start_t = parse(str(params.get("time_start") or ""))
            end_t = parse(str(params.get("time_end") or ""))
            index_start = int(params.get("index_start") or params.get("limit_start") or 0)
        except ValueError:
            raise services.ApiError("Parameter Format Error.")
        if start_t:
            q = q.where(m.Order.init_time >= start_t)
        if end_t:
            q = q.where(m.Order.init_time <= end_t)
        index_start = max(index_start, 0)
    except services.ApiError as exc:
        return fail(exc.msg, exc.data)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(m.Order.init_time.desc(), m.Order.id.desc()).offset(index_start).limit(PAGE_SIZE))
    data = [
        {
            "order_sid": o.order_sid,
            "order_id": o.order_id,
            "payer": o.payer,
            "type": o.type,
            "amount": str(o.amount),
            "fee": str(o.fee),
            "init_time": fmt(o.init_time),
            "status": o.status,
            "is_finish": o.is_finish,
            "finish_time": fmt(o.finish_time),
            "message": o.message,
            "callback": o.callback,
        }
        for o in rows
    ]
    return {"rows": total, "start": index_start, "data": data, "code": 200}


@router.api_route("/client/balance", methods=["GET", "POST"])
async def client_balance(request: Request, db: Session = Depends(get_db)):
    try:
        params = await read_params(request)
        merchant = authenticate(request, db, params)
    except services.ApiError as exc:
        return fail(exc.msg, exc.data)
    return {"code": 200, "balance": str(merchant.balance), "balance_frozen": str(merchant.balance_frozen)}


def cashier_url(request: Request, order: m.Order) -> str:
    return f"{settings.base_url}/cashier/{order.order_sid}?t={order.cashier_token}"


def jsonable(data: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()}
