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

import time
from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest

from app.utils import cobol_intrinsics
from app.utils.decimal_utils import StoreInto

NUMVAL_C_VALID_CASES = [
    ("  42  ", "42"),
    ("+12.50", "12.50"),
    ("-12.50", "-12.50"),
    ("12.50+", "12.50"),
    ("12.50-", "-12.50"),
    ("12.50CR", "-12.50"),
    ("12.50DB", "-12.50"),
    ("$1,234.567", "1234.567"),
    ("1,234.56", "1234.56"),
    ("+00000050.00", "50.00"),
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

# Each TEST-/converter pair by function name, so every case reports the pair it exercised.
NUMVAL_C_PAIR = ("TestNumvalC", "NumvalC")
NUMVAL_PAIR = ("TestNumval", "Numval")

# (case id, text, NUMVAL-C value text, NUMVAL value text). None marks text the pair's grammar
# rejects, so the TEST- function returns False and the converter exactly Decimal(0). "$" and
# "," belong to NUMVAL-C only, and "$" may follow a leading sign but never come before one.
GRAMMAR_ROWS = [
    ("currency-grouping-three-decimals", "$1,234.567", "1234.567", None),
    ("ct02-zero-padded-amount", "+00000050.00", "50.00", "50.00"),
    ("spaced-leading-minus", "  -1234.56  ", "-1234.56", "-1234.56"),
    ("trailing-cr", "1234.56CR", "-1234.56", "-1234.56"),
    ("spaced-lowercase-db", "1234.56 db", "-1234.56", "-1234.56"),
    ("spaced-mixed-case-cr", "12.50 Cr", "-12.50", "-12.50"),
    ("surrounded-mixed-case-db", " 12.50 dB ", "-12.50", "-12.50"),
    ("lowercase-cr", "12.50cr", "-12.50", "-12.50"),
    ("trailing-minus-integer", "12-", "-12", "-12"),
    ("minus-before-currency", "-$12.00", "-12.00", None),
    ("spaced-minus-before-currency", "- $5", "-5", None),
    ("currency-trailing-minus", "$5-", "-5", None),
    ("spaced-currency-grouping", "$ 1,000", "1000", None),
    ("grouping-comma", "1,000", "1000", None),
    ("leading-decimal-point", ".5", "0.5", "0.5"),
    ("minus-leading-decimal-point", "-.5", "-0.5", "-0.5"),
    ("trailing-decimal-point", "12.", "12", "12"),
    ("zero-padded-account-id", "00000000004", "4", "4"),
    ("minus-after-currency", "$-12.00", None, None),
    ("currency-after-number", "5$", None, None),
    ("two-currency-signs", "$$1", None, None),
    ("embedded-space", "1 2", None, None),
    ("space-before-decimal-point", "1 .5", None, None),
    ("two-decimal-points", "1.2.3", None, None),
    ("comma-in-fraction", "1.2,3", None, None),
    ("comma-after-decimal-point", "1.,5", None, None),
    ("leading-comma", ",1", None, None),
    ("two-leading-signs", "+-1", None, None),
    ("leading-plus-trailing-minus", "+1-", None, None),
    ("leading-minus-trailing-cr", "-5CR", None, None),
    ("split-cr", "12 C R", None, None),
    ("letters", "AB", None, None),
    ("decimal-point-only", ".", None, None),
    ("sign-only", "-", None, None),
    ("cr-only", "CR", None, None),
    ("empty", "", None, None),
    ("low-values", "\x00\x00", None, None),
]

PAIRS = [NUMVAL_C_PAIR, NUMVAL_PAIR]
PAIR_IDS = ["numval-c", "numval"]

# Text may carry up to 31 digits, the COBOL_CONTEXT precision. These rows are converted under
# a 5-digit, half-up ambient context, so arithmetic that rounds to the context loses digits.
LOW_AMBIENT_PRECISION = 5
NINES_31 = "9" * 31
MINUS_NINES_31 = "-" + NINES_31
# 31 digits whose fraction ends in a keyed trailing zero.
FRACTION_31 = "1" * 29 + ".50"
PRECISION_ROWS = [
    ("31-nines", NINES_31, NINES_31, NINES_31),
    ("31-nines-leading-minus", MINUS_NINES_31, MINUS_NINES_31, MINUS_NINES_31),
    ("31-nines-trailing-cr", NINES_31 + "CR", MINUS_NINES_31, MINUS_NINES_31),
    ("31-nines-after-currency", "$" + NINES_31, NINES_31, None),
    ("31-digits-fraction-trailing-minus", FRACTION_31 + "-", "-" + FRACTION_31, "-" + FRACTION_31),
]

# One digit over the limit, so the text does not conform in either pair.
OVERSIZE_TEXTS = ["9" * 32, "1" * 31 + ".0", "$" + "9" * 32]
OVERSIZE_IDS = ["32-nines", "31-ones-and-one-fraction-digit", "currency-32-nines"]

# Only " " is a space and only 0-9 are digits: control characters, NBSP and non-ASCII digits,
# none of which a 3270 field carries in a number, make the text not conform.
FOREIGN_CHARACTER_TEXTS = [
    "\t12",
    "12\n",
    "1\x002",
    "12\x00",
    "12\u00a0",
    "\uff11\uff12",
    "\u0661\u0662",
]
FOREIGN_CHARACTER_IDS = [
    "leading-tab",
    "trailing-newline",
    "embedded-low-value",
    "trailing-low-value",
    "trailing-nbsp",
    "full-width-digits",
    "arabic-indic-digits",
]

INTRINSIC_FUNCTION_NAMES = ["TestNumvalC", "NumvalC", "TestNumval", "Numval"]
# The float is supplied only to verify that it is refused.
NON_STRING_ARGUMENTS = [None, 12, 12.5, b"12", Decimal(12), ["12"]]
NON_STRING_IDS = ["none", "int", "float", "bytes", "decimal", "list"]


def AssertExactDecimal(result, expectedText):
    """Assert result is a Decimal with the sign, digits and exponent of Decimal(expectedText).

    Value equality alone would let 12.50 pass as 12.5 and lose the fraction digits as keyed.
    """
    assert type(result) is Decimal
    assert result.as_tuple() == Decimal(expectedText).as_tuple()


def CallPair(pairNames, text):
    """Return the TEST- function's verdict on text and the converter's result for it."""
    testName, converterName = pairNames
    conforms = getattr(cobol_intrinsics, testName)(text)
    return conforms, getattr(cobol_intrinsics, converterName)(text)


def AssertPairOutcome(outcome, expectedText):
    """Assert a CallPair outcome against expectedText, where None means text that does not conform.

    Conforming text gives True and exactly Decimal(expectedText); other text gives False and
    exactly Decimal(0).
    """
    conforms, result = outcome
    assert conforms is (expectedText is not None)
    AssertExactDecimal(result, "0" if expectedText is None else expectedText)


def PairCases(rows):
    """Expand (case id, text, NUMVAL-C value, NUMVAL value) rows into one case per pair."""
    numvalCCases = [
        pytest.param(NUMVAL_C_PAIR, text, numvalCText, id=f"numval-c-{caseId}")
        for caseId, text, numvalCText, _ in rows
    ]
    numvalCases = [
        pytest.param(NUMVAL_PAIR, text, numvalText, id=f"numval-{caseId}")
        for caseId, text, _, numvalText in rows
    ]
    return numvalCCases + numvalCases


@pytest.mark.parametrize("text, expectedText", NUMVAL_C_VALID_CASES, ids=NUMVAL_C_VALID_IDS)
def test_numval_c_accepts_currency_text(text, expectedText):
    assert cobol_intrinsics.TestNumvalC(text) is True
    AssertExactDecimal(cobol_intrinsics.NumvalC(text), expectedText)


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
    AssertExactDecimal(cobol_intrinsics.NumvalC(text), "0")


@pytest.mark.parametrize("text", NUMVAL_INVALID_TEXTS, ids=NUMVAL_INVALID_IDS)
def test_numval_rejects_currency_and_grouping(text):
    assert cobol_intrinsics.TestNumval(text) is False
    AssertExactDecimal(cobol_intrinsics.Numval(text), "0")


def test_numval_parses_zero_padded_identifier():
    """COTRN02C (line 204) applies NUMVAL to the keyed 11-digit account ID."""
    assert cobol_intrinsics.TestNumval("00000000004") is True
    AssertExactDecimal(cobol_intrinsics.Numval("00000000004"), "4")


def test_numval_accepts_signed_decimal():
    assert cobol_intrinsics.TestNumval(" -12.5 ") is True
    AssertExactDecimal(cobol_intrinsics.Numval(" -12.5 "), "-12.5")


@pytest.mark.parametrize("pairNames, text, expectedText", PairCases(GRAMMAR_ROWS))
def test_pair_follows_grammar_table(pairNames, text, expectedText):
    AssertPairOutcome(CallPair(pairNames, text), expectedText)


@pytest.mark.parametrize("pairNames, text, expectedText", PairCases(PRECISION_ROWS))
def test_pair_keeps_31_digits_under_low_ambient_precision(pairNames, text, expectedText):
    """The result keeps every digit whatever precision and rounding the caller's context has."""
    with localcontext(prec=LOW_AMBIENT_PRECISION, rounding=ROUND_HALF_UP):
        outcome = CallPair(pairNames, text)
    AssertPairOutcome(outcome, expectedText)


@pytest.mark.parametrize("text", OVERSIZE_TEXTS, ids=OVERSIZE_IDS)
@pytest.mark.parametrize("pairNames", PAIRS, ids=PAIR_IDS)
def test_pair_rejects_more_than_31_digits(pairNames, text):
    AssertPairOutcome(CallPair(pairNames, text), None)


@pytest.mark.parametrize("text", FOREIGN_CHARACTER_TEXTS, ids=FOREIGN_CHARACTER_IDS)
@pytest.mark.parametrize("pairNames", PAIRS, ids=PAIR_IDS)
def test_pair_rejects_control_and_non_ascii_characters(pairNames, text):
    AssertPairOutcome(CallPair(pairNames, text), None)


@pytest.mark.parametrize("argument", NON_STRING_ARGUMENTS, ids=NON_STRING_IDS)
@pytest.mark.parametrize("functionName", INTRINSIC_FUNCTION_NAMES)
def test_intrinsics_refuse_non_string_arguments(functionName, argument):
    with pytest.raises(TypeError, match="must be str"):
        getattr(cobol_intrinsics, functionName)(argument)


@pytest.mark.parametrize("functionName, text", RESULT_TYPE_CALLS, ids=RESULT_TYPE_IDS)
def test_results_are_decimal_never_float(functionName, text):
    result = getattr(cobol_intrinsics, functionName)(text)
    assert isinstance(result, Decimal)
    assert not isinstance(result, float)


# Long space runs around the optional trailing sign: a grammar that backtracks through them
# takes quadratic time, tens of seconds at this length, where a linear one takes milliseconds.
# The limit sits far from both, so scheduler stalls on a busy host do not fail the test.
LONG_SPACE_RUN = " " * 100_000
VALIDATOR_NAMES = ["TestNumvalC", "TestNumval"]
LONG_SPACE_RUN_CASES = [
    ("1" + LONG_SPACE_RUN + "x", False),
    (LONG_SPACE_RUN + "1" + LONG_SPACE_RUN + "x", False),
    ("1" + LONG_SPACE_RUN, True),
]
LONG_SPACE_RUN_IDS = ["run-then-letter", "runs-around-digit-then-letter", "run-after-digit"]
LINEAR_TIME_LIMIT_SECONDS = 2.0


@pytest.mark.parametrize("functionName", VALIDATOR_NAMES)
@pytest.mark.parametrize("text, expected", LONG_SPACE_RUN_CASES, ids=LONG_SPACE_RUN_IDS)
def test_validators_take_linear_time_on_long_space_runs(functionName, text, expected):
    validator = getattr(cobol_intrinsics, functionName)
    startTime = time.perf_counter()
    result = validator(text)
    elapsedSeconds = time.perf_counter() - startTime
    assert result is expected
    assert elapsedSeconds < LINEAR_TIME_LIMIT_SECONDS, f"{functionName} took {elapsedSeconds:.3f} s"
