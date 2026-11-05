"""Shared request/response building blocks for the CardDemo API.

Replaces the BMS ERRMSG/INFOMSG message lines of the 17 maps (``cpy-bms/*.CPY``), the
CSMSG01Y/CSMSG02Y message areas and the COMMAREA keys of ``cpy/COCOM01Y.cpy``.

Every screen field is raw text limited only by its BMS LENGTH and the code-page-037 printable
repertoire (``ScreenText``). A request a 3270 terminal could never send is rejected with 422,
while every accept and reject path of the legacy edits stays reachable in the services.
"""

import functools
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    StringConstraints,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

CP037_PRINTABLE: frozenset[str] = frozenset(
    character for character in bytes(range(0x40, 0xFF)).decode("cp037") if character.isprintable()
)
# Documentation only: published in the OpenAPI document, never used for validation.
CP037_JSON_PATTERN = r"^[\u0020-\u007e\u00a1-\u00ac\u00ae-\u00ff]*$"

# Structural limits of opaque server-issued strings; their decoders judge the content.
CARD_REF_MAX_LENGTH = 1024
SCREEN_TOKEN_MAX_LENGTH = 16384
START_KEY_MAX_LENGTH = 1024


@dataclass(frozen=True)
class Cp037JsonPattern:
    """Publishes ``CP037_JSON_PATTERN`` in the JSON schema of the string type it annotates.

    It documents the 3270 repertoire for API clients and for the schemathesis contract tests.
    It is a JSON-schema hook rather than an inner ``Field(json_schema_extra=...)`` because
    FastAPI keeps only the outer ``Path()`` or ``Query()`` of a parameter and drops an inner
    ``Field``, which would leave screen-text path and query parameters without the pattern.
    """

    def __get_pydantic_json_schema__(
        self, coreSchema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Return the schema the handler builds, with the documentation pattern added."""
        jsonSchema = handler(coreSchema)
        jsonSchema["pattern"] = CP037_JSON_PATTERN
        return jsonSchema


def RequireCp037Printable(value: str) -> str:
    """Return ``value`` unchanged when every character is one a 3270 terminal can send.

    The repertoire is ``CP037_PRINTABLE``. The text is never stripped, case-folded or
    normalised, because the services run each program's edits on it exactly as keyed.

    Raises:
        ValueError: ``value`` holds a character outside the repertoire. Pydantic reports it
            and FastAPI answers 422. The message never echoes the input, which may be a
            password, card number or SSN.
    """
    if not CP037_PRINTABLE.issuperset(value):
        raise ValueError("text contains characters a 3270 terminal cannot send (code page 037)")
    return value


def ScreenText(maxLength: int) -> Any:
    """Raw 3270 field text: at most ``maxLength`` characters (the BMS LENGTH) from the
    code-page-037 printable repertoire.

    The only type a screen field may use, in bodies, path and query parameters alike. The
    default is declared at the use site, for example
    ``acct_id: ScreenText(11) = Field(default="", description=...)``. Type, sign, range and
    format checks belong to the services, which receive the text as keyed: ``$1,234.567``,
    ``""`` for a blank field and ``*`` all reach the program's edits.
    """
    return Annotated[
        str,
        StringConstraints(max_length=maxLength),
        AfterValidator(RequireCp037Printable),
        Cp037JsonPattern(),
    ]


def RequireCp037PrintableScreenText(screenLength: int, value: str) -> str:
    """Return ``value`` unchanged, applying ``RequireCp037Printable`` only when it is screen text.

    A value of at most ``screenLength`` characters (the BMS LENGTH) is screen text. A longer
    one is an opaque token, whose decoder judges its characters.

    Raises:
        ValueError: screen text holds a character outside ``CP037_PRINTABLE``; FastAPI
            answers 422 without echoing the input.
    """
    if len(value) <= screenLength:
        return RequireCp037Printable(value)
    return value


def ScreenTextOrToken(screenLength: int, tokenLength: int) -> Any:
    """Raw 3270 field text of at most ``screenLength`` characters, or a longer opaque token of
    at most ``tokenLength`` characters, told apart by length alone.

    It serves a value that is either, as the CCDL and CCUP ``{cardNum}`` is the CARDSID text as
    keyed or a ``card_ref`` (``cocrdslc.DecodeCardKey`` draws the same line). On the two GET
    routes the query can declare a shorter value a token too (``isCardRef``), which no path type
    sees, so those routes apply this type only to an undeclared value
    (``app.api.v1.cards.RequirePrintableTypedCardKey``). Screen text keeps the ``ScreenText``
    repertoire check. A token is checked for length only, as ``OpaqueToken`` is, so a garbled
    ``card_ref`` reaches its decoder and gives ``Did not find cards for this search
    condition``, never a 422. The published pattern documents the repertoire, which every
    server-issued token also keeps to.
    """
    return Annotated[
        str,
        StringConstraints(max_length=tokenLength),
        AfterValidator(functools.partial(RequireCp037PrintableScreenText, screenLength)),
        Cp037JsonPattern(),
    ]


def OpaqueToken(maxLength: int) -> Any:
    """Opaque server-issued token text, length only: ``card_ref``, ``screen_token`` and the
    CCLI ``startKey``, which is a ``card_ref`` token.

    It has no pattern and no repertoire check: a forged or garbled ``screen_token`` must reach
    its decoder and restart the CAUP or CCUP flow, and a bad ``card_ref`` must give
    ``Did not find cards for this search condition``, never a 422. Raw record keys sent back
    for paging, such as the CT00 TRAN-ID and the CU00 user ID, are ``ScreenText`` instead.
    """
    return Annotated[str, StringConstraints(max_length=maxLength)]


def MoneyAmount(intDigits: int) -> Any:
    """Response-only signed amount with ``intDigits`` integer and 2 decimal digits (S9(n)V99).

    ``MoneyAmount(10)`` serves the ``S9(10)V99`` account amounts (``NUMERIC(12,2)``) and
    ``MoneyAmount(9)`` the ``S9(09)V99`` transaction amounts (``NUMERIC(11,2)``). Pydantic
    serialises ``Decimal`` as a JSON string, so no amount passes through a float. Screen
    amounts never use it: they arrive as ``ScreenText``.
    """
    return Annotated[Decimal, Field(max_digits=intDigits + 2, decimal_places=2)]


def RequireUnsignedDigits(value: object) -> object:
    """Return ``value`` unchanged when it is an ``int`` or text of ASCII decimal digits only.

    It guards the ``page`` query parameter of CCLI, CT00 and CU00, which a client sends back as
    the page number it was shown. A non-bool ``int`` passes, because an omitted query value is
    the ``int`` default and services and tests build query models by attribute name. Digit text,
    leading zeros included, is left for the ``int`` validation and the ``ge``/``le`` bounds of
    the field.

    Raises:
        ValueError: ``value`` is a bool, or text holding anything but ``0``-``9`` (a sign, a
            blank, a decimal point, a digit separator, a non-ASCII digit, or nothing at all).
            Pydantic reports it and FastAPI answers 422. The message never echoes the input.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    raise ValueError("page number must be an unsigned integer in decimal digits")


# The page number of a list query (CCLI, CT00 and CU00 ``page``). Query text is the only form a
# query page arrives in, so strict mode, which refuses all text, cannot apply; the digit check
# of RequireUnsignedDigits replaces it, refusing what lax int would coerce (true, " 1 ", "+1",
# "1.0", "1_0"). The JSON schema stays type integer; each use site declares ge and le in Field.
QUERY_PAGE_NUMBER = Annotated[int, BeforeValidator(RequireUnsignedDigits)]


class UpdateAction(StrEnum):
    """The CAUP (COACTUPC) and CCUP (COCRDUPC) keys: ENTER, F5 save and F12 cancel.

    An action the current screen state does not allow is processed as ``enter``, as the
    programs turn an invalid PF key into ENTER. A value outside the enum is a 422.
    """

    ENTER = "enter"
    SAVE = "save"
    CANCEL = "cancel"


class TransactionAddAction(StrEnum):
    """The CT02 (COTRN02C) keys: ENTER adds, F5 copies the last transaction into the form."""

    ENTER = "enter"
    COPY_LAST = "copy_last"


class Direction(StrEnum):
    """The paging keys of CCLI, CT00 and CU00: F8 (READNEXT) and F7 (READPREV).

    In CT00, ``FORWARD`` is the default and also reads the first page when no start key is
    sent. CU00 and CCLI send no direction for ENTER (``users.UserListQuery``,
    ``cards.CardListQuery``).
    """

    FORWARD = "forward"
    BACKWARD = "backward"


class ExportFormat(StrEnum):
    """The CR00 report download formats, replacing the TRANREPT print file."""

    CSV = "csv"
    PDF = "pdf"


class ReportType(StrEnum):
    """The CORPT00 report selector, replacing its MONTHLY, YEARLY and CUSTOM fields.

    ``NONE`` is an empty selection, for which CORPT00C answers
    ``Select a report type to print report...``.
    """

    NONE = ""
    MONTHLY = "monthly"
    YEARLY = "yearly"
    CUSTOM = "custom"


class UpdateScreenState(StrEnum):
    """The CAUP and CCUP screen state, replacing ACUP-CHANGE-ACTION and CCUP-CHANGE-ACTION.

    The legacy values are LOW-VALUES or SPACES (``DETAILS_NOT_FETCHED``), ``S``
    (``SHOW_DETAILS``), ``E`` (``CHANGES_NOT_OK``), ``N`` (``CHANGES_OK_NOT_CONFIRMED``),
    ``C`` (``CHANGES_DONE``), and ``L`` lock error or ``F`` update failed (``CHANGES_FAILED``).
    """

    DETAILS_NOT_FETCHED = "DETAILS_NOT_FETCHED"
    SHOW_DETAILS = "SHOW_DETAILS"
    CHANGES_NOT_OK = "CHANGES_NOT_OK"
    CHANGES_OK_NOT_CONFIRMED = "CHANGES_OK_NOT_CONFIRMED"
    CHANGES_DONE = "CHANGES_DONE"
    CHANGES_FAILED = "CHANGES_FAILED"


class ScreenFeedback(BaseModel):
    """The message lines and cursor of a screen response.

    Replaces the BMS ERRMSG and INFOMSG fields and the ``MOVE -1 TO <field>L`` cursor. The
    names in ``field_errors`` and ``cursor_field`` are the snake_case attribute names of the
    request or screen model fields (for example ``credit_limit``), also for query fields whose
    wire name is camelCase. CR00 (CORPT00C) is the exception: its error bodies, like
    ``reports.TransactionReportResponse``, use the ``ReportQuery`` wire names (``reportType``,
    ``startMm``, ``startDd``, ``startYyyy``, ``endMm``, ``endDd``, ``endYyyy`` and ``confirm``),
    because those query keys are the CORPT00 page's field names. The frontend maps the names to
    its inputs.
    """

    message: str = Field(
        default="",
        description="The ERRMSG line: the program's message literal, verbatim; empty for none.",
    )
    info_message: str = Field(
        default="",
        description=(
            "The INFOMSG line of CAVW, CAUP, CCLI, CCDL and CCUP, verbatim; empty for none."
        ),
    )
    field_errors: list[str] = Field(
        default_factory=list,
        description=(
            "Snake_case attribute names of the failing fields, in source edit order: every "
            "failing field in the all-field edit mode of CAVW, CAUP, CCLI, CCDL and CCUP, "
            "otherwise the single failing field. CR00 names the failing field by its ReportQuery "
            "wire name instead (reportType, startMm, startDd, startYyyy, endMm, endDd, endYyyy "
            "or confirm)."
        ),
    )
    cursor_field: str | None = Field(
        default=None,
        description=(
            "Snake_case attribute name of the field that receives focus, replacing "
            "MOVE -1 TO <field>L; null or empty when the program sets no cursor. CR00 names the "
            "field by its ReportQuery wire name instead (reportType, startMm, startDd, "
            "startYyyy, endMm, endDd, endYyyy or confirm)."
        ),
    )


class LegacyErrorResponse(ScreenFeedback):
    """The error body of every non-2xx legacy outcome.

    The outcomes are 400 edit failure, 401 sign-on or session failure, 403 role refusal,
    404 not found, 409 record changed, 423 record lock, 429 login lockout, and 503 file error
    or batch window. ``app/core/errors.py`` serialises ``LegacyMessageError`` into this shape,
    and routers list it in ``responses=`` so the OpenAPI document declares it.
    """

    message: str = Field(description="The program's message literal for the outcome, verbatim.")


class ValidationErrorDetail(BaseModel):
    """One item of the ``detail`` list of a 422 body: where the request is malformed, and how.

    ``app.core.errors.SanitisedDetail`` keeps only these three keys of FastAPI's validation
    error, so the rejected input, which may be a password, card number or SSN, is never echoed.
    """

    loc: list[str | int] = Field(
        description=(
            "FastAPI's error location: the request part (body, query or path), followed by the "
            "field name, list index or JSON decode position."
        ),
    )
    msg: str = Field(
        description="FastAPI's error message, for example String should have at most 8 characters."
    )
    type: str = Field(
        description="FastAPI's error type, for example string_too_long, enum or json_invalid."
    )


class LegacyValidationErrorResponse(BaseModel):
    """The 422 body of a structurally invalid request, built by HandleRequestValidationError.

    ``app.core.errors.HandleRequestValidationError`` answers every request FastAPI's
    validation rejects with it: malformed or unreadable JSON (not valid UTF-8, or nested too
    deeply), screen text over its BMS LENGTH or outside the code-page-037 repertoire, a value
    outside an enum, or a wrong JSON type. A 3270 terminal could never send such a request, so
    no program edit runs. FastAPI's ``detail`` list sits beside the four standard legacy keys,
    and every body carries all five. ``app.main`` declares this model as the 422 response of
    every operation FastAPI validates.
    """

    detail: list[ValidationErrorDetail] = Field(
        description="FastAPI's validation errors, each holding only loc, msg and type."
    )
    message: str = Field(description="Always empty: no legacy program edit ran.")
    info_message: str = Field(description="Always empty: no legacy program edit ran.")
    field_errors: list[str] = Field(
        description=(
            "The failing fields, de-duplicated in error order: the snake_case attribute of a body "
            "field; for a query or path key parameter, the screen field the program's own errors "
            "name (acctId as acct_id, cardNum as card_num, tranId as tran_id, userId as user_id, "
            "searchTranId as search_tran_id, searchUserId as search_user_id); for any other query "
            "or path parameter, its wire name (for example reportType); empty when no location "
            "names a field, as for malformed JSON."
        ),
    )
    cursor_field: str = Field(
        description="The first name in field_errors, or empty when field_errors is empty."
    )


class PageState(BaseModel):
    """The paging position of a list screen (CCLI, CT00, CU00).

    Replaces the page number and the first and last keys the COMMAREA kept between F7 and F8
    presses. List responses subclass it, so their JSON stays flat.
    """

    page: int = Field(
        default=0,
        ge=0,
        description=(
            "The page number shown (CDEMO-CT00-PAGE-NUM, CDEMO-CU00-PAGE-NUM, WS-CA-SCREEN-NUM)."
        ),
    )
    first_key: str = Field(
        default="",
        description=(
            "Key of the first row shown, sent back as startKey with direction backward (F7)."
        ),
    )
    last_key: str = Field(
        default="",
        description="Key of the last row shown, sent back as startKey with direction forward (F8).",
    )


class QueryModel(BaseModel):
    """Base of the GET query-parameter models, replacing the keys a program read on entry.

    Each subclass field declares an explicit camelCase ``alias``, its wire name (for example
    ``acctId`` or ``startKey``). ``populate_by_name`` lets services and tests build instances
    by the snake_case attribute name as well.
    """

    model_config = ConfigDict(populate_by_name=True)
