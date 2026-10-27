"""How a game site calls LSPay (same as FuturePay) and verifies callbacks.

    LSPAY_URL=http://localhost:8000 LSPAY_SID=0001 LSPAY_API_KEY=... LSPAY_SIGN_KEY=... \
        python scripts/client_example.py
"""

import hashlib
import os
import time

import httpx

URL = os.environ.get("LSPAY_URL", "http://localhost:8000")
SID = os.environ["LSPAY_SID"]
API_KEY = os.environ["LSPAY_API_KEY"]
SIGN_KEY = os.environ["LSPAY_SIGN_KEY"]


def sign(params: dict) -> str:
    text = "&".join(f"{k}={params[k]}" for k in sorted(params)) + f"&key={SIGN_KEY}"
    return hashlib.md5(text.encode()).hexdigest()


def call(method: str, path: str, params: dict) -> dict:
    body = {**params, "sign": sign(params)}
    headers = {"CLIENTSID": SID, "ACCESSTOKEN": API_KEY}
    return httpx.request(method, f"{URL}/v1s/{path}", json=body, headers=headers).json()


def verify_callback(headers: dict, body: dict) -> bool:
    """Use this in your callback endpoint, then reply with the text 'success'."""
    from decimal import Decimal

    if headers.get("ACCESSTOKEN") != API_KEY:
        return False
    amount = format(Decimal(str(body["amount"])).normalize(), "f")
    expected = hashlib.md5(f"{API_KEY}{amount}{body['order_id']}{SIGN_KEY}".encode()).hexdigest()
    return expected == body.get("sign")


if __name__ == "__main__":
    order_id = f"demo{int(time.time())}"
    print(
        call(
            "POST",
            "order/receive",
            {
                "payer": "王小明",
                "payer_cellphone": "0912345678",
                "payer_pid": "A123456789",
                "order_id": order_id,
                "amount": 1000,
                "callback": "https://game.example/lspay/callback",
                "message": "demo",
                "bank_no_check_title": "王小明",
                "bank_no_check": "822-0099999999",
            },
        )
    )
    print(call("GET", "order/list", {"order_id": order_id}))
    print(call("GET", "client/balance", {}))
