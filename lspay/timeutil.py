"""All times are GMT+8, stored naive, formatted as YYYY-MM-DD HH:mm:ss."""

from datetime import datetime, timedelta, timezone
from typing import Optional

TW = timezone(timedelta(hours=8))
FMT = "%Y-%m-%d %H:%M:%S"


def now_tw() -> datetime:
    return datetime.now(TW).replace(tzinfo=None, microsecond=0)


def fmt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(FMT) if dt else None


def parse(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    for f in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, f)
        except ValueError:
            continue
    raise ValueError(f"bad datetime: {text}")
