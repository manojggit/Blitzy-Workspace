"""Shared request/response building blocks for the CardDemo API.

Replaces the BMS ERRMSG/INFOMSG message lines of the 17 maps (``cpy-bms/*.CPY``), the
CSMSG01Y/CSMSG02Y message areas and the COMMAREA keys of ``cpy/COCOM01Y.cpy``.

Every screen field is raw text limited only by its BMS LENGTH and the code-page-037 printable
repertoire (``ScreenText``). A request a 3270 terminal could never send is rejected with 422,
while every accept and reject path of the legacy edits stays reachable in the services.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
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


def OpaqueToken(maxLength: int) -> Any:
    """Opaque server-issued text (``card_ref``, ``screen_token``, paging keys), length only.

    It has no pattern and no repertoire check: a forged or garbled ``screen_token`` must reach
    its decoder and restart the CAUP or CCUP flow, and a bad ``card_ref`` must give
    ``Did not find cards for this search condition``, never a 422.
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

    ``FORWARD`` also reads the first page when no start key is sent.
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


class MessageResponse(BaseModel):
    """A success body carrying only the program's message literal.

    Replaces a screen sent back with only its ERRMSG line set, for example the COUSR03C
    ``User <id> has been deleted ...`` confirmation.
    """

    message: str = Field(description="The program's success literal, verbatim.")


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
