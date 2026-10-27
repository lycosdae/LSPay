from decimal import Decimal

import pytest

from lspay import merchant_bot, services, telegram
from lspay import models as m
from lspay.timeutil import now_tw

from .conftest import HEADERS, signed
from .test_api import RECEIVE, WITHDRAW
from .test_merchant_portal import MP, mcsrf, mlogin, setup_merchant

TG = 5555


@pytest.fixture
def sent(monkeypatch):
    out = []

    def fake_call(method, payload):
        out.append(payload)
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    return out


def say(session, text, user_id=TG):
    msg = {"message_id": 1, "from": {"id": user_id, "first_name": "小明"}, "chat": {"id": user_id, "type": "private"}}
    assert telegram.handle_update(session, {"message": {**msg, "text": text}}) == []


def bind(session, merchant):
    code = services.new_bind_code(session, merchant)
    session.commit()
    say(session, f"/start {code}")
    return code


def buttons(payload):
    return [b for row in payload["reply_markup"]["keyboard"] for b in row]


def test_bind_with_one_time_code(session, merchant, sent):
    say(session, "/start")
    assert "還沒有綁定" in sent[-1]["text"]
    code = bind(session, merchant)
    assert "已綁定商戶" in sent[-1]["text"] and "✅ 開啟收款" in buttons(sent[-1])
    link = session.query(m.MerchantTgUser).one()
    assert link.merchant_id == merchant.id and link.tg_name == "小明"
    say(session, code, user_id=6666)  # already used
    assert "錯誤或已過期" in sent[-1]["text"]
    services.new_bind_code(session, merchant)
    merchant.tg_bind_expires = int(now_tw().timestamp()) - 1
    session.commit()
    say(session, merchant.tg_bind_code, user_id=6666)
    assert "錯誤或已過期" in sent[-1]["text"]


def test_balance_and_subtotals(client, session, merchant, account, reviewer, sent):
    order_sid = client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS).json()["data"]["order_sid"]
    order = session.query(m.Order).filter_by(order_sid=order_sid).one()
    services.confirm_deposit(session, order, services.Actor("web", "rev", reviewer), "822", "0099999999", order.amount)
    merchant.balance_hold = Decimal("100")
    session.commit()
    bind(session, merchant)
    say(session, merchant_bot.B_BALANCE)
    assert "可動用餘額" in sent[-1]["text"] and "凍結金額：100" in sent[-1]["text"]
    for label in ("今日小計", "本週小計", "本月小計"):
        say(session, label)
        assert "代收：1 筆" in sent[-1]["text"] and "代付：0 筆" in sent[-1]["text"]
    say(session, "昨日小計")
    assert "代收：0 筆" in sent[-1]["text"]


def test_switches_match_backend(client, session, merchant, sent):
    bind(session, merchant)
    say(session, merchant_bot.B_RECV_OFF)
    session.refresh(merchant)
    assert not merchant.receive_open and "✅ 關閉收款" in buttons(sent[-1])
    res = client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS).json()
    assert res["msg"] == "Receive Disabled."
    say(session, "✅ 開啟收款".removeprefix("✅ "))
    session.refresh(merchant)
    assert merchant.receive_open
    merchant.can_withdraw = False
    session.commit()
    say(session, merchant_bot.B_PAY_ON)
    assert "管理員已停用代付" in sent[-1]["text"]
    say(session, merchant_bot.B_PAY_OFF)
    assert "管理員已停用代付" in sent[-1]["text"]
    session.refresh(merchant)
    assert merchant.withdraw_open  # admin switch wins; merchant switch untouched
    merchant.can_withdraw = True
    merchant.balance = Decimal("5000")
    session.commit()
    say(session, merchant_bot.B_PAY_OFF)
    res = client.post("/v1s/order/withdraw", json=signed(WITHDRAW), headers=HEADERS).json()
    assert res["msg"] == "Withdraw Disabled."


def test_bank_account_flow(session, merchant, sent):
    bind(session, merchant)
    say(session, merchant_bot.B_BANK)
    assert "尚未設定" in sent[-2]["text"] and sent[-1]["reply_markup"]["force_reply"]
    say(session, "8")
    assert "3 碼" in sent[-1]["text"]
    say(session, "822")
    assert "中國信託" in sent[-1]["text"]
    say(session, "0123456789")
    say(session, "王小明")
    session.refresh(merchant)
    assert (merchant.settle_bank_code, merchant.settle_bank_no, merchant.settle_bank_title) == (
        "822",
        "0123456789",
        "王小明",
    )
    assert "已更新" in sent[-1]["text"]
    # A menu button in the middle of the questions cancels them.
    say(session, merchant_bot.B_BANK)
    say(session, merchant_bot.B_BALANCE)
    assert session.query(m.MerchantTgUser).one().state == ""


def test_order_lookup_only_own(client, session, merchant, account, sent):
    data = client.post("/v1s/order/receive", json=signed(RECEIVE), headers=HEADERS).json()["data"]
    other = m.Merchant(name="別家", client_sid="0009", api_key="k", sign_key="s")
    session.add(other)
    session.commit()
    link = m.MerchantTgUser(merchant_id=other.id, tg_user_id="6666", tg_name="x")
    session.add(link)
    session.commit()
    say(session, merchant_bot.B_ORDER, user_id=6666)
    say(session, data["order_sid"], user_id=6666)
    assert "找不到" in sent[-1]["text"]
    bind(session, merchant)
    say(session, merchant_bot.B_ORDER)
    say(session, data["order_sid"])
    assert data["order_sid"] in sent[-1]["text"] and "代收訂單" in sent[-1]["text"]


def test_invite_code_and_support(session, merchant, sent, monkeypatch):
    bind(session, merchant)
    say(session, merchant_bot.B_INVITE)
    session.refresh(merchant)
    assert merchant.invite_code and merchant.invite_code in sent[-1]["text"] and "已推薦 0 家" in sent[-1]["text"]
    monkeypatch.setattr(merchant_bot.settings, "support_contact", "@lspay_cs")
    say(session, merchant_bot.B_SUPPORT)
    assert "@lspay_cs" in sent[-1]["text"]


def test_portal_settings_page(client, session, merchant, sent):
    setup_merchant(session, merchant)
    mlogin(client)
    page = client.get(MP + "/settings").text
    assert "Telegram 機器人" in page and "推廣邀請碼" in page
    csrf = mcsrf(client, "/settings")
    res = client.post(MP + "/settings/bind-code", data={"csrf": csrf})
    session.refresh(merchant)
    assert merchant.tg_bind_code and merchant.tg_bind_code in res.text
    say(session, merchant.tg_bind_code)
    assert "小明" in client.get(MP + "/settings").text
    client.post(MP + "/settings/switches", data={"csrf": csrf, "receive_open": "0", "withdraw_open": "1"})
    session.refresh(merchant)
    assert not merchant.receive_open and merchant.withdraw_open
    res = client.post(MP + "/settings/settle", data={"csrf": csrf, "bank_code": "8", "bank_no": "1", "bank_title": ""})
    assert "銀行代碼需為 3 碼" in res.text
    client.post(
        MP + "/settings/settle",
        data={"csrf": csrf, "bank_code": "700", "bank_no": "00811810360901", "bank_title": "公司"},
    )
    session.refresh(merchant)
    assert merchant.settle_bank_no == "00811810360901"
    link = session.query(m.MerchantTgUser).one()
    client.post(MP + f"/settings/unbind/{link.id}", data={"csrf": csrf})
    assert session.query(m.MerchantTgUser).count() == 0
