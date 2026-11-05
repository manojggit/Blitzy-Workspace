"""Replaces: COCRDUPC (CCUP), BMS COCRDUP, copybooks CVCRD01Y, CVACT02Y, CVCUS01Y, CSMSG02Y

The CCUP card update, served as ``GET /api/v1/cards/{cardNum}/edit?acctId`` (``FetchCardForEdit``,
the entry from the card list) and ``PUT /api/v1/cards/{cardNum}`` (``ProcessCardUpdate``, one
ENTER, F5 or F12). The ``screen_token`` replaces the COMMAREA extension WS-THIS-PROGCOMMAREA:
the CCUP-CHANGE-ACTION state, both keys, the CCUP-OLD-* before-image and, once validated, the
CCUP-NEW-* values. An invalid or expired token, or one issued for another card than the
``{cardNum}`` path names, is DETAILS_NOT_FETCHED. The service reads from the request only the
fields the token's state leaves editable, as a protected 3270 field returns only what was sent
to it:

======================  ====================  =================  ===========  =============
State before            Fields read           ENTER              F5 save      F12 cancel
======================  ====================  =================  ===========  =============
not fetched (LOW-VAL)   account, card number  key edits, read    as ENTER     as ENTER
S show / E not ok       name, status, expiry  detail edits       as ENTER     re-read
N validated             none                  stays N            rewrite      re-read
C done / L, F failed    none                  empty key screen   the same     the same
======================  ====================  =================  ===========  =============

The REWRITE of 9200-WRITE-PROCESSING runs in one write unit with its READ UPDATE and the
9300 before-image compare. Three details of CARD-UPDATE-RECORD are resolved here:

- ``acct_id`` is the screen's account key (CC-ACCT-ID-N), so a card fetched under another
  valid account moves to that account; an account that does not exist fails the REWRITE.
- ``cvv_cd`` keeps the stored value, because CCUP-NEW-CVV-CD is never populated.
- ``expiration_date`` is built from the new year and month and the before-image day, and a
  date that cannot exist fails the REWRITE.
"""

import logging
import string
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import WriteUnit
from app.core.dependencies import OnlineContext
from app.core.errors import (
    HTTP_EDIT_FAILURE,
    HTTP_FILE_ERROR,
    HTTP_NOT_FOUND,
    HTTP_RECORD_CHANGED,
    HTTP_RECORD_LOCKED,
    LegacyErrorDetails,
    LegacyMessageError,
    LogFileCondition,
    RepositoryError,
)
from app.core.security import DecodeScreenToken, EncodeCardRef, EncodeScreenToken, MaskCardNumber
from app.repositories.card_repository import CARD_FILE_NAME, CardRepository
from app.schemas.cards import CardScreen, CardScreenFields, CardUpdateRequest
from app.schemas.common import UpdateAction, UpdateScreenState
from app.services.cards.cocrdslc import (
    ACCT_ID_FIELD,
    CARD_NOT_FOUND_MESSAGE,
    CARD_NUM_FIELD,
    CARD_NUM_LENGTH,
    PROMPT_FOR_KEYS_INFO,
    READ_OPERATION,
    BuildFileErrorMessage,
    DecodeCardKey,
    EditCardKeys,
    ExpiryParts,
    IsAsciiDigits,
    IsBlankKey,
    IsListEntry,
    PadScreenText,
)

if TYPE_CHECKING:
    from app.core.config import Settings

LOGGER = logging.getLogger(__name__)

# LIT-THISTRANID (lines 221-222).
TRANSACTION_ID = "CCUP"
# The CICS commands of 9200-WRITE-PROCESSING, as its logged file conditions name them.
READ_UPDATE_OPERATION = "READ UPDATE"
REWRITE_OPERATION = "REWRITE"

# WS-RETURN-MSG 88 levels set on a reachable path (lines 181-210). The never-set 88 texts, such
# as SEARCHED-ACCT-NOT-NUMERIC and XREF-READ-ERROR, are deliberately absent.
CARD_NAME_NOT_PROVIDED_MESSAGE = "Card name not provided"
CARD_NAME_ALPHA_MESSAGE = "Card name can only contain alphabets and spaces"
CARD_STATUS_MESSAGE = "Card Active Status must be Y or N"
EXPIRY_MONTH_MESSAGE = "Card expiry month must be between 1 and 12"
EXPIRY_YEAR_MESSAGE = "Invalid card expiry year"
NO_CHANGES_MESSAGE = "No change detected with respect to values fetched."
LOCK_FAILED_MESSAGE = "Could not lock record for update"
RECORD_CHANGED_MESSAGE = "Record changed by some one else. Please review"
UPDATE_FAILED_MESSAGE = "Update of record failed"
# WS-INFO-MSG 88 levels (lines 157-171); PROMPT-FOR-SEARCH-KEYS is cocrdslc.PROMPT_FOR_KEYS_INFO.
DETAILS_SHOWN_INFO = "Details of selected card shown above"
PROMPT_FOR_CHANGES_INFO = "Update card details presented above."
PROMPT_FOR_CONFIRMATION_INFO = "Changes validated.Press F5 to save"
UPDATE_SUCCESS_INFO = "Changes committed to database"
UPDATE_FAILURE_INFO = "Changes unsuccessful. Please try again"

# CCUP-CHANGE-ACTION values (lines 276-290); LOW-VALUES and SPACES are both "not fetched".
STATE_NOT_FETCHED = ""
STATE_SHOW_DETAILS = "S"
STATE_CHANGES_NOT_OK = "E"
STATE_CONFIRM = "N"
STATE_DONE = "C"
STATE_LOCK_ERROR = "L"
STATE_UPDATE_FAILED = "F"
SCREEN_STATES = {
    STATE_NOT_FETCHED: UpdateScreenState.DETAILS_NOT_FETCHED,
    STATE_SHOW_DETAILS: UpdateScreenState.SHOW_DETAILS,
    STATE_CHANGES_NOT_OK: UpdateScreenState.CHANGES_NOT_OK,
    STATE_CONFIRM: UpdateScreenState.CHANGES_OK_NOT_CONFIRMED,
    STATE_DONE: UpdateScreenState.CHANGES_DONE,
    STATE_LOCK_ERROR: UpdateScreenState.CHANGES_FAILED,
    STATE_UPDATE_FAILED: UpdateScreenState.CHANGES_FAILED,
}
# The states whose screen shows fetched details, so their token must carry the before-image.
FETCHED_STATES = (STATE_SHOW_DETAILS, STATE_CHANGES_NOT_OK, STATE_CONFIRM)
# 88 CCUP-CHANGES-OKAYED-AND-DONE and CCUP-CHANGES-FAILED: the next request starts afresh.
FINISHED_STATES = (STATE_DONE, STATE_LOCK_ERROR, STATE_UPDATE_FAILED)

