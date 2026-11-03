"""Request and response models of the CT00, CT01 and CT02 transaction screens.

Replaces: COTRN00C (CT00, Transaction List, BMS map COTRN00), COTRN01C (CT01, Transaction
View, BMS map COTRN01) and COTRN02C (CT02, Transaction Add, BMS map COTRN02), with the
symbolic maps cpy-bms/COTRN00.CPY, cpy-bms/COTRN01.CPY and cpy-bms/COTRN02.CPY, and the
transaction record copybook cpy/CVTRA05Y.cpy.

The three programs validate in first-failure mode: the first failing edit ends validation, so
their responses carry ``message`` and ``cursor_field`` rather than a list of failing fields.
Screen inputs are raw ``ScreenText`` limited to their BMS LENGTH, so every edit literal of the
programs (for example ``Tran ID must be Numeric ...`` or
``Amount should be in format -99999999.99``) stays reachable in the services.

Attribute names are snake_case, not the MK_Modernization Rule's camelCase, because they are the
API's JSON keys. CT00 and CT01 response fields that carry a stored value mirror the
transactions column names (``tran_type_cd``, ``tran_cat_cd``, ``tran_source``, ``tran_desc``,
``tran_amt``); ``tran_date`` and ``card_num_masked`` name a derived and a masked value. The
CT02 screen fields keep the request names the API contract prescribes (``type_cd``,
``cat_cd``, ``source``, ``desc``, ``amount`` and the rest). Query parameters keep camelCase
wire names through their aliases.
"""

from pydantic import BaseModel, Field

from app.schemas.common import (
    Direction,
    MoneyAmount,
    PageState,
    QueryModel,
    ScreenText,
    TransactionAddAction,
)

# The ten list rows of the COTRN00 map (SEL0001-SEL0010, TRNID01-TRNID10).
TRANSACTION_LIST_PAGE_SIZE = 10


class TransactionListQuery(QueryModel):
    """Query parameters of ``GET /transactions`` (CT00, COTRN00C, map COTRN00).

    Replaces the TRNIDIN search field and the CDEMO-CT00 paging keys of the COMMAREA.
    """

    search_tran_id: ScreenText(16) = Field(
        default="",
        alias="searchTranId",
        description=(
            "TRNIDIN search key as keyed, up to 16 characters. Blank starts at the lowest "
            "TRAN-ID; non-numeric text returns 'Tran ID must be Numeric ...'; otherwise the "
            "browse starts at the first TRAN-ID greater than or equal to it."
        ),
    )
    start_key: ScreenText(16) = Field(
        default="",
        alias="startKey",
        description=(
            "Paging key: the shown page's first_key with direction backward (F7), or its "
            "last_key with direction forward (F8). Empty reads the first page. A raw TRAN-ID "
            "of at most 16 code-page-037 characters."
        ),
    )
    first_key: ScreenText(16) = Field(
        default="",
        alias="firstKey",
        description=(
            "The shown page's first_key (CDEMO-CT00-TRNID-FIRST), sent with direction forward: "
            "with a startKey (F8) and with an empty one (a search). When COTRN00C re-sends the "
            "screen with its rows unchanged (F8 with no page after the last key, a non-numeric "
            "search, a search that finds nothing), the service re-reads those rows from it. "
            "A raw TRAN-ID of at most 16 code-page-037 characters; empty when no page is shown."
        ),
    )
    direction: Direction = Field(
        default=Direction.FORWARD,
        alias="direction",
        description="Paging direction: forward (F8, READNEXT) or backward (F7, READPREV).",
    )
    page: int = Field(
        default=0,
        ge=0,
        alias="page",
        description="The page number currently shown (CDEMO-CT00-PAGE-NUM); 0 before the first.",
    )


class TransactionListRow(BaseModel):
    """One list row of the COTRN00 map (TRNIDnn, TDATEnn, TDESCnn, TAMTnnn), from COTRN00C."""

    tran_id: str = Field(description="TRNIDnn: the 16-character TRAN-ID.")
    tran_date: str = Field(
        description=(
            "TDATEnn: MM/DD/YY (8 characters) built from the original timestamp TRAN-ORIG-TS, "
            "as COTRN00C POPULATE-TRAN-DATA formats it."
        ),
    )
    tran_desc: str = Field(
        description="TDESCnn: the first 26 characters of TRAN-DESC, truncated by the service.",
    )
    tran_amt: MoneyAmount(9) = Field(
        description="TAMTnnn: TRAN-AMT (S9(09)V99), serialised as a JSON decimal string.",
    )


