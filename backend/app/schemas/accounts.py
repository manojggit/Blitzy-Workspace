"""Request and response models of Account View (CAVW) and Account Update (CAUP).

Replaces: COACTVWC (CAVW) and COACTUPC (CAUP), BMS maps COACTVW and COACTUP
(cpy-bms/COACTVW.CPY, cpy-bms/COACTUP.CPY), copybooks CVACT01Y (account record) and CVCUS01Y
(customer record).

CAVW answers with typed values: exact decimal amounts and a masked SSN. CAUP exchanges every
COACTUP input as raw screen text, because COACTUPC runs its edits (TEST-NUMVAL-C, the CSUTLDPY
date edits, NANP area codes, state and ZIP) on the text as keyed.

Field names are snake_case JSON keys that mirror the database column names. They depart from
the MK_Modernization Rule's camelCase variables because the API contract fixes one key style.
"""

from pydantic import BaseModel, Field

from app.schemas.common import (
    SCREEN_TOKEN_MAX_LENGTH,
    LegacyErrorResponse,
    MoneyAmount,
    OpaqueToken,
    ScreenFeedback,
    ScreenText,
    UpdateAction,
    UpdateScreenState,
)


class AccountViewResponse(ScreenFeedback):
    """The COACTVW screen of COACTVWC (CAVW): one account and its customer, display only.

    The account comes from CVACT01Y and the customer from CVCUS01Y, found through the card
    cross-reference as COACTVWC reads CXACAIX, ACCTDAT and CUSTDAT. Field names are the
    ``accounts.accounts`` and ``customers.customers`` column names, in COACTVW map order. Dates
    are ISO ``YYYY-MM-DD`` text, empty when the stored date is NULL. The SSN is masked, and the
    screen carries no card number, CVV or password.
    """

    acct_id: str = Field(description="ACCTSID: the 11-digit account number (ACCT-ID).")
    active_status: str = Field(
        description="ACSTTUS: the account active status, Y or N (ACCT-ACTIVE-STATUS)."
    )
    open_date: str = Field(
        description="ADTOPEN: ACCT-OPEN-DATE as YYYY-MM-DD; empty when the date is NULL."
    )
    credit_limit: MoneyAmount(10) = Field(
        description="ACRDLIM: ACCT-CREDIT-LIMIT, S9(10)V99, serialised as a JSON string."
    )
    expiration_date: str = Field(
        description="AEXPDT: ACCT-EXPIRAION-DATE as YYYY-MM-DD; empty when the date is NULL."
    )
    cash_credit_limit: MoneyAmount(10) = Field(
        description="ACSHLIM: ACCT-CASH-CREDIT-LIMIT, S9(10)V99, serialised as a JSON string."
    )
    reissue_date: str = Field(
        description="AREISDT: ACCT-REISSUE-DATE as YYYY-MM-DD; empty when the date is NULL."
    )
    curr_balance: MoneyAmount(10) = Field(
        description="ACURBAL: ACCT-CURR-BAL, S9(10)V99, serialised as a JSON string."
    )
    curr_cycle_credit: MoneyAmount(10) = Field(
        description="ACRCYCR: ACCT-CURR-CYC-CREDIT, S9(10)V99, serialised as a JSON string."
    )
    group_id: str = Field(description="AADDGRP: the account group (ACCT-GROUP-ID).")
    curr_cycle_debit: MoneyAmount(10) = Field(
        description="ACRCYDB: ACCT-CURR-CYC-DEBIT, S9(10)V99, serialised as a JSON string."
    )
    cust_id: str = Field(description="ACSTNUM: the 9-digit customer number (CUST-ID).")
    ssn_masked: str = Field(
        description=(
            "ACSTSSN: CUST-SSN masked as ***-**- plus its last 4 digits; CAVW never returns "
            "the full SSN."
        )
    )
    date_of_birth: str = Field(
        description="ACSTDOB: CUST-DOB-YYYY-MM-DD as YYYY-MM-DD; empty when the date is NULL."
    )
    fico_credit_score: int = Field(
        ge=0,
        le=999,
        description="ACSTFCO: CUST-FICO-CREDIT-SCORE, a 9(03) code shown zero-padded to 3.",
    )
    first_name: str = Field(description="ACSFNAM: CUST-FIRST-NAME.")
    middle_name: str = Field(description="ACSMNAM: CUST-MIDDLE-NAME.")
    last_name: str = Field(description="ACSLNAM: CUST-LAST-NAME.")
    addr_line_1: str = Field(description="ACSADL1: CUST-ADDR-LINE-1.")
    addr_state_cd: str = Field(description="ACSSTTE: CUST-ADDR-STATE-CD, the state code.")
    addr_line_2: str = Field(description="ACSADL2: CUST-ADDR-LINE-2.")
    addr_zip: str = Field(description="ACSZIPC: CUST-ADDR-ZIP.")
    addr_line_3: str = Field(
        description="ACSCITY: CUST-ADDR-LINE-3, which COACTVWC shows under the City label."
    )
    addr_country_cd: str = Field(description="ACSCTRY: CUST-ADDR-COUNTRY-CD.")
    phone_num_1: str = Field(description="ACSPHN1: CUST-PHONE-NUM-1 as stored.")
    govt_issued_id: str = Field(description="ACSGOVT: CUST-GOVT-ISSUED-ID.")
    phone_num_2: str = Field(description="ACSPHN2: CUST-PHONE-NUM-2 as stored.")
    eft_account_id: str = Field(description="ACSEFTC: CUST-EFT-ACCOUNT-ID.")
    primary_holder_ind: str = Field(
        description="ACSPFLG: CUST-PRI-CARD-HOLDER-IND, the primary card holder flag Y or N."
    )