# The schemas.cards CardScreenFields attributes of the detail fields CRDNAME, CRDSTCD, EXPMON
# and EXPYEAR, as field_errors and cursor_field name them.
NAME_FIELD = "embossed_name"
STATUS_FIELD = "active_status"
MONTH_FIELD = "expiry_month"
YEAR_FIELD = "expiry_year"
# BMS COCRDUP LENGTH of CRDNAME, CRDSTCD, EXPMON, EXPYEAR and the dark EXPDAY.
NAME_LENGTH = 50
STATUS_LENGTH = 1
MONTH_LENGTH = 2
YEAR_LENGTH = 4
DAY_LENGTH = 2
# 88 VALID-MONTH VALUES 1 THRU 12 and 88 VALID-YEAR VALUES 1950 THRU 2099 (lines 95, 99).
VALID_MONTH_RANGE = range(1, 13)
VALID_YEAR_RANGE = range(1950, 2100)
# 88 FLG-YES-NO-VALID VALUES 'Y', 'N' (line 91).
VALID_STATUS_CODES = ("Y", "N")
# LIT-ALL-ALPHA-FROM (lines 255-257); 1230-EDIT-NAME accepts these letters and spaces only.
ASCII_LETTERS = string.ascii_uppercase + string.ascii_lowercase
NAME_CHARACTERS = frozenset(ASCII_LETTERS + " ")
# INSPECT ... CONVERTING LIT-LOWER TO LIT-UPPER: only the 26 unaccented letters change.
LOWER_TO_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
# 1100-RECEIVE-MAP moves LOW-VALUES to a field keyed as '*' or spaces. A blank field must
# never equal a stored value, which can hold no NUL, so NUL stands in for LOW-VALUES.
LOW_VALUE = "\x00"
# 3300-SETUP-SCREEN-ATTRS shows '*' in a blank detail field that failed its edit.
BLANK_FIELD_MARKER = "*"

# The program name the token carries, so a CAUP (COACTUPC) token is never accepted here.
SCREEN_STATE_PROGRAM = "COCRDUPC"
PAYLOAD_PROGRAM_KEY = "pgm"
PAYLOAD_STATE_KEY = "state"
PAYLOAD_ACCT_KEY = "acct"
PAYLOAD_CARD_KEY = "card"
PAYLOAD_OLD_KEY = "old"
PAYLOAD_NEW_KEY = "new"

STATUS_EDIT_FAILED = HTTP_EDIT_FAILURE
STATUS_NOT_FOUND = HTTP_NOT_FOUND
STATUS_CHANGED = HTTP_RECORD_CHANGED
STATUS_LOCKED = HTTP_RECORD_LOCKED
STATUS_WRITE_FAILED = HTTP_FILE_ERROR
STATUS_FILE_ERROR = HTTP_FILE_ERROR
STATUS_OK = HTTPStatus.OK

# The 2000-DECIDE-ACTION outcomes of 9200-WRITE-PROCESSING (lines 992-1001).
OUTCOME_DONE = "done"
OUTCOME_LOCK_FAILED = "lock-failed"
OUTCOME_CHANGED = "changed"
OUTCOME_UPDATE_FAILED = "update-failed"


@dataclass(frozen=True)
class BeforeImage:
    """CCUP-OLD-DETAILS: the card as 9000-READ-DATA fetched it (lines 1343-1370).

    ``name`` is CARD-EMBOSSED-NAME upper-cased (A-Z only), and ``year``, ``month`` and ``day``
    are CARD-EXPIRAION-DATE (1:4), (6:2) and (9:2). ``cvv`` takes part only in the 9300
    compare; a response carries it only inside the encrypted ``screen_token``, never as a
    plaintext response field.
    """

    cvv: str
    name: str
    year: str
    month: str
    day: str
    status: str


@dataclass(frozen=True)
class NewCardData:
    """CCUP-NEW-CARDDATA once 1230-1260 accept it: the values F5 rewrites.

    ``name`` is as entered, right-trimmed and not upper-cased; ``month`` and ``year`` are two
    and four digits. The day is not carried: the dark, protected EXPDAY field returns the
    before-image day it was sent (lines 621, 1110 and 1123).
    """

    name: str
    status: str
    month: str
    year: str


@dataclass(frozen=True)
class CardUpdateState:
    """WS-THIS-PROGCOMMAREA as the screen token carries it between requests.

    ``acctId`` and ``cardNum`` are the keys that passed 1210 and 1220, or the card-list keys
    read without those edits (lines 482-497); both are empty before fetch. ``beforeImage`` is
    CCUP-OLD-DETAILS once 9000-READ-DATA has run: the card read, or BLANK_BEFORE_IMAGE after
    a failed read that still shows details. ``newData`` is set on entering STATE_CONFIRM with
    the validated values F5 rewrites, and is kept into STATE_DONE, STATE_LOCK_ERROR and
    STATE_UPDATE_FAILED, whose screen shows the values written or attempted (3200
    CCUP-CHANGES-MADE, lines 1113-1123); STATE_SHOW_DETAILS and STATE_CHANGES_NOT_OK carry
    none.

    DecodeUpdateToken requires only the COCRDUPC program tag, a known state code and text
    keys; a state of FETCHED_STATES also needs both keys and ``beforeImage``, and
    STATE_CONFIRM ``newData`` too (IsCompleteState). Any other state is accepted with
    whatever well-formed records it carries.
    """

    stateCode: str
    acctId: str
    cardNum: str
    beforeImage: BeforeImage | None = None
    newData: NewCardData | None = None


NOT_FETCHED_STATE = CardUpdateState(STATE_NOT_FETCHED, "", "")
# 9000-READ-DATA opens with INITIALIZE CCUP-OLD-DETAILS (line 1345): every field is PIC X, the
# CVV included, so a read that finds no card leaves each one spaces, held here right-trimmed.
BLANK_BEFORE_IMAGE = BeforeImage(cvv="", name="", year="", month="", day="", status="")


