import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SWEEP_INTERVAL_SECONDS"] = "0"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = "-1001"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "hook-secret"

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lspay import db  # noqa: E402
from lspay import models as m  # noqa: E402
from lspay.signing import api_sign  # noqa: E402
from lspay.web.auth import hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.Base.metadata.drop_all(db.engine)
    db.init_db()
    yield


@pytest.fixture
def session():
    s = db.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client():
    from lspay.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def merchant(session):
    mc = m.Merchant(
        name="遊戲站A",
        client_sid="0001",
        api_key="apikey123",
        sign_key="signkey456",
        receive_fee_rate=Decimal("1.5"),
        withdraw_fee_rate=Decimal("0"),
        withdraw_fee_fixed=Decimal("10"),
    )
    session.add(mc)
    session.commit()
    return mc


@pytest.fixture
def account(session):
    acc = m.ReceivingAccount(
        kind="company", bank_code="004", bank_name="臺灣銀行", bank_title="LS公司", bank_no="123456789012"
    )
    session.add(acc)
    session.commit()
    return acc


@pytest.fixture
def team(session):
    t = m.Team(name="一隊")
    session.add(t)
    session.commit()
    return t


@pytest.fixture
def reviewer(session, team):
    u = m.AdminUser(
        username="rev",
        password_hash=hash_password("password1"),
        role=m.ROLE_REVIEWER,
        team_id=team.id,
        tg_user_id="777",
    )
    session.add(u)
    session.commit()
    return u


@pytest.fixture
def admin(session):
    u = m.AdminUser(username="boss", password_hash=hash_password("password1"), role=m.ROLE_ADMIN)
    session.add(u)
    session.commit()
    return u


def signed(params: dict, sign_key: str = "signkey456") -> dict:
    return {**params, "sign": api_sign(params, sign_key)}


HEADERS = {"CLIENTSID": "0001", "ACCESSTOKEN": "apikey123"}
