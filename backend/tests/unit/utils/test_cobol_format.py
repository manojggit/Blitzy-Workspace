"""Unit tests for app.utils.cobol_format.FormatEdited, the COBOL numeric-edited MOVE.

Expected texts are derived by hand from the IBM Enterprise COBOL numeric-edited MOVE rules for
the receivers the ported programs declare, never from the unit's output:
- cpy/CVTRA07Y.cpy line 30: TRAN-REPORT-AMT PIC -ZZZ,ZZZ,ZZZ.ZZ (15 characters).
- cpy/CVTRA07Y.cpy lines 54, 60, 66: REPT-PAGE-TOTAL, REPT-ACCOUNT-TOTAL and REPT-GRAND-TOTAL
    PIC +ZZZ,ZZZ,ZZZ.ZZ (15 characters).
- cbl/CBSTM03A.CBL line 113: ST-CURR-BAL PIC 9(9).99- (13 characters).
- cbl/CBSTM03A.CBL lines 137, 142: ST-TRANAMT and ST-TOTAL-TRAMT PIC Z(9).99- (13 characters).
- cbl/COTRN02C.cbl line 59: WS-TRAN-AMT-E PIC +99999999.99 (12 characters).

The high-order truncation cases follow the source MOVEs whose sender is wider than the receiver:
- cbl/CBSTM03A.CBL line 484: MOVE ACCT-CURR-BAL (S9(10)V99) TO ST-CURR-BAL.
- cbl/COTRN02C.cbl lines 58, 385, 481: WS-TRAN-AMT-N and TRAN-AMT (S9(9)V99) MOVE TO
    WS-TRAN-AMT-E.

A fixed leading - shows a space for a positive or zero value and - for a negative one, a fixed
+ shows + or -, and a trailing - behaves like the leading one. Z shows a space for a leading
zero, and so does a comma inside the suppressed area; suppression ends at the first non-zero
digit or at the decimal point. An all-Z picture holding zero is all spaces, sign included. The
MOVE drops surplus high-order integer digits.
"""

from decimal import Decimal

import pytest

from app.utils import cobol_format
from app.utils.cobol_format import (
    REPORT_DETAIL_AMOUNT_PICTURE,
    REPORT_TOTAL_AMOUNT_PICTURE,
    STATEMENT_AMOUNT_PICTURE,
    STATEMENT_BALANCE_PICTURE,
    FormatEdited,
    PictureError,
)

EDITED_PICTURE_CASES = (
    (Decimal("1234.56"), "-ZZZ,ZZZ,ZZZ.ZZ", "       1,234.56"),
    (Decimal("-1234.56"), "-ZZZ,ZZZ,ZZZ.ZZ", "-      1,234.56"),
    (Decimal("0.00"), "-ZZZ,ZZZ,ZZZ.ZZ", "               "),
    (Decimal("0.05"), "-ZZZ,ZZZ,ZZZ.ZZ", "            .05"),
    (Decimal("1234.56"), "+ZZZ,ZZZ,ZZZ.ZZ", "+      1,234.56"),
    (Decimal("-1234.56"), "+ZZZ,ZZZ,ZZZ.ZZ", "-      1,234.56"),
    (Decimal("1234.56"), "Z(9).99-", "     1234.56 "),
    (Decimal("-1234.56"), "Z(9).99-", "     1234.56-"),
    (Decimal("0.00"), "Z(9).99-", "         .00 "),
    (Decimal("1234.56"), "9(9).99-", "000001234.56 "),
    (Decimal("50.00"), "+99999999.99", "+00000050.00"),
    (Decimal("-12.50"), "+99999999.99", "-00000012.50"),
)

EDITED_PICTURE_IDS = (
    "report-detail-positive",
    "report-detail-negative",
    "report-detail-zero-all-spaces",
    "report-detail-fraction-only",
    "report-total-positive",
    "report-total-negative",
    "statement-amount-positive",
    "statement-amount-negative",
    "statement-amount-zero",
    "statement-balance-positive",
    "online-amount-positive",
    "online-amount-negative",
)

