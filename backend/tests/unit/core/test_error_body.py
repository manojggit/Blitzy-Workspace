"""Unit tests of the ``field_errors`` and ``cursor_field`` keys app.core.errors.ErrorBody builds.

AAP 0.6.5 sets two edit modes. A first-failure program's error names only its cursor field,
which the body lists as the failing field. An all-field-mode screen lists exactly its failing
fields, in source order, and may place the cursor on a field that did not fail, such as the
COCRDUPC (CCUP) name after NO-CHANGES-DETECTED (cbl/COCRDUPC.cbl lines 685-692), so it sets
``LegacyErrorDetails.exactFieldErrors``. The body is built from literals only: no settings or
database are needed.
"""

from typing import NamedTuple

import pytest

from app.core.errors import ErrorBody, LegacyErrorDetails

MESSAGE = "A legacy message literal"
INFO_MESSAGE = "A legacy information line"
ACCT_ID_FIELD = "acct_id"
NAME_FIELD = "embossed_name"
STATUS_FIELD = "active_status"
NO_FIELD = ""


class FieldMarkingCase(NamedTuple):
    """ErrorBody's ``field`` and details, and the failing fields and cursor the body carries."""

    field: str
    details: LegacyErrorDetails | None
    expectedFieldErrors: list[str]
    expectedCursor: str


FIELD_MARKING_CASES = [
    FieldMarkingCase(ACCT_ID_FIELD, None, [ACCT_ID_FIELD], ACCT_ID_FIELD),
    FieldMarkingCase(
        ACCT_ID_FIELD, LegacyErrorDetails(infoMessage=INFO_MESSAGE), [ACCT_ID_FIELD], ACCT_ID_FIELD
    ),
    FieldMarkingCase(
        NO_FIELD,
        LegacyErrorDetails(fieldErrors=(NAME_FIELD, STATUS_FIELD)),
        [NAME_FIELD, STATUS_FIELD],
        NAME_FIELD,
    ),
    FieldMarkingCase(NAME_FIELD, LegacyErrorDetails(exactFieldErrors=True), [], NAME_FIELD),
    FieldMarkingCase(
        STATUS_FIELD,
        LegacyErrorDetails(fieldErrors=(NAME_FIELD, STATUS_FIELD), exactFieldErrors=True),
        [NAME_FIELD, STATUS_FIELD],
        STATUS_FIELD,
    ),
    FieldMarkingCase(NO_FIELD, LegacyErrorDetails(exactFieldErrors=True), [], NO_FIELD),
]
FIELD_MARKING_IDS = [
    "first-failure-cursor-only",
    "first-failure-with-info-line",
    "failing-fields-no-cursor",
    "exact-no-failing-field",
    "exact-failing-fields-kept",
    "exact-no-field-no-cursor",
]


@pytest.mark.parametrize("markingCase", FIELD_MARKING_CASES, ids=FIELD_MARKING_IDS)
def test_error_body_marks_failing_fields_and_cursor(markingCase):
    body = ErrorBody(MESSAGE, markingCase.field, markingCase.details)
    assert body["message"] == MESSAGE
    assert body["field_errors"] == markingCase.expectedFieldErrors
    assert body["cursor_field"] == markingCase.expectedCursor