def EncodeUpdateToken(state: CardUpdateState, settings: "Settings") -> str:
    """Return the screen_token of ``state``, the COMMON-RETURN move of WS-THIS-PROGCOMMAREA.

    The payload is Fernet-encrypted by core.security, so the PAN and CVV inside it are never
    visible to the client.
    """
    payload = {
        PAYLOAD_PROGRAM_KEY: SCREEN_STATE_PROGRAM,
        PAYLOAD_STATE_KEY: state.stateCode,
        PAYLOAD_ACCT_KEY: state.acctId,
        PAYLOAD_CARD_KEY: state.cardNum,
        PAYLOAD_OLD_KEY: asdict(state.beforeImage) if state.beforeImage is not None else None,
        PAYLOAD_NEW_KEY: asdict(state.newData) if state.newData is not None else None,
    }
    return EncodeScreenToken(payload, settings)


def DecodeUpdateToken(token: str, settings: "Settings") -> CardUpdateState | None:
    """Return the screen state a screen_token carries, or None to restart the flow.

    None, which the caller treats as DETAILS_NOT_FETCHED, answers a missing or blank token, a
    forged or expired one, another program's token and any payload that is incomplete.
    """
    if not token.strip():
        return None
    try:
        payload = DecodeScreenToken(token, settings)
    except InvalidToken:
        return None
    return StateFromPayload(payload)


def StateFromPayload(payload: Mapping[str, Any]) -> CardUpdateState | None:
    """Return the CardUpdateState a decrypted payload describes, or None when it is not one."""
    stateCode = payload.get(PAYLOAD_STATE_KEY)
    keys = (payload.get(PAYLOAD_ACCT_KEY), payload.get(PAYLOAD_CARD_KEY))
    if payload.get(PAYLOAD_PROGRAM_KEY) != SCREEN_STATE_PROGRAM or stateCode not in SCREEN_STATES:
        return None
    if not all(isinstance(key, str) for key in keys):
        return None
    state = CardUpdateState(
        stateCode=stateCode,
        acctId=keys[0],
        cardNum=keys[1],
        beforeImage=RecordFromPayload(BeforeImage, payload.get(PAYLOAD_OLD_KEY)),
        newData=RecordFromPayload(NewCardData, payload.get(PAYLOAD_NEW_KEY)),
    )
    return state if IsCompleteState(state) else None


def RecordFromPayload[RecordType](
    recordClass: type[RecordType], value: object
) -> RecordType | None:
    """Return ``recordClass`` built from a payload object holding exactly its text fields."""
    if not isinstance(value, dict):
        return None
    fieldNames = {recordField.name for recordField in fields(recordClass)}
    if set(value) != fieldNames or not all(isinstance(text, str) for text in value.values()):
        return None
    return recordClass(**value)


def IsCompleteState(state: CardUpdateState) -> bool:
    """Return True when a decoded state carries what its screen needs.

    A fetched state needs both keys and the before-image; STATE_CONFIRM also needs the
    validated values its F5 rewrites.
    """
    if state.stateCode not in FETCHED_STATES:
        return True
    hasDetails = bool(state.acctId and state.cardNum) and state.beforeImage is not None
    if state.stateCode == STATE_CONFIRM:
        return hasDetails and state.newData is not None
    return hasDetails


@dataclass(frozen=True)
class ScreenReply:
    """Everything 3000-SEND-MAP puts on the COCRDUP screen for one response.

    ``values`` holds the detail fields shown, keyed by CardScreenFields attribute. The keys
    shown are ``shownKeys`` (account text, card text) when set, else the state's keys; the
    card number is always masked.
    """

    state: CardUpdateState
    info: str
    values: Mapping[str, str] = field(default_factory=dict)
    message: str = ""
    fieldErrors: tuple[str, ...] = ()
    cursorField: str = ACCT_ID_FIELD
    shownKeys: tuple[str, str] | None = None


type ScreenHandler = Callable[
    [OnlineContext, CardUpdateState, CardScreenFields], Awaitable[CardScreen]
]
# The screen a failed 9100 read answers with, from the keys read, the message and the flagged
# keys: ReadFailureReply where the state stays not fetched, ReadFailureDetailsReply where the
# caller sets CCUP-SHOW-DETAILS whatever the read found.
type ReadFailureBuilder = Callable[[tuple[str, str], str, tuple[str, ...]], ScreenReply]


async def FetchCardForEdit(
    context: OnlineContext, cardKey: str, acctId: str, *, isCardRef: bool = False
) -> CardScreen:
    """Port of the CCUP entry: fetch the card for update, from the card list or typed keys.

    ``cardKey`` is the ``{cardNum}`` path value, a keyed card number or a ``card_ref`` token
    (any length when ``isCardRef`` declares it one, as cocrdslc.DecodeCardKey reads it), and
    ``acctId`` the account. A ``card_ref`` sent with its list row's account is the
    card-list entry (lines 482-497): both keys are taken as the list sent them and the card is
    read without the 1210 and 1220 edits, so a card keyed all zeros opens. Any other keys,
    typed ones included, run the 1210 and 1220 edits and the card is read as the not-fetched
    ENTER reads it.

    Raises:
        LegacyMessageError: 404 ``Did not find cards for this search condition`` for an
            invalid or expired ``card_ref``, one carrying no card key, or a card that does not
            exist; 400 with the key edit literal, never for the card-list entry; 503 with the
            CARDDAT READ file-error text. The card-list entry sets CCUP-SHOW-DETAILS whatever
            its read found, so its 404 and 503 carry the SHOW_DETAILS screen with both keys
            kept and blank details (ReadFailureDetailsReply). Every other body carries the
            not-fetched key screen; for a ``card_ref`` carrying no card key it shows the
            account as sent and a blank card number, so the token is never echoed.
    """
    cardText = DecodeCardKey(cardKey, context.settings, isCardRef=isCardRef)
    if cardText is None:
        reply = ReadFailureReply(
            (acctId, ""), CARD_NOT_FOUND_MESSAGE, (ACCT_ID_FIELD, CARD_NUM_FIELD)
        )
        raise ScreenError(reply, STATUS_NOT_FOUND, context.settings)
    if IsListEntry(cardKey, acctId, isCardRef=isCardRef):
        return await ShowFetchedCard(context, acctId, cardText, ReadFailureDetailsReply)
    return await FetchWithKeys(context, acctId, cardText)


