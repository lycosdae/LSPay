"""Merchant backend: merchants log in with their login name to see their own
orders and, when the admin allows it, create withdrawal / deposit orders by hand.

Auth is a signed cookie scoped to MERCHANT_PATH (the admin session cookie is
scoped to ADMIN_PATH, so the two never mix). The cookie also carries the
CSRF token for the merchant's forms.
"""

import hmac
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models as m
from .. import services, telegram
from ..config import settings
from ..db import get_db
from ..timeutil import now_tw
from .auth import clear_failures, login_blocked, record_failure, verify_password
from .routes import templates

MP = settings.merchant_path
COOKIE = "lspay_merchant"
MAX_AGE = 12 * 3600
PAGE = 50

router = APIRouter(prefix=MP)
_serializer = URLSafeTimedSerializer(settings.session_secret or secrets.token_hex(32), salt="lspay-merchant-login")


class MerchantLoginRequired(Exception):
    pass


def _pw_tag(merchant: m.Merchant) -> str:
    # Changing the password logs out existing merchant sessions.
    return merchant.password_hash[-12:]


def _read_cookie(request: Request) -> Optional[dict]:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=MAX_AGE)
    except BadSignature:
        return None


def current_merchant(request: Request, db: Session = Depends(get_db)) -> m.Merchant:
    data = _read_cookie(request)
    merchant = db.get(m.Merchant, data.get("mid")) if data else None
    if (
        merchant is None
        or not merchant.enabled
        or not merchant.password_hash
        or not hmac.compare_digest(str(data.get("pw", "")), _pw_tag(merchant))
    ):
        raise MerchantLoginRequired()
    request.state.merchant_csrf = data["csrf"]
    return merchant


async def check_merchant_csrf(request: Request) -> None:
    form = await request.form()
    expected = getattr(request.state, "merchant_csrf", "")
    if not expected or not hmac.compare_digest(str(form.get("csrf") or ""), expected):
        raise HTTPException(400, "表單已過期，請重新整理頁面")


def page(request: Request, name: str, merchant: Optional[m.Merchant] = None, **ctx) -> HTMLResponse:
    base = {"mc": merchant, "mp": MP, "csrf": getattr(request.state, "merchant_csrf", ""), "flash": ""}
    return templates.TemplateResponse(request, name, {**base, **ctx})


def go(url: str) -> RedirectResponse:
    return RedirectResponse(MP + url, status_code=303)


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "merchant/login.html", error="")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    key = f"merchant:{username}|{request.client.host if request.client else ''}"
    if login_blocked(key):
        return page(request, "merchant/login.html", error="登入失敗次數過多，請 10 分鐘後再試")
    merchant = db.scalar(select(m.Merchant).where(m.Merchant.login_name == username.strip())) if username else None
    if (
        merchant is None
        or not merchant.enabled
        or not merchant.password_hash
        or not verify_password(password, merchant.password_hash)
    ):
        record_failure(key)
        return page(request, "merchant/login.html", error="帳號或密碼錯誤")
    clear_failures(key)
    db.add(m.AuditLog(actor=f"商戶 {merchant.login_name}", action="merchant_login", detail=merchant.name))
    db.commit()
    token = _serializer.dumps({"mid": merchant.id, "pw": _pw_tag(merchant), "csrf": secrets.token_urlsafe(24)})
    res = go("")
    res.set_cookie(
        COOKIE, token, max_age=MAX_AGE, path=MP, httponly=True, samesite="strict", secure=settings.cookie_secure
    )
    return res


@router.post("/logout")
def logout():
    res = go("/login")
    res.delete_cookie(COOKIE, path=MP)
    return res


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def home(request: Request, merchant=Depends(current_merchant), db: Session = Depends(get_db)):
    return page(request, "merchant/home.html", merchant, stats=period_stats(db, merchant.id))


def period_stats(db: Session, merchant_id: int) -> dict:
    """Completed orders today / yesterday / this month: {period: {type: (count, sum)}}."""
    today = now_tw().replace(hour=0, minute=0, second=0, microsecond=0)
    starts = {"today": today, "yesterday": today - timedelta(days=1), "month": today.replace(day=1)}
    since = min(starts.values())
    rows = db.execute(
        select(m.Order.type, m.Order.finish_time, m.Order.amount).where(
            m.Order.merchant_id == merchant_id, m.Order.status == m.ST_DONE, m.Order.finish_time >= since
        )
    ).all()
    out = {key: {m.TYPE_RECEIVE: [0, Decimal(0)], m.TYPE_WITHDRAW: [0, Decimal(0)]} for key in starts}
    for otype, finished, amount in rows:
        for key, start in starts.items():
            if finished >= start and (key != "yesterday" or finished < today):
                out[key][otype][0] += 1
                out[key][otype][1] += amount
    return out


