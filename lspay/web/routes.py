"""Admin web (server-rendered)."""

import hashlib
import pathlib
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import callbacks, services, telegram
from .. import models as m
from ..banks import bank_name
from ..config import settings
from ..db import get_db
from ..timeutil import fmt, now_tw, parse
from .auth import (
    check_csrf,
    clear_failures,
    csrf_token,
    current_user,
    hash_password,
    login_blocked,
    record_failure,
    require_admin,
    require_reviewer,
    verify_password,
)

templates = Jinja2Templates(directory=str(pathlib.Path(__file__).resolve().parent.parent / "templates"))


def money(value) -> str:
    """1234.5 -> '1,234.50', 1000 -> '1,000'."""
    value = Decimal(value or 0)
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"


def _static_version() -> str:
    """Short hash of the static files so browsers fetch new CSS after an update."""
    digest = hashlib.sha1()
    for path in sorted(STATIC_DIR.glob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

templates.env.globals.update(
    money=money,
    static_v=_static_version(),
    fmt=fmt,
    m=m,
    bank_name=bank_name,
    STATUS_LABELS=m.STATUS_LABELS,
    ROLE_LABELS=m.ROLE_LABELS,
)

ADMIN = settings.admin_path
templates.env.globals["admin"] = ADMIN

router = APIRouter(prefix=ADMIN)
public_router = APIRouter()
PAGE = 50


def render(request: Request, name: str, user: Optional[m.AdminUser] = None, **ctx) -> HTMLResponse:
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"user": user, "csrf": csrf_token(request), "flash": flash, **ctx})


def redirect(url: str, flash: Optional[str] = None, request: Optional[Request] = None) -> RedirectResponse:
    """Redirect to a path inside the admin web (``url`` is relative to ADMIN_PATH)."""
    if flash and request is not None:
        request.session["flash"] = flash
    return RedirectResponse(ADMIN + url, status_code=303)


def actor(user: m.AdminUser) -> services.Actor:
    return services.Actor("web", user.label, user)


def to_decimal(text: str, field: str) -> Decimal:
    try:
        return Decimal((text or "0").replace(",", "").strip())
    except InvalidOperation:
        raise HTTPException(400, f"{field} 格式錯誤")


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    key = f"{username}|{request.client.host if request.client else ''}"
    if login_blocked(key):
        return redirect("/login", "登入失敗次數過多，請 10 分鐘後再試", request)
    user = db.scalar(select(m.AdminUser).where(m.AdminUser.username == username))
    if user is None or not user.enabled or not verify_password(password, user.password_hash):
        record_failure(key)
        return redirect("/login", "帳號或密碼錯誤", request)
    clear_failures(key)
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login_at = now_tw()
    db.commit()
    return redirect("")


@router.post("/logout")
def logout(request: Request, _=Depends(check_csrf)):
    request.session.clear()
    return redirect("/login")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    today = now_tw().replace(hour=0, minute=0, second=0)

    def stat(order_type: int):
        row = db.execute(
            select(
                func.count(m.Order.id),
                func.coalesce(func.sum(case((m.Order.status == m.ST_DONE, m.Order.amount), else_=0)), 0),
                func.coalesce(func.sum(case((m.Order.status == m.ST_DONE, 1), else_=0)), 0),
            ).where(m.Order.type == order_type, m.Order.init_time >= today)
        ).one()
        return {"count": row[0], "done_amount": row[1], "done": row[2]}

    pending = db.scalar(
        select(func.count(m.Order.id)).where(m.Order.review_status == m.REVIEW_PENDING, m.Order.status < 90)
    )
    open_receive = db.scalar(
        select(func.count(m.Order.id)).where(
            m.Order.type == m.TYPE_RECEIVE, m.Order.status.in_([m.ST_NEW, m.ST_MATCHED, m.ST_ACCEPTED])
        )
    )
    to_pay = db.scalar(
        select(func.count(m.Order.id)).where(
            m.Order.review_status == m.REVIEW_APPROVED, m.Order.status == m.ST_ACCEPTED
        )
    )
    failed_cb = db.scalar(select(func.count(m.Order.id)).where(m.Order.callback_status == m.CALLBACK_FAILED))
    merchants = db.scalars(select(m.Merchant).order_by(m.Merchant.id)).all()
    return render(
        request,
        "dashboard.html",
        user,
        receive=stat(m.TYPE_RECEIVE),
        withdraw=stat(m.TYPE_WITHDRAW),
        pending=pending,
        open_receive=open_receive,
        to_pay=to_pay,
        failed_cb=failed_cb,
        merchants=merchants,
    )


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

