from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import now_tw

Money = Numeric(14, 2)

# Order types
TYPE_RECEIVE = 1  # 收款 / deposit
TYPE_WITHDRAW = 2  # 付款 / withdrawal

# Order statuses (same codes as FuturePay)
ST_NEW = 0
ST_MATCHED = 1
ST_ACCEPTED = 2
ST_DONE = 3
ST_CALLBACK_FAILED = 4
ST_PAY_TIMEOUT = 90
ST_RECEIVE_TIMEOUT = 91
ST_AMOUNT_MISMATCH = 92
ST_INVALID = 95
ST_REJECTED = 96
ST_NO_BALANCE = 98
ST_MATCH_TIMEOUT = 99

STATUS_LABELS = {
    ST_NEW: "新訂單",
    ST_MATCHED: "已配對",
    ST_ACCEPTED: "已收單",
    ST_DONE: "已完成",
    ST_CALLBACK_FAILED: "回調失敗",
    ST_PAY_TIMEOUT: "付款超時",
    ST_RECEIVE_TIMEOUT: "收款超時",
    ST_AMOUNT_MISMATCH: "金額不符",
    ST_INVALID: "訂單無效",
    ST_REJECTED: "駁回單",
    ST_NO_BALANCE: "餘額不足",
    ST_MATCH_TIMEOUT: "超時配對，無效單",
}
FINAL_STATUSES = {ST_DONE} | {s for s in STATUS_LABELS if s >= 90}

# Withdrawal review states
REVIEW_NONE = "none"
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"

REVIEW_LABELS = {
    REVIEW_NONE: "",
    REVIEW_PENDING: "待審核",
    REVIEW_APPROVED: "審核通過",
    REVIEW_REJECTED: "已駁回",
}

# Admin roles
ROLE_ADMIN = "admin"  # everything, incl. merchants, accounts, users
ROLE_REVIEWER = "reviewer"  # confirm deposits, review/pay withdrawals
ROLE_SUPPORT = "support"  # read-only + member management
ROLE_LABELS = {ROLE_ADMIN: "管理員", ROLE_REVIEWER: "審單員", ROLE_SUPPORT: "客服"}

CALLBACK_PENDING = "pending"
CALLBACK_OK = "ok"
CALLBACK_FAILED = "failed"


class Merchant(Base):
    """A game site (or other client) that calls the LSPay API."""

    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    client_sid: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    api_key: Mapped[str] = mapped_column(String(64))
    sign_key: Mapped[str] = mapped_column(String(64))
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    balance_frozen: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # Fee rates in percent, e.g. 1.5 means 1.5%.
    receive_fee_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    withdraw_fee_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    withdraw_fee_fixed: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # Comma separated list of allowed caller IPs; empty = any.
    ip_whitelist: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)


class Team(Base):
    """車隊: an internal customer-service / order-review team."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    leader: Mapped[str] = mapped_column(String(64), default="")
    # Telegram group for this team; falls back to TELEGRAM_CHAT_ID when empty.
    tg_chat_id: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)

    users: Mapped[list["AdminUser"]] = relationship(back_populates="team")


class AdminUser(Base):
    """用戶: a person who logs in to the admin web or acts from Telegram."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_SUPPORT)
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"), nullable=True)
    tg_user_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)

    team: Mapped[Optional[Team]] = relationship(back_populates="users")

    @property
    def label(self) -> str:
        return self.display_name or self.username


class Member(Base):
    """會員: a player of a merchant, with their own bank accounts."""

    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("merchant_id", "pid", name="uq_member_merchant_pid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(32))
    phone: Mapped[str] = mapped_column(String(20), default="")
    pid: Mapped[str] = mapped_column(String(20))  # national id
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / blocked
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)

    merchant: Mapped[Merchant] = relationship()
    team: Mapped[Optional[Team]] = relationship()
    bank_accounts: Mapped[list["MemberBankAccount"]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )


