"""Management commands.

python -m lspay.cli init-db
python -m lspay.cli create-admin <username>          # prompts for password
python -m lspay.cli set-telegram-webhook <https-url>  # e.g. https://pay.example.com/telegram/webhook
"""

import getpass
import os
import sys

from sqlalchemy import select

from . import db, telegram
from . import models as m
from .web.auth import hash_password


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "init-db":
        db.init_db()
        print("ok")
        return 0
    if cmd == "create-admin":
        if len(args) != 1:
            print("usage: create-admin <username>")
            return 1
        password = os.getenv("LSPAY_ADMIN_PASSWORD") or getpass.getpass("Password (min 8 chars): ")
        if len(password) < 8:
            print("password too short")
            return 1
        db.init_db()
        with db.session_scope() as s:
            user = s.scalar(select(m.AdminUser).where(m.AdminUser.username == args[0]))
            if user is None:
                user = m.AdminUser(username=args[0], password_hash="")
                s.add(user)
            user.password_hash = hash_password(password)
            user.role = m.ROLE_ADMIN
            user.enabled = True
        print(f"admin {args[0]} ready")
        return 0
    if cmd == "set-telegram-webhook":
        if len(args) != 1:
            print("usage: set-telegram-webhook <url>")
            return 1
        print(telegram.set_webhook(args[0]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