RECEIVER_BOUNDARY_CASES = (
    (Decimal("999999999.99"), "-ZZZ,ZZZ,ZZZ.ZZ", " 999,999,999.99"),
    (Decimal("-999999999.99"), "-ZZZ,ZZZ,ZZZ.ZZ", "-999,999,999.99"),
    (Decimal("1000000.00"), "-ZZZ,ZZZ,ZZZ.ZZ", "   1,000,000.00"),
    (Decimal("0.009"), "-ZZZ,ZZZ,ZZZ.ZZ", "               "),
    (Decimal("0.00"), "+ZZZ,ZZZ,ZZZ.ZZ", "               "),
    (Decimal("999999999.99"), "+ZZZ,ZZZ,ZZZ.ZZ", "+999,999,999.99"),
    (Decimal("-999999999.99"), "+ZZZ,ZZZ,ZZZ.ZZ", "-999,999,999.99"),
    (Decimal("0.05"), "+ZZZ,ZZZ,ZZZ.ZZ", "+           .05"),
    (Decimal("-0.01"), "+ZZZ,ZZZ,ZZZ.ZZ", "-           .01"),
    (Decimal("-1.00"), "+ZZZ,ZZZ,ZZZ.ZZ", "-          1.00"),
    (Decimal("123456789.12"), "+ZZZ,ZZZ,ZZZ.ZZ", "+123,456,789.12"),
    (Decimal("1000000.00"), "+ZZZ,ZZZ,ZZZ.ZZ", "+  1,000,000.00"),
    (Decimal("999999999.99"), "Z(9).99-", "999999999.99 "),
    (Decimal("-999999999.99"), "Z(9).99-", "999999999.99-"),
    (Decimal("0.05"), "Z(9).99-", "         .05 "),
    (Decimal("-0.05"), "Z(9).99-", "         .05-"),
    (Decimal("-12.50"), "Z(9).99-", "       12.50-"),
    (Decimal("0.00"), "9(9).99-", "000000000.00 "),
    (Decimal("-1234.56"), "9(9).99-", "000001234.56-"),
    (Decimal("999999999.99"), "9(9).99-", "999999999.99 "),
    (Decimal("-999999999.99"), "9(9).99-", "999999999.99-"),
    (Decimal("0.00"), "+99999999.99", "+00000000.00"),
    (Decimal("99999999.99"), "+99999999.99", "+99999999.99"),
    (Decimal("-99999999.99"), "+99999999.99", "-99999999.99"),
    (Decimal("-504.77"), "+99999999.99", "-00000504.77"),
    (Decimal("1234.567"), "+99999999.99", "+00001234.56"),
)

RECEIVER_BOUNDARY_IDS = (
    "report-detail-receiver-maximum",
    "report-detail-receiver-minimum",
    "report-detail-leading-comma-suppressed",
    "report-detail-fraction-truncates-to-zero-all-spaces",
    "report-total-zero-all-spaces",
    "report-total-receiver-maximum",
    "report-total-receiver-minimum",
    "report-total-fraction-only",
    "report-total-negative-fraction-only",
    "report-total-negative-units-commas-suppressed",
    "report-total-every-digit-shown",
    "report-total-leading-comma-suppressed",
    "statement-amount-receiver-maximum",
    "statement-amount-receiver-minimum",
    "statement-amount-fraction-only",
    "statement-amount-negative-fraction-only",
    "statement-amount-negative-partial-suppression",
    "statement-balance-zero-all-digits",
    "statement-balance-negative",
    "statement-balance-receiver-maximum",
    "statement-balance-receiver-minimum",
    "online-amount-zero-plus-sign",
    "online-amount-receiver-maximum",
    "online-amount-receiver-minimum",
    "online-amount-negative-leading-zeros",
    "online-amount-fraction-truncated-not-rounded",
)

