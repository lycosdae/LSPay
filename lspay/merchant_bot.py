"""Merchant bot: the same Telegram bot, used by merchants in a private chat.

A merchant links a Telegram account with a one-time bind code from the
merchant backend, then uses the menu keyboard to check balances and
subtotals, open or close receiving / payouts, look up orders, set the
settlement account, and see their invite code. Every value is read from and
written to the same database as the admin and merchant backends.
"""

import html
import json
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import models as m
from . import services, telegram
from .banks import bank_name
from .config import settings
from .timeutil import fmt

B_BALANCE = "💰 可用額度"
B_BANK = "🏦 銀行帳戶設定"
B_RECV_ON, B_RECV_OFF = "開啟收款", "關閉收款"
B_PAY_ON, B_PAY_OFF = "開啟代付", "關閉代付"
B_ORDER = "🔍 查詢訂單"
B_INVITE = "🎁 推廣邀請碼"
B_SUPPORT = "🆘 與客服人員聯絡 🆘"
SUBTOTAL_BUTTONS = {f"{label}小計": key for key, label in services.PERIODS.items()}
ON = "✅ "

NOT_BOUND = (
    "這個 Telegram 帳號還沒有綁定商戶。\n請登入商戶後台，打開「Telegram 機器人」頁面取得綁定碼，再把綁定碼傳給我。"
)


def _n(value) -> str:
    value = Decimal(value or 0)
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"


