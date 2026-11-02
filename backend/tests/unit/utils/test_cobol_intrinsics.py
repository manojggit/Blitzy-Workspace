"""Unit tests for app.utils.cobol_intrinsics, the port of FUNCTION TEST-NUMVAL-C, NUMVAL-C,
TEST-NUMVAL and NUMVAL (AAP 0.6.4, "Conversion").

Expected values follow the IBM Enterprise COBOL argument grammar the cited programs rely on,
never the unit's own output:
cbl/COACTUPC.cbl lines 2180-2215 (1250-EDIT-SIGNED-9V2 rejects with " is not valid" unless
TEST-NUMVAL-C returns 0, then stores NUMVAL-C into S9(10)V99), and
cbl/COTRN02C.cbl lines 340-346 (the positional amount format check) and 383-386 (NUMVAL-C on
the transaction amount). "$" and "," belong to NUMVAL-C only; a trailing "-", "CR" or "DB"
makes the value negative.

The module is imported rather than its names, so no module-level name begins with "Test" and
pytest collects only the test_* functions.
"""

from decimal import Decimal

import pytest

from app.utils import cobol_intrinsics
from app.utils.decimal_utils import StoreInto

NUMVAL_C_VALID_CASES = [
    ("  42  ", Decimal(42)),
    ("+12.50", Decimal("12.50")),
    ("-12.50", Decimal("-12.50")),
    ("12.50+", Decimal("12.50")),
    ("12.50-", Decimal("-12.50")),
    ("12.50CR", Decimal("-12.50")),
    ("12.50DB", Decimal("-12.50")),
    ("$1,234.567", Decimal("1234.567")),
    ("1,234.56", Decimal("1234.56")),
    ("+00000050.00", Decimal("50.00")),
]
NUMVAL_C_VALID_IDS = [
    "surrounding-spaces",
    "leading-plus",
    "leading-minus",
    "trailing-plus",
    "trailing-minus",
    "trailing-cr",
    "trailing-db",
    "currency-grouping-three-decimals",
    "grouping",
    "ct02-amount-form",
]

NUMVAL_C_INVALID_TEXTS = ["abc", "1.2.3", "--1", "1-2"]
NUMVAL_C_INVALID_IDS = ["letters", "two-decimal-points", "two-signs", "embedded-sign"]

NUMVAL_INVALID_TEXTS = ["$12.50", "1,234"]
NUMVAL_INVALID_IDS = ["currency-sign", "grouping-comma"]

# CAUP stores NUMVAL-C of its credit-limit text into ACCT-CREDIT-LIMIT PIC S9(10)V99.
CAUP_CURRENCY_TEXT = "$1,234.567"
CAUP_INTEGER_DIGITS = 10
CAUP_DECIMAL_DIGITS = 2

RESULT_TYPE_CALLS = [
    ("NumvalC", "12.50"),
    ("NumvalC", "abc"),
    ("Numval", "4"),
    ("Numval", "$1"),
]
RESULT_TYPE_IDS = ["numval-c-valid", "numval-c-invalid", "numval-valid", "numval-invalid"]


@pytest.mark.parametrize("text, expected", NUMVAL_C_VALID_CASES, ids=NUMVAL_C_VALID_IDS)
def test_numval_c_accepts_currency_text(text, expected):
    assert cobol_intrinsics.TestNumvalC(text) is True
    assert cobol_intrinsics.NumvalC(text) == expected


def test_numval_c_caup_credit_limit_stores_truncated():
    """CAUP accepts "$1,234.567" and the S9(10)V99 receiver truncates it to 1234.56."""
    assert cobol_intrinsics.TestNumvalC(CAUP_CURRENCY_TEXT) is True
    creditLimit = StoreInto(
        cobol_intrinsics.NumvalC(CAUP_CURRENCY_TEXT), CAUP_INTEGER_DIGITS, CAUP_DECIMAL_DIGITS
    )
    assert creditLimit == Decimal("1234.56")


@pytest.mark.parametrize("text", NUMVAL_C_INVALID_TEXTS, ids=NUMVAL_C_INVALID_IDS)
def test_numval_c_rejects_malformed_text(text):
    assert cobol_intrinsics.TestNumvalC(text) is False
    assert cobol_intrinsics.NumvalC(text) == Decimal(0)


@pytest.mark.parametrize("text", NUMVAL_INVALID_TEXTS, ids=NUMVAL_INVALID_IDS)
def test_numval_rejects_currency_and_grouping(text):
    assert cobol_intrinsics.TestNumval(text) is False
    assert cobol_intrinsics.Numval(text) == Decimal(0)


def test_numval_parses_zero_padded_identifier():
    """COTRN02C (line 204) applies NUMVAL to the keyed 11-digit account ID."""
    assert cobol_intrinsics.TestNumval("00000000004") is True
    assert cobol_intrinsics.Numval("00000000004") == Decimal(4)


def test_numval_accepts_signed_decimal():
    assert cobol_intrinsics.Numval(" -12.5 ") == Decimal("-12.5")


@pytest.mark.parametrize("functionName, text", RESULT_TYPE_CALLS, ids=RESULT_TYPE_IDS)
def test_results_are_decimal_never_float(functionName, text):
    result = getattr(cobol_intrinsics, functionName)(text)
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)
