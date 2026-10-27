"""Telegram bot: post order updates to groups and take review actions back.

Outgoing: every new / changed order is posted (or the existing post edited)
in the member's team group, falling back to TELEGRAM_CHAT_ID.
Incoming (webhook): inline buttons on withdrawals (通過 / 駁回 / 已出款 /
出款失敗) and text commands. Only Telegram users linked to an enabled admin
user with the reviewer or admin role may act, and only from known groups.
"""

import html
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import models as m
from . import services
from .config import settings
from .timeutil import fmt

log = logging.getLogger(__name__)

HELP = (
    "指令：\n"
    "/q 單號 — 查單（系統單號或商戶單號）\n"
    "/in 系統單號 銀行代碼-帳號 金額 — 確認收款入帳\n"
    "/pending — 待審核付款訂單\n"
    "/reject 系統單號 原因 — 駁回付款訂單\n"
    "/balance — 商戶餘額"
)

_client: Optional[httpx.Client] = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=10)
    return _client


def call(method: str, payload: dict) -> Optional[dict]:
    if not settings.telegram_enabled:
        return None
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    try:
        resp = _http().post(url, json=payload)
        data = resp.json()
        if not data.get("ok"):
            log.warning("telegram %s failed: %s", method, data.get("description"))
            return None
        return data.get("result")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("telegram %s error: %s", method, exc)
        return None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def mask(text: str, keep_start: int = 2, keep_end: int = 3) -> str:
    text = text or ""
    if len(text) <= keep_start + keep_end:
        return text
    return text[:keep_start] + "*" * (len(text) - keep_start - keep_end) + text[-keep_end:]


def render(order: m.Order) -> str:
    e = html.escape
    lines = [
        f"<b>{'💰 收款' if order.type == m.TYPE_RECEIVE else '💸 付款'}訂單 {e(order.order_sid)}</b>",
        f"商戶：{e(order.merchant.name)}　商戶單號：{e(order.order_id)}",
        f"會員：{e(order.payer)}　身分證：{e(mask(order.payer_pid))}",
        f"金額：<b>{order.amount}</b>　手續費：{order.fee}",
    ]
    if order.type == m.TYPE_RECEIVE:
        if order.bank_no_check:
            accounts = ", ".join(f"{c}-{mask(n, 0, 5)}" for c, n in services.parse_bank_no_check(order.bank_no_check))
            lines.append(f"會員登記轉出帳戶：{e(accounts)}")
        if order.receiving_account:
            lines.append(f"收款帳戶：{e(order.bank_name)} {e(mask(order.bank_no, 0, 5))}")
        if order.paid_from_bank_no:
            lines.append(f"實際轉出：{e(order.paid_from_bank_code)}-{e(mask(order.paid_from_bank_no, 0, 5))}")
    else:
        lines.append(
            f"收款人：{e(order.bank_title)}　{e(order.bank_name)} {e(order.bank_branch)}"
            f"　帳號：<code>{e(order.bank_no)}</code>"
        )
        for warn in services.withdraw_warnings(order):
            lines.append(f"⚠️ {e(warn)}")
    status = order.status_label
    if order.review_status not in (m.REVIEW_NONE, ""):
        status += f"／{order.review_label}"
    lines.append(f"狀態：<b>{e(status)}</b>")
    if order.handled_by:
        lines.append(f"處理人：{e(order.handled_by.label)}")
    if order.review_note:
        lines.append(f"備註：{e(order.review_note)}")
    lines.append(f"建立：{fmt(order.init_time)}")
    if order.finish_time:
        lines.append(f"完成：{fmt(order.finish_time)}")
    return "\n".join(lines)


def keyboard(order: m.Order) -> Optional[dict]:
    if order.type != m.TYPE_WITHDRAW or order.is_final:
        return None
    sid = order.order_sid
    if order.review_status == m.REVIEW_PENDING:
        row = [{"text": "✅ 通過", "callback_data": f"ap:{sid}"}, {"text": "❌ 駁回", "callback_data": f"rj:{sid}"}]
    elif order.review_status == m.REVIEW_APPROVED:
        row = [
            {"text": "💸 已出款", "callback_data": f"pd:{sid}"},
            {"text": "⚠️ 出款失敗", "callback_data": f"pf:{sid}"},
        ]
    else:
        return None
    return {"inline_keyboard": [row]}