async def ProcessCardUpdate(
    context: OnlineContext, cardKey: str, request: CardUpdateRequest
) -> CardScreen:
    """Port of 0000-MAIN (lines 366-543): one ENTER, F5 or F12 on the COCRDUP screen.

    ``cardKey`` is the ``{cardNum}`` path value, a keyed card number or a ``card_ref`` token.
    Before fetch the keys come from the request fields and the path is not read; afterwards
    they come from the screen token, which BindStateToPath accepts only for the card the
    path names.

    Raises:
        LegacyMessageError: 400 for an edit failure or no change, 404 for a card not found
            or, once details are fetched, an invalid or expired ``card_ref`` path, 409 for a
            record changed since fetch, 423 for a record lock, and 503 for a failed rewrite or
            a CARDDAT read error. Each body carries the screen ``fields``, ``state`` and
            ``screen_token``; a not-fetched ENTER whose read fails stays DETAILS_NOT_FETCHED,
            and an F12 whose re-read fails gives SHOW_DETAILS with the keys kept.
    """
    tokenState = DecodeUpdateToken(request.screen_token, context.settings) or NOT_FETCHED_STATE
    state = BindStateToPath(tokenState, cardKey, context.settings)
    action = NormaliseAction(request.action, state.stateCode)
    handler = SelectHandler(state.stateCode, action)
    return await handler(context, state, request.fields)


def BindStateToPath(state: CardUpdateState, cardKey: str, settings: "Settings") -> CardUpdateState:
    """Return ``state`` when the ``{cardNum}`` path names its card, NOT_FETCHED_STATE for another.

    A token issued for another card than the path names is treated as invalid, so a token
    replayed on another card's path restarts the flow and never reads or rewrites its card.
    Only FETCHED_STATES are bound, as only they read or rewrite the token's card; for the
    other states the path is not read, and done and failed states reset whatever the path.
    The path names only the card: the PUT route carries no account key. It is decoded as
    cocrdslc.DecodeCardKey decodes it, a keyed card number or a ``card_ref`` token, and both
    card texts are compared space-padded to CARDSID's 16 characters.

    Raises:
        LegacyMessageError: 404 ``Did not find cards for this search condition`` when the path
            of a fetched state is an invalid or expired ``card_ref``, or one carrying no card
            key, as FetchCardForEdit answers it. Nothing is read; the body carries the
            not-fetched key screen with blank keys, so neither the token's card nor the path
            token is echoed.
    """
    if state.stateCode not in FETCHED_STATES:
        return state
    cardText = DecodeCardKey(cardKey, settings)
    if cardText is None:
        reply = ReadFailureReply(("", ""), CARD_NOT_FOUND_MESSAGE, (ACCT_ID_FIELD, CARD_NUM_FIELD))
        raise ScreenError(reply, STATUS_NOT_FOUND, settings)
    pathCard = PadScreenText(cardText, CARD_NUM_LENGTH)
    if pathCard != PadScreenText(state.cardNum, CARD_NUM_LENGTH):
        return NOT_FETCHED_STATE
    return state


def NormaliseAction(action: UpdateAction, stateCode: str) -> UpdateAction:
    """Port of the AID check (lines 413-424): a key the state does not allow becomes ENTER.

    F5 is valid only in CCUP-CHANGES-OK-NOT-CONFIRMED and F12 only once details are fetched.
    """
    if action == UpdateAction.SAVE and stateCode == STATE_CONFIRM:
        return action
    if action == UpdateAction.CANCEL and stateCode != STATE_NOT_FETCHED:
        return action
    return UpdateAction.ENTER


def SelectHandler(stateCode: str, action: UpdateAction) -> ScreenHandler:
    """Port of the 0000-MAIN and 2000-DECIDE-ACTION EVALUATEs as one dispatch on state and key.

    Done or failed changes reset whatever the key (lines 517-528); before fetch every key is
    ENTER; F12 re-reads; in STATE_CONFIRM ENTER keeps the confirmation and F5 rewrites;
    otherwise ENTER edits the details.
    """
    if stateCode in FINISHED_STATES:
        return ResetScreen
    if stateCode == STATE_NOT_FETCHED:
        return FetchFromKeys
    if action == UpdateAction.CANCEL:
        return RereadCard
    if stateCode == STATE_CONFIRM:
        return SaveChanges if action == UpdateAction.SAVE else KeepConfirmation
    return EditDetails


def BuildScreen(reply: ScreenReply, settings: "Settings") -> CardScreen:
    """Port of 3000-SEND-MAP: 3200 screen values, 3250 information line, 3300 cursor.

    The card number is masked, the CVV and the day are never shown, and ``card_ref`` is set
    once the state holds a card key.
    """
    state = reply.state
    return CardScreen(
        fields=ScreenFieldsOf(reply),
        state=SCREEN_STATES[state.stateCode],
        screen_token=EncodeUpdateToken(state, settings),
        card_ref=EncodeCardRef(state.cardNum, settings) if state.cardNum else "",
        message=reply.message,
        info_message=reply.info,
        field_errors=list(reply.fieldErrors),
        cursor_field=reply.cursorField,
    )


def ScreenFieldsOf(reply: ScreenReply) -> CardScreenFields:
    """Return the COCRDUP fields of a reply: the keys, card masked, and the detail values."""
    acctText, cardText = reply.shownKeys or (reply.state.acctId, reply.state.cardNum)
    return CardScreenFields(acct_id=acctText, card_num=MaskCardNumber(cardText), **reply.values)


def ScreenError(reply: ScreenReply, httpStatus: int, settings: "Settings") -> LegacyMessageError:
    """Return the error carrying a reply's message and its whole screen.

    The screen ``fields``, ``state`` and ``screen_token`` ride beside the four standard keys,
    so the page keeps the state machine running on a 4xx or 5xx response. ``field_errors`` is
    exactly the reply's failing fields and ``cursor_field`` its cursor, so the outcomes that
    fail no field list none: NO-CHANGES-DETECTED sets every FLG-* ISVALID (lines 685-692) and
    the 9200 lock, record-changed and update-failed outcomes set no flag NOT-OK, so 3300
    colours no field while it still positions the cursor.
    """
    screenBody = BuildScreen(reply, settings).model_dump(
        mode="json", include={"fields", "state", "screen_token"}
    )
    return LegacyMessageError(
        reply.message,
        field=reply.cursorField,
        httpStatus=httpStatus,
        details=LegacyErrorDetails(
            infoMessage=reply.info,
            fieldErrors=reply.fieldErrors,
            extraBody=screenBody,
            exactFieldErrors=True,
        ),
    )


async def ResetScreen(
    context: OnlineContext, state: CardUpdateState, screenFields: CardScreenFields
) -> CardScreen:
    """Port of WHEN CCUP-CHANGES-OKAYED-AND-DONE / CCUP-CHANGES-FAILED (lines 517-528).

    The input is discarded and the empty key screen is sent with the key prompt, for every key
    and whatever the caller. The source returns a session that came from the card list to
    COCRDLIC instead (lines 436-439); here F3 returns to the main menu.
    """
    return BuildScreen(ScreenReply(NOT_FETCHED_STATE, PROMPT_FOR_KEYS_INFO), context.settings)


