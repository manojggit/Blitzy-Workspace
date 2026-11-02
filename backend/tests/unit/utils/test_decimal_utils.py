"""Unit tests for app.utils.decimal_utils, the COBOL numeric store and zoned-decimal codec.

Expected values are derived by hand from the COBOL rules, never from the unit:
- IBM Enterprise COBOL stores without ROUNDED or ON SIZE ERROR by truncating surplus fraction
    digits toward zero and dropping high-order integer digits, keeping the sign (AAP 0.6.6).
- cbl/CBACT04C.cbl lines 464-465: COMPUTE WS-MONTHLY-INT = (TRAN-CAT-BAL * DIS-INT-RATE) / 1200.
- cbl/COBIL00C.cbl line 224: MOVE ACCT-CURR-BAL (S9(10)V99) TO TRAN-AMT (S9(09)V99).
- cpy/CVACT01Y.cpy ACCT-CURR-BAL S9(10)V99 and cpy/CVTRA06Y.cpy DALYTRAN-AMT S9(09)V99, zoned
    DISPLAY fields with a trailing sign overpunch, read from data/ASCII/acctdata.txt record 1
    (offset 12) and data/ASCII/dailytran.txt (offset 132).
"""

from decimal import Decimal, localcontext

import pytest

from app.utils.decimal_utils import (
    COBOL_CONTEXT,
    DecodeZoned,
    EncodeZoned,
    StoreInto,
    ZonedDecimalError,
)

STORE_INTO_TRUNCATION_CASES = (
    ("1234.567", 10, 2, Decimal("1234.56")),
    ("-1234.567", 10, 2, Decimal("-1234.56")),
    ("0.009", 9, 2, Decimal("0.00")),
)

STORE_INTO_HIGH_ORDER_CASES = (
    ("1000000000.50", 9, 2, Decimal("0.50")),
    ("-1234567890.12", 9, 2, Decimal("-234567890.12")),
)

SAMPLE_FIELD_CASES = (
    ("00000001940{", 2, Decimal("194.00")),
    ("0000005047G", 2, Decimal("504.77")),
    ("0000009190}", 2, Decimal("-919.00")),
    ("0000000567P", 2, Decimal("-56.77")),
)

SAMPLE_FIELD_IDS = (
    "acctdata-1-ACCT-CURR-BAL",
    "dailytran-1-DALYTRAN-AMT",
    "dailytran-negative-zero-overpunch",
    "dailytran-negative-seven-overpunch",
)

OVERPUNCH_CASES = (
    ("{", Decimal(120)),
    ("A", Decimal(121)),
    ("B", Decimal(122)),
    ("C", Decimal(123)),
    ("D", Decimal(124)),
    ("E", Decimal(125)),
    ("F", Decimal(126)),
    ("G", Decimal(127)),
    ("H", Decimal(128)),
    ("I", Decimal(129)),
    ("}", Decimal(-120)),
    ("J", Decimal(-121)),
    ("K", Decimal(-122)),
    ("L", Decimal(-123)),
    ("M", Decimal(-124)),
    ("N", Decimal(-125)),
    ("O", Decimal(-126)),
    ("P", Decimal(-127)),
    ("Q", Decimal(-128)),
    ("R", Decimal(-129)),
)

MALFORMED_ZONED_CASES = (
    "00A0001940{",
    "0000000194S",
)

MALFORMED_ZONED_IDS = (
    "non-digit-body",
    "unknown-sign-character",
)

ENCODE_OVERPUNCH_CASES = (
    (Decimal("-56.77"), 9, 2, "0000000567P"),
    (Decimal("194.00"), 10, 2, "00000001940{"),
    (Decimal(0), 9, 2, "0000000000{"),
)

ROUND_TRIP_VALUES = (
    "0.00",
    "194.00",
    "504.77",
    "-56.77",
    "-919.00",
    "999999999.99",
    "-999999999.99",
)


def test_cobol_context_precision_is_31_digits():
    """31 digits cover the 30-digit ARITH(COMPAT) intermediates."""
    assert COBOL_CONTEXT.prec == 31


@pytest.mark.parametrize("valueText, intDigits, decDigits, expected", STORE_INTO_TRUNCATION_CASES)
def test_store_into_truncates_toward_zero(valueText, intDigits, decDigits, expected):
    assert StoreInto(Decimal(valueText), intDigits, decDigits) == expected


@pytest.mark.parametrize("valueText, intDigits, decDigits, expected", STORE_INTO_HIGH_ORDER_CASES)
def test_store_into_drops_high_order_digits_keeping_sign(valueText, intDigits, decDigits, expected):
    """COBIL00C line 224 moves an S9(10)V99 balance into the S9(09)V99 TRAN-AMT."""
    assert StoreInto(Decimal(valueText), intDigits, decDigits) == expected


def test_store_into_quantizes_to_receiver_scale():
    result = StoreInto(Decimal(5), 9, 2)
    assert isinstance(result, Decimal)
    assert str(result) == "5.00"


def test_store_into_unsigned_receiver_stores_absolute_value():
    """A signed value moved into an unsigned 9(n) receiver keeps its absolute value."""
    assert StoreInto(Decimal("-12.34"), 4, 2, signed=False) == Decimal("12.34")
    assert StoreInto(Decimal(-5), 4, 0, signed=False) == Decimal(5)


def test_store_into_monthly_interest_formula_cbact04c():
    """CBACT04C lines 464-465: 1234.56 * 15.00 / 1200 = 15.432, stored in S9(09)V99 as 15.43."""
    with localcontext(COBOL_CONTEXT):
        rawInterest = Decimal("1234.56") * Decimal("15.00") / Decimal(1200)
    assert StoreInto(rawInterest, 9, 2) == Decimal("15.43")


@pytest.mark.parametrize("text, scale, expected", SAMPLE_FIELD_CASES, ids=SAMPLE_FIELD_IDS)
def test_decode_zoned_real_sample_fields(text, scale, expected):
    result = DecodeZoned(text, scale)
    assert isinstance(result, Decimal)
    assert result == expected


@pytest.mark.parametrize("signChar, expectedValue", OVERPUNCH_CASES)
def test_decode_zoned_overpunch_table(signChar, expectedValue):
    """The 12 prefix makes the sign carried by a zero digit ({ and }) observable."""
    assert DecodeZoned("12" + signChar, 0) == expectedValue


def test_decode_zoned_plain_final_digit_is_positive():
    assert DecodeZoned("0000000123", 2) == Decimal("1.23")


@pytest.mark.parametrize("malformedText", MALFORMED_ZONED_CASES, ids=MALFORMED_ZONED_IDS)
def test_decode_zoned_rejects_malformed_text(malformedText):
    with pytest.raises(ZonedDecimalError):
        DecodeZoned(malformedText, 2)


def test_zoned_decimal_error_is_a_value_error():
    assert issubclass(ZonedDecimalError, ValueError)


@pytest.mark.parametrize("value, intDigits, decDigits, expectedText", ENCODE_OVERPUNCH_CASES)
def test_encode_zoned_overpunches_last_digit(value, intDigits, decDigits, expectedText):
    assert EncodeZoned(value, intDigits, decDigits) == expectedText


def test_encode_zoned_unsigned_gives_plain_digits():
    assert EncodeZoned(Decimal(123), 4, 0, signed=False) == "0123"


@pytest.mark.parametrize("valueText", ROUND_TRIP_VALUES)
def test_encode_then_decode_round_trips(valueText):
    value = Decimal(valueText)
    encodedText = EncodeZoned(value, 9, 2)
    assert len(encodedText) == 11
    assert DecodeZoned(encodedText, 2) == value