ORDER_VIEWS = {
    "receive": ("收款訂單", lambda q: q.where(m.Order.type == m.TYPE_RECEIVE)),
    "withdraw": ("付款訂單", lambda q: q.where(m.Order.type == m.TYPE_WITHDRAW)),
    "pending": (
        "待審核付款訂單",
        lambda q: q.where(
            m.Order.type == m.TYPE_WITHDRAW,
            or_(
                m.Order.review_status == m.REVIEW_PENDING,
                and_(m.Order.review_status == m.REVIEW_APPROVED, m.Order.status == m.ST_ACCEPTED),
            ),
            m.Order.status < 90,
            m.Order.status != m.ST_DONE,
        ),
    ),
}


@router.get("/orders/{view}", response_class=HTMLResponse)
def order_list(
    request: Request,
    view: str,
    q: str = "",
    status: str = "",
    merchant_id: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    if view not in ORDER_VIEWS:
        raise HTTPException(404)
    title, scope = ORDER_VIEWS[view]
    query = scope(select(m.Order))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                m.Order.order_sid == q.strip(),
                m.Order.order_id == q.strip(),
                m.Order.payer.like(like),
                m.Order.payer_pid == q.strip(),
                m.Order.bank_no.like(like),
            )
        )
    if status.strip().lstrip("-").isdigit():
        query = query.where(m.Order.status == int(status))
    if merchant_id.isdigit():
        query = query.where(m.Order.merchant_id == int(merchant_id))
    try:
        d_from, d_to = parse(date_from), parse(date_to)
    except ValueError:
        d_from = d_to = None
    if d_from:
        query = query.where(m.Order.init_time >= d_from)
    if d_to:
        if len(date_to.strip()) == 10:
            d_to += timedelta(days=1)
        query = query.where(m.Order.init_time < d_to)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    sub = query.subquery()
    totals = db.execute(
        select(
            func.coalesce(func.sum(sub.c.amount), 0),
            func.coalesce(func.sum(sub.c.fee), 0),
            func.coalesce(func.sum(sub.c.agent_fee), 0),
        )
    ).one()
    page = max(page, 1)
    order_by = m.Order.id.asc() if view == "pending" else m.Order.id.desc()
    rows = db.scalars(
        query.options(selectinload(m.Order.merchant), selectinload(m.Order.handled_by))
        .order_by(order_by)
        .offset((page - 1) * PAGE)
        .limit(PAGE)
    ).all()
    merchants = db.scalars(select(m.Merchant).order_by(m.Merchant.id)).all()
    warnings = {o.id: services.withdraw_warnings(o) for o in rows if o.type == m.TYPE_WITHDRAW and not o.is_final}
    return render(
        request,
        "orders.html",
        user,
        warnings=warnings,
        view=view,
        title=title,
        rows=rows,
        total=total,
        sum_amount=totals[0],
        sum_fee=totals[1],
        sum_agent=totals[2],
        page=page,
        pages=max((total + PAGE - 1) // PAGE, 1),
        merchants=merchants,
        f={"q": q, "status": status, "merchant_id": merchant_id, "date_from": date_from, "date_to": date_to},
    )


def get_order(db: Session, sid: str) -> m.Order:
    order = db.scalar(select(m.Order).where(m.Order.order_sid == sid))
    if order is None:
        raise HTTPException(404, "找不到訂單")
    return order


@router.get("/order/{sid}", response_class=HTMLResponse)
def order_detail(request: Request, sid: str, user=Depends(current_user), db: Session = Depends(get_db)):
    order = get_order(db, sid)
    warnings = services.withdraw_warnings(order) if order.type == m.TYPE_WITHDRAW else []
    declared = services.parse_bank_no_check(order.bank_no_check) if order.bank_no_check else []
    return render(
        request,
        "order_detail.html",
        user,
        o=order,
        warnings=warnings,
        declared=declared,
    )


@router.post("/order/{sid}/confirm")
def order_confirm(
    request: Request,
    tasks: BackgroundTasks,
    sid: str,
    bank_code: str = Form(...),
    bank_no: str = Form(...),
    amount: str = Form(...),
    force: str = Form(""),
    note: str = Form(""),
    user=Depends(require_reviewer),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    order = get_order(db, sid)
    try:
        result = services.confirm_deposit(
            db, order, actor(user), bank_code, bank_no, to_decimal(amount, "金額"), force=bool(force), note=note
        )
    except services.ActionError as exc:
        db.rollback()
        return redirect(f"/order/{sid}", f"❌ {exc}", request)
    if not result.ok and not force:
        db.rollback()
        return redirect(f"/order/{sid}", "❌ 資料不符，未入帳：" + "；".join(result.problems), request)
    db.commit()
    tasks.add_task(telegram.notify_safe, order.id)
    msg = "✅ 已入帳"
    if not result.name_ok:
        msg += "（注意：銀行戶名與會員姓名不同）"
    return redirect(f"/order/{sid}", msg, request)


@router.post("/order/{sid}/{action}")
def order_action(
    request: Request,
    tasks: BackgroundTasks,
    sid: str,
    action: str,
    note: str = Form(""),
    status: str = Form(""),
    user=Depends(require_reviewer),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    order = get_order(db, sid)
    a = actor(user)
    try:
        if action == "approve":
            services.approve_withdraw(db, order, a, note)
        elif action == "reject":
            if not note.strip():
                raise services.ActionError("駁回需填寫原因")
            services.reject_withdraw(db, order, a, note)
        elif action == "paid":
            services.complete_withdraw(db, order, a, note)
        elif action == "payfail":
            services.fail_withdraw(db, order, a, note)
        elif action == "fail":
            services.fail_deposit(db, order, a, int(status or m.ST_INVALID), note)
        elif action == "resend":
            callbacks.resend(db, order)
            db.add(m.OrderEvent(order_id=order.id, source="web", actor=user.label, action="resend_callback"))
        else:
            raise HTTPException(404)
    except (services.ActionError, ValueError) as exc:
        db.rollback()
        return redirect(f"/order/{sid}", f"❌ {exc}", request)
    db.commit()
    tasks.add_task(telegram.notify_safe, order.id)
    back = urlparse(request.headers.get("referer") or "")
    target = f"/order/{sid}"
    if back.path.startswith(ADMIN + "/"):
        target = back.path[len(ADMIN) :] + (f"?{back.query}" if back.query else "")
    return redirect(target, "✅ 已處理", request)


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------


@router.get("/members", response_class=HTMLResponse)
def member_list(
    request: Request,
    q: str = "",
    merchant_id: str = "",
    team_id: str = "",
    page: int = 1,
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    query = select(m.Member)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(m.Member.name.like(like), m.Member.phone.like(like), m.Member.pid == q.strip()))
    if merchant_id.isdigit():
        query = query.where(m.Member.merchant_id == int(merchant_id))
    if team_id.isdigit():
        query = query.where(m.Member.team_id == int(team_id))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    page = max(page, 1)
    rows = db.scalars(
        query.options(
            selectinload(m.Member.merchant), selectinload(m.Member.team), selectinload(m.Member.bank_accounts)
        )
        .order_by(m.Member.id.desc())
        .offset((page - 1) * PAGE)
        .limit(PAGE)
    ).all()
    return render(
        request,
        "members.html",
        user,
        rows=rows,
        total=total,
        page=page,
        pages=max((total + PAGE - 1) // PAGE, 1),
        merchants=db.scalars(select(m.Merchant).order_by(m.Merchant.id)).all(),
        teams=db.scalars(select(m.Team).order_by(m.Team.id)).all(),
        f={"q": q, "merchant_id": merchant_id, "team_id": team_id},
    )


@router.post("/members")
def member_create(
    request: Request,
    merchant_id: int = Form(...),
    name: str = Form(...),
    pid: str = Form(...),
    phone: str = Form(""),
    team_id: str = Form(""),
    bank_code: str = Form(""),
    bank_no: str = Form(""),
    note: str = Form(""),
    user=Depends(current_user),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    merchant = db.get(m.Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(400, "商戶不存在")
    pid = pid.strip().upper()
    if db.scalar(select(m.Member.id).where(m.Member.merchant_id == merchant_id, m.Member.pid == pid)):
        return redirect("/members", "❌ 此商戶已有相同身分證號的會員", request)
    member = m.Member(
        merchant_id=merchant_id,
        name=name.strip(),
        pid=pid,
        phone=phone.strip(),
        team_id=int(team_id) if team_id.isdigit() else None,
        note=note,
    )
    db.add(member)
    db.flush()
    if bank_code.strip() and bank_no.strip():
        services.add_member_account(db, member, bank_code, bank_no, name.strip())
    db.add(m.AuditLog(actor=user.label, action="member_create", detail=f"{member.id} {member.name}"))
    db.commit()
    return redirect(f"/members/{member.id}", "✅ 會員已建立", request)


@router.get("/order/{sid}/proof/{aid}")
def order_proof(sid: str, aid: int, user=Depends(current_user), db: Session = Depends(get_db)):
    order = get_order(db, sid)
    att = db.get(m.OrderAttachment, aid)
    if att is None or att.order_id != order.id:
        raise HTTPException(404)
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
    return Response(att.data, media_type=att.content_type, headers=headers)


@router.get("/members/{mid}", response_class=HTMLResponse)
def member_detail(request: Request, mid: int, user=Depends(current_user), db: Session = Depends(get_db)):
    member = db.get(m.Member, mid)
    if member is None:
        raise HTTPException(404)
    orders = db.scalars(select(m.Order).where(m.Order.member_id == mid).order_by(m.Order.id.desc()).limit(50)).all()
    return render(
        request,
        "member_detail.html",
        user,
        mb=member,
        orders=orders,
        pay_url=member_pay_url(member),
        teams=db.scalars(select(m.Team).order_by(m.Team.id)).all(),
    )


@router.post("/members/{mid}")
def member_update(
    request: Request,
    mid: int,
    name: str = Form(...),
    phone: str = Form(""),
    team_id: str = Form(""),
    status: str = Form("active"),
    note: str = Form(""),
    user=Depends(current_user),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    member = db.get(m.Member, mid)
    if member is None:
        raise HTTPException(404)
    member.name = name.strip()
    member.phone = phone.strip()
    member.team_id = int(team_id) if team_id.isdigit() else None
    member.status = "blocked" if status == "blocked" else "active"
    member.note = note
    db.add(m.AuditLog(actor=user.label, action="member_update", detail=f"{member.id} status={member.status}"))
    db.commit()
    return redirect(f"/members/{mid}", "✅ 已儲存", request)


@router.post("/members/{mid}/accounts")
def member_add_account(
    request: Request,
    mid: int,
    bank_code: str = Form(...),
    bank_no: str = Form(...),
    account_name: str = Form(""),
    user=Depends(current_user),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    member = db.get(m.Member, mid)
    if member is None:
        raise HTTPException(404)
    if len(services.norm_bank_code(bank_code)) != 3:
        return redirect(f"/members/{mid}", "❌ 銀行代碼需為 3 碼", request)
    added = services.add_member_account(db, member, bank_code, bank_no, account_name.strip())
    db.add(m.AuditLog(actor=user.label, action="member_account_add", detail=f"{mid} {bank_code}-{bank_no[-5:]}"))
    db.commit()
    return redirect(f"/members/{mid}", "✅ 已新增帳戶" if added else "帳戶已存在", request)


@router.post("/members/{mid}/accounts/{aid}/delete")
def member_del_account(
    request: Request,
    mid: int,
    aid: int,
    user=Depends(require_reviewer),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    acc = db.get(m.MemberBankAccount, aid)
    if acc is None or acc.member_id != mid:
        raise HTTPException(404)
    db.delete(acc)
    db.add(
        m.AuditLog(actor=user.label, action="member_account_delete", detail=f"{mid} {acc.bank_code}-{acc.bank_no[-5:]}")
    )
    db.commit()
    return redirect(f"/members/{mid}", "✅ 已刪除帳戶", request)


# --------------------------------------------------------------------------
# Teams (車隊) and team order stats (車隊跑單)
# --------------------------------------------------------------------------


@router.get("/teams", response_class=HTMLResponse)
def team_list(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    teams = db.scalars(select(m.Team).options(selectinload(m.Team.users)).order_by(m.Team.id)).all()
    member_counts = dict(db.execute(select(m.Member.team_id, func.count()).group_by(m.Member.team_id)).all())
    return render(request, "teams.html", user, teams=teams, member_counts=member_counts)


@router.post("/teams")
def team_save(
    request: Request,
    team_id: str = Form(""),
    name: str = Form(...),
    leader: str = Form(""),
    tg_chat_id: str = Form(""),
    note: str = Form(""),
    enabled: str = Form(""),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    team = db.get(m.Team, int(team_id)) if team_id.isdigit() else m.Team()
    if team is None:
        raise HTTPException(404)
    team.name, team.leader, team.tg_chat_id, team.note = name.strip(), leader.strip(), tg_chat_id.strip(), note
    team.enabled = bool(enabled) or not team_id
    db.add(team)
    db.add(m.AuditLog(actor=user.label, action="team_save", detail=team.name))
    try:
        db.commit()
    except Exception:
        db.rollback()
        return redirect("/teams", "❌ 車隊名稱重複", request)
    return redirect("/teams", "✅ 已儲存", request)


@router.get("/team-orders", response_class=HTMLResponse)
def team_orders(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    user=Depends(current_user),
    db: Session = Depends(get_db),
):
    today = now_tw().date().isoformat()
    date_from = date_from or today
    date_to = date_to or today
    try:
        start = parse(date_from)
        end = parse(date_to) + timedelta(days=1)
    except (ValueError, TypeError):
        start = parse(today)
        end = start + timedelta(days=1)

    done = m.Order.status == m.ST_DONE
    cols = (
        func.count(m.Order.id),
        func.coalesce(func.sum(case((and_(done, m.Order.type == m.TYPE_RECEIVE), 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(done, m.Order.type == m.TYPE_RECEIVE), m.Order.amount), else_=0)), 0),
        func.coalesce(func.sum(case((and_(done, m.Order.type == m.TYPE_WITHDRAW), 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(done, m.Order.type == m.TYPE_WITHDRAW), m.Order.amount), else_=0)), 0),
        func.coalesce(func.sum(case((m.Order.status == m.ST_REJECTED, 1), else_=0)), 0),
    )
    where = and_(m.Order.finish_time >= start, m.Order.finish_time < end, m.Order.handled_by_id.is_not(None))

    team_rows = db.execute(select(m.Order.team_id, *cols).where(where).group_by(m.Order.team_id)).all()
    user_rows = db.execute(select(m.Order.handled_by_id, *cols).where(where).group_by(m.Order.handled_by_id)).all()
    teams = {t.id: t for t in db.scalars(select(m.Team)).all()}
    users = {u.id: u for u in db.scalars(select(m.AdminUser)).all()}
    return render(
        request,
        "team_orders.html",
        user,
        team_rows=team_rows,
        user_rows=user_rows,
        teams=teams,
        users=users,
        f={"date_from": date_from, "date_to": date_to},
    )


# --------------------------------------------------------------------------
# Admin users (用戶資料)
# --------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
def user_list(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    users = db.scalars(select(m.AdminUser).options(selectinload(m.AdminUser.team)).order_by(m.AdminUser.id)).all()
    teams = db.scalars(select(m.Team).order_by(m.Team.id)).all()
    logs = []
    if user.role == m.ROLE_ADMIN:
        logs = db.scalars(select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(30)).all()
    return render(request, "users.html", user, users=users, teams=teams, logs=logs)


@router.post("/users")
def user_save(
    request: Request,
    user_id: str = Form(""),
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form(m.ROLE_SUPPORT),
    team_id: str = Form(""),
    tg_user_id: str = Form(""),
    enabled: str = Form(""),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    if role not in m.ROLE_LABELS:
        raise HTTPException(400)
    if user_id.isdigit():
        target = db.get(m.AdminUser, int(user_id))
        if target is None:
            raise HTTPException(404)
        if target.id == user.id and (role != m.ROLE_ADMIN or not enabled):
            return redirect("/users", "❌ 不能停用自己或移除自己的管理員權限", request)
    else:
        username = username.strip()
        if not username or len(password) < 8:
            return redirect("/users", "❌ 需填帳號，密碼至少 8 碼", request)
        if db.scalar(select(m.AdminUser.id).where(m.AdminUser.username == username)):
            return redirect("/users", "❌ 帳號已存在", request)
        target = m.AdminUser(username=username, password_hash="")
        db.add(target)
    if password:
        if len(password) < 8:
            return redirect("/users", "❌ 密碼至少 8 碼", request)
        target.password_hash = hash_password(password)
    target.display_name = display_name.strip()
    target.role = role
    target.team_id = int(team_id) if team_id.isdigit() else None
    target.tg_user_id = tg_user_id.strip()
    target.enabled = bool(enabled) or not user_id
    db.add(m.AuditLog(actor=user.label, action="user_save", detail=f"{target.username} role={role}"))
    db.commit()
    return redirect("/users", "✅ 已儲存", request)


@router.post("/profile/password")
def change_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    user=Depends(current_user),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    if not verify_password(old_password, user.password_hash):
        return redirect("/users", "❌ 舊密碼錯誤", request)
    if len(new_password) < 8:
        return redirect("/users", "❌ 新密碼至少 8 碼", request)
    user.password_hash = hash_password(new_password)
    db.commit()
    return redirect("/users", "✅ 密碼已更新", request)


# --------------------------------------------------------------------------
# Merchants and receiving accounts (admin only)
# --------------------------------------------------------------------------


LOGIN_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")


def merchant_day_stats(db: Session) -> dict:
    """Completed orders yesterday and today per merchant: {mid: {"today"|"yesterday": {type: (count, sum)}}}."""
    today = now_tw().replace(hour=0, minute=0, second=0)
    yesterday = today - timedelta(days=1)
    is_today = case((m.Order.finish_time >= today, 1), else_=0)
    rows = db.execute(
        select(m.Order.merchant_id, m.Order.type, is_today, func.count(), func.coalesce(func.sum(m.Order.amount), 0))
        .where(m.Order.status == m.ST_DONE, m.Order.finish_time >= yesterday)
        .group_by(m.Order.merchant_id, m.Order.type, is_today)
    ).all()
    out: dict = {}
    for mid, otype, today_flag, count, total in rows:
        day = out.setdefault(mid, {"today": {}, "yesterday": {}})["today" if today_flag else "yesterday"]
        day[otype] = (count, Decimal(total))
    return out


@router.get("/merchants", response_class=HTMLResponse)
def merchant_list(request: Request, user=Depends(require_admin), db: Session = Depends(get_db)):
    merchants = db.scalars(select(m.Merchant).order_by(m.Merchant.id)).all()
    return render(
        request,
        "merchants.html",
        user,
        merchants=merchants,
        stats=merchant_day_stats(db),
        merchant_url=settings.base_url + settings.merchant_path,
        reveal=request.session.pop("reveal", None),
    )


def _new_client_sid(db: Session) -> str:
    last = db.scalar(select(func.max(m.Merchant.id))) or 0
    sid = f"{last + 1:04d}"
    while db.scalar(select(m.Merchant.id).where(m.Merchant.client_sid == sid)):
        sid = f"{int(sid) + 1:04d}"
    return sid


def _check_login_name(db: Session, login_name: str, merchant_id: Optional[int] = None) -> Optional[str]:
    if not login_name:
        return None
    if not LOGIN_NAME_RE.fullmatch(login_name):
        return "❌ 登入帳號只能用英文、數字和 _ . -"
    taken = db.scalar(select(m.Merchant.id).where(m.Merchant.login_name == login_name))
    if taken and taken != merchant_id:
        return "❌ 此登入帳號已被其他商戶使用"
    return None


@router.post("/merchants")
def merchant_create(
    request: Request,
    name: str = Form(...),
    login_name: str = Form(""),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    login_name = login_name.strip()
    error = _check_login_name(db, login_name)
    if error:
        return redirect("/merchants", error, request)
    merchant = m.Merchant(
        client_sid=_new_client_sid(db),
        name=name.strip(),
        login_name=login_name,
        api_key=secrets.token_hex(16),
        sign_key=secrets.token_hex(16),
        enabled=True,
    )
    db.add(merchant)
    db.add(m.AuditLog(actor=user.label, action="merchant_create", detail=merchant.name))
    db.commit()
    request.session["reveal"] = merchant.id
    return redirect("/merchants", "✅ 商戶已建立，請立即複製金鑰，並按「設定」填寫費率與限額", request)


def _non_negative(text: str, field: str) -> Decimal:
    value = to_decimal(text, field)
    if value < 0:
        raise HTTPException(400, f"{field} 不能是負數")
    return value


def _int_field(text: str, field: str) -> int:
    text = (text or "0").strip()
    if not text.isdigit():
        raise HTTPException(400, f"{field} 需為 0 以上的整數")
    return int(text)


@router.post("/merchants/{mid}/settings")
def merchant_settings(
    request: Request,
    mid: int,
    name: str = Form(...),
    login_name: str = Form(""),
    enabled: str = Form("0"),
    can_receive: str = Form("0"),
    can_withdraw: str = Form("0"),
    test_mode: str = Form("0"),
    receive_min: str = Form("0"),
    receive_max: str = Form("0"),
    withdraw_min: str = Form("0"),
    withdraw_max: str = Form("0"),
    daily_receive_limit: str = Form("0"),
    receive_fee_rate: str = Form("0"),
    receive_fee_fixed: str = Form("0"),
    withdraw_fee_rate: str = Form("0"),
    withdraw_fee_fixed: str = Form("0"),
    ip_whitelist: str = Form(""),
    tg_chat_id: str = Form(""),
    same_name_max_orders: str = Form("0"),
    same_name_window_minutes: str = Form("10"),
    return_bank_info: str = Form("0"),
    return_bank_title: str = Form("0"),
    color: str = Form(""),
    note: str = Form(""),
    password: str = Form(""),
    allow_manual_withdraw: str = Form("0"),
    allow_manual_receive: str = Form("0"),
    agent_rate: str = Form("0"),
    balance_hold: str = Form("0"),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    merchant = db.get(m.Merchant, mid)
    if merchant is None:
        raise HTTPException(404)
    login_name = login_name.strip()
    error = _check_login_name(db, login_name, merchant.id)
    if error:
        return redirect("/merchants", error, request)
    limits = {
        "receive_min": _non_negative(receive_min, "最低收/筆"),
        "receive_max": _non_negative(receive_max, "最高收/筆"),
        "withdraw_min": _non_negative(withdraw_min, "最低付/筆"),
        "withdraw_max": _non_negative(withdraw_max, "最高付/筆"),
    }
    if (limits["receive_max"] and limits["receive_min"] > limits["receive_max"]) or (
        limits["withdraw_max"] and limits["withdraw_min"] > limits["withdraw_max"]
    ):
        return redirect("/merchants", "❌ 最低金額不能大於最高金額", request)
    if password and len(password) < 8:
        return redirect("/merchants", "❌ 商戶後台密碼至少 8 個字元", request)
    if password and not login_name:
        return redirect("/merchants", "❌ 請先設定登入帳號，才能設定商戶後台密碼", request)
    for key, value in limits.items():
        setattr(merchant, key, value)
    if password:
        merchant.password_hash = hash_password(password)
        db.add(m.AuditLog(actor=user.label, action="merchant_password", detail=merchant.name))
    merchant.allow_manual_withdraw = allow_manual_withdraw == "1"
    merchant.allow_manual_receive = allow_manual_receive == "1"
    merchant.agent_rate = _non_negative(agent_rate, "中人(%)")
    merchant.balance_hold = _non_negative(balance_hold, "凍結金額")
    merchant.name = name.strip()
    merchant.login_name = login_name
    merchant.enabled = enabled == "1"
    merchant.can_receive = can_receive == "1"
    merchant.can_withdraw = can_withdraw == "1"
    merchant.test_mode = test_mode == "1"
    merchant.daily_receive_limit = _non_negative(daily_receive_limit, "每日最大收量")
    merchant.receive_fee_rate = _non_negative(receive_fee_rate, "收款費率")
    merchant.receive_fee_fixed = _non_negative(receive_fee_fixed, "收款單筆手續費")
    merchant.withdraw_fee_rate = _non_negative(withdraw_fee_rate, "付款費率")
    merchant.withdraw_fee_fixed = _non_negative(withdraw_fee_fixed, "付款單筆手續費")
    merchant.ip_whitelist = ",".join(ip.strip() for ip in re.split(r"[,\s]+", ip_whitelist) if ip.strip())
    merchant.tg_chat_id = tg_chat_id.strip()
    merchant.same_name_max_orders = _int_field(same_name_max_orders, "同姓名單數")
    merchant.same_name_window_minutes = _int_field(same_name_window_minutes, "同姓名判定時間")
    merchant.return_bank_info = return_bank_info == "1"
    merchant.return_bank_title = return_bank_title == "1"
    merchant.color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color or "") else ""
    merchant.note = note
    db.add(m.AuditLog(actor=user.label, action="merchant_settings", detail=f"{merchant.client_sid} {merchant.name}"))
    db.commit()
    return redirect("/merchants", f"✅ 已儲存 {merchant.name} 的設定", request)


@router.post("/merchants/{mid}/keys")
def merchant_keys(
    request: Request,
    mid: int,
    rotate: str = Form(""),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    merchant = db.get(m.Merchant, mid)
    if merchant is None:
        raise HTTPException(404)
    if rotate:
        merchant.api_key = secrets.token_hex(16)
        merchant.sign_key = secrets.token_hex(16)
    db.add(
        m.AuditLog(
            actor=user.label, action="merchant_keys_rotate" if rotate else "merchant_keys_view", detail=merchant.name
        )
    )
    db.commit()
    request.session["reveal"] = merchant.id
    return redirect("/merchants", "✅ 已更換金鑰" if rotate else None, request)


def merchant_info_text(merchant: m.Merchant) -> str:
    ips = [ip.strip() for ip in settings.callback_ips.split(",") if ip.strip()]
    lines = [
        settings.env_label,
        f"商戶名: {merchant.name}",
        "",
        "API資訊",
        "=" * 47,
        f"API URL: {settings.base_url}",
        f"Client sid: {merchant.client_sid}",
        f"API Key: {merchant.api_key}",
        f"Sign Key: {merchant.sign_key}",
        "",
        "回調IP:",
        *(ips or ["（尚未設定，請洽 LSPay 管理員）"]),
        "",
    ]
    if merchant.login_name:
        lines += [
            "商戶後台",
            "=" * 47,
            f"後台網址: {settings.base_url}{settings.merchant_path}",
            f"後台帳號: {merchant.login_name}",
            "後台密碼: （由管理員另行提供）",
            "",
        ]
    return "\r\n".join(lines)


@router.post("/merchants/{mid}/info.txt")
def merchant_info_download(
    mid: int,
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    merchant = db.get(m.Merchant, mid)
    if merchant is None:
        raise HTTPException(404)
    db.add(m.AuditLog(actor=user.label, action="merchant_info_download", detail=merchant.name))
    db.commit()
    prefix = settings.env_label.replace("環境", "")
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', "", merchant.name)
    filename = f"{prefix}商戶-{merchant.client_sid}{safe_name}_{now_tw():%Y%m%d%H%M%S}.txt"
    headers = {
        "Content-Disposition": f'attachment; filename="merchant-{merchant.client_sid}.txt"; '
        f"filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    }
    return Response(
        merchant_info_text(merchant).encode("utf-8"), media_type="text/plain; charset=utf-8", headers=headers
    )


@router.post("/merchants/{mid}/balance")
def merchant_balance(
    request: Request,
    mid: int,
    delta: str = Form(...),
    note: str = Form(...),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    merchant = db.get(m.Merchant, mid)
    if merchant is None:
        raise HTTPException(404)
    if not note.strip():
        return redirect("/merchants", "❌ 請填寫調整原因", request)
    try:
        services.adjust_balance(db, merchant, to_decimal(delta, "金額"), user.label, note.strip())
    except services.ActionError as exc:
        db.rollback()
        return redirect("/merchants", f"❌ {exc}", request)
    db.commit()
    return redirect("/merchants", "✅ 餘額已調整", request)


@router.get("/accounts", response_class=HTMLResponse)
def account_list(request: Request, user=Depends(require_admin), db: Session = Depends(get_db)):
    accounts = db.scalars(select(m.ReceivingAccount).order_by(m.ReceivingAccount.id)).all()
    today = now_tw().replace(hour=0, minute=0, second=0)
    used = dict(
        db.execute(
            select(m.Order.receiving_account_id, func.coalesce(func.sum(m.Order.amount), 0))
            .where(m.Order.init_time >= today, m.Order.status == m.ST_DONE)
            .group_by(m.Order.receiving_account_id)
        ).all()
    )
    return render(request, "accounts.html", user, accounts=accounts, used=used)


@router.post("/accounts")
def account_save(
    request: Request,
    account_id: str = Form(""),
    kind: str = Form("company"),
    bank_code: str = Form(...),
    bank_name: str = Form(...),
    bank_branch: str = Form(""),
    bank_title: str = Form(...),
    bank_no: str = Form(...),
    daily_limit: str = Form("0"),
    note: str = Form(""),
    enabled: str = Form(""),
    user=Depends(require_admin),
    db: Session = Depends(get_db),
    _=Depends(check_csrf),
):
    if kind not in ("company", "virtual"):
        raise HTTPException(400)
    acc = db.get(m.ReceivingAccount, int(account_id)) if account_id.isdigit() else m.ReceivingAccount()
    if acc is None:
        raise HTTPException(404)
    acc.kind = kind
    acc.bank_code = services.norm_bank_code(bank_code)
    acc.bank_name = bank_name.strip()
    acc.bank_branch = bank_branch.strip()
    acc.bank_title = bank_title.strip()
    acc.bank_no = bank_no.strip()
    acc.daily_limit = to_decimal(daily_limit, "每日上限")
    acc.note = note
    acc.enabled = bool(enabled) or not account_id
    db.add(acc)
    db.add(m.AuditLog(actor=user.label, action="account_save", detail=f"{acc.bank_code}-{acc.bank_no[-5:]}"))
    db.commit()
    return redirect("/accounts", "✅ 已儲存", request)


# --------------------------------------------------------------------------
# Public cashier page (fronttable_url)
# --------------------------------------------------------------------------


def member_pay_url(member: m.Member) -> str:
    token = services.member_pay_token(member)
    return f"{settings.base_url}/pay/{token}" if token else ""


def payer_accounts(order: m.Order) -> list[tuple[str, str]]:
    """The accounts the player may transfer from: declared on the order, then registered."""
    out = services.parse_bank_no_check(order.bank_no_check) if order.bank_no_check else []
    if order.member:
        for a in order.member.bank_accounts:
            if not any(services.same_account(a.bank_code, a.bank_no, c, n) for c, n in out):
                out.append((a.bank_code, a.bank_no))
    return out


def cashier_order(db: Session, sid: str, t: str) -> m.Order:
    order = db.scalar(select(m.Order).where(m.Order.order_sid == sid, m.Order.type == m.TYPE_RECEIVE))
    if order is None or not t or not secrets.compare_digest(t, order.cashier_token):
        raise HTTPException(404)
    return order


def render_cashier(request: Request, order: m.Order, error: str = "", uploaded: bool = False, status_code: int = 200):
    ctx = {
        "o": order,
        "accounts": payer_accounts(order),
        "can_upload": services.can_upload_proof(order),
        "uploaded": uploaded or bool(order.attachments),
        "error": error,
        "remaining": max(0, int((order.end_time - now_tw()).total_seconds())) if order.end_time else 0,
    }
    return templates.TemplateResponse(request, "cashier.html", ctx, status_code=status_code)


@public_router.get("/cashier/{sid}", response_class=HTMLResponse)
def cashier(request: Request, sid: str, t: str = "", db: Session = Depends(get_db)):
    return render_cashier(request, cashier_order(db, sid, t))


@public_router.post("/cashier/{sid}/proof", response_class=HTMLResponse)
async def cashier_proof(
    request: Request,
    sid: str,
    tasks: BackgroundTasks,
    t: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    order = cashier_order(db, sid, t)
    data = await file.read(services.MAX_PROOF_BYTES + 1)
    try:
        services.add_proof(db, order, data)
        db.commit()
    except services.ApiError as exc:
        db.rollback()
        return render_cashier(request, order, error=exc.msg, status_code=400)
    tasks.add_task(telegram.notify_safe, order.id)
    return RedirectResponse(f"/cashier/{order.order_sid}?t={order.cashier_token}", status_code=303)


# --------------------------------------------------------------------------
# LS支付 member deposit page (personal link per member)
# --------------------------------------------------------------------------


def pay_member(db: Session, token: str) -> m.Member:
    member = services.member_from_pay_token(db, token)
    if member is None:
        raise HTTPException(404)
    return member


@public_router.get("/pay/{token}", response_class=HTMLResponse)
def pay_page(request: Request, token: str, db: Session = Depends(get_db)):
    member = pay_member(db, token)
    ctx = {"mb": member, "error": "", "amount": "", "timeout": settings.member_deposit_timeout_minutes}
    return templates.TemplateResponse(request, "pay.html", ctx)


@public_router.post("/pay/{token}", response_class=HTMLResponse)
def pay_create(
    request: Request,
    token: str,
    tasks: BackgroundTasks,
    amount: str = Form(""),
    db: Session = Depends(get_db),
):
    member = pay_member(db, token)
    try:
        order = services.create_member_deposit(db, member, amount)
        db.commit()
    except services.ApiError as exc:
        db.rollback()
        ctx = {"mb": member, "error": exc.msg, "amount": amount, "timeout": settings.member_deposit_timeout_minutes}
        return templates.TemplateResponse(request, "pay.html", ctx, status_code=400)
    tasks.add_task(telegram.notify_safe, order.id)
    return RedirectResponse(f"/cashier/{order.order_sid}?t={order.cashier_token}", status_code=303)
