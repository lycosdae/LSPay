"""Fill a local database with demo data (never run against production).

DATABASE_URL=sqlite:///./demo.db python scripts/demo_seed.py
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lspay import db, services  # noqa: E402
from lspay import models as m  # noqa: E402
from lspay.web.auth import hash_password  # noqa: E402

if "sqlite" not in db.settings.database_url and os.getenv("FORCE") != "1":
    sys.exit("refusing to seed a non-sqlite database; set FORCE=1 if you really mean it")

db.init_db()
with db.session_scope() as s:
    team = m.Team(name="一隊", leader="阿明")
    s.add(team)
    s.flush()
    s.add(m.AdminUser(username="admin", display_name="管理員", password_hash=hash_password("admin1234"), role="admin"))
    rev = m.AdminUser(
        username="rev1", display_name="審單小張", password_hash=hash_password("rev12345"), role="reviewer", team=team
    )
    s.add(rev)
    mc = m.Merchant(
        name="示範遊戲站",
        client_sid="0001",
        api_key="demo-api-key",
        sign_key="demo-sign-key",
        balance=Decimal("50000"),
        receive_fee_rate=Decimal("1.5"),
        withdraw_fee_fixed=Decimal("10"),
    )
    s.add(mc)
    s.add(
        m.ReceivingAccount(bank_code="004", bank_name="臺灣銀行", bank_title="LS科技有限公司", bank_no="012345678901")
    )
    s.flush()
    base = {"callback": "https://game.example/cb", "message": "demo", "payer_cellphone": "0912345678"}
    players = [("王小明", "A123456789", "822-0099999999"), ("陳美麗", "B223456789", "004-123123123")]
    for i, (name, pid, acct) in enumerate(players):
        services.create_receive(
            s,
            mc,
            {
                **base,
                "payer": name,
                "payer_pid": pid,
                "order_id": f"R{i}",
                "amount": 1000 * (i + 1),
                "bank_no_check": acct,
                "bank_no_check_title": name,
            },
        )
        code, no = acct.split("-")
        services.create_withdraw(
            s,
            mc,
            {
                **base,
                "payer": name,
                "payer_pid": pid,
                "order_id": f"W{i}",
                "amount": 500,
                "bank_title": name if i == 0 else "別人",
                "bank_name": code,
                "bank_no": no,
            },
        )
    member = s.query(m.Member).first()
    member.team = team
print("seeded: admin / admin1234, rev1 / rev12345")
