import json
from decimal import Decimal

import httpx

from lspay import callbacks, services
from lspay import models as m
from lspay.signing import callback_sign

from .conftest import HEADERS, signed

RECEIVE = {
    "payer": "王小明",
    "payer_cellphone": "0912345678",
    "payer_pid": "A123456789",
    "order_id": "r1",
    "amount": 1000,
    "callback": "https://game.example/cb",
    "message": "hello",
    "bank_no_check_title": "王小明",
    "bank_no_check": "822-0099999999,004-888888888",
}

WITHDRAW = {
    "payer": "王小明",
    "payer_pid": "A123456789",
    "order_id": "w1",
    "amount": 500,
    "bank_title": "王小明",
    "bank_name": "822",
    "bank_no": "99999999",
    "callback": "https://game.example/cb",
    "message": "wd",
}


def receive(client, **over):
    return client.post("/v1s/order/receive", json=signed({**RECEIVE, **over}), headers=HEADERS).json()


def test_receive_returns_company_account(client, merchant, account):
    res = receive(client)
    assert res["code"] == 200
    d = res["data"]
    assert d["order_id"] == "r1"
    assert d["bank_no"] == "123456789012"
    assert d["bank_title"] == "LS公司"
    assert d["amount"] == "1000.00"
    assert "/cashier/" in d["fronttable_url"]


def test_receive_without_account_only_returns_cashier(client, merchant):
    d = receive(client)["data"]
    assert "bank_no" not in d and d["fronttable_url"]


def test_auth_sign_and_duplicates(client, merchant, account):
    bad = client.post("/v1s/order/receive", json=signed(RECEIVE), headers={**HEADERS, "ACCESSTOKEN": "x"}).json()
    assert bad == {"code": 400, "msg": "Authentication Failed."}
    tampered = {**signed(RECEIVE), "amount": 9999}
    assert client.post("/v1s/order/receive", json=tampered, headers=HEADERS).json()["msg"] == "Sign Error."
    assert receive(client)["code"] == 200
    dup = receive(client)
    assert dup == {"data": {"order_id": "r1"}, "code": 400, "msg": "Duplicate order_id."}
    assert receive(client, order_id="r2", payer="")["msg"] == "payer Empty."
    assert receive(client, order_id="r3", amount="1.234")["msg"] == "amount Format Error."
    assert receive(client, order_id="r4", callback="ftp://x")["msg"] == "callback Format Error."


def test_form_encoded_request(client, merchant, account):
    body = signed({**RECEIVE, "amount": "1000"})
    res = client.post("/v1s/order/receive", data=body, headers=HEADERS).json()
    assert res["code"] == 200


def test_deposit_must_come_from_registered_account(client, merchant, account, session, reviewer):
    sid = receive(client)["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    actor = services.Actor("web", reviewer.label, reviewer)

    wrong = services.confirm_deposit(session, order, actor, "812", "11111111", Decimal("1000"))
    assert not wrong.ok and not wrong.account_ok
    session.rollback()

    short = services.confirm_deposit(session, order, actor, "822", "99999999", Decimal("999"))
    assert short.account_ok and not short.amount_ok
    session.rollback()

    # leading zeros / dashes don't matter
    ok = services.confirm_deposit(session, order, actor, "822", "0099-999-999", Decimal("1000"))
    assert ok.ok
    session.commit()
    session.refresh(order)
    assert order.status == m.ST_DONE and order.is_finish
    assert order.team_id == reviewer.team_id
    session.refresh(merchant)
    assert merchant.balance == Decimal("985.00")  # 1000 - 1.5% fee


def test_force_requires_admin_and_note(client, merchant, account, session, reviewer, admin):
    sid = receive(client)["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    try:
        services.confirm_deposit(
            session, order, services.Actor("web", "rev", reviewer), "812", "1", Decimal("1000"), force=True, note="x"
        )
        assert False, "reviewer must not force"
    except services.ActionError:
        session.rollback()
    res = services.confirm_deposit(
        session, order, services.Actor("web", "boss", admin), "812", "1", Decimal("1000"), force=True, note="客服確認"
    )
    session.commit()
    assert not res.ok
    session.refresh(order)
    assert order.status == m.ST_DONE and "強制入帳" in order.review_note


def test_callback_is_sent_in_futurepay_format(client, merchant, account, session, reviewer):
    sid = receive(client)["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    services.confirm_deposit(
        session, order, services.Actor("web", "rev", reviewer), "004", "888888888", Decimal("1000")
    )
    session.commit()

    seen = []

    def handler(request: httpx.Request):
        seen.append((request.headers["ACCESSTOKEN"], json.loads(request.content)))
        return httpx.Response(200, text="success")

    with httpx.Client(transport=httpx.MockTransport(handler)) as hc:
        assert callbacks.process_due(session, hc) == 1
    token, body = seen[0]
    assert token == "apikey123"
    assert body["order_id"] == "r1" and body["status"] == "3" and body["Is_finish"] is True
    assert body["amount"] == "1000" and body["bank_no"] == "88888"
    assert body["sign"] == callback_sign("apikey123", "1000", "r1", "signkey456")
    session.refresh(order)
    assert order.callback_status == m.CALLBACK_OK


def test_callback_retries_then_fails(client, merchant, account, session, reviewer):
    sid = receive(client)["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    services.fail_deposit(session, order, services.Actor("web", "rev", reviewer), m.ST_AMOUNT_MISMATCH, "x")
    session.commit()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="err"))) as hc:
        callbacks.process_due(session, hc)
    session.refresh(order)
    assert order.callback_status == m.CALLBACK_PENDING and order.callback_attempts == 1
    assert order.callback_next_at is not None