class TransactionListResponse(PageState):
    """A page of the Transaction List screen (CT00, COTRN00C, map COTRN00).

    The inherited ``page``, ``first_key`` and ``last_key`` carry the page number and the raw
    TRAN-IDs of the first and last rows shown (CDEMO-CT00-PAGE-NUM, CDEMO-CT00-TRNID-FIRST,
    CDEMO-CT00-TRNID-LAST). Two outcomes differ, as COTRN00C leaves those keys: a backward page
    shorter than 10 rows keeps the first_key it browsed from, and a search that finds nothing
    returns last_key HIGH-VALUES (sixteen U+00FF), after which F8 finds no page.
    """

    rows: list[TransactionListRow] = Field(
        default_factory=list,
        max_length=TRANSACTION_LIST_PAGE_SIZE,
        description="Up to 10 rows in TRAN-ID (EBCDIC key) order.",
    )
    message: str = Field(
        default="",
        description=(
            "The ERRMSG line, verbatim: one of the five paging literals, "
            "'Invalid selection. Valid value is S' or 'Tran ID must be Numeric ...'; empty for "
            "none."
        ),
    )
    cursor_field: str | None = Field(
        default=None,
        description=(
            "Snake_case name of the field that receives focus (search_tran_id), replacing "
            "MOVE -1 TO TRNIDINL; null when the program sets no cursor."
        ),
    )


class TransactionSelection(BaseModel):
    """One SELnnnn selection code of the COTRN00 map with the TRNIDnn of its row."""

    tran_id: ScreenText(16) = Field(
        default="",
        description="TRNIDnn: the row's TRAN-ID exactly as displayed.",
    )
    selection: ScreenText(1) = Field(
        default="",
        description=(
            "SELnnnn as keyed. COTRN00C takes the first non-blank selection; S or s opens "
            "the Transaction View, any other code gives 'Invalid selection. Valid value is S'."
        ),
    )


class TransactionSelectionRequest(BaseModel):
    """Body of ``POST /transactions/selection``: ENTER on the Transaction List (COTRN00C).

    Replaces PROCESS-ENTER-KEY's reading of the SELnnnn, TRNIDnn and TRNIDIN fields, and the
    CDEMO-CT00-TRNID-FIRST and CDEMO-CT00-PAGE-NUM COMMAREA keys of the page on screen.
    """

    selections: list[TransactionSelection] = Field(
        default_factory=list,
        max_length=TRANSACTION_LIST_PAGE_SIZE,
        description="The selection codes of the rows shown, in screen order (at most 10).",
    )
    search_tran_id: ScreenText(16) = Field(
        default="",
        description="TRNIDIN search key as keyed, applied after the selection is processed.",
    )
    first_key: ScreenText(16) = Field(
        default="",
        description=(
            "TRAN-ID of the first row shown (CDEMO-CT00-TRNID-FIRST), so the service can "
            "return the unchanged list when the search key is non-numeric. A raw TRAN-ID of at "
            "most 16 code-page-037 characters."
        ),
    )
    page: int = Field(
        default=0,
        ge=0,
        description="The page number shown (CDEMO-CT00-PAGE-NUM).",
    )


class TransactionSelectionResponse(TransactionListResponse):
    """Outcome of ENTER on the Transaction List (COTRN00C, map COTRN00).

    A valid ``S`` selection sets ``route``, replacing the XCTL to COTRN01C. Otherwise
    ``route`` is null and the inherited list fields carry the re-browsed page and its message.
    """

    route: str | None = Field(
        default=None,
        description=(
            "Frontend path of the Transaction View for a valid S selection, for example "
            "/transactions/view?tranId=TRAN_ID; null otherwise."
        ),
    )


class TransactionDetailResponse(BaseModel):
    """The Transaction View screen (CT01, COTRN01C, map COTRN01), from a CVTRA05Y record.

    The service truncates each text value to its map width; the widths are documented on the
    fields rather than enforced, because they are output formatting, not input limits.
    """

    tran_id: str = Field(description="TRNID: the 16-character TRAN-ID.")
    card_num_masked: str = Field(
        description="CARDNUM: TRAN-CARD-NUM masked as twelve asterisks plus the last 4 digits.",
    )
    tran_type_cd: str = Field(description="TTYPCD: TRAN-TYPE-CD, 2 characters.")
    tran_cat_cd: int = Field(
        description="TCATCD: TRAN-CAT-CD, a 9(04) code rendered zero-padded to 4 digits.",
    )
    tran_source: str = Field(description="TRNSRC: TRAN-SOURCE, 10 characters.")
    tran_desc: str = Field(description="TDESC: the first 60 characters of TRAN-DESC.")
    tran_amt: MoneyAmount(9) = Field(
        description="TRNAMT: TRAN-AMT (S9(09)V99), serialised as a JSON decimal string.",
    )
    orig_ts: str = Field(
        description=(
            "TRAN-ORIG-TS: the 26-character timestamp text exactly as the legacy record holds "
            "it; the map shows its first 10 characters in TORIGDT."
        ),
    )
    proc_ts: str = Field(
        description=(
            "TRAN-PROC-TS: the 26-character timestamp text exactly as the legacy record holds "
            "it; the map shows its first 10 characters in TPROCDT."
        ),
    )
    merchant_id: str = Field(description="MID: TRAN-MERCHANT-ID, 9 digits.")
    merchant_name: str = Field(
        description="MNAME: the first 30 characters of TRAN-MERCHANT-NAME.",
    )
    merchant_city: str = Field(
        description="MCITY: the first 25 characters of TRAN-MERCHANT-CITY.",
    )
    merchant_zip: str = Field(description="MZIP: TRAN-MERCHANT-ZIP, 10 characters.")
    message: str = Field(
        default="",
        description="The ERRMSG line, verbatim; empty for none.",
    )


