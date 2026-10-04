"""Order business logic shared by the merchant API, admin web and Telegram bot.

Functions here change the session but never commit; callers commit.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .config import settings
from .timeutil import now_tw


class ApiError(Exception):
    def __init__(self, msg: str, data: Optional[dict] = None):
        super().__init__(msg)
        self.msg = msg
        self.data = data


class ActionError(Exception):
    """An admin / Telegram action that is not allowed in the order's current state."""


CENT = Decimal("0.01")


def parse_amount(value) -> Decimal:
    if value is None or value == "":
        raise ApiError("amount Empty.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ApiError("amount Format Error.")
    if not amount.is_finite() or amount <= 0:
        raise ApiError("amount Format Error.")
    if amount != amount.quantize(CENT):
        raise ApiError("amount Format Error.")
    if amount >= Decimal("100000000"):
        raise ApiError("amount Format Error.")
    return amount.quantize(CENT)


def calc_fee(amount: Decimal, rate_percent: Decimal, fixed: Decimal = Decimal("0")) -> Decimal:
    fee = amount * Decimal(rate_percent) / Decimal(100) + Decimal(fixed)
    return fee.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Bank account matching
# --------------------------------------------------------------------------


def norm_bank_code(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits[:3]


def norm_bank_no(no: str) -> str:
    digits = re.sub(r"\D", "", no or "")
    return digits.lstrip("0") or digits


def parse_bank_no_check(text: str) -> list[tuple[str, str]]:
    """'822-9999999999,004-888888888' -> [('822', '9999999999'), ('004', '888888888')]"""
    out = []
    for part in re.split(r"[,，;\s]+", text or ""):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ApiError("bank_no_check Format Error.")
        code, no = part.split("-", 1)
        code, no = norm_bank_code(code), re.sub(r"\D", "", no)
        if len(code) != 3 or not no:
            raise ApiError("bank_no_check Format Error.")
        out.append((code, no))
    return out


def same_account(code_a: str, no_a: str, code_b: str, no_b: str) -> bool:
    return norm_bank_code(code_a) == norm_bank_code(code_b) and norm_bank_no(no_a) == norm_bank_no(no_b)


def norm_name(name: str) -> str:
    return re.sub(r"\s+", "", name or "")


@dataclass
class MatchResult:
    account_ok: bool
    amount_ok: bool
    name_ok: bool
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.account_ok and self.amount_ok


def check_deposit(order: m.Order, bank_code: str, bank_no: str, amount: Decimal) -> MatchResult:
    """Check a reported incoming transfer against what the merchant declared for the player."""
    allowed = list(parse_bank_no_check(order.bank_no_check)) if order.bank_no_check else []
    if order.member:
        allowed += [(a.bank_code, a.bank_no) for a in order.member.bank_accounts]
    account_ok = any(same_account(bank_code, bank_no, c, n) for c, n in allowed)
    amount_ok = amount == order.amount
    name_ok = True
    if order.bank_no_check_title and order.payer:
        name_ok = norm_name(order.bank_no_check_title) == norm_name(order.payer)
    problems = []
    if not allowed:
        problems.append("此會員沒有登記任何銀行帳戶")
    elif not account_ok:
        problems.append(f"轉出帳戶 {bank_code}-{bank_no} 不在會員登記的帳戶中")
    if not amount_ok:
        problems.append(f"到帳金額 {amount} 與訂單金額 {order.amount} 不符")
    if not name_ok:
        problems.append("銀行戶名與會員姓名不同")
    return MatchResult(account_ok, amount_ok, name_ok, problems)


def withdraw_warnings(order: m.Order) -> list[str]:
    """Things a reviewer should look at before approving a withdrawal."""
    warns = []
    if norm_name(order.bank_title) != norm_name(order.payer):
        warns.append("收款戶名與會員姓名不同")
    member = order.member
    if member:
        if member.status != "active":
            warns.append("會員已停用")
        if member.bank_accounts and not any(
            same_account(order.bank_code or order.bank_name, order.bank_no, a.bank_code, a.bank_no)
            or norm_bank_no(order.bank_no) == norm_bank_no(a.bank_no)
            for a in member.bank_accounts
        ):
            warns.append("收款帳號不在會員登記的帳戶中")
    return warns


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------


def get_or_create_member(db: Session, merchant: m.Merchant, name: str, phone: str, pid: str) -> Optional[m.Member]:
    if not pid:
        return None
    member = db.scalar(select(m.Member).where(m.Member.merchant_id == merchant.id, m.Member.pid == pid))
    if member is None:
        member = m.Member(merchant_id=merchant.id, name=name, phone=phone, pid=pid)
        db.add(member)
        db.flush()
    elif phone and not member.phone:
        member.phone = phone
    return member


def add_member_account(db: Session, member: m.Member, code: str, no: str, name: str = "") -> bool:
    code = norm_bank_code(code)
    no = re.sub(r"\D", "", no)
    for a in member.bank_accounts:
        if same_account(a.bank_code, a.bank_no, code, no):
            return False
    member.bank_accounts.append(m.MemberBankAccount(bank_code=code, bank_no=no, account_name=name))
    db.flush()
    return True


# --------------------------------------------------------------------------
# Order creation
# --------------------------------------------------------------------------


def _event(db: Session, order: m.Order, source: str, actor: str, action: str, frm, to, note: str = ""):
    db.add(
        m.OrderEvent(order=order, source=source, actor=actor, action=action, from_status=frm, to_status=to, note=note)
    )


def _assign_sid(db: Session, order: m.Order) -> None:
    db.flush()
    order.order_sid = f"{order.init_time:%Y%m%d}{order.id:07d}"


def _require(data: dict, key: str, max_len: int) -> str:
    val = data.get(key)
    if val is None or str(val).strip() == "":
        raise ApiError(f"{key} Empty.")
    val = str(val).strip()
    if len(val) > max_len:
        raise ApiError(f"{key} Too Long.")
    return val


def _optional(data: dict, key: str, max_len: int) -> str:
    val = data.get(key)
    if val is None:
        return ""
    val = str(val).strip()
    if len(val) > max_len:
        raise ApiError(f"{key} Too Long.")
    return val


def _require_url(data: dict) -> str:
    url = _require(data, "callback", 256)
    if not re.match(r"^https?://[^\s/]+", url):
        raise ApiError("callback Format Error.")
    return url


def _check_duplicate(db: Session, merchant: m.Merchant, order_id: str) -> None:
    if db.scalar(select(m.Order.id).where(m.Order.merchant_id == merchant.id, m.Order.order_id == order_id)):
        raise ApiError("Duplicate order_id.", {"order_id": order_id})


def pick_receiving_account(db: Session, amount: Decimal) -> Optional[m.ReceivingAccount]:
    """Least recently used enabled company account that is under its daily limit."""
    from sqlalchemy import func

    today = now_tw().replace(hour=0, minute=0, second=0)
    accounts = db.scalars(
        select(m.ReceivingAccount)
        .where(m.ReceivingAccount.enabled.is_(True))
        .order_by(m.ReceivingAccount.last_used_at.is_not(None), m.ReceivingAccount.last_used_at, m.ReceivingAccount.id)
    ).all()
    for acc in accounts:
        if acc.daily_limit and acc.daily_limit > 0:
            used = db.scalar(
                select(func.coalesce(func.sum(m.Order.amount), 0)).where(
                    m.Order.receiving_account_id == acc.id,
                    m.Order.init_time >= today,
                    m.Order.status.in_([m.ST_NEW, m.ST_MATCHED, m.ST_ACCEPTED, m.ST_DONE]),
                )
            )
            if Decimal(used) + amount > acc.daily_limit:
                continue
        return acc
    return None


def create_receive(db: Session, merchant: m.Merchant, data: dict) -> m.Order:
    payer = _require(data, "payer", 15)
    order_id = _require(data, "order_id", 20)
    amount = parse_amount(data.get("amount"))
    callback = _require_url(data)
    message = _optional(data, "message", 256)
    phone = _optional(data, "payer_cellphone", 20)
    pid = _optional(data, "payer_pid", 20)
    check_title = _optional(data, "bank_no_check_title", 15)
    check = _optional(data, "bank_no_check", 128)
    accounts = parse_bank_no_check(check)
    _check_duplicate(db, merchant, order_id)

    member = get_or_create_member(db, merchant, payer, phone, pid)
    if member and member.status != "active":
        raise ApiError("Member blocked.")
    if member:
        for code, no in accounts:
            add_member_account(db, member, code, no, check_title)

    now = now_tw()
    order = m.Order(
        merchant_id=merchant.id,
        order_id=order_id,
        type=m.TYPE_RECEIVE,
        member=member,
        payer=payer,
        payer_cellphone=phone,
        payer_pid=pid,
        amount=amount,
        fee=calc_fee(amount, merchant.receive_fee_rate),
        callback=callback,
        message=message,
        bank_no_check=",".join(f"{c}-{n}" for c, n in accounts),
        bank_no_check_title=check_title,
        init_time=now,
        end_time=now + timedelta(minutes=settings.receive_timeout_minutes),
        cashier_token=secrets.token_urlsafe(24),
        status=m.ST_NEW,
    )
    db.add(order)
    acc = pick_receiving_account(db, amount)
    if acc:
        acc.last_used_at = now
        order.receiving_account = acc
        order.bank_code = acc.bank_code
        order.bank_name = acc.bank_name
        order.bank_branch = acc.bank_branch
        order.bank_title = acc.bank_title
        order.bank_no = acc.bank_no
        order.status = m.ST_MATCHED
    _assign_sid(db, order)
    _event(db, order, "api", merchant.name, "create", None, order.status)
    return order


def create_withdraw(db: Session, merchant: m.Merchant, data: dict) -> m.Order:
    payer = _require(data, "payer", 15)
    order_id = _require(data, "order_id", 20)
    amount = parse_amount(data.get("amount"))
    bank_title = _require(data, "bank_title", 20)
    bank_name = _require(data, "bank_name", 20)
    bank_branch = _optional(data, "bank_branch", 20)
    bank_no = _require(data, "bank_no", 30)
    callback = _require_url(data)
    message = _optional(data, "message", 256)
    phone = _optional(data, "payer_cellphone", 20)
    pid = _optional(data, "payer_pid", 20)
    _check_duplicate(db, merchant, order_id)

    member = get_or_create_member(db, merchant, payer, phone, pid)
    if member and member.status != "active":
        raise ApiError("Member blocked.")

    fee = calc_fee(amount, merchant.withdraw_fee_rate, merchant.withdraw_fee_fixed)
    total = amount + fee
    # Lock the merchant row while moving money between balance and frozen.
    locked = db.scalar(select(m.Merchant).where(m.Merchant.id == merchant.id).with_for_update())
    if locked.balance < total:
        raise ApiError("Insufficient balance.")
    locked.balance -= total
    locked.balance_frozen += total

    now = now_tw()
    bank_code = norm_bank_code(bank_name) if re.fullmatch(r"\s*\d{3}\s*", bank_name) else ""
    order = m.Order(
        merchant_id=merchant.id,
        order_id=order_id,
        type=m.TYPE_WITHDRAW,
        member=member,
        payer=payer,
        payer_cellphone=phone,
        payer_pid=pid,
        amount=amount,
        fee=fee,
        callback=callback,
        message=message,
        bank_code=bank_code,
        bank_title=bank_title,
        bank_name=bank_name,
        bank_branch=bank_branch,
        bank_no=bank_no,
        init_time=now,
        status=m.ST_NEW,
        review_status=m.REVIEW_PENDING,
    )
    db.add(order)
    _assign_sid(db, order)
    _event(db, order, "api", merchant.name, "create", None, order.status)
    return order


# --------------------------------------------------------------------------
# State changes (admin web / Telegram / system)
# --------------------------------------------------------------------------


@dataclass
class Actor:
    source: str  # web / telegram / system
    name: str
    user: Optional[m.AdminUser] = None


def _lock(db: Session, order: m.Order) -> m.Order:
    return db.scalar(select(m.Order).where(m.Order.id == order.id).with_for_update())


def _finish(db: Session, order: m.Order, status: int, actor: Actor, action: str, note: str = "") -> None:
    frm = order.status
    order.status = status
    order.is_finish = status == m.ST_DONE
    order.finish_time = now_tw()
    if actor.user is not None:
        order.handled_by_id = actor.user.id
        order.team_id = actor.user.team_id
    if note:
        order.review_note = note
    order.callback_status = m.CALLBACK_PENDING if order.callback else ""
    order.callback_attempts = 0
    order.callback_next_at = now_tw()
    _event(db, order, actor.source, actor.name, action, frm, status, note)


def _merchant_locked(db: Session, order: m.Order) -> m.Merchant:
    return db.scalar(select(m.Merchant).where(m.Merchant.id == order.merchant_id).with_for_update())


def confirm_deposit(
    db: Session,
    order: m.Order,
    actor: Actor,
    bank_code: str,
    bank_no: str,
    amount: Decimal,
    force: bool = False,
    note: str = "",
) -> MatchResult:
    """Mark a deposit as received. Refuses when the source account or amount
    doesn't match, unless an admin forces it."""
    order = _lock(db, order)
    if order.type != m.TYPE_RECEIVE:
        raise ActionError("不是收款訂單")
    if order.is_final:
        raise ActionError(f"訂單已是終態：{order.status_label}")
    result = check_deposit(order, bank_code, bank_no, amount)
    if not result.ok:
        if not force:
            return result
        if actor.user is None or actor.user.role != m.ROLE_ADMIN:
            raise ActionError("只有管理員可以強制入帳")
        if not note:
            raise ActionError("強制入帳需填寫原因")
    order.paid_from_bank_code = norm_bank_code(bank_code)
    order.paid_from_bank_no = re.sub(r"\D", "", bank_no)
    order.paid_amount = amount
    merchant = _merchant_locked(db, order)
    merchant.balance += order.amount - order.fee
    full_note = note
    if result.problems:
        full_note = "強制入帳：" + "；".join(result.problems) + (f"；{note}" if note else "")
    _finish(db, order, m.ST_DONE, actor, "confirm", full_note)
    return result


def fail_deposit(db: Session, order: m.Order, actor: Actor, status: int, note: str = "") -> None:
    order = _lock(db, order)
    if order.type != m.TYPE_RECEIVE:
        raise ActionError("不是收款訂單")
    if order.is_final:
        raise ActionError(f"訂單已是終態：{order.status_label}")
    if status not in (m.ST_RECEIVE_TIMEOUT, m.ST_AMOUNT_MISMATCH, m.ST_INVALID, m.ST_REJECTED):
        raise ActionError("不支援的狀態")
    _finish(db, order, status, actor, "fail", note)


def _check_reviewer(actor: Actor) -> None:
    if actor.user is not None and actor.user.role not in (m.ROLE_ADMIN, m.ROLE_REVIEWER):
        raise ActionError("沒有審單權限")


def approve_withdraw(db: Session, order: m.Order, actor: Actor, note: str = "") -> None:
    _check_reviewer(actor)
    order = _lock(db, order)
    if order.type != m.TYPE_WITHDRAW or order.review_status != m.REVIEW_PENDING or order.is_final:
        raise ActionError("此訂單不在待審核狀態")
    frm = order.status
    order.review_status = m.REVIEW_APPROVED
    order.status = m.ST_ACCEPTED
    if actor.user is not None:
        order.handled_by_id = actor.user.id
        order.team_id = actor.user.team_id
    if note:
        order.review_note = note
    _event(db, order, actor.source, actor.name, "approve", frm, order.status, note)


def _release_frozen(db: Session, order: m.Order, refund: bool) -> None:
    merchant = _merchant_locked(db, order)
    total = order.amount + order.fee
    merchant.balance_frozen -= total
    if refund:
        merchant.balance += total


def reject_withdraw(db: Session, order: m.Order, actor: Actor, note: str = "") -> None:
    _check_reviewer(actor)
    order = _lock(db, order)
    if order.type != m.TYPE_WITHDRAW or order.is_final or order.review_status != m.REVIEW_PENDING:
        raise ActionError("此訂單不在待審核狀態")
    order.review_status = m.REVIEW_REJECTED
    _release_frozen(db, order, refund=True)
    _finish(db, order, m.ST_REJECTED, actor, "reject", note)


def complete_withdraw(db: Session, order: m.Order, actor: Actor, note: str = "") -> None:
    """The payout transfer has been made from the company account."""
    _check_reviewer(actor)
    order = _lock(db, order)
    if order.type != m.TYPE_WITHDRAW or order.review_status != m.REVIEW_APPROVED or order.is_final:
        raise ActionError("此訂單尚未審核通過或已結束")
    _release_frozen(db, order, refund=False)
    _finish(db, order, m.ST_DONE, actor, "paid", note)


def fail_withdraw(db: Session, order: m.Order, actor: Actor, note: str = "") -> None:
    """Approved, but the payout could not be made; money goes back to the merchant."""
    _check_reviewer(actor)
    order = _lock(db, order)
    if order.type != m.TYPE_WITHDRAW or order.review_status != m.REVIEW_APPROVED or order.is_final:
        raise ActionError("此訂單尚未審核通過或已結束")
    _release_frozen(db, order, refund=True)
    _finish(db, order, m.ST_INVALID, actor, "pay_failed", note)


def expire_receive_orders(db: Session) -> list[int]:
    """Time out open deposits past their end_time. Returns affected order ids."""
    now = now_tw()
    rows = db.scalars(
        select(m.Order).where(
            m.Order.type == m.TYPE_RECEIVE,
            m.Order.status.in_([m.ST_NEW, m.ST_MATCHED, m.ST_ACCEPTED]),
            m.Order.end_time.is_not(None),
            m.Order.end_time < now,
        )
    ).all()
    actor = Actor("system", "system")
    for order in rows:
        status = m.ST_MATCH_TIMEOUT if order.status == m.ST_NEW else m.ST_RECEIVE_TIMEOUT
        _finish(db, order, status, actor, "timeout")
    return [o.id for o in rows]


def adjust_balance(db: Session, merchant: m.Merchant, delta: Decimal, actor_name: str, note: str) -> None:
    locked = db.scalar(select(m.Merchant).where(m.Merchant.id == merchant.id).with_for_update())
    if locked.balance + delta < 0:
        raise ActionError("調整後餘額不可小於 0")
    locked.balance += delta
    db.add(m.AuditLog(actor=actor_name, action="balance_adjust", detail=f"{locked.name} {delta:+} {note}"))