async def KeepConfirmation(
    context: OnlineContext, state: CardUpdateState, screenFields: CardScreenFields
) -> CardScreen:
    """Port of WHEN CCUP-CHANGES-OK-NOT-CONFIRMED with ENTER (lines 685-692, 1006-1007).

    No edit runs and the validated values stay on the protected screen.
    """
    reply = ScreenReply(state, PROMPT_FOR_CONFIRMATION_INFO, NewDataValues(state.newData))
    return BuildScreen(reply, context.settings)


def ImageValues(beforeImage: BeforeImage) -> dict[str, str]:
    """The detail fields 3200-SETUP-SCREEN-VARS shows from CCUP-OLD-* (lines 1107-1112)."""
    return {
        NAME_FIELD: beforeImage.name,
        STATUS_FIELD: beforeImage.status,
        MONTH_FIELD: beforeImage.month,
        YEAR_FIELD: beforeImage.year,
    }


def NewDataValues(newData: NewCardData | None) -> dict[str, str]:
    """The detail fields 3200-SETUP-SCREEN-VARS shows from CCUP-NEW-* (lines 1113-1117)."""
    if newData is None:
        return {}
    return {
        NAME_FIELD: newData.name,
        STATUS_FIELD: newData.status,
        MONTH_FIELD: newData.month,
        YEAR_FIELD: newData.year,
    }


async def FetchFromKeys(
    context: OnlineContext, state: CardUpdateState, screenFields: CardScreenFields
) -> CardScreen:
    """Port of the not-fetched ENTER: 1210/1220 key edits, then 9000-READ-DATA (lines 645-661).

    Only the account and card number are read from the request: line 653 moves LOW-VALUES
    to CCUP-NEW-CARDDATA, so detail values keyed before fetch are ignored.
    """
    return await FetchWithKeys(context, screenFields.acct_id, screenFields.card_num)


async def FetchWithKeys(context: OnlineContext, acctText: str, cardText: str) -> CardScreen:
    """Edit both keys as COCRDSLC does (identical 1210/1220), then read and show the card."""
    keys = EditCardKeys(acctText, cardText)
    if not keys.IsValid():
        reply = ScreenReply(
            NOT_FETCHED_STATE,
            PROMPT_FOR_KEYS_INFO,
            message=keys.message,
            fieldErrors=keys.fieldErrors,
            cursorField=keys.cursorField,
            shownKeys=(acctText, cardText),
        )
        raise ScreenError(reply, STATUS_EDIT_FAILED, context.settings)
    return await ShowFetchedCard(context, keys.acctId, keys.cardNum, ReadFailureReply)


async def ShowFetchedCard(
    context: OnlineContext, acctId: str, cardNum: str, failureReply: ReadFailureBuilder
) -> CardScreen:
    """Port of SET CCUP-SHOW-DETAILS after a successful 9000-READ-DATA.

    The before-image is shown with the upper-cased name, the information line is
    FOUND-CARDS-FOR-ACCOUNT and the cursor is the name (3300, lines 1212-1214). A failed
    read raises the screen ``failureReply`` builds for the caller.
    """
    beforeImage = await ReadBeforeImage(context, acctId, cardNum, failureReply)
    state = CardUpdateState(STATE_SHOW_DETAILS, acctId, cardNum, beforeImage)
    reply = ScreenReply(state, DETAILS_SHOWN_INFO, ImageValues(beforeImage), cursorField=NAME_FIELD)
    return BuildScreen(reply, context.settings)


async def RereadCard(
    context: OnlineContext, state: CardUpdateState, screenFields: CardScreenFields
) -> CardScreen:
    """Port of WHEN CCARD-AID-PFK12 (lines 958-966): re-read the card, discarding the edits.

    The keys come from the token and no edit runs. The screen shows details whatever the
    read found: a list session's F12 sets CCUP-SHOW-DETAILS unconditionally (lines 484-494),
    and a menu session's does so IF FOUND-CARDS-FOR-ACCOUNT, which 1200-EDIT-MAP-INPUTS set
    with the details fetched (line 668) and a failed 9100 leaves set. So a failed re-read
    keeps both keys with a blank before-image (ReadFailureDetailsReply).
    """
    return await ShowFetchedCard(context, state.acctId, state.cardNum, ReadFailureDetailsReply)


async def ReadBeforeImage(
    context: OnlineContext, acctId: str, cardNum: str, failureReply: ReadFailureBuilder
) -> BeforeImage:
    """Port of 9000-READ-DATA and 9100-GETCARD-BYACCTCARD (lines 1343-1417).

    CARDDAT is read by card number only; the account key is never compared with the card's.

    Raises:
        LegacyMessageError: 404 ``Did not find cards for this search condition`` (NOTFND),
            or 503 with the READ file-error text (WHEN OTHER), each on the screen
            ``failureReply`` builds from the keys read.
    """
    try:
        card = await CardRepository(context).GetCard(cardNum)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, READ_OPERATION, error)
        message = BuildFileErrorMessage(READ_OPERATION, CARD_FILE_NAME, error.resp, error.resp2)
        reply = failureReply((acctId, cardNum), message, (ACCT_ID_FIELD,))
        raise ScreenError(reply, STATUS_FILE_ERROR, context.settings) from error
    if card is None:
        reply = failureReply(
            (acctId, cardNum), CARD_NOT_FOUND_MESSAGE, (ACCT_ID_FIELD, CARD_NUM_FIELD)
        )
        raise ScreenError(reply, STATUS_NOT_FOUND, context.settings)
    return BeforeImageOf(card)


def ReadFailureReply(
    shownKeys: tuple[str, str], message: str, fieldErrors: tuple[str, ...]
) -> ScreenReply:
    """The key screen of a failed 9100 read: the keys shown, the cursor on the account.

    NOTFND sets both filter flags NOT-OK and WHEN OTHER the account flag (lines 1395-1406).
    For the not-fetched ENTER, 2000-DECIDE-ACTION sets CCUP-SHOW-DETAILS only IF
    FOUND-CARDS-FOR-ACCOUNT (lines 954-965), which a failed read never sets there, so the
    state stays DETAILS_NOT_FETCHED and the information line is the key prompt.
    """
    return ScreenReply(
        NOT_FETCHED_STATE,
        PROMPT_FOR_KEYS_INFO,
        message=message,
        fieldErrors=fieldErrors,
        cursorField=ACCT_ID_FIELD,
        shownKeys=shownKeys,
    )


