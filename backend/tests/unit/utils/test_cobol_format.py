"""Unit tests for app.utils.cobol_format.FormatEdited, the COBOL numeric-edited MOVE.

Expected texts are derived by hand from the IBM Enterprise COBOL numeric-edited MOVE rules for
the receivers the ported programs declare, never from the unit's output:
- cpy/CVTRA07Y.cpy line 30: TRAN-REPORT-AMT PIC -ZZZ,ZZZ,ZZZ.ZZ (15 characters).
- cpy/CVTRA07Y.cpy lines 54, 60, 66: REPT-PAGE-TOTAL, REPT-ACCOUNT-TOTAL and REPT-GRAND-TOTAL
    PIC +ZZZ,ZZZ,ZZZ.ZZ (15 characters).
- cbl/CBSTM03A.CBL line 113: ST-CURR-BAL PIC 9(9).99- (13 characters).
- cbl/CBSTM03A.CBL lines 137, 142: ST-TRANAMT and ST-TOTAL-TRAMT PIC Z(9).99- (13 characters).
- cbl/COTRN02C.cbl line 59: WS-TRAN-AMT-E PIC +99999999.99 (12 characters).

A fixed leading - shows a space for a positive or zero value and - for a negative one, a fixed
+ shows + or -, and a trailing - behaves like the leading one. Z shows a space for a leading
zero, and so does a comma inside the suppressed area; suppression ends at the first non-zero
digit or at the decimal point. An all-Z picture holding zero is all spaces, sign included. The
MOVE drops surplus high-order integer digits.
"""

from decimal import Decimal

import pytest

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

UNSUPPORTED_PICTURES = (
    "X(10)",
    "9(9)V99",
)


@pytest.mark.parametrize(
    "value, picture, expectedText", EDITED_PICTURE_CASES, ids=EDITED_PICTURE_IDS
)
def test_format_edited_matches_cobol_picture(value, picture, expectedText):
    assert FormatEdited(value, picture) == expectedText


def test_format_edited_truncates_high_order_digits():
    assert FormatEdited(Decimal("1234567890.12"), "Z(9).99-") == "234567890.12 "


def test_picture_constants_equal_their_texts():
    assert REPORT_DETAIL_AMOUNT_PICTURE == "-ZZZ,ZZZ,ZZZ.ZZ"
    assert REPORT_TOTAL_AMOUNT_PICTURE == "+ZZZ,ZZZ,ZZZ.ZZ"
    assert STATEMENT_AMOUNT_PICTURE == "Z(9).99-"
    assert STATEMENT_BALANCE_PICTURE == "9(9).99-"


@pytest.mark.parametrize("picture", UNSUPPORTED_PICTURES)
def test_format_edited_rejects_unsupported_picture(picture):
    with pytest.raises(PictureError):
        FormatEdited(Decimal("1234.56"), picture)