HIGH_ORDER_TRUNCATION_CASES = (
    (Decimal("1234567890.12"), "Z(9).99-", "234567890.12 "),
    (Decimal("-1234567890.12"), "Z(9).99-", "234567890.12-"),
    (Decimal("1000000000.00"), "Z(9).99-", "         .00 "),
    (Decimal("1234567890.12"), "-ZZZ,ZZZ,ZZZ.ZZ", " 234,567,890.12"),
    (Decimal("-1234567890.12"), "+ZZZ,ZZZ,ZZZ.ZZ", "-234,567,890.12"),
    (Decimal("1000000194.00"), "9(9).99-", "000000194.00 "),
    (Decimal("9999999999.99"), "9(9).99-", "999999999.99 "),
    (Decimal("-9999999999.99"), "9(9).99-", "999999999.99-"),
    (Decimal("1000000000.00"), "9(9).99-", "000000000.00 "),
    (Decimal("123456789.12"), "+99999999.99", "+23456789.12"),
    (Decimal("-999999999.99"), "+99999999.99", "-99999999.99"),
    (Decimal("100000000.00"), "+99999999.99", "+00000000.00"),
)

HIGH_ORDER_TRUNCATION_IDS = (
    "statement-amount-high-order-dropped",
    "statement-amount-negative-high-order-dropped",
    "statement-amount-high-order-only-leaves-zero",
    "report-detail-high-order-dropped",
    "report-total-negative-high-order-dropped",
    "statement-balance-s9-10-high-order-dropped",
    "statement-balance-s9-10-maximum",
    "statement-balance-s9-10-minimum",
    "statement-balance-s9-10-high-order-only-leaves-zero",
    "online-amount-s9-9-high-order-dropped",
    "online-amount-s9-9-minimum",
    "online-amount-s9-9-high-order-only-leaves-zero",
)

UNSUPPORTED_PICTURES = (
    "X(10)",
    "9(9)V99",
)


@pytest.mark.parametrize(
    "value, picture, expectedText",
    EDITED_PICTURE_CASES + RECEIVER_BOUNDARY_CASES,
    ids=EDITED_PICTURE_IDS + RECEIVER_BOUNDARY_IDS,
)
def test_format_edited_matches_cobol_picture(value, picture, expectedText):
    assert FormatEdited(value, picture) == expectedText


@pytest.mark.parametrize(
    "value, picture, expectedText", HIGH_ORDER_TRUNCATION_CASES, ids=HIGH_ORDER_TRUNCATION_IDS
)
def test_format_edited_truncates_high_order_digits(value, picture, expectedText):
    assert FormatEdited(value, picture) == expectedText


def test_picture_constants_equal_their_texts():
    assert REPORT_DETAIL_AMOUNT_PICTURE == "-ZZZ,ZZZ,ZZZ.ZZ"
    assert REPORT_TOTAL_AMOUNT_PICTURE == "+ZZZ,ZZZ,ZZZ.ZZ"
    assert STATEMENT_AMOUNT_PICTURE == "Z(9).99-"
    assert STATEMENT_BALANCE_PICTURE == "9(9).99-"


@pytest.mark.parametrize("picture", UNSUPPORTED_PICTURES)
def test_format_edited_rejects_unsupported_picture(picture):
    with pytest.raises(PictureError):
        FormatEdited(Decimal("1234.56"), picture)


def test_format_edited_refuses_a_result_of_another_width(monkeypatch):
    realRenderBody = cobol_format._RenderBody

    def WidenedRenderBody(pictureSpec, digits):
        return realRenderBody(pictureSpec, digits) + " "

    monkeypatch.setattr(cobol_format, "_RenderBody", WidenedRenderBody)
    with pytest.raises(PictureError) as refusal:
        FormatEdited(Decimal("1234.56"), REPORT_DETAIL_AMOUNT_PICTURE)
    assert str(refusal.value) == (
        "picture '-ZZZ,ZZZ,ZZZ.ZZ' is 15 characters wide; the edited result is 16"
    )
