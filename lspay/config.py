"""Runtime settings, all read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./lspay.db"))
    # Public base URL, used to build cashier (fronttable_url) links.
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"))
    # Secret URL prefix for the admin web, e.g. "/console-8f3k2x9q". Only people
    # who know it can reach the login page; every other path returns 404.
    admin_path: str = field(default_factory=lambda: "/" + os.getenv("ADMIN_PATH", "admin").strip().strip("/"))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))

    # Minutes a deposit order stays open before it times out (status 91).
    receive_timeout_minutes: int = field(default_factory=lambda: int(os.getenv("RECEIVE_TIMEOUT_MINUTES", "30")))
    # Seconds between background sweeps (timeouts, callback retries). 0 disables the loop.
    sweep_interval_seconds: int = field(default_factory=lambda: int(os.getenv("SWEEP_INTERVAL_SECONDS", "60")))
    callback_max_attempts: int = field(default_factory=lambda: int(os.getenv("CALLBACK_MAX_ATTEMPTS", "6")))

    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    # Default group chat id for notifications when an order has no team group.
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    # Value Telegram sends in X-Telegram-Bot-Api-Secret-Token on webhook calls.
    telegram_webhook_secret: str = field(default_factory=lambda: os.getenv("TELEGRAM_WEBHOOK_SECRET", ""))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)


settings = Settings()
