"""Request and response models of the card transactions CCLI, CCDL and CCUP.

Replaces:
    COCRDLIC (CCLI, Card List), BMS map COCRDLI (cpy-bms/COCRDLI.CPY)
    COCRDSLC (CCDL, Card View), BMS map COCRDSL (cpy-bms/COCRDSL.CPY)
    COCRDUPC (CCUP, Card Update), BMS map COCRDUP (cpy-bms/COCRDUP.CPY)
    copybooks CVACT02Y (CARD-RECORD) and CVCRD01Y (CC-WORK-AREAS)

A full card number never appears in a response. Responses carry the masked PAN
(``************`` + last 4) or an opaque ``card_ref`` token, and no model has a CVV field.

Attribute names are snake_case because they are the JSON keys mirroring the snake_case
columns; MK_Modernization Rule clause 1 yields to that fixed key style. Query parameters keep
camelCase wire names through explicit aliases.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import (
    CARD_REF_MAX_LENGTH,
    QUERY_PAGE_NUMBER,
    SCREEN_TOKEN_MAX_LENGTH,
    START_KEY_MAX_LENGTH,
    Direction,
    LegacyErrorResponse,
    OpaqueToken,
    PageState,
    QueryModel,
    ScreenFeedback,
    ScreenText,
    UpdateAction,
    UpdateScreenState,
)

CARD_LIST_PAGE_SIZE = 7
# WS-CA-SCREEN-NUM PIC 9(1) (cbl/COCRDLIC.cbl line 237): no COMMAREA holds a larger page.
CCLI_PAGE_NUMBER_DIGITS = 1
MAX_CCLI_PAGE_NUMBER = 10**CCLI_PAGE_NUMBER_DIGITS - 1


class CardListQuery(QueryModel):
    """Query of ``GET /cards``: the COCRDLI filters, paging keys and row selection codes read by
    COCRDLIC (CCLI).

    The filters and codes are raw screen text, so the COCRDLIC edits
    ``ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER``,
    ``CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER``,
    ``PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE`` and ``INVALID ACTION CODE`` stay
    reachable on ENTER, F7 and F8 alike.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        alias="acctId",
        description="Account filter (ACCTSID), as keyed; blank lists every account's cards.",
    )
    card_num: ScreenText(16) = Field(
        default="",
        alias="cardNum",
        description="Card number filter (CARDSID), as keyed; blank applies no card filter.",
    )
    start_key: OpaqueToken(START_KEY_MAX_LENGTH) = Field(
        default="",
        alias="startKey",
        description=(
            "The first_key (ENTER, F7) or last_key (F8) of the page shown: an opaque paging "
            "token, never a card number. A blank, expired or foreign key reads the first page."
        ),
    )
    # None, not FORWARD, is the default: FastAPI fills an omitted query value with the default,
    # so only a None default lets COCRDLIC tell ENTER from F8 (as users.UserListQuery does).
    direction: Direction | None = Field(
        default=None,
        alias="direction",
        description=(
            "Paging key: forward (F8, READNEXT) or backward (F7, READPREV). Omitted for ENTER, "
            "which re-reads the page shown from startKey."
        ),
    )
    page: QUERY_PAGE_NUMBER = Field(
        default=0,
        ge=0,
        le=MAX_CCLI_PAGE_NUMBER,
        alias="page",
        description=(
            "The page number shown, replacing the COMMAREA WS-CA-SCREEN-NUM counter (PIC 9(1)): "
            "an integer from 0 to 9 in decimal digits only."
        ),
    )
    selections: list[ScreenText(1)] = Field(
        default_factory=list,
        max_length=CARD_LIST_PAGE_SIZE,
        alias="selections",
        description=(
            "The CRDSEL1-7 code of each row shown, in row order and as keyed (blank for an "
            "untouched row), sent as one repeated selections parameter per row; at most 7. "
            "More than one S or U, or any other non-blank code, is a 400 carrying the "
            "redisplayed rows, page keys and page. A single S or U never transfers here: only "
            "POST /cards/selection opens the card view or update."
        ),
    )