def ReadFailureDetailsReply(
    heldKeys: tuple[str, str], message: str, fieldErrors: tuple[str, ...]
) -> ScreenReply:
    """The details screen of a failed 9100 read on the card-list entry or the F12 re-read.

    Both SET CCUP-SHOW-DETAILS after 9000-READ-DATA whatever 9100 found (lines 482-497,
    958-966), so the keys stay held with the blank before-image INITIALIZE left, and 3200
    shows it. 3250 sets FOUND-CARDS-FOR-ACCOUNT and 3300 puts the cursor on the name (lines
    1145-1146, 1212-1214); NOTFND flags both keys and WHEN OTHER the account (lines 1395-1406).
    """
    acctId, cardNum = heldKeys
    state = CardUpdateState(STATE_SHOW_DETAILS, acctId, cardNum, BLANK_BEFORE_IMAGE)
    return ScreenReply(
        state,
        DETAILS_SHOWN_INFO,
        ImageValues(BLANK_BEFORE_IMAGE),
        message=message,
        fieldErrors=fieldErrors,
        cursorField=NAME_FIELD,
    )


def BeforeImageOf(card: Mapping[str, Any]) -> BeforeImage:
    """Return CCUP-OLD-DETAILS of a CARDDAT record (lines 1354-1367, 1499-1517)."""
    year, month, day = ExpiryParts(card["expiration_date"])
    return BeforeImage(
        cvv=card["cvv_cd"],
        name=UpperAscii(card["embossed_name"]),
        year=year,
        month=month,
        day=day,
        status=card["active_status"],
    )


def UpperAscii(text: str) -> str:
    """Port of ``INSPECT ... CONVERTING LIT-LOWER TO LIT-UPPER``: a-z become A-Z, nothing else."""
    return text.translate(LOWER_TO_UPPER)


@dataclass(frozen=True)
class FieldEditResult:
    """The outcome of one detail edit (1230-1260).

    ``blank`` is the FLG-*-BLANK flag: the field was not supplied, so 3300 shows '*' in it.
    ``message`` is "" when the field passed.
    """

    failed: bool
    blank: bool = False
    message: str = ""


@dataclass(frozen=True)
class DetailEditOutcome:
    """The outcome of the four detail edits in all-field mode.

    ``message`` is the first failing edit's literal, as each edit sets one only while
    WS-RETURN-MSG-OFF holds; ``fieldErrors`` lists every failing field in source order.
    """

    message: str
    fieldErrors: tuple[str, ...]
    blankFields: frozenset[str]


FIELD_PASSED = FieldEditResult(failed=False)


async def EditDetails(
    context: OnlineContext, state: CardUpdateState, screenFields: CardScreenFields
) -> CardScreen:
    """Port of 1200-EDIT-MAP-INPUTS with details shown (lines 667-715); never writes.

    The no-change test runs first and short-circuits every field edit, invalid values
    included. Otherwise all four edits run; any failure gives CHANGES_NOT_OK, and a clean
    pass gives CHANGES_OK_NOT_CONFIRMED with the validated values kept in the token.
    """
    received = ReceiveDetails(screenFields)
    if IsUnchanged(received, state.beforeImage):
        raise ScreenError(NoChangeReply(state, received), STATUS_EDIT_FAILED, context.settings)
    edits = RunDetailEdits(received)
    if edits.fieldErrors:
        reply = EditFailureReply(state, received, edits)
        raise ScreenError(reply, STATUS_EDIT_FAILED, context.settings)
    return BuildScreen(ConfirmationReply(state, received), context.settings)


def ReceiveDetails(screenFields: CardScreenFields) -> dict[str, str]:
    """Port of 1100-RECEIVE-MAP for CRDNAME, CRDSTCD, EXPMON and EXPYEAR (lines 607-635).

    Name and status are padded right with spaces, month and year right-justified
    (JUSTIFY=(RIGHT)). A field keyed as '*' or all spaces becomes LOW-VALUES.
    """
    return {
        NAME_FIELD: ReceiveField(screenFields.embossed_name, NAME_LENGTH, PadScreenText),
        STATUS_FIELD: ReceiveField(screenFields.active_status, STATUS_LENGTH, PadScreenText),
        MONTH_FIELD: ReceiveField(screenFields.expiry_month, MONTH_LENGTH, str.rjust),
        YEAR_FIELD: ReceiveField(screenFields.expiry_year, YEAR_LENGTH, str.rjust),
    }


def ReceiveField(text: str, length: int, justify: Callable[[str, int], str]) -> str:
    """Return one received field: LOW-VALUES for '*' or spaces, else the justified text."""
    if IsBlankKey(text):
        return LOW_VALUE * length
    return justify(text, length)


def IsUnchanged(received: Mapping[str, str], beforeImage: BeforeImage | None) -> bool:
    """Port of lines 680-683: UPPER-CASE(CCUP-NEW-CARDDATA) = UPPER-CASE(CCUP-OLD-CARDDATA).

    The images are name, year, month, day and status at their PIC widths, with the new day
    taken from the before-image. A blank (LOW-VALUES) new field never equals a stored value.
    """
    if beforeImage is None:
        return False
    newImage = (
        received[NAME_FIELD]
        + received[YEAR_FIELD]
        + received[MONTH_FIELD]
        + beforeImage.day.ljust(DAY_LENGTH)
        + received[STATUS_FIELD]
    )
    return UpperAscii(newImage) == UpperAscii(OldCardData(beforeImage))


def OldCardData(beforeImage: BeforeImage) -> str:
    """Return CCUP-OLD-CARDDATA: CRDNAME X(50), EXPYEAR X(4), EXPMON X(2), EXPDAY X(2), CRDSTCD."""
    return (
        beforeImage.name.ljust(NAME_LENGTH)
        + beforeImage.year.ljust(YEAR_LENGTH)
        + beforeImage.month.ljust(MONTH_LENGTH)
        + beforeImage.day.ljust(DAY_LENGTH)
        + beforeImage.status.ljust(STATUS_LENGTH)
    )


def IsNotSupplied(screenValue: str, length: int) -> bool:
    """The ``EQUAL LOW-VALUES OR EQUAL SPACES OR EQUAL ZEROS`` test that opens each edit."""
    return screenValue in (LOW_VALUE * length, " " * length, "0" * length)


def EditCardName(screenValue: str) -> FieldEditResult:
    """Port of 1230-EDIT-NAME (lines 806-840): supplied, and only letters and spaces.

    The source converts every letter to a space and accepts a name whose TRIM is empty.
    """
    if IsNotSupplied(screenValue, NAME_LENGTH):
        return FieldEditResult(failed=True, blank=True, message=CARD_NAME_NOT_PROVIDED_MESSAGE)
    if not NAME_CHARACTERS.issuperset(screenValue):
        return FieldEditResult(failed=True, message=CARD_NAME_ALPHA_MESSAGE)
    return FIELD_PASSED