class AccountScreenFields(BaseModel):
    """Every input of the COACTUP map (COACTUPC, CAUP) as raw 3270 text, in map order.

    Each field is limited only to its BMS LENGTH and the code-page-037 repertoire. Type, sign,
    range and format checks run in the service's port of COACTUPC's edits on the text as keyed,
    so a blank field, ``*`` (which COACTUPC treats as blank) and ``$1,234.567`` all reach them.
    Descriptions name the map field and the label COACTUPC puts in its messages.

    In responses the fields carry the display text the service builds: amounts formatted as
    ``+ZZZ,ZZZ,ZZZ.99``, dates split into parts, and the SSN parts unmasked, because CAUP edits
    the SSN. ``GET /accounts/{acctId}/edit`` and ``PUT /accounts/{acctId}`` are the only
    endpoints that return a full SSN.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        description=(
            "ACCTSID: Account Number (ACCT-ID), the key. Editable only before details are "
            "fetched and after a failed save."
        ),
    )
    active_status: ScreenText(1) = Field(
        default="", description="ACSTTUS: Account Status (ACCT-ACTIVE-STATUS), Y or N."
    )
    open_year: ScreenText(4) = Field(
        default="", description="OPNYEAR: Open Date (ACCT-OPEN-DATE) year, CCYY."
    )
    open_month: ScreenText(2) = Field(
        default="", description="OPNMON: Open Date (ACCT-OPEN-DATE) month, MM."
    )
    open_day: ScreenText(2) = Field(
        default="", description="OPNDAY: Open Date (ACCT-OPEN-DATE) day, DD."
    )
    credit_limit: ScreenText(15) = Field(
        default="",
        description=(
            "ACRDLIM: Credit Limit (ACCT-CREDIT-LIMIT), signed amount text checked by "
            "TEST-NUMVAL-C."
        ),
    )
    expiration_year: ScreenText(4) = Field(
        default="", description="EXPYEAR: Expiry Date (ACCT-EXPIRAION-DATE) year, CCYY."
    )
    expiration_month: ScreenText(2) = Field(
        default="", description="EXPMON: Expiry Date (ACCT-EXPIRAION-DATE) month, MM."
    )
    expiration_day: ScreenText(2) = Field(
        default="", description="EXPDAY: Expiry Date (ACCT-EXPIRAION-DATE) day, DD."
    )
    cash_credit_limit: ScreenText(15) = Field(
        default="",
        description=(
            "ACSHLIM: Cash Credit Limit (ACCT-CASH-CREDIT-LIMIT), signed amount text checked "
            "by TEST-NUMVAL-C."
        ),
    )
    reissue_year: ScreenText(4) = Field(
        default="", description="RISYEAR: Reissue Date (ACCT-REISSUE-DATE) year, CCYY."
    )
    reissue_month: ScreenText(2) = Field(
        default="", description="RISMON: Reissue Date (ACCT-REISSUE-DATE) month, MM."
    )
    reissue_day: ScreenText(2) = Field(
        default="", description="RISDAY: Reissue Date (ACCT-REISSUE-DATE) day, DD."
    )
    curr_balance: ScreenText(15) = Field(
        default="",
        description=(
            "ACURBAL: Current Balance (ACCT-CURR-BAL), signed amount text checked by TEST-NUMVAL-C."
        ),
    )
    curr_cycle_credit: ScreenText(15) = Field(
        default="",
        description=(
            "ACRCYCR: Current Cycle Credit Limit (ACCT-CURR-CYC-CREDIT), signed amount text "
            "checked by TEST-NUMVAL-C."
        ),
    )
    group_id: ScreenText(10) = Field(
        default="",
        description=(
            "AADDGRP: Account Group (ACCT-GROUP-ID). COACTUPC compares it for change but "
            "applies no field edit."
        ),
    )
    curr_cycle_debit: ScreenText(15) = Field(
        default="",
        description=(
            "ACRCYDB: Current Cycle Debit Limit (ACCT-CURR-CYC-DEBIT), signed amount text "
            "checked by TEST-NUMVAL-C."
        ),
    )
    cust_id: ScreenText(9) = Field(
        default="",
        description="ACSTNUM: Customer id (CUST-ID), protected in every screen state.",
    )
    ssn_part_1: ScreenText(3) = Field(
        default="", description="ACTSSN1: SSN: First 3 chars (CUST-SSN positions 1-3)."
    )
    ssn_part_2: ScreenText(2) = Field(
        default="", description="ACTSSN2: SSN 4th & 5th chars (CUST-SSN positions 4-5)."
    )
    ssn_part_3: ScreenText(4) = Field(
        default="", description="ACTSSN3: SSN Last 4 chars (CUST-SSN positions 6-9)."
    )
    dob_year: ScreenText(4) = Field(
        default="", description="DOBYEAR: Date of Birth (CUST-DOB-YYYY-MM-DD) year, CCYY."
    )
    dob_month: ScreenText(2) = Field(
        default="", description="DOBMON: Date of Birth (CUST-DOB-YYYY-MM-DD) month, MM."
    )
    dob_day: ScreenText(2) = Field(
        default="", description="DOBDAY: Date of Birth (CUST-DOB-YYYY-MM-DD) day, DD."
    )
    fico_credit_score: ScreenText(3) = Field(
        default="",
        description="ACSTFCO: FICO Score (CUST-FICO-CREDIT-SCORE), 3 digits from 300 to 850.",
    )
    first_name: ScreenText(25) = Field(
        default="", description="ACSFNAM: First Name (CUST-FIRST-NAME)."
    )
    middle_name: ScreenText(25) = Field(
        default="", description="ACSMNAM: Middle Name (CUST-MIDDLE-NAME), optional."
    )
    last_name: ScreenText(25) = Field(
        default="", description="ACSLNAM: Last Name (CUST-LAST-NAME)."
    )
    addr_line_1: ScreenText(50) = Field(
        default="", description="ACSADL1: Address Line 1 (CUST-ADDR-LINE-1)."
    )
    addr_state_cd: ScreenText(2) = Field(
        default="", description="ACSSTTE: State (CUST-ADDR-STATE-CD), a US state code."
    )
    addr_line_2: ScreenText(50) = Field(
        default="",
        description=(
            "ACSADL2: address line 2 (CUST-ADDR-LINE-2). COACTUPC compares it for change but "
            "applies no field edit."
        ),
    )
    addr_zip: ScreenText(5) = Field(
        default="",
        description=(
            "ACSZIPC: Zip (CUST-ADDR-ZIP). The map holds the first 5 characters of the 10-byte "
            "field, as the legacy MOVE into the map truncates."
        ),
    )
    addr_line_3: ScreenText(50) = Field(
        default="",
        description="ACSCITY: City, which COACTUPC keeps in CUST-ADDR-LINE-3.",
    )
    addr_country_cd: ScreenText(3) = Field(
        default="",
        description="ACSCTRY: Country (CUST-ADDR-COUNTRY-CD), protected in every screen state.",
    )
    phone_num_1_area: ScreenText(3) = Field(
        default="",
        description="ACSPH1A: Phone Number 1 area code (CUST-PHONE-NUM-1 positions 2-4).",
    )
    phone_num_1_prefix: ScreenText(3) = Field(
        default="",
        description="ACSPH1B: Phone Number 1 prefix code (CUST-PHONE-NUM-1 positions 6-8).",
    )
    phone_num_1_line: ScreenText(4) = Field(
        default="",
        description=(
            "ACSPH1C: Phone Number 1 line number code (CUST-PHONE-NUM-1 positions 10-13)."
        ),
    )
    govt_issued_id: ScreenText(20) = Field(
        default="",
        description=(
            "ACSGOVT: Government Issued Id Ref (CUST-GOVT-ISSUED-ID). COACTUPC compares it for "
            "change but applies no field edit."
        ),
    )
    phone_num_2_area: ScreenText(3) = Field(
        default="",
        description="ACSPH2A: Phone Number 2 area code (CUST-PHONE-NUM-2 positions 2-4).",
    )
    phone_num_2_prefix: ScreenText(3) = Field(
        default="",
        description="ACSPH2B: Phone Number 2 prefix code (CUST-PHONE-NUM-2 positions 6-8).",
    )
    phone_num_2_line: ScreenText(4) = Field(
        default="",
        description=(
            "ACSPH2C: Phone Number 2 line number code (CUST-PHONE-NUM-2 positions 10-13)."
        ),
    )
    eft_account_id: ScreenText(10) = Field(
        default="", description="ACSEFTC: EFT Account Id (CUST-EFT-ACCOUNT-ID), 10 digits."
    )
    primary_holder_ind: ScreenText(1) = Field(
        default="",
        description="ACSPFLG: Primary Card Holder (CUST-PRI-CARD-HOLDER-IND), Y or N.",
    )


class AccountScreen(ScreenFeedback):
    """The COACTUP screen of COACTUPC (CAUP), after the edit fetch and after every update.

    It is the 200 body; a 400, 404, 409, 423 or 503 outcome sends the same screen back in an
    ``AccountScreenErrorResponse``, so the page renders each field editable or read-only from
    ``state``, as the COACTUP attribute bytes did, whatever the status. ``field_errors`` lists
    every failing field in source edit order (all-field mode) and ``cursor_field`` the first of
    them.
    """

    fields: AccountScreenFields = Field(
        default_factory=AccountScreenFields,
        description="The COACTUP fields to display, as the service formatted them.",
    )
    state: UpdateScreenState = Field(
        description="The screen state after the request, replacing ACUP-CHANGE-ACTION."
    )
    screen_token: str = Field(
        default="",
        description=(
            "Encrypted replacement of the COACTUPC COMMAREA: the state, the account key, the "
            "fetched before-image and, in CHANGES_OK_NOT_CONFIRMED, the validated new values. "
            "Send it back unchanged with the next request."
        ),
    )


class AccountScreenErrorResponse(LegacyErrorResponse):
    """The error body of a CAUP outcome (COACTUPC), with the COACTUP screen it sends back.

    Every 400, 404, 409, 423 and 503 of ``GET /accounts/{acctId}/edit`` and
    ``PUT /accounts/{acctId}`` is built by ``coactupc.RaiseScreenError``: the four standard keys
    of ``LegacyErrorResponse`` plus the screen's ``fields``, ``state`` and ``screen_token``,
    typed as ``AccountScreen`` types them, so the page keeps the state machine running on the
    error. The session and Origin refusals (401, 403) carry the four keys alone and are
    declared as ``LegacyErrorResponse``. The screen keys have defaults, the empty
    DETAILS_NOT_FETCHED key screen with no token, only so that a four-key body validates too;
    none of them is ever null.
    """

    fields: AccountScreenFields = Field(
        default_factory=AccountScreenFields,
        description="The COACTUP fields shown with the message, as the service formatted them.",
    )
    state: UpdateScreenState = Field(
        default=UpdateScreenState.DETAILS_NOT_FETCHED,
        description=(
            "The screen state after the request, replacing ACUP-CHANGE-ACTION: for example "
            "CHANGES_NOT_OK after a field edit failure, SHOW_DETAILS with the fresh records "
            "after 409, CHANGES_FAILED after 423 or the 503 rewrite failure."
        ),
    )
    screen_token: str = Field(
        default="",
        description=(
            "The new encrypted screen state, replacing the COACTUPC COMMAREA; send it back "
            "unchanged with the next request."
        ),
    )


class AccountUpdateRequest(BaseModel):
    """A key press on the COACTUP screen of COACTUPC (CAUP), sent to ``PUT /accounts/{acctId}``.

    The service reads from ``fields`` only the fields the token's state leaves editable and
    takes every protected value from ``screen_token``, as a protected 3270 field returns only
    what was sent to it. The path ``acctId`` and ``fields.acct_id`` both reach the service,
    which decides per state which key applies. A ``screen_token`` issued for another account
    than the path ``acctId`` is treated as DETAILS_NOT_FETCHED.
    """

    action: UpdateAction = Field(
        default=UpdateAction.ENTER,
        description=(
            "ENTER (enter), F5 (save) or F12 (cancel). An action the state does not allow is "
            "processed as enter."
        ),
    )
    screen_token: OpaqueToken(SCREEN_TOKEN_MAX_LENGTH) = Field(
        default="",
        description=(
            "The screen_token of the previous AccountScreen; empty on the first request. A "
            "forged or expired token, or one issued for another account than the path acctId, "
            "is treated as DETAILS_NOT_FETCHED, restarting the flow."
        ),
    )
    fields: AccountScreenFields = Field(
        default_factory=AccountScreenFields,
        description=(
            "The keyed COACTUP fields. Only those the token's state leaves editable are read; "
            "protected values come from the token."
        ),
    )