class MemberBankAccount(Base):
    """A bank account the member owns and is allowed to transfer from."""

    __tablename__ = "member_bank_accounts"
    __table_args__ = (UniqueConstraint("member_id", "bank_code", "bank_no", name="uq_member_bank"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    bank_code: Mapped[str] = mapped_column(String(8))
    bank_no: Mapped[str] = mapped_column(String(32))
    account_name: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)

    member: Mapped[Member] = relationship(back_populates="bank_accounts")


class ReceivingAccount(Base):
    """收款帳戶: a company-owned bank account (or a bank / licensed PSP virtual
    account) that players transfer deposits into."""

    __tablename__ = "receiving_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="company")  # company / virtual
    bank_code: Mapped[str] = mapped_column(String(8))
    bank_name: Mapped[str] = mapped_column(String(32))
    bank_branch: Mapped[str] = mapped_column(String(32), default="")
    bank_title: Mapped[str] = mapped_column(String(64))  # account holder (company name)
    bank_no: Mapped[str] = mapped_column(String(32))
    daily_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # 0 = unlimited
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("merchant_id", "order_id", name="uq_order_merchant_order_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_sid: Mapped[str] = mapped_column(String(20), unique=True, index=True, default="")
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(20))  # merchant's order number
    type: Mapped[int] = mapped_column(Integer, index=True)
    member_id: Mapped[Optional[int]] = mapped_column(ForeignKey("members.id"), nullable=True, index=True)

    payer: Mapped[str] = mapped_column(String(32), default="")
    payer_cellphone: Mapped[str] = mapped_column(String(20), default="")
    payer_pid: Mapped[str] = mapped_column(String(20), default="")
    amount: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    status: Mapped[int] = mapped_column(Integer, default=ST_NEW, index=True)
    is_finish: Mapped[bool] = mapped_column(Boolean, default=False)
    init_time: Mapped[datetime] = mapped_column(DateTime, default=now_tw, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finish_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    callback: Mapped[str] = mapped_column(String(256), default="")
    message: Mapped[str] = mapped_column(String(256), default="")

    # Deposit: the player's declared source accounts, "<code>-<no>,..."
    bank_no_check: Mapped[str] = mapped_column(String(256), default="")
    bank_no_check_title: Mapped[str] = mapped_column(String(32), default="")
    receiving_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("receiving_accounts.id"), nullable=True)
    # Deposit: the account the transfer actually came from (entered when confirming).
    paid_from_bank_code: Mapped[str] = mapped_column(String(8), default="")
    paid_from_bank_no: Mapped[str] = mapped_column(String(32), default="")
    paid_amount: Mapped[Optional[Decimal]] = mapped_column(Money, nullable=True)

    # Bank details shown on the order: receiving account (deposit) or payee (withdrawal).
    bank_code: Mapped[str] = mapped_column(String(8), default="")
    bank_title: Mapped[str] = mapped_column(String(64), default="")
    bank_name: Mapped[str] = mapped_column(String(32), default="")
    bank_branch: Mapped[str] = mapped_column(String(32), default="")
    bank_no: Mapped[str] = mapped_column(String(32), default="")

    review_status: Mapped[str] = mapped_column(String(16), default=REVIEW_NONE, index=True)
    handled_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    team_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)
    review_note: Mapped[str] = mapped_column(Text, default="")

    callback_status: Mapped[str] = mapped_column(String(16), default="")
    callback_attempts: Mapped[int] = mapped_column(Integer, default=0)
    callback_next_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    callback_last_response: Mapped[str] = mapped_column(Text, default="")

    cashier_token: Mapped[str] = mapped_column(String(48), default="")

    merchant: Mapped[Merchant] = relationship()
    member: Mapped[Optional[Member]] = relationship()
    receiving_account: Mapped[Optional[ReceivingAccount]] = relationship()
    handled_by: Mapped[Optional[AdminUser]] = relationship()
    team: Mapped[Optional[Team]] = relationship()
    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order", order_by="OrderEvent.id", cascade="all, delete-orphan"
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, str(self.status))

    @property
    def type_label(self) -> str:
        return "收款" if self.type == TYPE_RECEIVE else "付款"

    @property
    def review_label(self) -> str:
        return REVIEW_LABELS.get(self.review_status, self.review_status)

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


class OrderEvent(Base):
    """Audit trail: every status change and who/what caused it."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # api / web / telegram / system / callback
    actor: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    to_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)

    order: Mapped[Order] = relationship(back_populates="events")


class TelegramMessage(Base):
    """Telegram messages posted for an order, so they can be edited later."""

    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)


class AuditLog(Base):
    """Admin actions that are not order status changes (users, merchants...)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)