def chat_for(order: m.Order) -> str:
    if order.member and order.member.team and order.member.team.tg_chat_id:
        return order.member.team.tg_chat_id
    return settings.telegram_chat_id


def known_chats(db: Session) -> set[str]:
    chats = {c for c in db.scalars(select(m.Team.tg_chat_id).where(m.Team.tg_chat_id != "")).all()}
    if settings.telegram_chat_id:
        chats.add(settings.telegram_chat_id)
    return chats


# --------------------------------------------------------------------------
# Outgoing
# --------------------------------------------------------------------------


def notify(db: Session, order_id: int) -> None:
    """Post a new message for the order, or edit the ones already posted."""
    if not settings.telegram_enabled:
        return
    order = db.get(m.Order, order_id)
    if order is None:
        return
    text, markup = render(order), keyboard(order)
    posts = db.scalars(select(m.TelegramMessage).where(m.TelegramMessage.order_id == order.id)).all()
    if posts:
        for post in posts:
            payload = {"chat_id": post.chat_id, "message_id": post.message_id, "text": text, "parse_mode": "HTML"}
            payload["reply_markup"] = markup or {"inline_keyboard": []}
            call("editMessageText", payload)
        return
    chat = chat_for(order)
    if not chat:
        return
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    result = call("sendMessage", payload)
    if result:
        db.add(m.TelegramMessage(order_id=order.id, chat_id=str(chat), message_id=result["message_id"]))
        db.commit()