class CardKeyQuery(QueryModel):
    """Query of ``GET /cards/{cardNum}`` (COCRDSLC, CCDL) and its ``/edit`` form (COCRDUPC, CCUP).

    CCDL and CCUP edit the account and the card key for a typed card number, so ``Account
    number not provided`` stays reachable, and skip the edits for a ``card_ref`` from the card
    list sent with its row's 11-digit account. The ``{cardNum}`` path value is a 16-digit card
    number or a ``card_ref`` token. A value longer than CARDSID is always a token; ``isCardRef``
    declares a shorter one a token too, so a bad ``card_ref`` from a frontend URL gives ``Did
    not find cards for this search condition`` rather than the typed card number's key edit.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        alias="acctId",
        description="Account number (ACCTSID), as keyed.",
    )
    is_card_ref: bool = Field(
        default=False,
        alias="isCardRef",
        description=(
            "True when the cardNum path value is a card_ref token the server issued, whatever "
            "its length; false or omitted when it is the card number as keyed."
        ),
    )


class CardListRow(BaseModel):
    """One COCRDLI list row (CRDSELn, ACCTNOn, CRDNUMn, CRDSTSn) built by COCRDLIC."""

    card_ref: str = Field(
        description="Opaque token of the row's card, sent back for selection and navigation.",
    )
    acct_id: str = Field(description="Account number of the card (ACCTNOn, CARD-ACCT-ID).")
    card_num_masked: str = Field(
        description="Card number masked as ************ plus the last 4 digits (CRDNUMn).",
    )
    active_status: str = Field(description="Card active status (CRDSTSn, CARD-ACTIVE-STATUS).")


class CardListResponse(PageState, ScreenFeedback):
    """A COCRDLI page from COCRDLIC (CCLI): at most 7 rows (WS-MAX-SCREEN-LINES).

    ``first_key`` and ``last_key`` are opaque tokens holding the COMMAREA paging state (the first
    and last card keys and the page flags), never card numbers. ``message`` carries ERRMSG
    literals such as ``NO RECORDS FOUND FOR THIS SEARCH CONDITION.`` and
    ``NO MORE PAGES TO DISPLAY``;
    ``info_message`` carries ``TYPE S FOR DETAIL, U TO UPDATE ANY RECORD``. ``field_errors`` and
    ``cursor_field`` name the failing filters ``acct_id`` and ``card_num``. A filter or
    selection edit failure is never this model: it is the 400 ``CardListErrorResponse``.
    """

    rows: list[CardListRow] = Field(
        default_factory=list,
        max_length=CARD_LIST_PAGE_SIZE,
        description="The cards of the page, in card number order; at most 7.",
    )


class CardListErrorResponse(PageState, LegacyErrorResponse):
    """The 400 of ``GET /cards`` (COCRDLIC, CCLI), with the page a selection error redisplays.

    A selection error (2250-EDIT-ARRAY: more than one S or U, or any other code) redisplays the
    list from the first card, so ``cocrdlic.SelectionError`` adds that page's ``rows``, ``page``,
    ``first_key`` and ``last_key``, typed as ``CardListResponse`` types them, to the four
    standard keys of ``LegacyErrorResponse``. A filter edit failure carries the four keys alone,
    and the client keeps the rows it shows, so every added key has a default; none is ever null.
    The 503 file error and the session refusals are declared as ``LegacyErrorResponse``.
    """

    rows: list[CardListRow] = Field(
        default_factory=list,
        max_length=CARD_LIST_PAGE_SIZE,
        description="The cards of the redisplayed page, in card number order; at most 7.",
    )


class CardSelectionRow(BaseModel):
    """One COCRDLI row's selection code (CRDSELn) as sent on ENTER."""

    card_ref: OpaqueToken(CARD_REF_MAX_LENGTH) = Field(
        default="",
        description="The row's card_ref token, as received in CardListRow.",
    )
    selection: ScreenText(1) = Field(
        default="",
        description=(
            "Selection code (CRDSELn), as keyed: S views, U updates; any other non-blank "
            "value gives INVALID ACTION CODE."
        ),
    )


