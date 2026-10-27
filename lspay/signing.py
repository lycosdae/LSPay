"""Signatures, compatible with the FuturePay v1s API.

API request sign: sort parameter names by ASCII, join as ``k=v`` with ``&``,
append ``&key=<sign key>``, MD5, lowercase hex. No URL encoding.

Callback sign: md5(api_key + amount + order_id + sign_key), where amount has
no trailing zeros (123.40 -> "123.4", 100.00 -> "100").
"""

import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def param_to_str(value: Any) -> str:
    """Render a parameter value the way a PHP client's strval() would."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, Decimal):
        return format_amount(value)
    return str(value)


def sign_string(params: Mapping[str, Any], sign_key: str) -> str:
    parts = [f"{k}={param_to_str(params[k])}" for k in sorted(params) if k != "sign"]
    parts.append(f"key={sign_key}")
    return "&".join(parts)


def api_sign(params: Mapping[str, Any], sign_key: str) -> str:
    return md5_hex(sign_string(params, sign_key))


def verify_api_sign(params: Mapping[str, Any], sign_key: str) -> bool:
    given = params.get("sign")
    if not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(given.lower(), api_sign(params, sign_key))


def format_amount(amount: Any) -> str:
    """100 -> '100', 123.40 -> '123.4', '100.50' -> '100.5'."""
    try:
        d = Decimal(str(amount))
    except InvalidOperation:
        return str(amount)
    text = format(d.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def callback_sign(api_key: str, amount: Any, order_id: str, sign_key: str) -> str:
    return md5_hex(f"{api_key}{format_amount(amount)}{order_id}{sign_key}")