def EditCardStatus(screenValue: str) -> FieldEditResult:
    """Port of 1240-EDIT-CARDSTATUS (lines 845-873): supplied, and upper-case Y or N."""
    if IsNotSupplied(screenValue, STATUS_LENGTH):
        return FieldEditResult(failed=True, blank=True, message=CARD_STATUS_MESSAGE)
    if screenValue not in VALID_STATUS_CODES:
        return FieldEditResult(failed=True, message=CARD_STATUS_MESSAGE)
    return FIELD_PASSED


def EditExpiryMonth(screenValue: str) -> FieldEditResult:
    """Port of 1250-EDIT-EXPIRY-MON (lines 877-908): supplied, and 01 to 12.

    Only two ASCII digits pass 88 VALID-MONTH. Its PIC 9(2) redefinition gives no defined
    value for a leading blank such as `` 5``, so that is refused rather than written into
    the expiry date as text the DATE column cannot hold.
    """
    if IsNotSupplied(screenValue, MONTH_LENGTH):
        return FieldEditResult(failed=True, blank=True, message=EXPIRY_MONTH_MESSAGE)
    if not IsDigitsInRange(screenValue, MONTH_LENGTH, VALID_MONTH_RANGE):
        return FieldEditResult(failed=True, message=EXPIRY_MONTH_MESSAGE)
    return FIELD_PASSED


def EditExpiryYear(screenValue: str) -> FieldEditResult:
    """Port of 1260-EDIT-EXPIRY-YEAR (lines 913-944): supplied, four digits, 1950 to 2099."""
    if IsNotSupplied(screenValue, YEAR_LENGTH):
        return FieldEditResult(failed=True, blank=True, message=EXPIRY_YEAR_MESSAGE)
    if not IsDigitsInRange(screenValue, YEAR_LENGTH, VALID_YEAR_RANGE):
        return FieldEditResult(failed=True, message=EXPIRY_YEAR_MESSAGE)
    return FIELD_PASSED


def IsDigitsInRange(screenValue: str, length: int, validRange: range) -> bool:
    if len(screenValue) != length or not IsAsciiDigits(screenValue):
        return False
    return int(screenValue) in validRange


# 1200-EDIT-MAP-INPUTS performs the detail edits in this order (lines 698-708).
CARD_FIELD_EDITS: tuple[tuple[str, Callable[[str], FieldEditResult]], ...] = (
    (NAME_FIELD, EditCardName),
    (STATUS_FIELD, EditCardStatus),
    (MONTH_FIELD, EditExpiryMonth),
    (YEAR_FIELD, EditExpiryYear),
)


def RunDetailEdits(received: Mapping[str, str]) -> DetailEditOutcome:
    """Run every detail edit in source order and keep each failure (all-field mode)."""
    failures = [
        (fieldName, result)
        for fieldName, editFunction in CARD_FIELD_EDITS
        if (result := editFunction(received[fieldName])).failed
    ]
    return DetailEditOutcome(
        message=next((result.message for fieldName, result in failures), ""),
        fieldErrors=tuple(fieldName for fieldName, result in failures),
        blankFields=frozenset(fieldName for fieldName, result in failures if result.blank),
    )


def ShownReceived(received: Mapping[str, str], blankFields: frozenset[str]) -> dict[str, str]:
    """The received values as 3200 and 3300 show them: '*' in each blank failing field.

    LOW-VALUES show as an empty field and trailing padding is dropped.
    """
    return {
        fieldName: (
            BLANK_FIELD_MARKER
            if fieldName in blankFields
            else screenValue.replace(LOW_VALUE, "").rstrip(" ")
        )
        for fieldName, screenValue in received.items()
    }


def NoChangeReply(state: CardUpdateState, received: Mapping[str, str]) -> ScreenReply:
    """The NO-CHANGES-DETECTED screen; the state, and so the information line, is kept.

    SHOW_DETAILS shows the fetched values and CHANGES_NOT_OK the received ones (3200); the
    cursor is the name (3300, lines 1213-1214).
    """
    if state.stateCode == STATE_SHOW_DETAILS:
        values, info = ImageValues(state.beforeImage), DETAILS_SHOWN_INFO
    else:
        values, info = ShownReceived(received, frozenset()), PROMPT_FOR_CHANGES_INFO
    return ScreenReply(state, info, values, message=NO_CHANGES_MESSAGE, cursorField=NAME_FIELD)


def EditFailureReply(
    state: CardUpdateState, received: Mapping[str, str], edits: DetailEditOutcome
) -> ScreenReply:
    """The CCUP-CHANGES-NOT-OK screen: first message, every failing field, cursor on the first."""
    return ScreenReply(
        replace(state, stateCode=STATE_CHANGES_NOT_OK, newData=None),
        PROMPT_FOR_CHANGES_INFO,
        ShownReceived(received, edits.blankFields),
        message=edits.message,
        fieldErrors=edits.fieldErrors,
        cursorField=edits.fieldErrors[0],
    )


def ConfirmationReply(state: CardUpdateState, received: Mapping[str, str]) -> ScreenReply:
    """The CCUP-CHANGES-OK-NOT-CONFIRMED screen, the validated values kept in the token.

    The name is kept as entered, right-trimmed; the cursor is the account (3300 WHEN OTHER).
    """
    newData = NewCardData(
        name=received[NAME_FIELD].rstrip(" "),
        status=received[STATUS_FIELD],
        month=received[MONTH_FIELD],
        year=received[YEAR_FIELD],
    )
    confirmState = replace(state, stateCode=STATE_CONFIRM, newData=newData)
    return ScreenReply(confirmState, PROMPT_FOR_CONFIRMATION_INFO, NewDataValues(newData))


@dataclass(frozen=True)
class WriteOutcomeScreen:
    """How 2000-DECIDE-ACTION and 3000-SEND-MAP present one 9200-WRITE-PROCESSING outcome."""

    stateCode: str
    httpStatus: int
    message: str
    info: str
    cursorField: str