class CardSelectionRequest(BaseModel):
    """Body of ``POST /cards/selection``: the COCRDLI screen as COCRDLIC (CCLI) reads it on ENTER.

    COCRDLIC edits the filters before the selection codes.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        description="Account filter shown on the screen (ACCTSID), as keyed.",
    )
    card_num: ScreenText(16) = Field(
        default="",
        description="Card number filter shown on the screen (CARDSID), as keyed.",
    )
    rows: list[CardSelectionRow] = Field(
        default_factory=list,
        max_length=CARD_LIST_PAGE_SIZE,
        description="The selection code of every row shown; at most 7.",
    )


class CardSelectionResponse(ScreenFeedback):
    """Outcome of a COCRDLI selection, replacing COCRDLIC's XCTL to COCRDSLC or COCRDUPC.

    With no selection code there is no ``action``: the client re-reads the page. A refused
    selection (``PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE`` or ``INVALID ACTION CODE``)
    is never this model: it is the 400 ``CardSelectionErrorResponse``.
    """

    action: Literal["view", "update"] | None = Field(
        default=None,
        description="view (S, COCRDSLC page) or update (U, COCRDUPC page); null for no selection.",
    )
    card_ref: str = Field(
        default="",
        description="The selected card's card_ref token, for the cardRef URL parameter.",
    )


class CardSelectionErrorResponse(LegacyErrorResponse):
    """The 400 of ``POST /cards/selection`` (COCRDLIC, CCLI), with the redisplayed rows.

    A selection error redisplays the list from the first card, so ``cocrdlic.SelectionError``
    adds that page's ``rows``, ``first_key`` and ``last_key``, typed as ``CardListResponse``
    types them, to the four standard keys of ``LegacyErrorResponse``. It adds no ``page``: the
    client keeps the page number it shows. A filter edit failure carries the four keys alone,
    so every added key has a default; none is ever null. The 404 of a stale ``card_ref``, the
    503 file error and the session refusals are declared as ``LegacyErrorResponse``.
    """

    rows: list[CardListRow] = Field(
        default_factory=list,
        max_length=CARD_LIST_PAGE_SIZE,
        description="The cards of the redisplayed page, in card number order; at most 7.",
    )
    first_key: str = Field(
        default="",
        description=(
            "Opaque paging token of the redisplayed page, sent back as startKey with direction "
            "backward (F7)."
        ),
    )
    last_key: str = Field(
        default="",
        description=(
            "Opaque paging token of the redisplayed page, sent back as startKey with direction "
            "forward (F8)."
        ),
    )


class CardDetailResponse(ScreenFeedback):
    """The COCRDSL screen of COCRDSLC (CCDL): one card's details, read by card number.

    ``info_message`` carries ``   Displaying requested details`` with its three leading
    spaces. The card number is masked, and the card verification code is never returned.
    """

    acct_id: str = Field(description="Account number (ACCTSID, CARD-ACCT-ID).")
    card_ref: str = Field(
        description="The card's card_ref token, so the card number never enters a frontend URL.",
    )
    card_num_masked: str = Field(
        description="Card number masked as ************ plus the last 4 digits (CARDSID).",
    )
    embossed_name: str = Field(description="Name on card (CRDNAME, CARD-EMBOSSED-NAME).")
    active_status: str = Field(description="Card active status (CRDSTCD, CARD-ACTIVE-STATUS).")
    expiry_month: str = Field(description="Expiry month MM (EXPMON, CARD-EXPIRAION-DATE).")
    expiry_year: str = Field(description="Expiry year YYYY (EXPYEAR, CARD-EXPIRAION-DATE).")


class CardScreenFields(BaseModel):
    """The COCRDUP input fields of COCRDUPC (CCUP), each limited to its BMS LENGTH.

    In a request the service reads only the fields the screen token's state leaves editable.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        description="Account number (ACCTSID), as keyed; editable only before details are fetched.",
    )
    card_num: ScreenText(16) = Field(
        default="",
        description=(
            "Card number (CARDSID): the keyed card number only in DETAILS_NOT_FETCHED. Every "
            "response carries the masked card number; the true key travels in screen_token."
        ),
    )
    embossed_name: ScreenText(50) = Field(
        default="",
        description="Name on card (CRDNAME), as keyed.",
    )
    active_status: ScreenText(1) = Field(
        default="",
        description="Card active status Y/N (CRDSTCD), as keyed.",
    )
    expiry_month: ScreenText(2) = Field(
        default="",
        description="Expiry month (EXPMON), as keyed.",
    )
    expiry_year: ScreenText(4) = Field(
        default="",
        description="Expiry year (EXPYEAR), as keyed.",
    )
    expiry_day: ScreenText(2) = Field(
        default="",
        description="Expiry day (EXPDAY): dark and protected, carried for the card rewrite.",
    )


