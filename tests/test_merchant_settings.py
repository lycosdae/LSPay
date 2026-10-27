from decimal import Decimal

from sqlalchemy import inspect

from lspay import db, services
from lspay import models as m

from .conftest import ADMIN, HEADERS, signed
from .test_api import WITHDRAW, receive
from .test_web import login

SETTINGS = {
    "name": "信鑫",
    "login_name": "xinxin1688",
    "enabled": "1",
    "can_receive": "1",
    "can_withdraw": "0",
    "test_mode": "0",
    "receive_min": "1000",
    "receive_max": "50000",
    "withdraw_min": "1000",
    "withdraw_max": "50000",
    "daily_receive_limit": "0",
    "receive_fee_rate": "4",
    "receive_fee_fixed": "35",
    "withdraw_fee_rate": "1",
    "withdraw_fee_fixed": "35",
    "ip_whitelist": "115.78.94.215\n13.113.46.145",
    "tg_chat_id": "-5488722855",
    "same_name_max_orders": "3",
    "same_name_window_minutes": "10",
    "return_bank_info": "1",
    "return_bank_title": "0",
    "color": "#ff0000",
    "note": "",
}


def withdraw(client, **over):
    return client.post("/v1s/order/withdraw", json=signed({**WITHDRAW, **over}), headers=HEADERS).json()


def test_settings_dialog_saves_everything(client, admin, merchant, session):
    csrf = login(client)
    page = client.get(ADMIN + "/merchants").text
    assert f'id="set-{merchant.id}"' in page and "可動用商餘" in page and "今日金額" in page
    res = client.post(ADMIN + f"/merchants/{merchant.id}/settings", data={**SETTINGS, "csrf": csrf})
    assert res.status_code == 200
    session.refresh(merchant)
    assert merchant.name == "信鑫" and merchant.login_name == "xinxin1688"
    assert merchant.can_receive and not merchant.can_withdraw and not merchant.return_bank_title
    assert merchant.receive_min == 1000 and merchant.receive_max == 50000 and merchant.receive_fee_fixed == 35
    assert merchant.ip_whitelist == "115.78.94.215,13.113.46.145" and merchant.tg_chat_id == "-5488722855"
    assert merchant.same_name_max_orders == 3 and merchant.color == "#ff0000"
    page = client.get(ADMIN + "/merchants").text
    assert "xinxin1688" in page and "50,000 / 1,000" in page and "4.0% / 35" in page


def test_settings_validation(client, admin, merchant, session):
    csrf = login(client)
    session.add(m.Merchant(name="B", client_sid="0002", api_key="k", sign_key="s", login_name="taken"))
    session.commit()
    url = ADMIN + f"/merchants/{merchant.id}/settings"
    assert "已被其他商戶使用" in client.post(url, data={**SETTINGS, "login_name": "taken", "csrf": csrf}).text
    assert "只能用英文" in client.post(url, data={**SETTINGS, "login_name": "中文", "csrf": csrf}).text
    assert "最低金額不能大於最高金額" in client.post(url, data={**SETTINGS, "receive_min": "60000", "csrf": csrf}).text
    assert client.post(url, data={**SETTINGS, "receive_fee_rate": "-1", "csrf": csrf}).status_code == 400
    session.refresh(merchant)
    assert merchant.login_name == ""


def test_create_with_login_name(client, admin, session):
    csrf = login(client)
    client.post(ADMIN + "/merchants", data={"name": "信鑫", "login_name": "xinxin1688", "csrf": csrf})
    assert session.query(m.Merchant).one().login_name == "xinxin1688"


def test_receive_rules(client, merchant, account, session):
    merchant.can_receive = False
    session.commit()
    assert receive(client)["msg"] == "Receive Disabled."
    merchant.can_receive = True
    merchant.receive_min = Decimal("1000")
    merchant.receive_max = Decimal("5000")
    session.commit()
    assert receive(client, amount=999)["msg"] == "amount Out Of Range."
    assert receive(client, amount=5001)["msg"] == "amount Out Of Range."
    assert receive(client, amount=5000)["code"] == 200


def test_daily_limit_and_same_name(client, merchant, account, session):
    merchant.daily_receive_limit = Decimal("2500")
    session.commit()
    assert receive(client, order_id="a1", amount=1000)["code"] == 200
    assert receive(client, order_id="a2", amount=1000)["code"] == 200
    assert receive(client, order_id="a3", amount=1000)["msg"] == "Daily Limit Exceeded."
    merchant.daily_receive_limit = Decimal("0")
    merchant.same_name_max_orders = 2
    session.commit()
    assert receive(client, order_id="a4")["msg"] == "Too Many Orders."
    assert receive(client, order_id="a5", payer="別人", bank_no_check_title="別人")["code"] == 200


def test_return_bank_info_flags(client, merchant, account, session):
    data = receive(client, order_id="b1")["data"]
    assert data["bank_no"] and data["bank_title"]
    merchant.return_bank_title = False
    session.commit()
    data = receive(client, order_id="b2")["data"]
    assert data["bank_no"] and "bank_title" not in data
    merchant.return_bank_info = False
    session.commit()
    data = receive(client, order_id="b3")["data"]
    assert "bank_no" not in data and data["fronttable_url"]


def test_receive_fixed_fee(client, merchant, account, session):
    merchant.receive_fee_fixed = Decimal("35")
    session.commit()
    receive(client, order_id="f1", amount=1000)
    order = session.query(m.Order).filter_by(order_id="f1").one()
    assert order.fee == Decimal("50.00")  # 1.5% + 35


def test_withdraw_rules(client, merchant, session):
    merchant.balance = Decimal("100000")
    merchant.can_withdraw = False
    session.commit()
    assert withdraw(client)["msg"] == "Withdraw Disabled."
    merchant.can_withdraw = True
    merchant.withdraw_min = Decimal("1000")
    session.commit()
    assert withdraw(client, amount=500)["msg"] == "amount Out Of Range."
    assert withdraw(client, amount=1000)["code"] == 200


def test_day_stats_on_list(client, admin, merchant, account, session, reviewer):
    receive(client, order_id="s1", amount=1000)
    order = session.query(m.Order).one()
    services.confirm_deposit(
        session, order, services.Actor("web", "rev", reviewer), "822", "0099999999", Decimal("1000")
    )
    session.commit()
    login(client)
    page = client.get(ADMIN + "/merchants").text
    assert "1 / 0" in page and "1,000 / 0" in page


def test_add_missing_columns_upgrades_old_table(merchant, session):
    with db.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE merchants DROP COLUMN can_receive")
        conn.exec_driver_sql("ALTER TABLE merchants DROP COLUMN receive_min")
    db.add_missing_columns()
    cols = {c["name"] for c in inspect(db.engine).get_columns("merchants")}
    assert {"can_receive", "receive_min"} <= cols
    session.expire_all()
    mc = session.get(m.Merchant, merchant.id)
    assert mc.can_receive is True and mc.receive_min == 0


def test_merchant_group_gets_notifications(merchant, account, session, client):
    from lspay import telegram

    merchant.tg_chat_id = "-5488722855"
    session.commit()
    receive(client, order_id="t1")
    order = session.query(m.Order).one()
    assert telegram.chat_for(order) == "-5488722855"
    assert "-5488722855" in telegram.known_chats(session)