class TransactionAddFields(BaseModel):
    """The input fields of the Transaction Add screen (CT02, COTRN02C, map COTRN02).

    Every field is raw text limited to its BMS LENGTH (cpy-bms/COTRN02.CPY), so COTRN02C's
    edits run on it exactly as keyed. As a response it is the screen as the program leaves it,
    including the values F5 copies from the last transaction.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        description=(
            "ACTIDIN: account ID as keyed (11). Either it or card_num must be entered; "
            "non-numeric text gives 'Account ID must be Numeric...'."
        ),
    )
    card_num: ScreenText(16) = Field(
        default="",
        description=(
            "CARDNIN: card number as keyed (16). Responses return the masked PAN; the frontend "
            "keeps the user's own key input and never sends a masked value back."
        ),
    )
    type_cd: ScreenText(2) = Field(
        default="",
        description="TTYPCD: transaction type code as keyed (2).",
    )
    cat_cd: ScreenText(4) = Field(
        default="",
        description="TCATCD: transaction category code as keyed (4).",
    )
    source: ScreenText(10) = Field(
        default="",
        description="TRNSRC: transaction source as keyed (10).",
    )
    desc: ScreenText(60) = Field(
        default="",
        description="TDESC: transaction description as keyed (60).",
    )
    amount: ScreenText(12) = Field(
        default="",
        description=(
            "TRNAMT: amount as keyed (12), for example +00000050.00. Text that is not a sign, "
            "8 digits, a point and 2 digits, such as $1,234.567, gives "
            "'Amount should be in format -99999999.99'."
        ),
    )
    orig_date: ScreenText(10) = Field(
        default="",
        description="TORIGDT: original date as keyed (10), edited as YYYY-MM-DD by COTRN02C.",
    )
    proc_date: ScreenText(10) = Field(
        default="",
        description="TPROCDT: processing date as keyed (10), edited as YYYY-MM-DD by COTRN02C.",
    )
    merchant_id: ScreenText(9) = Field(
        default="",
        description="MID: merchant ID as keyed (9); non-numeric text is rejected by COTRN02C.",
    )
    merchant_name: ScreenText(30) = Field(
        default="",
        description="MNAME: merchant name as keyed (30).",
    )
    merchant_city: ScreenText(25) = Field(
        default="",
        description="MCITY: merchant city as keyed (25).",
    )
    merchant_zip: ScreenText(10) = Field(
        default="",
        description="MZIP: merchant ZIP as keyed (10).",
    )
    confirm: ScreenText(1) = Field(
        default="",
        description=(
            "CONFIRM as keyed: Y adds the transaction; blank or N gives "
            "'Confirm to add this transaction...'; any other character gives "
            "'Invalid value. Valid values are (Y/N)...'."
        ),
    )


class TransactionAddRequest(TransactionAddFields):
    """Body of ``POST /transactions`` (CT02, COTRN02C, map COTRN02).

    The JSON body is flat: ``action`` sits beside the 14 screen fields.
    """

    action: TransactionAddAction = Field(
        default=TransactionAddAction.ENTER,
        description=(
            "The key pressed: enter (ENTER) validates and adds; copy_last (F5) copies the "
            "last transaction's fields into the form, then runs the enter flow."
        ),
    )


class TransactionAddResponse(BaseModel):
    """Outcome of a Transaction Add request (CT02, COTRN02C, map COTRN02)."""

    message: str = Field(
        description=(
            "The ERRMSG line, verbatim, for example "
            "'Transaction added successfully.  Your Tran ID is 0000000000000001.' with its "
            "two spaces."
        ),
    )
    cursor_field: str | None = Field(
        default=None,
        description=(
            "Snake_case name of the field that receives focus, replacing MOVE -1 TO <field>L; "
            "null when the program sets no cursor."
        ),
    )
    fields: TransactionAddFields = Field(
        default_factory=TransactionAddFields,
        description=(
            "The screen as the program leaves it, including the values copied by copy_last."
        ),
    )
    tran_id: str | None = Field(
        default=None,
        description="The new 16-character TRAN-ID, set only after a successful add.",
    )
