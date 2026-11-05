"""Unit tests for app.utils.lookup_codes, the port of copybook CSLKPCDY used by COACTUPC.

Expected tables, counts and samples come from cpy/CSLKPCDY.cpy, never from the unit:
88 VALID-GENERAL-PURP-CODE (line 521), 88 VALID-US-STATE-CODE (line 1013) and
88 VALID-US-STATE-ZIP-CD2-COMBO (line 1073), the three lists the COACTUPC account-update
edits test. Each list is extracted from the copybook's active lines, skipping fixed-format
comment lines (indicator column 7 holding * or /), and compared whole with its ported table.
"""

import re

import pytest

from app.core.paths import REPO_ROOT
from app.utils.lookup_codes import (
    VALID_NANP_AREA_CODES,
    VALID_US_STATE_CODES,
    VALID_US_STATE_ZIP_PREFIXES,
    IsValidNanpAreaCode,
    IsValidUsStateCode,
    IsValidUsStateZipPrefix,
)

CSLKPCDY_PATH = REPO_ROOT / "cpy" / "CSLKPCDY.cpy"
INDICATOR_AREA = slice(6, 7)
COMMENT_INDICATORS = frozenset({"*", "/"})
VALUE_LITERAL_PATTERN = re.compile(r"'([^']*)'")


def ReadActiveCopybookText():
    """Return CSLKPCDY without its fixed-format comment lines, one source line per text line."""
    copybookLines = CSLKPCDY_PATH.read_text(encoding="ascii").splitlines()
    activeLines = [line for line in copybookLines if line[INDICATOR_AREA] not in COMMENT_INDICATORS]
    return "\n".join(activeLines)


def ExtractConditionValues(conditionName):
    """Return every literal of the named 88-level's VALUES clause, in copybook order."""
    conditionPattern = rf"\b88\s+{re.escape(conditionName)}\s+VALUES\s+([^.]*)\."
    conditionMatch = re.search(conditionPattern, ReadActiveCopybookText())
    assert conditionMatch is not None, f"88 {conditionName} not found in {CSLKPCDY_PATH}"
    return VALUE_LITERAL_PATTERN.findall(conditionMatch.group(1))


@pytest.mark.parametrize(
    "table, conditionName, expectedCount",
    [
        (VALID_NANP_AREA_CODES, "VALID-GENERAL-PURP-CODE", 410),
        (VALID_US_STATE_CODES, "VALID-US-STATE-CODE", 56),
        (VALID_US_STATE_ZIP_PREFIXES, "VALID-US-STATE-ZIP-CD2-COMBO", 240),
    ],
    ids=["VALID-GENERAL-PURP-CODE", "VALID-US-STATE-CODE", "VALID-US-STATE-ZIP-CD2-COMBO"],
)
def test_tables_equal_copybook_value_lists(table, conditionName, expectedCount):
    copybookValues = ExtractConditionValues(conditionName)
    assert len(copybookValues) == expectedCount
    assert isinstance(table, frozenset)
    assert len(table) == expectedCount
    assert table == frozenset(copybookValues)


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
    ["000", "200", "555", "800", "201 ", " 201", "20", "20 ", "", "   "],
    ids=[
        "zero",
        "easy-recognisable-only",
        "555-directory",
        "800-toll-free",
        "trailing-pad",
        "leading-pad",
        "short",
        "short-space-filled",
        "empty",
        "blank-field",
    ],
)
def test_non_general_purpose_area_codes_rejected(areaCode):
    """VALID-PHONE-AREA-CODE (line 30) holds 200, 555 and 800; COACTUPC never tests it.

    VALID-GENERAL-PURP-CODE compares the whole PIC XXX field, so padded, short or blank text
    never matches.
    """
    assert IsValidNanpAreaCode(areaCode) is False


@pytest.mark.parametrize("stateCode", ["DC", "PR", "VI", "AS"])
def test_state_codes_include_territories(stateCode):
    assert IsValidUsStateCode(stateCode) is True


@pytest.mark.parametrize(
    "stateCode",
    ["AA", "ZZ", "ny", "Ny", "NY ", " NY", "N", "N ", "", "  "],
    ids=[
        "military-region",
        "unknown",
        "lower-case",
        "mixed-case",
        "trailing-pad",
        "leading-pad",
        "short",
        "short-space-filled",
        "empty",
        "blank-field",
    ],
)
def test_unknown_state_code_rejected(stateCode):
    """VALID-US-STATE-CODE compares the whole PIC X(2) field: no trimming, no case folding."""
    assert IsValidUsStateCode(stateCode) is False


@pytest.mark.parametrize(
    "stateCode, zipCode, expected",
    [
        ("NY", "10001", True),
        ("CA", "90210", True),
        ("NY", "90210", False),
        pytest.param("NY", "", False, id="zip-empty"),
        pytest.param("NY", "1", False, id="zip-one-character"),
        pytest.param("NY", "  ", False, id="zip-blank"),
        pytest.param("NY", " 10001", False, id="zip-leading-pad"),
        pytest.param("ny", "10001", False, id="state-lower-case"),
        pytest.param(" NY", "10001", False, id="state-leading-pad"),
        pytest.param("N", "10001", False, id="state-short"),
        pytest.param("", "10001", False, id="state-empty"),
        pytest.param("ZZ", "10001", False, id="state-unknown"),
        pytest.param("NYC", "10001", True, id="state-truncated-to-two-characters"),
    ],
)
def test_state_zip_prefix(stateCode, zipCode, expected):
    """COACTUPC STRINGs the PIC X(02) state and ZIP(1:2) DELIMITED BY SIZE into one X(4) key.

    Neither operand is trimmed or case-folded, and a longer state contributes only its first two
    characters.
    """
    assert IsValidUsStateZipPrefix(stateCode, zipCode) is expected


def test_state_zip_prefix_uses_first_two_zip_characters():
    assert "NY10" in VALID_US_STATE_ZIP_PREFIXES
    assert IsValidUsStateZipPrefix("NY", "10") is True
    assert IsValidUsStateZipPrefix("NY", "10999") is True
