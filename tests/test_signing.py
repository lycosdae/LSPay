from decimal import Decimal

from lspay.signing import api_sign, callback_sign, format_amount, md5_hex, sign_string, verify_api_sign


def test_callback_sign_matches_futurepay_doc_example():
    # Doc: API KEY=abc, amount 100, order id def, sign key WXYZ -> "abc100defWXYZ"
    assert callback_sign("abc", 100, "def", "WXYZ") == md5_hex("abc100defWXYZ")
    assert callback_sign("abc", Decimal("100.00"), "def", "WXYZ") == md5_hex("abc100defWXYZ")


def test_format_amount_drops_trailing_zeros():
    assert format_amount(Decimal("123.40")) == "123.4"
    assert format_amount("100.00") == "100"
    assert format_amount(100) == "100"
    assert format_amount(Decimal("0.50")) == "0.5"
    assert format_amount(Decimal("1E+3")) == "1000"


def test_sign_string_matches_doc_layout():
    params = {
        "payer": "王小明",
        "order_id": "a100328102",
        "message": "test order",
        "callback": "https://localhost/test",
        "amount": 500,
        "sign": "ignored",
    }
    assert sign_string(params, "K") == (
        "amount=500&callback=https://localhost/test&message=test order&order_id=a100328102&payer=王小明&key=K"
    )


def test_float_and_int_render_the_same():
    assert api_sign({"amount": 100.0}, "k") == api_sign({"amount": 100}, "k")


def test_verify():
    p = {"a": "1", "b": "2"}
    p["sign"] = api_sign(p, "k")
    assert verify_api_sign(p, "k")
    assert verify_api_sign({**p, "sign": p["sign"].upper()}, "k")
    assert not verify_api_sign({**p, "b": "3"}, "k")
    assert not verify_api_sign({"a": "1"}, "k")
