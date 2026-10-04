from decimal import Decimal

from lspay import models as m
from lspay import telegram

from .conftest import HEADERS, signed
from .test_api import RECEIVE, WITHDRAW


def button(data, user_id="777", chat_id=-1001):
    return {
        "callback_query": {
            "id": "1",
            "from": {"id": int(user_id)},
            "message": {"message_id": 5, "chat": {"id": chat_id}},
            "data": data,
        }
    }


def text(t, user_id="777", chat_id=-1001):
    return {"message": {"message_id": 9, "from": {"id": int(user_id)}, "chat": {"id": chat_id}, "text": t}}


def make_withdraw(client, merchant, session):
    merchant.balance = Decimal("5000")
    session.commit()
    return client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()["data"]["order_sid"]


def test_buttons_approve_and_pay(client, merchant, session, reviewer):
    sid = make_withdraw(client, merchant, session)
    assert telegram.handle_update(session, button(f"ap:{sid}"))
    assert telegram.handle_update(session, button(f"pd:{sid}"))
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    session.refresh(order)
    assert order.status == m.ST_DONE and order.handled_by_id == reviewer.id
    assert [e.source for e in order.events][-1] == "telegram"


def test_unknown_user_or_chat_is_ignored(client, merchant, session, reviewer):
    sid = make_withdraw(client, merchant, session)
    assert telegram.handle_update(session, button(f"ap:{sid}", user_id="999")) == []
    assert telegram.handle_update(session, button(f"ap:{sid}", chat_id=-42)) == []
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    assert order.review_status == m.REVIEW_PENDING


def test_deposit_command_checks_account(client, merchant, account, session, reviewer):
    sid = client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS).json()["data"]["order_sid"]
    assert telegram.handle_update(session, text(f"/in {sid} 812-1111 1000")) == []
    assert telegram.handle_update(session, text(f"/in {sid} 822-99999999 1000"))
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    session.refresh(order)
    assert order.status == m.ST_DONE


def test_reject_command(client, merchant, session, reviewer):
    sid = make_withdraw(client, merchant, session)
    assert telegram.handle_update(session, text(f"/reject {sid} 戶名不符"))
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    session.refresh(order)
    assert order.status == m.ST_REJECTED and order.review_note == "戶名不符"


def test_render_masks_pid(client, merchant, account, session):
    sid = client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS).json()["data"]["order_sid"]
    out = telegram.render(session.query(m.Order).filter_by(order_sid=sid).one())
    assert "A123456789" not in out and "A1*****789" in out


def test_webhook_requires_secret(client):
    assert client.post("/telegram/webhook", json={}).status_code == 403
    ok = client.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
    assert ok.status_code == 200
