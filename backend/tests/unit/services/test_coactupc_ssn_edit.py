"""Unit tests for the COACTUPC (CAUP) SSN edit of the COACTUP screen, 1265-EDIT-US-SSN.

cbl/COACTUPC.cbl 1265-EDIT-US-SSN edits part 1 with 1245-EDIT-NUM-REQD (lines 2439-2445).
Its ``IF FLG-EDIT-US-SSN-PART1-ISVALID`` (line 2448) has no END-IF of its own: the END-IF at
line 2463 closes the WS-RETURN-MSG-OFF guard and the one at line 2464 ``IF INVALID-SSN-PART1``,
so the part 2 and part 3 edits (lines 2466-2487) run inside it, up to the period at line 2488.
Expected values are typed from those lines and from 1245-EDIT-NUM-REQD (lines 2109-2178):
- a blank, non-numeric or zero part 1 is the only failing SSN field, whatever parts 2 and 3
    hold;
- a part 1 of 666 or 900-999 passes 1245-EDIT-NUM-REQD, fails INVALID-SSN-PART1 (lines
    121-123), and parts 2 and 3 are still edited;
- a valid part 1 leaves parts 2 and 3 to fail under their own labels;
- three valid parts fail nothing.

Each case walks the whole 1200-EDIT-MAP-INPUTS table (coactupc.RunEditTable) over one screen
whose every other field passes its edit, so the SSN fields are the only failures. The
response's ``field_errors`` are the collector's fields in 3300-SETUP-SCREEN-ATTRS order and
its ``message`` the first WS-RETURN-MSG set. No database is needed.
"""

from datetime import date
from types import MappingProxyType
from typing import NamedTuple

import pytest

from app.services.accounts import coactupc

# The AccountScreenFields attributes of ACTSSN1, ACTSSN2 and ACTSSN3.
SSN_PART_1_FIELD = "ssn_part_1"
SSN_PART_2_FIELD = "ssn_part_2"
SSN_PART_3_FIELD = "ssn_part_3"
ALL_SSN_FIELDS = (SSN_PART_1_FIELD, SSN_PART_2_FIELD, SSN_PART_3_FIELD)

# FUNCTION CURRENT-DATE of EDIT-DATE-OF-BIRTH: after VALID_SCREEN_TEXTS' date of birth.
TODAY = date(2022, 7, 18)

# STRING TRIM(WS-EDIT-VARIABLE-NAME) and the 1245-EDIT-NUM-REQD suffixes (lines 2126, 2146,
# 2163) under the labels of lines 2439 and 2469; the INVALID-SSN-PART1 text of line 2457.
SSN_PART_1_REQUIRED_MESSAGE = "SSN: First 3 chars must be supplied."
SSN_PART_1_NOT_NUMERIC_MESSAGE = "SSN: First 3 chars must be all numeric."
SSN_PART_1_ZERO_MESSAGE = "SSN: First 3 chars must not be zero."
SSN_PART_1_INVALID_MESSAGE = "SSN: First 3 chars: should not be 000, 666, or between 900 and 999"
SSN_PART_2_REQUIRED_MESSAGE = "SSN 4th & 5th chars must be supplied."
NO_MESSAGE = ""
NO_FIELD_ERRORS: tuple[str, ...] = ()

