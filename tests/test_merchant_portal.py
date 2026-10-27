import re
from decimal import Decimal

from lspay import models as m
from lspay.timeutil import now_tw
from lspay.web.auth import hash_password

from .conftest import ADMIN
from .test_merchant_settings import SETTINGS
from .test_web import login as admin_login

MP = "/merchant"


def setup_merchant(session, merchant, withdraw=False, receive=False):
    merchant.login_name = "ma1688"
    merchant.password_hash = hash_password("merchantpw")
    merchant.allow_manual_withdraw = withdraw
    merchant.allow_manual_receive = receive
    merchant.balance = Decimal("10000")
    session.commit()


def mlogin(client, username="ma1688", password="merchantpw"):
    return client.post(MP + "/login", data={"username": username, "password": password}, follow_redirects=False)


def mcsrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(MP + path).text).group(1)


def test_login_required_and_bad_password(client, merchant, session):
    setup_merchant(session, merchant)
    res = client.get(MP, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == MP + "/login"
    assert "帳號或密碼錯誤" in mlogin(client, password="wrong").text
    assert client.get(MP, follow_redirects=False).status_code == 303


def test_menu_shows_manual_pages_only_when_allowed(client, merchant, session):
    setup_merchant(session, merchant)
    assert mlogin(client).status_code == 303
    home = client.get(MP).text
    assert "商戶資訊" in home and "收款訂單列表" in home and "付款訂單列表" in home
    assert "手動建立付款訂單" not in home and "手動建立代收訂單" not in home
    assert client.get(MP + "/withdraw/new").status_code == 404
    assert client.get(MP + "/receive/new").status_code == 404
    merchant.allow_manual_withdraw = True
    merchant.allow_manual_receive = True
    session.commit()
    home = client.get(MP).text
    assert "手動建立付款訂單" in home and "手動建立代收訂單" in home


def test_manual_withdraw(client, merchant, session):
    setup_merchant(session, merchant, withdraw=True)
    mlogin(client)
    csrf = mcsrf(client, "/withdraw/new")
    form = {"payer": "王小名", "bank_name": "永豐", "bank_title": "王小名", "bank_no": "1234567890", "amount": "1000"}
    assert client.post(MP + "/withdraw/new", data={**form, "csrf": "bad"}).status_code == 400
    res = client.post(MP + "/withdraw/new", data={**form, "csrf": csrf})
    assert res.status_code == 200 and "付款訂單已建立" in res.text
    order = session.query(m.Order).one()
    assert order.type == m.TYPE_WITHDRAW and order.review_status == m.REVIEW_PENDING and order.callback == ""
    assert order.order_id.startswith("M") and order.events[0].source == "merchant"
    session.refresh(merchant)
    assert merchant.balance_frozen == Decimal("1010.00")  # fixture: 10 fixed fee
    res = client.post(MP + "/withdraw/new", data={**form, "amount": "999999", "csrf": csrf})
    assert "可動用商餘不足" in res.text
    assert "王小名" in client.get(MP + "/orders/withdraw").text


def test_manual_receive(client, merchant, account, session):
    setup_merchant(session, merchant, receive=True)
    mlogin(client)
    csrf = mcsrf(client, "/receive/new")
    form = {"payer": "王小名", "bank_code": "822", "bank_no": "0099999999", "amount": "1000", "csrf": csrf}
    res = client.post(MP + "/receive/new", data=form)
    assert res.status_code == 200 and "收款訂單已建立" in res.text and "/cashier/" in res.text
    order = session.query(m.Order).one()
    assert order.bank_no_check == "822-0099999999" and order.status == m.ST_MATCHED
    assert "請填寫玩家的轉出銀行代碼" in client.post(MP + "/receive/new", data={**form, "bank_code": ""}).text


def test_only_own_orders_and_password_change_logs_out(client, merchant, session):
    setup_merchant(session, merchant)
    other = m.Merchant(name="B", client_sid="0002", api_key="k", sign_key="s")
    session.add(other)
    session.flush()
    session.add(m.Order(order_sid="X1", merchant_id=other.id, order_id="other-order", type=1, amount=1, payer="別人"))
    session.commit()
    mlogin(client)
    assert "other-order" not in client.get(MP + "/orders/receive").text
    merchant.password_hash = hash_password("newpassword")
    session.commit()
    assert client.get(MP, follow_redirects=False).status_code == 303


def test_admin_sets_password_and_switches(client, admin, merchant, session):
    csrf = admin_login(client)
    url = ADMIN + f"/merchants/{merchant.id}/settings"
    data = {**SETTINGS, "password": "short", "csrf": csrf}
    assert "至少 8 個字元" in client.post(url, data=data).text
    data.update(password="merchantpw", allow_manual_withdraw="1", allow_manual_receive="0")
    client.post(url, data=data)
    session.refresh(merchant)
    assert merchant.password_hash and merchant.allow_manual_withdraw and not merchant.allow_manual_receive
    keep = merchant.password_hash
    client.post(url, data={**data, "password": ""})
    session.refresh(merchant)
    assert merchant.password_hash == keep
    assert mlogin(client, "xinxin1688", "merchantpw").status_code == 303
    txt = client.post(ADMIN + f"/merchants/{merchant.id}/info.txt", data={"csrf": csrf}).content.decode()
    assert "後台網址: http://localhost:8000/merchant" in txt and "後台帳號: xinxin1688" in txt


def test_home_cards(client, merchant, session):
    setup_merchant(session, merchant)
    merchant.balance_frozen = Decimal("500")
    merchant.balance_hold = Decimal("1000")
    session.add(
        m.Order(
            merchant_id=merchant.id,
            type=m.TYPE_RECEIVE,
            order_id="h1",
            order_sid="S-h1",
            amount=Decimal("1234"),
            fee=0,
            status=m.ST_DONE,
            finish_time=now_tw(),
            payer="王小名",
        )
    )
    session.commit()
    mlogin(client)
    home = client.get(MP).text
    for label in ("商戶餘額", "暫時凍結(待出款)", "可動用餘額", "凍結金額", "本月累計代付筆數", "昨日累計代收交易量"):
        assert label in home
    assert "10,500" in home and "9,000" in home and "1,234" in home
    assert "費率" not in home