def notify_safe(order_id: int) -> None:
    """Background-task entry point with its own session."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        notify(db, order_id)
    except Exception:  # never let a Telegram problem break the request
        log.exception("telegram notify failed for order %s", order_id)
    finally:
        db.close()


def send_text(chat_id, text: str, reply_to: Optional[int] = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    call("sendMessage", payload)


# --------------------------------------------------------------------------
# Incoming
# --------------------------------------------------------------------------


def _user_for(db: Session, tg_user_id) -> Optional[m.AdminUser]:
    if tg_user_id is None:
        return None
    return db.scalar(
        select(m.AdminUser).where(m.AdminUser.tg_user_id == str(tg_user_id), m.AdminUser.enabled.is_(True))
    )


def _find_order(db: Session, key: str) -> Optional[m.Order]:
    key = key.strip()
    if not key:
        return None
    return db.scalars(
        select(m.Order).where(or_(m.Order.order_sid == key, m.Order.order_id == key)).order_by(m.Order.id.desc())
    ).first()


def handle_update(db: Session, update: dict) -> list[int]:
    """Process one Telegram update. Returns ids of orders that changed."""
    if "callback_query" in update:
        return _handle_button(db, update["callback_query"])
    msg = update.get("message") or {}
    if msg.get("text"):
        return _handle_text(db, msg)
    return []


BUTTON_ACTIONS = {
    "ap": (services.approve_withdraw, "Telegram 審核通過"),
    "rj": (services.reject_withdraw, "Telegram 駁回"),
    "pd": (services.complete_withdraw, "Telegram 已出款"),
    "pf": (services.fail_withdraw, "Telegram 出款失敗"),
}


def _handle_button(db: Session, cq: dict) -> list[int]:
    def answer(text: str) -> None:
        call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": text, "show_alert": True})

    chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
    if chat_id not in known_chats(db):
        answer("此群組未授權")
        return []
    user = _user_for(db, (cq.get("from") or {}).get("id"))
    if user is None or user.role not in (m.ROLE_ADMIN, m.ROLE_REVIEWER):
        answer("你沒有審單權限，請先在後台綁定 Telegram ID")
        return []
    action, _, sid = (cq.get("data") or "").partition(":")
    if action not in BUTTON_ACTIONS:
        answer("未知操作")
        return []
    order = db.scalar(select(m.Order).where(m.Order.order_sid == sid))
    if order is None:
        answer("找不到訂單")
        return []
    fn, note = BUTTON_ACTIONS[action]
    try:
        fn(db, order, services.Actor("telegram", user.label, user), note)
        db.commit()
    except services.ActionError as exc:
        db.rollback()
        answer(str(exc))
        return [order.id]  # refresh the post anyway, it may be stale
    answer("已處理")
    return [order.id]


def _handle_text(db: Session, msg: dict) -> list[int]:
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = msg["text"].strip()
    reply_to = msg.get("message_id")
    if not text.startswith("/"):
        return []
    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    if chat_id not in known_chats(db):
        return []
    user = _user_for(db, (msg.get("from") or {}).get("id"))
    if user is None:
        send_text(chat_id, "你尚未綁定後台帳號，請管理員在「用戶資料」填入你的 Telegram ID。", reply_to)
        return []

    if cmd in ("/start", "/help"):
        send_text(chat_id, html.escape(HELP), reply_to)
        return []

    if cmd == "/q":
        order = _find_order(db, args[0]) if args else None
        send_text(chat_id, render(order) if order else "找不到訂單", reply_to)
        return []

    if cmd == "/balance":
        rows = db.scalars(select(m.Merchant).where(m.Merchant.enabled.is_(True)).order_by(m.Merchant.id)).all()
        lines = [f"{html.escape(r.name)}：餘額 {r.balance}，凍結 {r.balance_frozen}" for r in rows]
        send_text(chat_id, "\n".join(lines) or "沒有商戶", reply_to)
        return []

    if cmd == "/pending":
        rows = db.scalars(
            select(m.Order)
            .where(m.Order.review_status == m.REVIEW_PENDING, m.Order.status < 90)
            .order_by(m.Order.id)
            .limit(30)
        ).all()
        lines = [f"{r.order_sid}　{html.escape(r.payer)}　{r.amount}" for r in rows]
        send_text(chat_id, "待審核付款訂單：\n" + "\n".join(lines) if lines else "沒有待審核付款訂單", reply_to)
        return []

    if cmd == "/in":
        if len(args) < 3 or "-" not in args[1]:
            send_text(chat_id, "格式：/in 系統單號 銀行代碼-帳號 金額", reply_to)
            return []
        order = db.scalar(select(m.Order).where(m.Order.order_sid == args[0]))
        if order is None:
            send_text(chat_id, "找不到訂單", reply_to)
            return []
        if user.role not in (m.ROLE_ADMIN, m.ROLE_REVIEWER):
            send_text(chat_id, "你沒有審單權限", reply_to)
            return []
        code, no = args[1].split("-", 1)
        try:
            amount = Decimal(args[2].replace(",", ""))
        except InvalidOperation:
            send_text(chat_id, "金額格式錯誤", reply_to)
            return []
        try:
            result = services.confirm_deposit(db, order, services.Actor("telegram", user.label, user), code, no, amount)
        except services.ActionError as exc:
            db.rollback()
            send_text(chat_id, html.escape(str(exc)), reply_to)
            return []
        if not result.ok:
            db.rollback()
            send_text(
                chat_id,
                "❌ 資料不符，未入帳：\n" + html.escape("\n".join(result.problems)) + "\n請到後台處理。",
                reply_to,
            )
            return []
        db.commit()
        note = "" if result.name_ok else "\n⚠️ 銀行戶名與會員姓名不同"
        send_text(chat_id, f"✅ {order.order_sid} 已入帳{html.escape(note)}", reply_to)
        return [order.id]

    if cmd == "/reject":
        if len(args) < 2:
            send_text(chat_id, "格式：/reject 系統單號 原因", reply_to)
            return []
        order = db.scalar(select(m.Order).where(m.Order.order_sid == args[0]))
        if order is None:
            send_text(chat_id, "找不到訂單", reply_to)
            return []
        try:
            services.reject_withdraw(db, order, services.Actor("telegram", user.label, user), " ".join(args[1:]))
            db.commit()
        except services.ActionError as exc:
            db.rollback()
            send_text(chat_id, html.escape(str(exc)), reply_to)
            return []
        send_text(chat_id, f"已駁回 {order.order_sid}", reply_to)
        return [order.id]

    return []


def set_webhook(url: str) -> Optional[dict]:
    payload = {"url": url, "allowed_updates": ["message", "callback_query"]}
    if settings.telegram_webhook_secret:
        payload["secret_token"] = settings.telegram_webhook_secret
    return call("setWebhook", payload)