# COACTUP as keyed, with every field 1200-EDIT-MAP-INPUTS edits set to a value that passes:
# NY with ZIP 10001 is a VALID-US-STATE-ZIP-CD2-COMBO, and 212 and 201 are
# VALID-GENERAL-PURP-CODE area codes. A field not listed is blank.
VALID_SCREEN_TEXTS = MappingProxyType(
    {
        "active_status": "Y",
        "open_year": "2010",
        "open_month": "01",
        "open_day": "15",
        "credit_limit": "5000.00",
        "expiration_year": "2025",
        "expiration_month": "06",
        "expiration_day": "30",
        "cash_credit_limit": "1000.00",
        "reissue_year": "2020",
        "reissue_month": "06",
        "reissue_day": "30",
        "curr_balance": "100.00",
        "curr_cycle_credit": "0.00",
        "curr_cycle_debit": "0.00",
        SSN_PART_1_FIELD: "123",
        SSN_PART_2_FIELD: "45",
        SSN_PART_3_FIELD: "6789",
        "dob_year": "1980",
        "dob_month": "05",
        "dob_day": "20",
        "fico_credit_score": "750",
        "first_name": "Jane",
        "last_name": "Doe",
        "addr_line_1": "123 Main Street",
        "addr_state_cd": "NY",
        "addr_zip": "10001",
        "addr_line_3": "New York",
        "addr_country_cd": "USA",
        "phone_num_1_area": "212",
        "phone_num_1_prefix": "555",
        "phone_num_1_line": "0100",
        "phone_num_2_area": "201",
        "phone_num_2_prefix": "555",
        "phone_num_2_line": "0101",
        "eft_account_id": "0123456789",
        "primary_holder_ind": "Y",
    }
)


class SsnCase(NamedTuple):
    """The three SSN parts as keyed, and the field_errors and message the edits leave."""

    ssnParts: tuple[str, str, str]
    expectedFieldErrors: tuple[str, ...]
    expectedMessage: str


SSN_CASES = [
    SsnCase(("", "", "x"), (SSN_PART_1_FIELD,), SSN_PART_1_REQUIRED_MESSAGE),
    SsnCase(("abc", "", "x"), (SSN_PART_1_FIELD,), SSN_PART_1_NOT_NUMERIC_MESSAGE),
    SsnCase(("000", "", "x"), (SSN_PART_1_FIELD,), SSN_PART_1_ZERO_MESSAGE),
    SsnCase(("666", "", ""), ALL_SSN_FIELDS, SSN_PART_1_INVALID_MESSAGE),
    SsnCase(("900", "", ""), ALL_SSN_FIELDS, SSN_PART_1_INVALID_MESSAGE),
    SsnCase(("999", "", ""), ALL_SSN_FIELDS, SSN_PART_1_INVALID_MESSAGE),
    SsnCase(("123", "", "x"), (SSN_PART_2_FIELD, SSN_PART_3_FIELD), SSN_PART_2_REQUIRED_MESSAGE),
    SsnCase(("123", "45", "6789"), NO_FIELD_ERRORS, NO_MESSAGE),
]
SSN_CASE_IDS = [
    "part1-blank",
    "part1-non-numeric",
    "part1-zero",
    "part1-666",
    "part1-900",
    "part1-999",
    "part1-valid-trailing-invalid",
    "all-valid",
]


def ScreenValues(ssnParts: tuple[str, str, str]) -> coactupc.NewAccountData:
    """Return VALID_SCREEN_TEXTS with the SSN parts replaced, as 1100-RECEIVE-MAP leaves it."""
    keyedTexts = dict(VALID_SCREEN_TEXTS)
    keyedTexts.update(zip(ALL_SSN_FIELDS, ssnParts, strict=True))
    texts = {
        fieldName: coactupc.ReceiveField(fieldName, keyedTexts.get(fieldName, ""))
        for fieldName in coactupc.SCREEN_FIELD_LENGTHS
    }
    amounts = {
        fieldName: coactupc.ReceivedAmount(texts[fieldName]) for fieldName in coactupc.AMOUNT_FIELDS
    }
    return coactupc.NewAccountData(texts, amounts)


@pytest.mark.parametrize("ssnCase", SSN_CASES, ids=SSN_CASE_IDS)
def test_ssn_edit_flags_only_the_parts_1265_edits(ssnCase):
    """Parts 2 and 3 are edited only when part 1 passes 1245-EDIT-NUM-REQD (lines 2448-2488)."""
    collector = coactupc.RunEditTable(ScreenValues(ssnCase.ssnParts), TODAY)
    assert collector.SortedFieldErrors() == ssnCase.expectedFieldErrors
    assert collector.message == ssnCase.expectedMessage
    assert collector.inputError is bool(ssnCase.expectedFieldErrors)
