"""Request and response models of CB00 bill payment, ``POST /api/v1/billpay``.

Replaces: COBIL00C (CB00), BMS map COBIL00 (``cpy-bms/COBIL00.CPY``), copybook CVACT01Y.

COBIL00C edits in first-failure mode: the first failing check ends validation, so a response
carries one ``message`` and one ``cursor_field``. The two screen inputs arrive as raw
``ScreenText`` and the service runs the program's edits on them as keyed.
"""

from pydantic import BaseModel, Field

from app.schemas.common import MoneyAmount, ScreenText


class BillPayRequest(BaseModel):
    """The unprotected fields of map COBIL00 sent on ENTER to COBIL00C (CB00).

    Both fields are raw screen text limited to their BMS LENGTH and the code-page-037
    repertoire, so a blank account and every confirm value reach the program's edits.
    """

    acct_id: ScreenText(11) = Field(
        default="",
        description=(
            "ACTIDIN, LENGTH 11: the account ID as keyed. A blank value gives "
            "'Acct ID can NOT be empty...'; an unknown one gives 'Account ID NOT found...'."
        ),
    )
    confirm: ScreenText(1) = Field(
        default="",
        description=(
            "CONFIRM, LENGTH 1: Y or y pays the balance, N or n clears the screen, blank asks "
            "'Confirm to make a bill payment...', any other character gives "
            "'Invalid value. Valid values are (Y/N)...'."
        ),
    )


class BillPayResponse(BaseModel):
    """The COBIL00 screen COBIL00C (CB00) sends back with HTTP 200.

    It is the confirmation prompt with the current balance, the payment confirmation with
    the new transaction ID, or the cleared screen after ``N``. Failures use the legacy error
    body of ``app.schemas.common.LegacyErrorResponse``.
    """

    current_balance: MoneyAmount(10) | None = Field(
        default=None,
        description=(
            "CURBAL: ACCT-CURR-BAL (S9(10)V99) as a decimal string, read before the payment; "
            "null when no account was read or the program cleared the screen."
        ),
    )
    message: str = Field(
        description=(
            "The ERRMSG line: the program's literal verbatim, for example "
            "'Payment successful.  Your Transaction ID is 0000000000000001.'; empty after N."
        ),
    )
    cursor_field: str | None = Field(
        default=None,
        description="The field that receives focus: acct_id or confirm.",
    )
    tran_id: str | None = Field(
        default=None,
        description="TRAN-ID of the BILL PAYMENT - ONLINE transaction, set once it is written.",
    )
