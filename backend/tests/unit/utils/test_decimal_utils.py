"""Unit tests for app.utils.decimal_utils, the COBOL numeric store and zoned-decimal codec.

Expected values are derived by hand from the COBOL rules, never from the unit:
- IBM Enterprise COBOL stores without ROUNDED or ON SIZE ERROR by truncating surplus fraction
    digits toward zero and dropping high-order integer digits, keeping the sign (AAP 0.6.6).
- ARITH(COMPAT) keeps intermediates of up to 30 digits, such as the product of two 9(15)
    operands; the 31 digits of COBOL_CONTEXT hold them exactly.
- cbl/CBACT04C.cbl lines 464-465: COMPUTE WS-MONTHLY-INT = (TRAN-CAT-BAL * DIS-INT-RATE) / 1200.
- cbl/CBACT04C.cbl lines 474-479: ADD 1 TO WS-TRANID-SUFFIX, a 9(06) field, wraps 999999 to 0.
- cbl/COBIL00C.cbl line 224: MOVE ACCT-CURR-BAL (S9(10)V99) TO TRAN-AMT (S9(09)V99).
- cbl/COBIL00C.cbl line 234: COMPUTE ACCT-CURR-BAL = ACCT-CURR-BAL - TRAN-AMT, into S9(10)V99.
- cbl/COTRN02C.cbl lines 444-449 and cbl/COBIL00C.cbl lines 212-217: ADD 1 to the 9(16) TRAN-ID
    wraps 9999999999999999 to 0.
- cpy/CVACT01Y.cpy ACCT-CURR-BAL S9(10)V99 and cpy/CVTRA06Y.cpy DALYTRAN-AMT S9(09)V99, zoned
    DISPLAY fields with a trailing sign overpunch, read from data/ASCII/acctdata.txt record 1
    (offset 12) and data/ASCII/dailytran.txt (offset 132).
"""

import re
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    getcontext,
    localcontext,
)

import pytest

from app.utils.decimal_utils import (
    COBOL_CONTEXT,
    DecodeZoned,
    EncodeZoned,
    StoreInto,
    ZonedDecimalError,
)

STORE_CASE_ARGNAMES = "valueText, intDigits, decDigits, expectedText"

VALUE_CASE_ARGNAMES = "value, intDigits, decDigits, expectedText"

STORE_INTO_TRUNCATION_CASES = (
    ("1234.567", 10, 2, "1234.56"),
    ("-1234.567", 10, 2, "-1234.56"),
    ("0.009", 9, 2, "0.00"),
)

STORE_INTO_HIGH_ORDER_CASES = (
    ("1000000000.50", 9, 2, "0.50"),
    ("-1234567890.12", 9, 2, "-234567890.12"),
)

STORE_INTO_HIGH_ORDER_IDS = (
    "COBIL00C-balance-into-S9(09)V99-TRAN-AMT",
    "S9(09)V99-ten-integer-digits-negative",
)

# A receiver keeps a value at its full width and drops the integer digits beyond it:
# 12345678901 keeps 2345678901 in S9(10)V99, 1000000001 keeps 1 in S9(09)V99, 12345 keeps 2345
# in S9(04)V99, and the fraction-only SV99 keeps no integer digit at all.
RECEIVER_WIDTH_CASES = (
    ("9999999999.99", 10, 2, "9999999999.99"),
    ("10000000000.00", 10, 2, "0.00"),
    ("12345678901.23", 10, 2, "2345678901.23"),
    ("-12345678901.23", 10, 2, "-2345678901.23"),
    ("999999999.99", 9, 2, "999999999.99"),
    ("-1000000001.25", 9, 2, "-1.25"),
    ("9999.99", 4, 2, "9999.99"),
    ("12345.67", 4, 2, "2345.67"),
    ("123.456", 0, 2, "0.45"),
    ("-0.987", 0, 2, "-0.98"),
)