class CardScreen(ScreenFeedback):
    """The COCRDUP screen of COCRDUPC (CCUP), answering every fetch, ENTER, F5 and F12.

    ``state`` decides which fields the page renders editable. ``info_message`` carries the
    program's information line, such as ``Details of selected card shown above`` or
    ``Changes validated.Press F5 to save``.
    """

    fields: CardScreenFields = Field(
        default_factory=CardScreenFields,
        description="The screen fields; card_num is always masked.",
    )
    state: UpdateScreenState = Field(
        description="The screen state, replacing CCUP-CHANGE-ACTION.",
    )
    screen_token: str = Field(
        default="",
        description=(
            "Encrypted screen state (state, keys, fetched and validated values), replacing the "
            "COMMAREA; sent back unchanged on the next request."
        ),
    )
    card_ref: str = Field(
        default="",
        description="The card_ref token of the card shown; empty before a card is fetched.",
    )


class CardScreenErrorResponse(LegacyErrorResponse):
    """The error body of a CCUP outcome (COCRDUPC), with the COCRDUP screen it sends back.

    Every 400, 404, 409, 423 and 503 of ``GET /cards/{cardNum}/edit`` and
    ``PUT /cards/{cardNum}`` is built by ``cocrdupc.ScreenError``: the four standard keys of
    ``LegacyErrorResponse`` plus the screen's ``fields``, ``state`` and ``screen_token``, typed
    as ``CardScreen`` types them, so the page keeps the state machine running on the error. It
    carries no ``card_ref``. The session and Origin refusals (401, 403) carry the four keys
    alone and are declared as ``LegacyErrorResponse``. The screen keys have defaults, the empty
    DETAILS_NOT_FETCHED key screen with no token, only so that a four-key body validates too;
    none of them is ever null.
    """

    fields: CardScreenFields = Field(
        default_factory=CardScreenFields,
        description="The screen fields shown with the message; card_num is always masked.",
    )
    state: UpdateScreenState = Field(
        default=UpdateScreenState.DETAILS_NOT_FETCHED,
        description=(
            "The screen state after the request, replacing CCUP-CHANGE-ACTION: for example "
            "DETAILS_NOT_FETCHED after a key edit or read failure, CHANGES_NOT_OK after a field "
            "edit failure, CHANGES_FAILED after 423 or the 503 rewrite failure."
        ),
    )
    screen_token: str = Field(
        default="",
        description=(
            "The new encrypted screen state, replacing the COCRDUPC COMMAREA; send it back "
            "unchanged with the next request."
        ),
    )


class CardUpdateRequest(BaseModel):
    """Body of ``PUT /cards/{cardNum}``: one ENTER, F5 or F12 on the COCRDUP screen (CCUP)."""

    action: UpdateAction = Field(
        default=UpdateAction.ENTER,
        description="enter (ENTER), save (F5) or cancel (F12); one the state forbids is ENTER.",
    )
    screen_token: OpaqueToken(SCREEN_TOKEN_MAX_LENGTH) = Field(
        default="",
        description=(
            "The screen_token of the last CardScreen; a missing, forged or expired token, or "
            "one issued for another card than the cardNum path names, restarts the flow at "
            "DETAILS_NOT_FETCHED."
        ),
    )
    fields: CardScreenFields = Field(
        default_factory=CardScreenFields,
        description="The keyed fields; only those the token's state leaves editable are read.",
    )
