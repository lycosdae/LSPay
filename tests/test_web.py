import re

from lspay import models as m

from .conftest import ADMIN, HEADERS, signed
from .test_api import RECEIVE, WITHDRAW


def login(client, username="boss", password="password1"):
    page = client.get(ADMIN + "/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    res = client.post(
        ADMIN + "/login", data={"username": username, "password": password, "csrf": csrf}, follow_redirects=False
    )
    assert res.status_code == 303
    return csrf_of(client)  # the session (and its token) is renewed on login


def csrf_of(client, path=ADMIN):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_requires_login(client):
    res = client.get(ADMIN, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == ADMIN + "/login"


def test_bad_password(client, admin):
    page = client.get(ADMIN + "/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    client.post(ADMIN + "/login", data={"username": "boss", "password": "nope", "csrf": csrf})
    assert client.get(ADMIN, follow_redirects=False).status_code == 303


def test_all_pages_render(client, admin, merchant, account, team, session):
    client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS)
    merchant.balance = 5000
    session.commit()
    client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS)
    login(client)
    sid = session.query(m.Order).first().order_sid
    member_id = session.query(m.Member).first().id
    for path in [
        ADMIN,
        ADMIN + "/members",
        ADMIN + f"/members/{member_id}",
        ADMIN + "/teams",
        ADMIN + "/team-orders",
        ADMIN + "/orders/receive",
        ADMIN + "/orders/withdraw",
        ADMIN + "/orders/pending",
        ADMIN + f"/order/{sid}",
        ADMIN + "/users",
        ADMIN + "/merchants",
        ADMIN + "/accounts",
    ]:
        res = client.get(path)
        assert res.status_code == 200, path
    assert "王小明" in client.get(ADMIN + "/orders/pending").text
    assert "收款戶名與會員姓名不同" not in client.get(ADMIN + "/orders/pending").text
    assert "共 1 筆，金額 1000.00" in client.get(ADMIN + "/orders/receive").text


def test_csrf_required(client, admin):
    login(client)
    res = client.post(ADMIN + "/teams", data={"name": "x"})
    assert res.status_code == 400


def test_web_confirm_and_review_flow(client, admin, merchant, account, session):
    sid = client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS).json()["data"]["order_sid"]
    csrf = login(client)
    res = client.post(
        ADMIN + f"/order/{sid}/confirm",
        data={"bank_code": "999", "bank_no": "1", "amount": "1000", "csrf": csrf},
    )
    assert "資料不符" in res.text
    res = client.post(
        ADMIN + f"/order/{sid}/confirm",
        data={"bank_code": "822", "bank_no": "99999999", "amount": "1000", "csrf": csrf},
    )
    assert "已入帳" in res.text
    order = session.query(m.Order).filter_by(order_sid=sid).one()
    session.refresh(order)
    assert order.status == m.ST_DONE

    wsid = client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()["data"]["order_sid"]
    client.post(ADMIN + f"/order/{wsid}/reject", data={"note": "", "csrf": csrf})
    w = session.query(m.Order).filter_by(order_sid=wsid).one()
    assert w.review_status == m.REVIEW_PENDING  # empty reason refused
    client.post(ADMIN + f"/order/{wsid}/approve", data={"csrf": csrf})
    client.post(ADMIN + f"/order/{wsid}/paid", data={"csrf": csrf})
    session.refresh(w)
    assert w.status == m.ST_DONE and w.handled_by_id == admin.id
    stats = client.get(ADMIN + "/team-orders").text
    assert "boss" in stats


def test_support_cannot_review(client, merchant, session):
    from lspay.web.auth import hash_password

    session.add(m.AdminUser(username="cs", password_hash=hash_password("password1"), role=m.ROLE_SUPPORT))
    merchant.balance = 5000
    session.commit()
    wsid = client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()["data"]["order_sid"]
    csrf = login(client, "cs")
    assert client.post(ADMIN + f"/order/{wsid}/approve", data={"csrf": csrf}).status_code == 403
    assert client.get(ADMIN + "/merchants").status_code == 403


def test_create_merchant_and_member(client, admin, session):
    csrf = login(client)
    res = client.post(ADMIN + "/merchants", data={"name": "新站", "csrf": csrf})
    assert "API Key" in res.text
    mc = session.query(m.Merchant).one()
    assert len(mc.api_key) == 32 and mc.client_sid == "0001"
    client.post(
        ADMIN + "/members",
        data={
            "merchant_id": mc.id,
            "name": "李四",
            "pid": "b223456789",
            "bank_code": "822",
            "bank_no": "123",
            "csrf": csrf,
        },
    )
    member = session.query(m.Member).one()
    assert member.pid == "B223456789" and member.bank_accounts[0].bank_no == "123"


def test_admin_only_reachable_at_secret_path(client, admin):
    assert client.get("/admin", follow_redirects=False).status_code == 404
    assert client.get("/admin/login").status_code == 404
    assert client.get("/", follow_redirects=False).status_code == 404
    page = client.get(ADMIN + "/login")
    assert page.status_code == 200 and page.headers["x-robots-tag"] == "noindex, nofollow"
    login(client)
    assert 'href="/console-t3st/members"' in client.get(ADMIN).text