RECEIVER_WIDTH_IDS = (
    "S9(10)V99-full-width-kept",
    "S9(10)V99-one-past-width-wraps-to-zero",
    "S9(10)V99-eleven-integer-digits",
    "S9(10)V99-eleven-integer-digits-negative",
    "S9(09)V99-full-width-kept",
    "S9(09)V99-keeps-one-integer-digit-negative",
    "S9(04)V99-full-width-kept",
    "S9(04)V99-five-integer-digits",
    "SV99-drops-integer-part",
    "SV99-drops-integer-part-negative",
)

COBOL_CONTEXT_TRAPS = (InvalidOperation, DivisionByZero, Overflow)

# Two 9(15) operands, 123456789012345 * 987654321098765, give this exact 30-digit ARITH(COMPAT)
# intermediate. A receiver keeps its low-order integer digits (the last 10, 9, 4, 16 or 11) and,
# as the product has no fraction, zero decimals.
THIRTY_DIGIT_PRODUCT = "121932631137021071359549253925"

# 1234567890123456789012345678.12 has 30 significant digits; its last nine integer digits are
# 012345678, so S9(09)V99 keeps 12345678.12.
THIRTY_DIGIT_STORE_CASES = (
    (THIRTY_DIGIT_PRODUCT, 10, 2, "9549253925.00"),
    (THIRTY_DIGIT_PRODUCT, 9, 2, "549253925.00"),
    (THIRTY_DIGIT_PRODUCT, 4, 2, "3925.00"),
    (THIRTY_DIGIT_PRODUCT, 16, 0, "1071359549253925"),
    (THIRTY_DIGIT_PRODUCT, 11, 0, "59549253925"),
    ("-" + THIRTY_DIGIT_PRODUCT, 10, 2, "-9549253925.00"),
    ("-" + THIRTY_DIGIT_PRODUCT, 9, 2, "-549253925.00"),
    ("-" + THIRTY_DIGIT_PRODUCT, 4, 2, "-3925.00"),
    ("-" + THIRTY_DIGIT_PRODUCT, 16, 0, "-1071359549253925"),
    ("-" + THIRTY_DIGIT_PRODUCT, 11, 0, "-59549253925"),
    ("1234567890123456789012345678.12", 9, 2, "12345678.12"),
    ("-1234567890123456789012345678.12", 9, 2, "-12345678.12"),
)

THIRTY_DIGIT_STORE_IDS = (
    "30-digit-product-S9(10)V99",
    "30-digit-product-S9(09)V99",
    "30-digit-product-S9(04)V99",
    "30-digit-product-S9(16)",
    "30-digit-product-S9(11)",
    "negative-30-digit-product-S9(10)V99",
    "negative-30-digit-product-S9(09)V99",
    "negative-30-digit-product-S9(04)V99",
    "negative-30-digit-product-S9(16)",
    "negative-30-digit-product-S9(11)",
    "30-digits-with-fraction-S9(09)V99",
    "negative-30-digits-with-fraction-S9(09)V99",
)

# The largest 31-digit integer is a legal intermediate; a receiver keeps its last 9 or 11 nines.
INTERMEDIATE_LIMIT_CASES = (
    ("9" * 31, 9, 2, "999999999.00"),
    ("9" * 31, 11, 2, "99999999999.00"),
)

HIGH_ORDER_STORE_CASES = (
    STORE_INTO_HIGH_ORDER_CASES
    + RECEIVER_WIDTH_CASES
    + THIRTY_DIGIT_STORE_CASES
    + INTERMEDIATE_LIMIT_CASES
)

HIGH_ORDER_STORE_IDS = (
    STORE_INTO_HIGH_ORDER_IDS
    + RECEIVER_WIDTH_IDS
    + THIRTY_DIGIT_STORE_IDS
    + ("31-nines-S9(09)V99", "31-nines-S9(11)V99")
)

ISOLATION_STORE_CASES = THIRTY_DIGIT_STORE_CASES + STORE_INTO_TRUNCATION_CASES

ISOLATION_STORE_IDS = THIRTY_DIGIT_STORE_IDS + (
    "truncation-positive",
    "truncation-negative",
    "truncation-to-zero",
)

# A caller's low-precision, half-up context, trapping and not, that StoreInto must not use.
HOSTILE_PRECISION = 5