def test_withdraw_freeze_reject_refund(client, merchant, session, reviewer):
    res = client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()
    assert res == {"code": 400, "msg": "Insufficient balance."}

    merchant.balance = Decimal("2000")
    session.commit()
    res = client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()
    assert res["code"] == 200
    session.refresh(merchant)
    assert merchant.balance == Decimal("1490.00") and merchant.balance_frozen == Decimal("510.00")

    order = session.query(m.Order).filter_by(order_sid=res["data"]["order_sid"]).one()
    assert order.review_status == m.REVIEW_PENDING
    services.reject_withdraw(session, order, services.Actor("web", "rev", reviewer), "資料不符")
    session.commit()
    session.refresh(merchant)
    session.refresh(order)
    assert order.status == m.ST_REJECTED
    assert merchant.balance == Decimal("2000.00") and merchant.balance_frozen == Decimal("0.00")


def test_withdraw_approve_and_pay(client, merchant, session, reviewer):
    merchant.balance = Decimal("2000")
    session.commit()
    sid = client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    actor = services.Actor("web", "rev", reviewer)
    try:
        services.complete_withdraw(session, order, actor)
        assert False, "cannot pay before approval"
    except services.ActionError:
        session.rollback()
    services.approve_withdraw(session, order, actor)
    services.complete_withdraw(session, order, actor)
    session.commit()
    session.refresh(merchant)
    session.refresh(order)
    assert order.status == m.ST_DONE
    assert merchant.balance == Decimal("1490.00") and merchant.balance_frozen == Decimal("0.00")


def test_withdraw_warns_on_name_mismatch(client, merchant, session):
    merchant.balance = Decimal("2000")
    session.commit()
    sid = client.post("/v1s/order/withdraw", json=signed({**WITHDRAW, "bank_title": "別人"}), headers=HEADERS).json()[
        "data"
    ]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    assert "收款戶名與會員姓名不同" in services.withdraw_warnings(order)


def test_list_and_balance(client, merchant, account):
    for i in range(3):
        receive(client, order_id=f"r{i}")
    res = client.request("GET", "/v1s/order/list", json=signed({"index_start": 0}), headers=HEADERS).json()
    assert res["code"] == 200 and res["rows"] == 3 and res["start"] == 0
    assert [r["order_id"] for r in res["data"]] == ["r2", "r1", "r0"]
    one = client.request("GET", "/v1s/order/list", json=signed({"order_id": "r1"}), headers=HEADERS).json()
    assert one["rows"] == 1 and one["data"][0]["type"] == 1
    bal = client.request("GET", "/v1s/client/balance", json=signed({}), headers=HEADERS).json()
    assert bal == {"code": 200, "balance": "0.00", "balance_frozen": "0.00"}


def test_blocked_member_rejected(client, merchant, account, session):
    receive(client)
    member = session.query(m.Member).one()
    assert len(member.bank_accounts) == 2
    member.status = "blocked"
    session.commit()
    assert receive(client, order_id="r9")["msg"] == "Member blocked."


def test_ip_whitelist(client, merchant, account, session):
    merchant.ip_whitelist = "10.0.0.1"
    session.commit()
    assert receive(client)["msg"] == "IP Not Allowed."


def test_receive_timeout(client, merchant, account, session):
    from datetime import timedelta

    sid = receive(client)["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    order.end_time = order.end_time - timedelta(hours=2)
    session.commit()
    assert services.expire_receive_orders(session) == [order.id]
    session.commit()
    session.refresh(order)
    assert order.status == m.ST_RECEIVE_TIMEOUT and order.callback_status == m.CALLBACK_PENDING


def test_cashier_page(client, merchant, account, session):
    url = receive(client)["data"]["fronttable_url"]
    path = url.split("localhost:8000")[1]
    page = client.get(path)
    assert page.status_code == 200 and "123456789012" in page.text
    assert client.get(path.split("?")[0] + "?t=wrong").status_code == 404
