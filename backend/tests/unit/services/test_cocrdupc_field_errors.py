"""Unit tests for the fields COCRDUPC (CCUP) error bodies mark as failing.

cbl/COCRDUPC.cbl 3300-SETUP-SCREEN-ATTRS colours a field red only when its FLG-* flag is NOT-OK
or BLANK, and positions the cursor separately (lines 1205-1260). AAP 0.6.5 carries that as
``field_errors`` (the failing fields in source order) beside ``cursor_field``. Expected values
are typed from those lines and the 88 levels they set:

- NO-CHANGES-DETECTED sets every FLG-* ISVALID (lines 685-692) and moves the cursor to the
    name, in SHOW_DETAILS and in CHANGES_NOT_OK alike: 400 ``No change detected with respect
    to values fetched.`` with no failing field;
- the 9200-WRITE-PROCESSING outcomes COULD-NOT-LOCK-FOR-UPDATE, DATA-WAS-CHANGED-BEFORE-UPDATE
    and LOCKED-BUT-UPDATE-FAILED set no flag NOT-OK (lines 1420-1496): 423, 409 and 503 with
    no failing field, the cursor on the account (WHEN OTHER) or, for the fresh record, on the
    name (FOUND-CARDS-FOR-ACCOUNT);
- a detail edit failure (1230-1260) still lists every failing field, the first message kept
    and the cursor on the first failing field.

No database is used: the edits read nothing, and cocrdupc.WriteProcessing is replaced by an
AsyncMock returning the WriteAttempt of the outcome under test.
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import AsyncMock

import pytest

from app.core.dependencies import OnlineContext
from app.core.errors import LegacyMessageError
from app.schemas.cards import CardScreenFields, CardUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.cards import cocrdupc

if TYPE_CHECKING:
    from app.core.config import Settings

# The keys the screen token carries; the router's path value is a placeholder after fetch.
ACCT_ID = "00000000004"
CARD_NUM = "0000000000007788"
MASKED_CARD_NUM = "************7788"
# CCUP-OLD-* of the fetched card (lines 1354-1367), and the validated CCUP-NEW-* values.
OLD_NAME = "FIELD ERROR HOLDER"
OLD_YEAR = "2027"
OLD_MONTH = "03"
OLD_DAY = "15"
OLD_STATUS = "Y"
OLD_CVV = "123"
NEW_NAME = "Field Error Renamed"
NEW_STATUS = "N"
NEW_MONTH = "11"
NEW_YEAR = "2031"
# The record DATA-WAS-CHANGED-BEFORE-UPDATE finds, changed by another task since fetch.
FRESH_NAME = "CHANGED ELSEWHERE"

# WS-RETURN-MSG and WS-INFO-MSG 88 levels (lines 157-210).
NO_CHANGES_MESSAGE = "No change detected with respect to values fetched."
LOCK_FAILED_MESSAGE = "Could not lock record for update"
RECORD_CHANGED_MESSAGE = "Record changed by some one else. Please review"
UPDATE_FAILED_MESSAGE = "Update of record failed"
CARD_NAME_ALPHA_MESSAGE = "Card name can only contain alphabets and spaces"
EXPIRY_MONTH_MESSAGE = "Card expiry month must be between 1 and 12"
DETAILS_SHOWN_INFO = "Details of selected card shown above"
PROMPT_FOR_CHANGES_INFO = "Update card details presented above."
UPDATE_FAILURE_INFO = "Changes unsuccessful. Please try again"

ACCT_ID_FIELD = "acct_id"
NAME_FIELD = "embossed_name"
STATUS_FIELD = "active_status"
MONTH_FIELD = "expiry_month"
YEAR_FIELD = "expiry_year"
EDIT_FAILED_STATUS = 400
RECORD_CHANGED_STATUS = 409
RECORD_LOCKED_STATUS = 423
WRITE_FAILED_STATUS = 503
# The keys every CCUP error body carries: the four standard ones and the screen's three.
CCUP_ERROR_BODY_KEYS = {
    "message",
    "info_message",
    "field_errors",
    "cursor_field",
    "fields",
    "state",
    "screen_token",
}
ROUTE_CARD_KEY = "placeholder"

BEFORE_IMAGE = cocrdupc.BeforeImage(
    cvv=OLD_CVV, name=OLD_NAME, year=OLD_YEAR, month=OLD_MONTH, day=OLD_DAY, status=OLD_STATUS
)
FRESH_IMAGE = cocrdupc.BeforeImage(
    cvv=OLD_CVV, name=FRESH_NAME, year=OLD_YEAR, month=OLD_MONTH, day=OLD_DAY, status=OLD_STATUS
)
NEW_DATA = cocrdupc.NewCardData(name=NEW_NAME, status=NEW_STATUS, month=NEW_MONTH, year=NEW_YEAR)


def BuildContext(settings: "Settings") -> OnlineContext:
    """Return an online context holding only the settings the screen-token cipher uses."""
    return OnlineContext(request=None, session=SimpleNamespace(), settings=settings)


def ScreenToken(stateCode: str, settings: "Settings") -> str:
    """Return the screen_token of the fetched card in ``stateCode``; N carries CCUP-NEW-*."""
    newData = NEW_DATA if stateCode == cocrdupc.STATE_CONFIRM else None
    state = cocrdupc.CardUpdateState(stateCode, ACCT_ID, CARD_NUM, BEFORE_IMAGE, newData)
    return cocrdupc.EncodeUpdateToken(state, settings)


async def PressKey(context: OnlineContext, updateRequest: CardUpdateRequest) -> LegacyMessageError:
    """Send one key on the COCRDUP screen and return the LegacyMessageError it raises."""
    with pytest.raises(LegacyMessageError) as raised:
        await cocrdupc.ProcessCardUpdate(context, ROUTE_CARD_KEY, updateRequest)
    return raised.value


def DetailFields(name: str, status: str, month: str, year: str) -> CardScreenFields:
    """Return the COCRDUP fields keyed with details shown; the protected keys are not read."""
    return CardScreenFields(
        embossed_name=name, active_status=status, expiry_month=month, expiry_year=year
    )


class NoChangeCase(NamedTuple):
    """A details-shown state and the information line its no-change screen keeps."""

    stateCode: str
    expectedState: UpdateScreenState
    expectedInfo: str


NO_CHANGE_CASES = [
    NoChangeCase(cocrdupc.STATE_SHOW_DETAILS, UpdateScreenState.SHOW_DETAILS, DETAILS_SHOWN_INFO),
    NoChangeCase(
        cocrdupc.STATE_CHANGES_NOT_OK, UpdateScreenState.CHANGES_NOT_OK, PROMPT_FOR_CHANGES_INFO
    ),
]
NO_CHANGE_IDS = ["show-details", "changes-not-ok"]


@pytest.mark.parametrize("noChangeCase", NO_CHANGE_CASES, ids=NO_CHANGE_IDS)
async def test_no_change_marks_no_field_and_keeps_name_cursor(testSettings, noChangeCase):
    """ENTER with the fetched values back is NO-CHANGES-DETECTED: no field fails."""
    updateRequest = CardUpdateRequest(
        action=UpdateAction.ENTER,
        screen_token=ScreenToken(noChangeCase.stateCode, testSettings),
        fields=DetailFields(OLD_NAME.lower(), OLD_STATUS, OLD_MONTH, OLD_YEAR),
    )
    noChangeError = await PressKey(BuildContext(testSettings), updateRequest)
    body = noChangeError.Body()
    assert noChangeError.httpStatus == EDIT_FAILED_STATUS
    assert (body["message"], body["info_message"]) == (
        NO_CHANGES_MESSAGE,
        noChangeCase.expectedInfo,
    )
    assert (body["field_errors"], body["cursor_field"]) == ([], NAME_FIELD)
    assert set(body) == CCUP_ERROR_BODY_KEYS
    assert body["state"] == noChangeCase.expectedState
    assert body["fields"]["card_num"] == MASKED_CARD_NUM


class WriteOutcomeCase(NamedTuple):
    """A 9200-WRITE-PROCESSING outcome and the error screen 2000-DECIDE-ACTION sends for it."""

    attempt: cocrdupc.WriteAttempt
    expectedStatus: int
    expectedMessage: str
    expectedInfo: str
    expectedState: UpdateScreenState
    expectedCursor: str
    expectedName: str


WRITE_OUTCOME_CASES = [
    WriteOutcomeCase(
        cocrdupc.WriteAttempt(outcome=cocrdupc.OUTCOME_LOCK_FAILED),
        RECORD_LOCKED_STATUS,
        LOCK_FAILED_MESSAGE,
        UPDATE_FAILURE_INFO,
        UpdateScreenState.CHANGES_FAILED,
        ACCT_ID_FIELD,
        NEW_NAME,
    ),
    WriteOutcomeCase(
        cocrdupc.WriteAttempt(outcome=cocrdupc.OUTCOME_CHANGED, freshImage=FRESH_IMAGE),
        RECORD_CHANGED_STATUS,
        RECORD_CHANGED_MESSAGE,
        DETAILS_SHOWN_INFO,
        UpdateScreenState.SHOW_DETAILS,
        NAME_FIELD,
        FRESH_NAME,
    ),
    WriteOutcomeCase(
        cocrdupc.WriteAttempt(outcome=cocrdupc.OUTCOME_UPDATE_FAILED),
        WRITE_FAILED_STATUS,
        UPDATE_FAILED_MESSAGE,
        UPDATE_FAILURE_INFO,
        UpdateScreenState.CHANGES_FAILED,
        ACCT_ID_FIELD,
        NEW_NAME,
    ),
]
WRITE_OUTCOME_IDS = ["could-not-lock", "data-was-changed", "locked-but-update-failed"]


@pytest.mark.parametrize("outcomeCase", WRITE_OUTCOME_CASES, ids=WRITE_OUTCOME_IDS)
async def test_write_outcome_marks_no_field(monkeypatch, testSettings, outcomeCase):
    """F5 after validation: a lock, a changed record or a failed REWRITE fails no field."""
    writeProcessing = AsyncMock(return_value=outcomeCase.attempt)
    monkeypatch.setattr(cocrdupc, "WriteProcessing", writeProcessing)
    updateRequest = CardUpdateRequest(
        action=UpdateAction.SAVE, screen_token=ScreenToken(cocrdupc.STATE_CONFIRM, testSettings)
    )
    outcomeError = await PressKey(BuildContext(testSettings), updateRequest)
    body = outcomeError.Body()
    writeProcessing.assert_awaited_once()
    assert outcomeError.httpStatus == outcomeCase.expectedStatus
    assert (body["message"], body["info_message"]) == (
        outcomeCase.expectedMessage,
        outcomeCase.expectedInfo,
    )
    assert (body["field_errors"], body["cursor_field"]) == ([], outcomeCase.expectedCursor)
    assert set(body) == CCUP_ERROR_BODY_KEYS
    assert body["state"] == outcomeCase.expectedState
    assert body["fields"][NAME_FIELD] == outcomeCase.expectedName


class DetailEditCase(NamedTuple):
    """Detail values 1230-1260 refuse, the first literal and every failing field in order."""

    fields: CardScreenFields
    expectedMessage: str
    expectedFieldErrors: list[str]


DETAIL_EDIT_CASES = [
    DetailEditCase(
        DetailFields(OLD_NAME, OLD_STATUS, "13", OLD_YEAR), EXPIRY_MONTH_MESSAGE, [MONTH_FIELD]
    ),
    DetailEditCase(
        DetailFields("N4ME", "X", OLD_MONTH, "1949"),
        CARD_NAME_ALPHA_MESSAGE,
        [NAME_FIELD, STATUS_FIELD, YEAR_FIELD],
    ),
]
DETAIL_EDIT_IDS = ["one-failing-field", "three-failing-fields"]


@pytest.mark.parametrize("detailEditCase", DETAIL_EDIT_CASES, ids=DETAIL_EDIT_IDS)
async def test_detail_edit_failure_marks_every_failing_field(testSettings, detailEditCase):
    """CHANGES_NOT_OK still lists each failing field, the cursor on the first of them."""
    updateRequest = CardUpdateRequest(
        action=UpdateAction.ENTER,
        screen_token=ScreenToken(cocrdupc.STATE_SHOW_DETAILS, testSettings),
        fields=detailEditCase.fields,
    )
    editError = await PressKey(BuildContext(testSettings), updateRequest)
    body = editError.Body()
    assert editError.httpStatus == EDIT_FAILED_STATUS
    assert body["message"] == detailEditCase.expectedMessage
    assert body["field_errors"] == detailEditCase.expectedFieldErrors
    assert body["cursor_field"] == detailEditCase.expectedFieldErrors[0]
    assert body["state"] == UpdateScreenState.CHANGES_NOT_OK