@router.get("/orders/{kind}", response_class=HTMLResponse)
def orders(
    request: Request,
    kind: str,
    q: str = "",
    p: int = 1,
    merchant=Depends(current_merchant),
    db: Session = Depends(get_db),
):
    if kind not in ("receive", "withdraw"):
        raise HTTPException(404)
    otype = m.TYPE_RECEIVE if kind == "receive" else m.TYPE_WITHDRAW
    query = select(m.Order).where(m.Order.merchant_id == merchant.id, m.Order.type == otype)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(m.Order.order_id.like(like), m.Order.order_sid.like(like), m.Order.payer.like(like)))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    p = max(p, 1)
    rows = db.scalars(query.order_by(m.Order.id.desc()).offset((p - 1) * PAGE).limit(PAGE)).all()
    pages = max(1, (total + PAGE - 1) // PAGE)
    title = "收款訂單列表" if kind == "receive" else "付款訂單列表"
    ctx = {"rows": rows, "kind": kind, "q": q, "page_no": p, "pages": pages, "total": total, "title": title}
    return page(request, "merchant/orders.html", merchant, **ctx)


def _need(flag: bool) -> None:
    if not flag:
        raise HTTPException(404)


@router.get("/withdraw/new", response_class=HTMLResponse)
def withdraw_form(request: Request, merchant=Depends(current_merchant)):
    _need(merchant.allow_manual_withdraw)
    return page(request, "merchant/withdraw_new.html", merchant, form={}, error="")


@router.post("/withdraw/new", response_class=HTMLResponse)
def withdraw_create(
    request: Request,
    tasks: BackgroundTasks,
    payer: str = Form(""),
    bank_name: str = Form(""),
    bank_title: str = Form(""),
    bank_no: str = Form(""),
    amount: str = Form(""),
    merchant=Depends(current_merchant),
    db: Session = Depends(get_db),
    _=Depends(check_merchant_csrf),
):
    _need(merchant.allow_manual_withdraw)
    form = {"payer": payer, "bank_name": bank_name, "bank_title": bank_title, "bank_no": bank_no, "amount": amount}
    data = {**form, "order_id": services.manual_order_id(), "message": "商戶後台手動建單"}
    try:
        order = services.create_withdraw(db, merchant, data, source="merchant")
        db.commit()
    except services.ApiError as exc:
        db.rollback()
        return page(request, "merchant/withdraw_new.html", merchant, form=form, error=error_text(exc.msg))
    tasks.add_task(telegram.notify_safe, order.id)
    return page(request, "merchant/created.html", merchant, o=order)


@router.get("/receive/new", response_class=HTMLResponse)
def receive_form(request: Request, merchant=Depends(current_merchant)):
    _need(merchant.allow_manual_receive)
    return page(request, "merchant/receive_new.html", merchant, form={}, error="")


@router.post("/receive/new", response_class=HTMLResponse)
def receive_create(
    request: Request,
    tasks: BackgroundTasks,
    payer: str = Form(""),
    bank_code: str = Form(""),
    bank_no: str = Form(""),
    amount: str = Form(""),
    merchant=Depends(current_merchant),
    db: Session = Depends(get_db),
    _=Depends(check_merchant_csrf),
):
    _need(merchant.allow_manual_receive)
    form = {"payer": payer, "bank_code": bank_code, "bank_no": bank_no, "amount": amount}
    code, no = services.norm_bank_code(bank_code), "".join(ch for ch in bank_no if ch.isdigit())
    data = {
        "payer": payer,
        "amount": amount,
        "order_id": services.manual_order_id(),
        "message": "商戶後台手動建單",
        "bank_no_check": f"{code}-{no}" if code and no else "",
        "bank_no_check_title": payer,
    }
    try:
        if len(code) != 3 or not no:
            raise services.ApiError("請填寫玩家的轉出銀行代碼（3 碼）和帳號")
        order = services.create_receive(db, merchant, data, source="merchant")
        db.commit()
    except services.ApiError as exc:
        db.rollback()
        return page(request, "merchant/receive_new.html", merchant, form=form, error=error_text(exc.msg))
    tasks.add_task(telegram.notify_safe, order.id)
    cashier = f"{settings.base_url}/cashier/{order.order_sid}?t={order.cashier_token}"
    return page(request, "merchant/created.html", merchant, o=order, cashier=cashier)


ERRORS = {
    "Insufficient balance.": "可動用商餘不足",
    "amount Out Of Range.": "金額超出單筆上下限",
    "amount Format Error.": "金額格式錯誤",
    "amount Empty.": "請填寫金額",
    "Withdraw Disabled.": "此商戶目前不可代付",
    "Receive Disabled.": "此商戶目前不可代收",
    "Daily Limit Exceeded.": "已超過每日最大收量",
    "Too Many Orders.": "同一玩家短時間內單數過多",
    "Member blocked.": "此玩家已被停用",
}


def error_text(msg: str) -> str:
    if msg.endswith(" Empty."):
        return "請填寫所有欄位"
    if msg.endswith(" Too Long."):
        return "有欄位內容太長"
    return ERRORS.get(msg, msg)
