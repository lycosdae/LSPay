import re
from datetime import timedelta

from lspay import models as m
from lspay import services
from lspay.timeutil import now_tw

from .conftest import ADMIN
from .test_web import login

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_member(session, merchant, status="active", with_account=True):
    mb = m.Member(merchant_id=merchant.id, name="王小明", pid="A123456789", phone="0912345678", status=status)
    if with_account:
        mb.bank_accounts.append(m.MemberBankAccount(bank_code="822", bank_no="0099999999", account_name="王小明"))
    session.add(mb)
    session.commit()
    return mb


def pay_path(member):
    return "/pay/" + services.member_pay_token(member)


def create(client, member, amount="1000"):
    res = client.post(pay_path(member), data={"amount": amount}, follow_redirects=False)
    assert res.status_code == 303, res.text
    return res.headers["location"]


def test_pay_page_shows_member(client, merchant, account, session):
    mb = make_member(session, merchant)
    page = client.get(pay_path(mb))
    assert page.status_code == 200
    assert "LS支付 銀行轉帳" in page.text and "王小明" in page.text


def test_bad_or_blocked_token_is_404(client, merchant, account, session):
    mb = make_member(session, merchant)
    assert client.get(f"/pay/{mb.id}.deadbeef").status_code == 404
    assert client.get("/pay/nonsense").status_code == 404
    mb.status = "blocked"
    session.commit()
    assert client.get(pay_path(mb)).status_code == 404


def test_create_deposit_and_cashier_flow(client, merchant, account, session):
    mb = make_member(session, merchant)
    url = create(client, mb, "1000")
    session.expire_all()
    order = session.query(m.Order).one()
    assert order.member_id == mb.id and order.amount == 1000 and order.status == m.ST_MATCHED
    assert order.bank_no_check == "822-0099999999" and order.callback == ""
    left = (order.end_time - order.init_time).total_seconds()
    assert left == 600
    page = client.get(url)
    text = page.text
    assert "前使用 ATM／網路銀行轉帳完成支付" in text
    assert "請勿輸入任何備註" in text and "銀行帳戶對銀行帳戶" in text and "付款人姓名" in text
    assert "王小明" in text and "822 中國信託" in text and "*****99999" in text
    assert "0099999999" not in text  # member's own account is masked
    assert 'data-copy="123456789012"' in text and 'data-copy="1000"' in text and 'data-copy="004"' in text
    assert "已完成轉帳，點我上傳轉帳截圖" in text


def test_amount_validation_and_open_limit(client, merchant, account, session):
    mb = make_member(session, merchant)
    for bad in ("", "0", "abc", "10.5", "-5"):
        res = client.post(pay_path(mb), data={"amount": bad})
        assert res.status_code == 400
    for _ in range(services.MAX_OPEN_MEMBER_DEPOSITS):
        create(client, mb)
    res = client.post(pay_path(mb), data={"amount": "100"})
    assert res.status_code == 400 and "尚未完成的儲值單" in res.text


def test_needs_member_account_and_receiving_account(client, merchant, session):
    mb = make_member(session, merchant, with_account=False)
    res = client.post(pay_path(mb), data={"amount": "100"})
    assert res.status_code == 400 and "尚未登記銀行帳戶" in res.text
    mb.bank_accounts.append(m.MemberBankAccount(bank_code="822", bank_no="0099999999"))
    session.commit()
    res = client.post(pay_path(mb), data={"amount": "100"})
    assert res.status_code == 400 and "沒有可用的收款帳戶" in res.text


def upload(client, url, data, name="shot.png"):
    sid = re.search(r"/cashier/(\d+)", url).group(1)
    token = re.search(r"t=([^&]+)", url).group(1)
    return client.post(
        f"/cashier/{sid}/proof", data={"t": token}, files={"file": (name, data, "image/png")}, follow_redirects=False
    )


def test_upload_proof_shows_in_admin(client, merchant, account, admin, session):
    mb = make_member(session, merchant)
    url = create(client, mb)
    res = upload(client, url, PNG)
    assert res.status_code == 303
    page = client.get(url)
    assert "已收到您的轉帳截圖（1 張）" in page.text
    order = session.query(m.Order).one()
    session.refresh(order)
    att = order.attachments[0]
    assert att.content_type == "image/png" and att.data == PNG
    assert any(e.action == "proof_upload" for e in order.events)

    # only logged-in staff can see it
    proof = f"{ADMIN}/order/{order.order_sid}/proof/{att.id}"
    assert client.get(proof, follow_redirects=False).status_code == 303
    login(client)
    assert client.get(proof).content == PNG
    assert proof in client.get(f"{ADMIN}/order/{order.order_sid}").text


def test_upload_rejects_bad_files_and_tokens(client, merchant, account, session):
    mb = make_member(session, merchant)
    url = create(client, mb)
    assert upload(client, url, b"<html>not an image</html>").status_code == 400
    assert upload(client, url, b"").status_code == 400
    assert upload(client, url, PNG + b"\x00" * services.MAX_PROOF_BYTES).status_code == 400
    sid = re.search(r"/cashier/(\d+)", url).group(1)
    bad = client.post(f"/cashier/{sid}/proof", data={"t": "wrong"}, files={"file": ("a.png", PNG, "image/png")})
    assert bad.status_code == 404
    for _ in range(services.MAX_PROOFS_PER_ORDER):
        assert upload(client, url, PNG).status_code == 303
    assert upload(client, url, PNG).status_code == 400


def test_upload_allowed_shortly_after_timeout(client, merchant, account, session):
    mb = make_member(session, merchant)
    url = create(client, mb)
    order = session.query(m.Order).one()
    order.end_time = now_tw() - timedelta(minutes=1)
    session.commit()
    services.expire_receive_orders(session)
    session.commit()
    assert "付款期限已過" in client.get(url).text
    assert upload(client, url, PNG).status_code == 303
    order.end_time = now_tw() - timedelta(hours=2)
    session.commit()
    assert upload(client, url, PNG).status_code == 400


def test_member_detail_shows_pay_link(client, merchant, account, admin, session):
    mb = make_member(session, merchant)
    login(client)
    assert services.member_pay_token(mb) in client.get(f"{ADMIN}/members/{mb.id}").text