HOSTILE_CONTEXTS = (
    Context(prec=HOSTILE_PRECISION, rounding=ROUND_HALF_UP),
    Context(prec=HOSTILE_PRECISION, rounding=ROUND_HALF_UP, traps=[]),
)

HOSTILE_CONTEXT_IDS = ("prec-5-half-up-trapping", "prec-5-half-up-no-traps")

STORE_FUNCTIONS = (StoreInto, EncodeZoned)

# The float is supplied only to prove that it is refused: COBOL money is never floating point.
WRONG_TYPE_VALUES = (1.5, True, "1.00", None)

WRONG_TYPE_IDS = ("float", "bool", "str", "None")

NON_FINITE_VALUES = (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"))

IMPOSSIBLE_PICTURES = ((-1, 2), (9, -1), (0, 0))

IMPOSSIBLE_PICTURE_IDS = ("negative-integer-digits", "negative-decimal-digits", "no-digits")

# Each integer part is longer than the 31 digits of a COBOL intermediate.
OVER_CONTEXT_VALUES = (
    Decimal("1E+40"),
    Decimal(10**31 + 7),
    Decimal("1" * 35),
    Decimal("9" * 32 + ".5"),
)

OVER_CONTEXT_IDS = ("1E+40", "32-digit-integer", "35-digit-integer", "32-digit-integer-and-half")

NON_STR_ZONED_INPUTS = (b"0001", 1, None)

NON_STR_ZONED_IDS = ("bytes", "int", "None")

OUT_OF_RANGE_SCALE_CASES = (
    (-1, "scale -1 is outside 0 to 4"),
    (5, "scale 5 is outside 0 to 4"),
)

OUT_OF_RANGE_SCALE_IDS = ("negative-scale", "scale-beyond-length")

# Each message names the character's repr and its 1-based position in the zoned text.
BAD_CHARACTER_CASES = (
    (" 0001", "invalid character ' ' at position 1"),
    ("0001 ", "invalid character ' ' at position 5"),
    ("00a1", "invalid character 'a' at position 3"),
    ("000a", "invalid character 'a' at position 4"),
    ("+001", "invalid character '+' at position 1"),
    ("000-", "invalid character '-' at position 4"),
    ("\x00\x00", "invalid character '\\x00' at position 1"),
    ("00\u0663", "invalid character '\u0663' at position 3"),
    ("\uff1100", "invalid character '\uff11' at position 1"),
)

BAD_CHARACTER_IDS = (
    "leading-space",
    "trailing-space",
    "lowercase-body-letter",
    "lowercase-final-letter",
    "plus-sign",
    "minus-sign",
    "nul",
    "arabic-indic-digit",
    "fullwidth-digit",
)

# The stored exponent is always -decDigits: an integer gains zeros, surplus decimals truncate,
# and 1234567 keeps only its six low-order digits 234567 in an S9(06) receiver.
STORE_INTO_SCALE_CASES = (
    ("5", 9, 2, "5.00"),
    ("7", 13, 4, "7.0000"),
    ("-123.456789", 13, 4, "-123.4567"),
    ("1234567.89", 6, 0, "234567"),
    ("0.5", 0, 2, "0.50"),
)

STORE_INTO_SCALE_IDS = (
    "S9(09)V99-integer-gains-zeros",
    "S9(13)V9(4)-integer-gains-zeros",
    "S9(13)V9(4)-truncates-fifth-decimal",
    "S9(06)-drops-fraction-and-high-order",
    "SV99-fraction-only",
)

# CBACT04C lines 474-479 add 1 to the 9(06) WS-TRANID-SUFFIX, and COTRN02C lines 444-449 and
# COBIL00C lines 212-217 add 1 to the 9(16) TRAN-ID: one past the largest value wraps to zero.
STORE_INTO_UNSIGNED_CASES = (
    (Decimal("-12.34"), 4, 2, "12.34"),
    (Decimal(-5), 4, 0, "5"),
    (999999, 6, 0, "999999"),
    (999999 + 1, 6, 0, "0"),
    (9999999999999999, 16, 0, "9999999999999999"),
    (9999999999999999 + 1, 16, 0, "0"),
)

STORE_INTO_UNSIGNED_IDS = (
    "negative-decimal-into-9(04)V99",
    "negative-decimal-into-9(04)",
    "9(06)-largest-suffix-kept",
    "9(06)-suffix-wraps-to-zero",
    "9(16)-largest-tran-id-kept",
    "9(16)-tran-id-wraps-to-zero",
)

SAMPLE_FIELD_CASES = (
    ("00000001940{", 2, "194.00"),
    ("0000005047G", 2, "504.77"),
    ("0000009190}", 2, "-919.00"),
    ("0000000567P", 2, "-56.77"),
)

SAMPLE_FIELD_IDS = (
    "acctdata-1-ACCT-CURR-BAL",
    "dailytran-1-DALYTRAN-AMT",
    "dailytran-negative-zero-overpunch",
    "dailytran-negative-seven-overpunch",
)

OVERPUNCH_CASES = (
    ("{", "120"),
    ("A", "121"),
    ("B", "122"),
    ("C", "123"),
    ("D", "124"),
    ("E", "125"),
    ("F", "126"),
    ("G", "127"),
    ("H", "128"),
    ("I", "129"),
    ("}", "-120"),
    ("J", "-121"),
    ("K", "-122"),
    ("L", "-123"),
    ("M", "-124"),
    ("N", "-125"),
    ("O", "-126"),
    ("P", "-127"),
    ("Q", "-128"),
    ("R", "-129"),
)

# The final character carries the last digit and the sign: "J" is -1, "}" is -0 and "E" is +5.
# The result exponent is -scale, so "1000}" at scale 3 keeps its three trailing zeros.
DECODE_SCALE_CASES = (
    ("00000000001", 0, "1"),
    ("000012J", 1, "-12.1"),
    ("1000}", 3, "-10.000"),
    ("0012345E", 4, "12.3455"),
    ("123", 3, "0.123"),
)

DECODE_SCALE_IDS = (
    "scale-0",
    "scale-1-negative",
    "scale-3-trailing-zeros",
    "scale-4",
    "scale-equals-length",
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

# "{" and "A"-"I" overpunch a last digit of +0 to +9, "}" and "J"-"R" one of -0 to -9, so in
# S9(03) the values 10 to 19 end "01{" to "01I" and -10 to -19 end "01}" to "01R".
ENCODE_EVERY_OVERPUNCH_CASES = (
    (Decimal(10), 3, 0, "01{"),
    (Decimal(11), 3, 0, "01A"),
    (Decimal(12), 3, 0, "01B"),
    (Decimal(13), 3, 0, "01C"),
    (Decimal(14), 3, 0, "01D"),
    (Decimal(15), 3, 0, "01E"),
    (Decimal(16), 3, 0, "01F"),
    (Decimal(17), 3, 0, "01G"),
    (Decimal(18), 3, 0, "01H"),
    (Decimal(19), 3, 0, "01I"),
    (Decimal(-10), 3, 0, "01}"),
    (Decimal(-11), 3, 0, "01J"),
    (Decimal(-12), 3, 0, "01K"),
    (Decimal(-13), 3, 0, "01L"),
    (Decimal(-14), 3, 0, "01M"),
    (Decimal(-15), 3, 0, "01N"),
    (Decimal(-16), 3, 0, "01O"),
    (Decimal(-17), 3, 0, "01P"),
    (Decimal(-18), 3, 0, "01Q"),
    (Decimal(-19), 3, 0, "01R"),
)

# The value is stored before it is encoded: 504.779 and -56.779 lose their third decimal,
# 1000000504.77 keeps 504.77 in S9(09)V99, -12345678901.23 keeps -2345678901.23 in S9(10)V99,
# and the 30-digit product keeps 9549253925.00, digits 954925392500, in S9(10)V99.
ENCODE_TRUNCATION_CASES = (
    (Decimal("504.779"), 9, 2, "0000005047G"),
    (Decimal("-56.779"), 9, 2, "0000000567P"),
    (Decimal("1000000504.77"), 9, 2, "0000005047G"),
    (Decimal("-12345678901.23"), 10, 2, "23456789012L"),
    (Decimal(THIRTY_DIGIT_PRODUCT), 10, 2, "95492539250{"),
    (Decimal("-" + THIRTY_DIGIT_PRODUCT), 10, 2, "95492539250}"),
)

ENCODE_SIGNED_CASES = (
    ENCODE_OVERPUNCH_CASES + ENCODE_EVERY_OVERPUNCH_CASES + ENCODE_TRUNCATION_CASES
)

# An unsigned picture has no overpunch and keeps the absolute value of a negative input.
ENCODE_UNSIGNED_CASES = (
    (Decimal(123), 4, 0, "0123"),
    (Decimal(-123), 4, 0, "0123"),
    (Decimal("-12.34"), 4, 2, "001234"),
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


def AssertExactDecimal(result, expectedText):
    """Assert result is the Decimal expectedText, exponent included; a zero's sign is not pinned."""
    expected = Decimal(expectedText)
    assert isinstance(result, Decimal)
    assert result.as_tuple().exponent == expected.as_tuple().exponent
    assert result.as_tuple().digits == expected.as_tuple().digits
    assert result == expected
    if not expected.is_zero():
        assert str(result) == expectedText


def AssertContextUntouched(context, expectedPrecision, expectedRounding):
    """Assert a decimal context kept its precision and rounding and has no flag raised."""
    assert context.prec == expectedPrecision
    assert context.rounding == expectedRounding
    assert not any(context.flags.values())


def test_cobol_context_precision_is_31_digits():
    """31 digits cover the 30-digit ARITH(COMPAT) intermediates."""
    assert COBOL_CONTEXT.prec == 31


def test_cobol_context_truncates_toward_zero():
    """No in-scope COBOL statement says ROUNDED, so intermediates truncate."""
    assert COBOL_CONTEXT.rounding == ROUND_DOWN


@pytest.mark.parametrize("trappedSignal", COBOL_CONTEXT_TRAPS)
def test_cobol_context_traps_invalid_intermediates(trappedSignal):
    """An invalid, divide-by-zero or overflowing intermediate raises rather than being stored."""
    assert COBOL_CONTEXT.traps[trappedSignal]


def test_store_into_stores_product_of_two_15_digit_operands():
    """Two 9(15) operands multiply exactly under COBOL_CONTEXT into a 30-digit intermediate."""
    with localcontext(COBOL_CONTEXT):
        product = Decimal(123456789012345) * Decimal(987654321098765)
        negativeProduct = Decimal(-123456789012345) * Decimal(987654321098765)
    AssertExactDecimal(product, THIRTY_DIGIT_PRODUCT)
    AssertExactDecimal(StoreInto(product, 10, 2), "9549253925.00")
    AssertExactDecimal(StoreInto(negativeProduct, 10, 2), "-9549253925.00")


@pytest.mark.parametrize("hostileContext", HOSTILE_CONTEXTS, ids=HOSTILE_CONTEXT_IDS)
@pytest.mark.parametrize("storeCase", ISOLATION_STORE_CASES, ids=ISOLATION_STORE_IDS)
def test_store_into_ignores_and_preserves_caller_context(storeCase, hostileContext):
    valueText, intDigits, decDigits, expectedText = storeCase
    value = Decimal(valueText)
    with localcontext(hostileContext) as callerContext:
        result = StoreInto(value, intDigits, decDigits)
        contextAfterCall = getcontext()
    AssertExactDecimal(result, expectedText)
    assert contextAfterCall is callerContext
    AssertContextUntouched(callerContext, HOSTILE_PRECISION, ROUND_HALF_UP)
    AssertContextUntouched(COBOL_CONTEXT, 31, ROUND_DOWN)


@pytest.mark.parametrize(STORE_CASE_ARGNAMES, STORE_INTO_TRUNCATION_CASES)
def test_store_into_truncates_toward_zero(valueText, intDigits, decDigits, expectedText):
    AssertExactDecimal(StoreInto(Decimal(valueText), intDigits, decDigits), expectedText)


@pytest.mark.parametrize(STORE_CASE_ARGNAMES, HIGH_ORDER_STORE_CASES, ids=HIGH_ORDER_STORE_IDS)
def test_store_into_drops_high_order_digits_keeping_sign(
    valueText, intDigits, decDigits, expectedText
):
    """Without ON SIZE ERROR a receiver keeps only its low-order integer digits (COBIL00C l.224)."""
    AssertExactDecimal(StoreInto(Decimal(valueText), intDigits, decDigits), expectedText)


def test_store_into_bill_payment_at_width_boundary_cobil00c():
    """COBIL00C lines 224 and 234: a 1000000000.50 balance pays 0.50 and keeps 1000000000.00."""
    currBal = Decimal("1000000000.50")
    tranAmt = StoreInto(currBal, 9, 2)
    AssertExactDecimal(tranAmt, "0.50")
    with localcontext(COBOL_CONTEXT):
        remaining = currBal - tranAmt
    AssertExactDecimal(StoreInto(remaining, 10, 2), "1000000000.00")


@pytest.mark.parametrize(STORE_CASE_ARGNAMES, STORE_INTO_SCALE_CASES, ids=STORE_INTO_SCALE_IDS)
def test_store_into_quantizes_to_receiver_scale(valueText, intDigits, decDigits, expectedText):
    AssertExactDecimal(StoreInto(Decimal(valueText), intDigits, decDigits), expectedText)


@pytest.mark.parametrize(
    VALUE_CASE_ARGNAMES, STORE_INTO_UNSIGNED_CASES, ids=STORE_INTO_UNSIGNED_IDS
)
def test_store_into_unsigned_receiver_stores_absolute_value(
    value, intDigits, decDigits, expectedText
):
    """An unsigned 9(n) receiver keeps the absolute value, and ADD 1 past 10**n - 1 wraps to 0."""
    AssertExactDecimal(StoreInto(value, intDigits, decDigits, signed=False), expectedText)


def test_store_into_monthly_interest_formula_cbact04c():
    """CBACT04C lines 464-465: 1234.56 * 15.00 / 1200 = 15.432, stored in S9(09)V99 as 15.43."""
    with localcontext(COBOL_CONTEXT):
        rawInterest = Decimal("1234.56") * Decimal("15.00") / Decimal(1200)
    AssertExactDecimal(StoreInto(rawInterest, 9, 2), "15.43")


@pytest.mark.parametrize("wrongTypeValue", WRONG_TYPE_VALUES, ids=WRONG_TYPE_IDS)
@pytest.mark.parametrize("storeFunction", STORE_FUNCTIONS)
def test_store_functions_reject_non_numeric_types(storeFunction, wrongTypeValue):
    with pytest.raises(TypeError, match="must be Decimal or int"):
        storeFunction(wrongTypeValue, 9, 2)


@pytest.mark.parametrize("nonFiniteValue", NON_FINITE_VALUES, ids=str)
@pytest.mark.parametrize("storeFunction", STORE_FUNCTIONS)
def test_store_functions_reject_non_finite_values(storeFunction, nonFiniteValue):
    with pytest.raises(ZonedDecimalError, match="non-finite"):
        storeFunction(nonFiniteValue, 9, 2)


@pytest.mark.parametrize("picture", IMPOSSIBLE_PICTURES, ids=IMPOSSIBLE_PICTURE_IDS)
@pytest.mark.parametrize("storeFunction", STORE_FUNCTIONS)
def test_store_functions_reject_impossible_pictures(storeFunction, picture):
    """A picture needs non-negative digit counts and at least one digit."""
    intDigits, decDigits = picture
    with pytest.raises(ZonedDecimalError, match="impossible picture"):
        storeFunction(Decimal("1.00"), intDigits, decDigits)


@pytest.mark.parametrize("overContextValue", OVER_CONTEXT_VALUES, ids=OVER_CONTEXT_IDS)
@pytest.mark.parametrize("storeFunction", STORE_FUNCTIONS)
def test_store_functions_reject_values_beyond_31_digit_intermediates(
    storeFunction, overContextValue
):
    with pytest.raises(InvalidOperation):
        storeFunction(overContextValue, 9, 2)


@pytest.mark.parametrize("text, scale, expectedText", SAMPLE_FIELD_CASES, ids=SAMPLE_FIELD_IDS)
def test_decode_zoned_real_sample_fields(text, scale, expectedText):
    AssertExactDecimal(DecodeZoned(text, scale), expectedText)


@pytest.mark.parametrize("signChar, expectedText", OVERPUNCH_CASES)
def test_decode_zoned_overpunch_table(signChar, expectedText):
    """The 12 prefix makes the sign carried by a zero digit ({ and }) observable."""
    AssertExactDecimal(DecodeZoned("12" + signChar, 0), expectedText)


def test_decode_zoned_plain_final_digit_is_positive():
    AssertExactDecimal(DecodeZoned("0000000123", 2), "1.23")


@pytest.mark.parametrize("text, scale, expectedText", DECODE_SCALE_CASES, ids=DECODE_SCALE_IDS)
def test_decode_zoned_keeps_every_implied_decimal(text, scale, expectedText):
    AssertExactDecimal(DecodeZoned(text, scale), expectedText)


@pytest.mark.parametrize("malformedText", MALFORMED_ZONED_CASES, ids=MALFORMED_ZONED_IDS)
def test_decode_zoned_rejects_malformed_text(malformedText):
    with pytest.raises(ZonedDecimalError):
        DecodeZoned(malformedText, 2)


@pytest.mark.parametrize("nonText", NON_STR_ZONED_INPUTS, ids=NON_STR_ZONED_IDS)
def test_decode_zoned_rejects_non_str_input(nonText):
    """Zoned fields are decoded from text; bytes are decoded with cp037 first."""
    with pytest.raises(TypeError, match="zoned text must be str"):
        DecodeZoned(nonText, 0)


def test_decode_zoned_rejects_empty_text():
    with pytest.raises(ZonedDecimalError, match="zoned text is empty"):
        DecodeZoned("", 0)


@pytest.mark.parametrize(
    "badScale, expectedMessage", OUT_OF_RANGE_SCALE_CASES, ids=OUT_OF_RANGE_SCALE_IDS
)
def test_decode_zoned_rejects_scale_outside_field(badScale, expectedMessage):
    """A four-digit field has from 0 to 4 implied decimal places."""
    with pytest.raises(ZonedDecimalError, match=re.escape(expectedMessage)):
        DecodeZoned("0001", badScale)


@pytest.mark.parametrize("badText, expectedMessage", BAD_CHARACTER_CASES, ids=BAD_CHARACTER_IDS)
def test_decode_zoned_rejects_bad_character_naming_its_position(badText, expectedMessage):
    with pytest.raises(ZonedDecimalError, match=re.escape(expectedMessage)):
        DecodeZoned(badText, 0)


def test_zoned_decimal_error_is_a_value_error():
    assert issubclass(ZonedDecimalError, ValueError)


@pytest.mark.parametrize(VALUE_CASE_ARGNAMES, ENCODE_SIGNED_CASES, ids=str)
def test_encode_zoned_overpunches_last_digit(value, intDigits, decDigits, expectedText):
    assert EncodeZoned(value, intDigits, decDigits) == expectedText


@pytest.mark.parametrize(VALUE_CASE_ARGNAMES, ENCODE_UNSIGNED_CASES, ids=str)
def test_encode_zoned_unsigned_gives_plain_digits(value, intDigits, decDigits, expectedText):
    assert EncodeZoned(value, intDigits, decDigits, signed=False) == expectedText


@pytest.mark.parametrize("valueText", ROUND_TRIP_VALUES)
def test_encode_then_decode_round_trips(valueText):
    encodedText = EncodeZoned(Decimal(valueText), 9, 2)
    assert len(encodedText) == 11
    AssertExactDecimal(DecodeZoned(encodedText, 2), valueText)
