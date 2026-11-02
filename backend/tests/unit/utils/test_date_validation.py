"""Unit tests for app.utils.date_validation.EditDateOfBirth, the port of copybook CSUTLDPY.

Expected values come from cpy/CSUTLDPY.cpy EDIT-DATE-OF-BIRTH (lines 341-372), never from the
unit: MOVE FUNCTION CURRENT-DATE TO WS-CURRENT-DATE-YYYYMMDD keeps only the calendar date, and
the date of birth is valid only when INTEGER-OF-DATE of that date is greater. Otherwise
INPUT-ERROR is set, FLG-DAY-NOT-OK, FLG-MONTH-NOT-OK and FLG-YEAR-NOT-OK are set and the
message ends ':cannot be in the future ' (line 363). today may be a datetime such as
datetime.now(), the online programs' clock, whose time of day never counts.
"""

from datetime import date, datetime

import pytest

from app.utils.date_validation import DatePartStatus, EditDateOfBirth

TODAY = date(2024, 3, 15)
FUTURE_DATE_MESSAGE = ":cannot be in the future "


@pytest.mark.parametrize(
    "dateText, today",
    [
        ("20240315", TODAY),
        ("20240316", TODAY),
        ("20240315", datetime(2024, 3, 15, 10, 30)),
        ("20240316", datetime(2024, 3, 15, 23, 59, 59)),
    ],
    ids=["today", "tomorrow", "today-datetime-after-midnight", "tomorrow-datetime-end-of-day"],
)
def test_birth_date_today_or_later_is_in_the_future(dateText, today):
    result = EditDateOfBirth(dateText, today)
    assert result.yearStatus is DatePartStatus.NOT_OK
    assert result.monthStatus is DatePartStatus.NOT_OK
    assert result.dayStatus is DatePartStatus.NOT_OK
    assert result.inputError is True
    assert result.messageSuffix == FUTURE_DATE_MESSAGE
    assert result.isValid is False


@pytest.mark.parametrize(
    "dateText, today",
    [
        ("20240314", TODAY),
        ("20000101", datetime(2024, 1, 1)),
        ("20240314", datetime(2024, 3, 15, 10, 30)),
    ],
    ids=["yesterday", "year-2000-datetime-midnight", "yesterday-datetime-after-midnight"],
)
def test_birth_date_before_today_is_valid(dateText, today):
    result = EditDateOfBirth(dateText, today)
    assert result.yearStatus is DatePartStatus.VALID
    assert result.monthStatus is DatePartStatus.VALID
    assert result.dayStatus is DatePartStatus.VALID
    assert result.inputError is False
    assert result.messageSuffix == ""
    assert result.isValid is True