# 2000-DECIDE-ACTION (lines 992-1001) with the 3250 information line and 3300 cursor of each.
WRITE_OUTCOMES = {
    OUTCOME_DONE: WriteOutcomeScreen(STATE_DONE, STATUS_OK, "", UPDATE_SUCCESS_INFO, ACCT_ID_FIELD),
    OUTCOME_LOCK_FAILED: WriteOutcomeScreen(
        STATE_LOCK_ERROR, STATUS_LOCKED, LOCK_FAILED_MESSAGE, UPDATE_FAILURE_INFO, ACCT_ID_FIELD
    ),
    OUTCOME_CHANGED: WriteOutcomeScreen(
        STATE_SHOW_DETAILS, STATUS_CHANGED, RECORD_CHANGED_MESSAGE, DETAILS_SHOWN_INFO, NAME_FIELD
    ),
    OUTCOME_UPDATE_FAILED: WriteOutcomeScreen(
        STATE_UPDATE_FAILED,
        STATUS_WRITE_FAILED,
        UPDATE_FAILED_MESSAGE,
        UPDATE_FAILURE_INFO,
        ACCT_ID_FIELD,
    ),
}


@dataclass
class WriteAttempt:
    """The progress of one 9200-WRITE-PROCESSING, read once its write unit has ended.

    ``outcome`` is what the unit ending now with a file condition would mean: a lock failure
    until the record is locked, then an update failure. ``rewritten`` turns into success only
    when the unit also commits. ``freshImage`` is the record 9300 found changed.
    """

    outcome: str = OUTCOME_LOCK_FAILED
    rewritten: bool = False
    freshImage: BeforeImage | None = None

    def StepInProgress(self) -> str:
        """Return the CICS command a file condition ending the unit now would have failed."""
        if self.outcome == OUTCOME_UPDATE_FAILED:
            return REWRITE_OPERATION
        return READ_UPDATE_OPERATION


async def SaveChanges(
    context: OnlineContext, state: CardUpdateState, screenFields: CardScreenFields
) -> CardScreen:
    """Port of WHEN CCUP-CHANGES-OK-NOT-CONFIRMED AND CCARD-AID-PFK05 (lines 988-1001).

    The token's validated values are written whatever the request's fields hold.

    Raises:
        LegacyMessageError: 423 ``Could not lock record for update``, 409
            ``Record changed by some one else. Please review`` with the fresh record shown,
            or 503 ``Update of record failed``.
    """
    attempt = await WriteProcessing(context, state)
    outcomeScreen = WRITE_OUTCOMES[attempt.outcome]
    reply = WriteOutcomeReply(state, attempt, outcomeScreen)
    if outcomeScreen.httpStatus == STATUS_OK:
        return BuildScreen(reply, context.settings)
    raise ScreenError(reply, outcomeScreen.httpStatus, context.settings)


async def WriteProcessing(context: OnlineContext, state: CardUpdateState) -> WriteAttempt:
    """Port of 9200-WRITE-PROCESSING (lines 1420-1496) in one CARDDAT write unit.

    The locked read, the compare and the rewrite share the unit, which commits after the
    rewrite. A file condition at any step, the unit's connect and commit included, ends the
    attempt with the outcome of the step in progress; no legacy error is raised inside the
    unit, so a refusal discards nothing and a success commits once.
    """
    attempt = WriteAttempt()
    try:
        async with WriteUnit(context.engine, CARD_FILE_NAME) as unit:
            await LockCompareAndRewrite(CardRepository(context), unit, state, attempt)
    except RepositoryError as error:
        LogFileCondition(LOGGER, TRANSACTION_ID, attempt.StepInProgress(), error)
        return attempt
    if attempt.rewritten:
        attempt.outcome = OUTCOME_DONE
    return attempt


async def LockCompareAndRewrite(
    repository: CardRepository,
    unit: AsyncConnection,
    state: CardUpdateState,
    attempt: WriteAttempt,
) -> None:
    """The READ UPDATE, 9300-CHECK-CHANGE-IN-REC and REWRITE steps of 9200-WRITE-PROCESSING.

    Every non-NORMAL READ UPDATE response, NOTFND included, is COULD-NOT-LOCK-FOR-UPDATE
    (lines 1441-1449). A record that differs from the before-image is not rewritten.
    """
    current = await repository.GetCardForUpdate(unit, state.cardNum)
    if current is None:
        return
    currentImage = BeforeImageOf(current)
    if currentImage != state.beforeImage:
        attempt.outcome, attempt.freshImage = OUTCOME_CHANGED, currentImage
        return
    attempt.outcome = OUTCOME_UPDATE_FAILED
    rewriteValues = RewriteValues(state)
    if rewriteValues is None:
        return
    await repository.UpdateCard(unit, rewriteValues)
    attempt.rewritten = True


def RewriteValues(state: CardUpdateState) -> dict[str, object] | None:
    """Return CARD-UPDATE-RECORD (lines 1461-1475), or None when its date cannot be stored.

    ``acct_id`` is the screen's account key (MOVE CC-ACCT-ID-N), not the card's account, so
    the card moves to it; an account that does not exist fails the REWRITE. ``cvv_cd`` is
    left out, so the stored code stays: lines 1464-1465 move CCUP-NEW-CVV-CD, which nothing
    populates, through a PIC 9(3) redefinition, an undefined value that the digit check
    would refuse, and 9300 has just proved the stored code equals the before-image.
    """
    newData, beforeImage = state.newData, state.beforeImage
    if newData is None or beforeImage is None:
        return None
    expirationDate = ExpirationDateOf(newData, beforeImage.day)
    if expirationDate is None:
        return None
    return {
        "card_num": state.cardNum,
        "acct_id": state.acctId,
        "embossed_name": newData.name,
        "expiration_date": expirationDate,
        "active_status": newData.status,
    }


def ExpirationDateOf(newData: NewCardData, day: str) -> date | None:
    """Return ``STRING NEW-YEAR '-' NEW-MON '-' NEW-DAY`` as a date, or None (lines 1467-1474).

    A date that cannot exist, such as 31 February or a blank day from a NULL stored date, is
    text the DATE column cannot hold, so the REWRITE fails (LOCKED-BUT-UPDATE-FAILED).
    """
    try:
        return date(int(newData.year), int(newData.month), int(day))
    except ValueError:
        return None


def WriteOutcomeReply(
    state: CardUpdateState, attempt: WriteAttempt, outcomeScreen: WriteOutcomeScreen
) -> ScreenReply:
    """The screen after 9200: the fresh record for a change, otherwise the new values (3200)."""
    if attempt.freshImage is not None:
        freshState = CardUpdateState(
            STATE_SHOW_DETAILS, state.acctId, state.cardNum, attempt.freshImage
        )
        values = ImageValues(attempt.freshImage)
    else:
        freshState = replace(state, stateCode=outcomeScreen.stateCode)
        values = NewDataValues(state.newData)
    return ScreenReply(
        freshState,
        outcomeScreen.info,
        values,
        message=outcomeScreen.message,
        cursorField=outcomeScreen.cursorField,
    )