def keyboard(merchant: m.Merchant) -> dict:
    def mark(label: str, on: bool) -> str:
        return ON + label if on else label

    recv, pay = merchant.receive_open, merchant.withdraw_open
    periods = list(SUBTOTAL_BUTTONS)
    return {
        "keyboard": [
            [B_BALANCE, B_BANK],
            [mark(B_RECV_ON, recv), mark(B_RECV_OFF, not recv)],
            [mark(B_PAY_ON, pay), mark(B_PAY_OFF, not pay)],
            periods[:3],
            periods[3:],
            [B_ORDER, B_INVITE],
            [B_SUPPORT],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def send(chat_id, text: str, markup: Optional[dict] = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    telegram.call("sendMessage", payload)


def ask(chat_id, text: str) -> None:
    send(chat_id, text, {"force_reply": True, "input_field_placeholder": text[:64]})


def notify_staff(merchant: m.Merchant, text: str) -> None:
    chat = merchant.tg_chat_id or settings.telegram_chat_id
    if chat:
        telegram.send_text(chat, text)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


def balance_text(merchant: m.Merchant) -> str:
    def state(admin_ok: bool, is_open: bool) -> str:
        if not admin_ok:
            return "⛔ 管理員已停用"
        return "✅ 開啟" if is_open else "⏸ 關閉"

    return (
        f"<b>{html.escape(merchant.client_sid)} {html.escape(merchant.name)}</b>\n"
        f"商戶餘額：{_n(merchant.total_balance)}\n"
        f"暫時凍結(待出款)：{_n(merchant.balance_frozen)}\n"
        f"凍結金額：{_n(merchant.balance_hold)}\n"
        f"可動用餘額：<b>{_n(merchant.available_balance)}</b>\n"
        f"收款：{state(merchant.can_receive, merchant.receive_open)}\n"
        f"代付：{state(merchant.can_withdraw, merchant.withdraw_open)}"
    )


def subtotal_text(db: Session, merchant: m.Merchant, key: str) -> str:
    start, end = services.period_range(key)
    totals = services.merchant_totals(db, merchant.id, start, end)
    last = end - timedelta(days=1)
    span = f"{start:%Y-%m-%d}" if last == start else f"{start:%Y-%m-%d} ～ {last:%Y-%m-%d}"
    lines = [f"📊 <b>{services.PERIODS[key]}小計</b>（{span}）"]
    for otype, label in ((m.TYPE_RECEIVE, "代收"), (m.TYPE_WITHDRAW, "代付")):
        count, amount, fee = totals[otype]
        lines.append(f"{label}：{count} 筆　金額 {_n(amount)}　手續費 {_n(fee)}")
    return "\n".join(lines)


def bank_text(merchant: m.Merchant) -> str:
    if not merchant.settle_bank_no:
        current = "尚未設定"
    else:
        code = merchant.settle_bank_code
        current = (
            f"{code} {html.escape(bank_name(code) or '')}\n"
            f"帳號：{html.escape(merchant.settle_bank_no)}\n"
            f"戶名：{html.escape(merchant.settle_bank_title)}"
        )
    return f"🏦 <b>結算帳戶</b>（商戶餘額提領到這個帳戶）\n{current}"


def order_text(order: m.Order) -> str:
    kind = "代收" if order.type == m.TYPE_RECEIVE else "代付"
    return (
        f"<b>{kind}訂單</b>\n"
        f"系統單號：{html.escape(order.order_sid)}\n"
        f"商戶單號：{html.escape(order.order_id)}\n"
        f"金額：{_n(order.amount)}　手續費：{_n(order.fee)}\n"
        f"狀態：{html.escape(m.STATUS_LABELS.get(order.status, str(order.status)))}\n"
        f"建立：{fmt(order.init_time) or '-'}\n"
        f"完成：{fmt(order.finish_time) or '-'}"
    )


def invite_text(db: Session, merchant: m.Merchant) -> str:
    code = services.ensure_invite_code(db, merchant)
    count = db.scalar(select(func.count()).where(m.Merchant.referred_by_id == merchant.id))
    return (
        f"🎁 <b>推廣邀請碼：<code>{code}</code></b>\n"
        "把這組邀請碼給想開戶的新商戶，開戶時提供邀請碼，就會記錄為您推薦。\n"
        f"目前已推薦 {count} 家商戶。"
    )


def support_text() -> str:
    contact = settings.support_contact
    if contact:
        return f"🆘 請直接聯絡客服：{html.escape(contact)}"
    return "🆘 請聯絡您的業務窗口或客服群組。"


# --------------------------------------------------------------------------
# Incoming
# --------------------------------------------------------------------------


def _link(db: Session, tg_user_id) -> Optional[m.MerchantTgUser]:
    if tg_user_id is None:
        return None
    return db.scalar(select(m.MerchantTgUser).where(m.MerchantTgUser.tg_user_id == str(tg_user_id)))


def _set_state(link: m.MerchantTgUser, state: str, data: Optional[dict] = None) -> None:
    link.state = state
    link.state_data = json.dumps(data or {}, ensure_ascii=False)


def _toggle(db: Session, merchant: m.Merchant, kind: str, is_open: bool, who: str) -> str:
    admin_ok = merchant.can_receive if kind == "receive" else merchant.can_withdraw
    label = "收款" if kind == "receive" else "代付"
    if not admin_ok:
        return f"⛔ 管理員已停用{label}，請聯絡客服。"
    current = merchant.receive_open if kind == "receive" else merchant.withdraw_open
    if current == is_open:
        return f"{label}已經是{'開啟' if is_open else '關閉'}狀態。"
    if kind == "receive":
        merchant.receive_open = is_open
    else:
        merchant.withdraw_open = is_open
    action = f"{'開啟' if is_open else '關閉'}{label}"
    db.add(m.AuditLog(actor=who, action="merchant_switch", detail=f"{merchant.client_sid} {action}"))
    notify_staff(
        merchant, f"ℹ️ 商戶 {html.escape(merchant.client_sid)} {html.escape(merchant.name)} 已{action}（Telegram）"
    )
    if is_open:
        return f"✅ 已{action}。"
    return f"⏸ 已{action}，新的{label}訂單會被拒絕，進行中的訂單不受影響。"


def handle(db: Session, msg: dict) -> None:
    """A text message in a private chat with the bot."""
    chat_id = (msg.get("chat") or {}).get("id")
    sender = msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    link = _link(db, sender.get("id"))
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or sender.get("username", "")

    if link is None or not link.merchant.enabled:
        code = text.split()[-1] if text.startswith("/start") and len(text.split()) > 1 else text
        merchant = services.bind_telegram(db, code, sender.get("id"), name) if code and code[0] != "/" else None
        if merchant is None:
            send(
                chat_id, NOT_BOUND if not code or code.startswith("/") else "綁定碼錯誤或已過期，請到商戶後台重新產生。"
            )
            return
        db.commit()
        notify_staff(merchant, f"🔗 商戶 {html.escape(merchant.client_sid)} 綁定了 Telegram：{html.escape(name)}")
        send(
            chat_id,
            f"✅ 已綁定商戶 {html.escape(merchant.client_sid)} {html.escape(merchant.name)}",
            keyboard(merchant),
        )
        return

    merchant = link.merchant
    who = f"商戶 {merchant.login_name or merchant.client_sid}（TG {link.tg_name}）"
    button = text.removeprefix(ON)

    if text in ("/start", "/menu", "/cancel"):
        _set_state(link, "")
        db.commit()
        send(chat_id, balance_text(merchant), keyboard(merchant))
        return
    if text == "/unbind":
        db.delete(link)
        db.add(m.AuditLog(actor=who, action="tg_unbind", detail=merchant.client_sid))
        db.commit()
        send(chat_id, "已解除綁定。", {"remove_keyboard": True})
        return

    # Any menu button cancels a question in progress.
    menu = {B_BALANCE, B_BANK, B_RECV_ON, B_RECV_OFF, B_PAY_ON, B_PAY_OFF, B_ORDER, B_INVITE, B_SUPPORT}
    if link.state and button not in menu and button not in SUBTOTAL_BUTTONS:
        _answer_state(db, link, merchant, chat_id, text, who)
        return
    _set_state(link, "")

    if button == B_BALANCE:
        send(chat_id, balance_text(merchant), keyboard(merchant))
    elif button in (B_RECV_ON, B_RECV_OFF, B_PAY_ON, B_PAY_OFF):
        kind = "receive" if button in (B_RECV_ON, B_RECV_OFF) else "withdraw"
        reply = _toggle(db, merchant, kind, button in (B_RECV_ON, B_PAY_ON), who)
        db.commit()
        send(chat_id, reply, keyboard(merchant))
        return
    elif button in SUBTOTAL_BUTTONS:
        send(chat_id, subtotal_text(db, merchant, SUBTOTAL_BUTTONS[button]), keyboard(merchant))
    elif button == B_BANK:
        send(chat_id, bank_text(merchant) + "\n\n要修改的話，請依序輸入新的資料（輸入 /cancel 取消）。")
        _set_state(link, "bank_code")
        ask(chat_id, "請輸入銀行代碼，例如 822")
    elif button == B_ORDER:
        _set_state(link, "order")
        ask(chat_id, "請輸入訂單號（商戶單號或系統單號）")
    elif button == B_INVITE:
        send(chat_id, invite_text(db, merchant), keyboard(merchant))
    elif button == B_SUPPORT:
        send(chat_id, support_text(), keyboard(merchant))
    else:
        send(chat_id, "請使用下方選單。", keyboard(merchant))
    db.commit()


def _answer_state(db: Session, link: m.MerchantTgUser, merchant: m.Merchant, chat_id, text: str, who: str) -> None:
    data = json.loads(link.state_data or "{}")
    state = link.state

    if state == "order":
        _set_state(link, "")
        db.commit()
        order = db.scalars(
            select(m.Order)
            .where(m.Order.merchant_id == merchant.id, or_(m.Order.order_id == text, m.Order.order_sid == text))
            .order_by(m.Order.id.desc())
        ).first()
        send(chat_id, order_text(order) if order else "找不到這筆訂單。", keyboard(merchant))
        return

    if state == "bank_code":
        code = "".join(ch for ch in text if ch.isdigit())
        if len(code) != 3:
            ask(chat_id, "銀行代碼需為 3 碼數字，請重新輸入，例如 822")
            return
        _set_state(link, "bank_no", {"code": code})
        db.commit()
        ask(chat_id, f"{code} {bank_name(code) or ''}\n請輸入帳號")
        return

    if state == "bank_no":
        _set_state(link, "bank_title", {**data, "no": text})
        db.commit()
        ask(chat_id, "請輸入戶名")
        return

    if state == "bank_title":
        try:
            services.set_settle_account(db, merchant, data.get("code", ""), data.get("no", ""), text, who)
        except services.ActionError as exc:
            _set_state(link, "bank_code")
            db.commit()
            send(chat_id, f"❌ {html.escape(str(exc))}")
            ask(chat_id, "請重新輸入銀行代碼，例如 822")
            return
        _set_state(link, "")
        db.commit()
        notify_staff(merchant, f"🏦 商戶 {html.escape(merchant.client_sid)} 透過 Telegram 修改了結算帳戶，請留意。")
        send(chat_id, "✅ 已更新\n" + bank_text(merchant), keyboard(merchant))
        return

    _set_state(link, "")
    db.commit()
    send(chat_id, "請使用下方選單。", keyboard(merchant))
