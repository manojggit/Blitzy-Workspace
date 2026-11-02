"""Unit tests for app.utils.lookup_codes, the port of copybook CSLKPCDY used by COACTUPC.

Expected counts and samples come from cpy/CSLKPCDY.cpy, never from the unit:
88 VALID-GENERAL-PURP-CODE (line 521), 88 VALID-US-STATE-CODE (line 1013) and
88 VALID-US-STATE-ZIP-CD2-COMBO (line 1073), the three lists the COACTUPC account-update
edits test.
"""

import pytest

from app.utils.lookup_codes import (
    VALID_NANP_AREA_CODES,
    VALID_US_STATE_CODES,
    VALID_US_STATE_ZIP_PREFIXES,
    IsValidNanpAreaCode,
    IsValidUsStateCode,
    IsValidUsStateZipPrefix,
)


@pytest.mark.parametrize(
    "table, expectedCount",
    [
        (VALID_NANP_AREA_CODES, 410),
        (VALID_US_STATE_CODES, 56),
        (VALID_US_STATE_ZIP_PREFIXES, 240),
    ],
    ids=["VALID-GENERAL-PURP-CODE", "VALID-US-STATE-CODE", "VALID-US-STATE-ZIP-CD2-COMBO"],
)
def test_table_sizes_match_copybook(table, expectedCount):
    assert isinstance(table, frozenset)
    assert len(table) == expectedCount


def test_table_entries_have_copybook_widths():
    # The ZIP-prefix check is letters-then-digits rather than membership in VALID_US_STATE_CODES:
    # the copybook's VALID-US-STATE-ZIP-CD2-COMBO also lists AA, AE, AP, FM, MH and PW prefixes
    # that VALID-US-STATE-CODE lacks, and the copybook is the authority for these tables.
    assert all(len(areaCode) == 3 and areaCode.isdigit() for areaCode in VALID_NANP_AREA_CODES)
    assert all(
        len(stateCode) == 2 and stateCode.isalpha() and stateCode.isupper()
        for stateCode in VALID_US_STATE_CODES
    )
    assert all(
        len(zipPrefix) == 4
        and zipPrefix[:2].isalpha()
        and zipPrefix[:2].isupper()
        and zipPrefix[2:].isdigit()
        for zipPrefix in VALID_US_STATE_ZIP_PREFIXES
    )


@pytest.mark.parametrize("zipPrefix", ["AA34", "AE90", "AP96", "FM96", "MH96", "PW96"])
def test_zip_prefixes_keep_copybook_regions_outside_state_codes(zipPrefix):
    """Military and freely associated state prefixes sit only in VALID-US-STATE-ZIP-CD2-COMBO."""
    assert zipPrefix in VALID_US_STATE_ZIP_PREFIXES
    assert IsValidUsStateCode(zipPrefix[:2]) is False


def test_general_purpose_area_code_accepted():
    assert IsValidNanpAreaCode("201") is True


@pytest.mark.parametrize(
    "areaCode",
    ["000", "200", "555", "800"],
    ids=["zero", "easy-recognisable-only", "555-directory", "800-toll-free"],
)
def test_non_general_purpose_area_codes_rejected(areaCode):
    """VALID-PHONE-AREA-CODE (line 30) holds 200, 555 and 800; COACTUPC never tests it."""
    assert IsValidNanpAreaCode(areaCode) is False


@pytest.mark.parametrize("stateCode", ["DC", "PR", "VI", "AS"])
def test_state_codes_include_territories(stateCode):
    assert IsValidUsStateCode(stateCode) is True


def test_unknown_state_code_rejected():
    assert IsValidUsStateCode("AA") is False


@pytest.mark.parametrize(
    "stateCode, zipCode, expected",
    [
        ("NY", "10001", True),
        ("CA", "90210", True),
        ("NY", "90210", False),
    ],
)
def test_state_zip_prefix(stateCode, zipCode, expected):
    assert IsValidUsStateZipPrefix(stateCode, zipCode) is expected


def test_state_zip_prefix_uses_first_two_zip_characters():
    assert "NY10" in VALID_US_STATE_ZIP_PREFIXES
    assert IsValidUsStateZipPrefix("NY", "10") is True
    assert IsValidUsStateZipPrefix("NY", "10999") is True
