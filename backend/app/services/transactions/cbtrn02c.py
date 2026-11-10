"""Replaces: CBTRN02C (POSTTRAN), copybooks CVTRA06Y, CVTRA05Y, CVACT01Y, CVACT03Y, CVTRA01Y

The posting rules of cbl/CBTRN02C.cbl: 1500-VALIDATE-TRAN with 1500-A-LOOKUP-XREF and
1500-B-LOOKUP-ACCT, 2000-POST-TRANSACTION with 2700-UPDATE-TCATBAL (2700-A-CREATE-TCATBAL-REC,
2700-B-UPDATE-TCATBAL-REC), 2800-UPDATE-ACCOUNT-REC and 2900-WRITE-TRANSACTION-FILE, plus
Z-GET-DB2-FORMAT-TIMESTAMP and the WS-VALIDATION-TRAILER layout. The module is pure: it reads no
settings and performs no I/O. batch/jobs/posttran.py performs every read and write, in
record_seq order, committing each write as it is made.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from app.utils.decimal_utils import COBOL_CONTEXT, StoreInto
from app.utils.record_layouts import (
    BLANK_DATE,
    DATE_LENGTH,
    MICROSECONDS_PER_HUNDREDTH,
    TIMESTAMP_FORMAT_DB2,
    EncodeTimestamp,
)


@dataclass(frozen=True)
class RejectOutcome:
    """WS-VALIDATION-FAIL-REASON 9(04) and WS-VALIDATION-FAIL-REASON-DESC X(76) (lines 180-182)."""

    reasonCode: int
    reasonDescription: str


# 1500-A-LOOKUP-XREF, READ XREF-FILE INVALID KEY (lines 384-387).
REJECT_INVALID_CARD = RejectOutcome(100, "INVALID CARD NUMBER FOUND")
# 1500-B-LOOKUP-ACCT, READ ACCOUNT-FILE INVALID KEY (lines 396-399).
REJECT_ACCOUNT_NOT_FOUND = RejectOutcome(101, "ACCOUNT RECORD NOT FOUND")
# ACCT-CREDIT-LIMIT below WS-TEMP-BAL (lines 407-412).
REJECT_OVERLIMIT = RejectOutcome(102, "OVERLIMIT TRANSACTION")
# ACCT-EXPIRAION-DATE below DALYTRAN-ORIG-TS (1:10) (lines 414-419); tested after the credit
# limit, so it overwrites a 102.
REJECT_AFTER_EXPIRATION = RejectOutcome(103, "TRANSACTION RECEIVED AFTER ACCT EXPIRATION")
# 2800-UPDATE-ACCOUNT-REC, REWRITE ACCOUNT-FILE INVALID KEY (lines 554-558). This is not a
# validation reject: the reject count and the DALYREJS write were decided before posting (lines
# 211-215). When the caller's account UPDATE matches no row it records this outcome, still
# inserts the transaction (2900-WRITE-TRANSACTION-FILE runs unconditionally, line 442), writes no
# DALYREJS record and counts no reject. The row lock makes it unreachable in practice, so it is
# tested with a stubbed rewrite.
ACCOUNT_REWRITE_NOT_FOUND = RejectOutcome(109, "ACCOUNT RECORD NOT FOUND")

# End-of-run DISPLAY labels (lines 227-228), each followed by its PIC 9(09) counter.
PROCESSED_COUNT_LABEL = "TRANSACTIONS PROCESSED :"
REJECTED_COUNT_LABEL = "TRANSACTIONS REJECTED  :"
# MOVE 4 TO RETURN-CODE when WS-REJECT-COUNT > 0 (lines 229-231).
REJECTS_RETURN_CODE = 4

# WS-VALIDATION-TRAILER (lines 180-182): the reason 9(04), then its description X(76).
VALIDATION_TRAILER_LENGTH = 80
REASON_CODE_DIGITS = 4
REASON_DESCRIPTION_LENGTH = 76

# TRAN-PROC-TS takes the DB2 form YYYY-MM-DD-HH.MM.SS.cc0000 (Z-GET-DB2-FORMAT-TIMESTAMP).
PROC_TS_FORMAT = TIMESTAMP_FORMAT_DB2

# S9(09)V99 receivers: WS-TEMP-BAL (line 187), TRAN-CAT-BAL (CVTRA01Y), TRAN-AMT (CVTRA05Y).
TRAN_AMOUNT_DIGITS = (9, 2)
# S9(10)V99 receivers: ACCT-CURR-BAL, ACCT-CURR-CYC-CREDIT, ACCT-CURR-CYC-DEBIT (CVACT01Y).
ACCOUNT_AMOUNT_DIGITS = (10, 2)
# TRAN-CAT-BAL after INITIALIZE TRAN-CAT-BAL-RECORD in 2700-A-CREATE-TCATBAL-REC (line 504).
INITIAL_CATEGORY_BALANCE = Decimal("0.00")

# IF DALYTRAN-AMT >= 0 (line 548) picks the cycle field the amount is added to.
CYCLE_CREDIT_COLUMN = "curr_cycle_credit"
CYCLE_DEBIT_COLUMN = "curr_cycle_debit"

# Fields moved unchanged from DALYTRAN-RECORD to TRAN-RECORD (lines 425-436); ORIG-TS keeps its
# stored form code. TRAN-AMT is a numeric MOVE and PROC-TS comes from the clock, so both are
# built separately.
COPIED_TRANSACTION_COLUMNS = (
    "tran_id",
    "tran_type_cd",
    "tran_cat_cd",
    "tran_source",
    "tran_desc",
    "merchant_id",
    "merchant_name",
    "merchant_city",
    "merchant_zip",
    "card_num",
    "orig_ts",
    "orig_ts_format",
)

# Alphanumeric comparisons follow the EBCDIC collating sequence of the legacy program.
EBCDIC_CODE_PAGE = "cp037"


@dataclass(frozen=True)
class PostingInput:
    """The records CBTRN02C holds while it validates and posts one DALYTRAN-RECORD.

    Each mapping is keyed by its table's snake_case column names, and a record that was not found
    is None:
    - staged: one transactions.daily_transactions row (CVTRA06Y);
    - xref: the xref.card_xref row for the staged card_num (CVACT03Y); acct_id is used;
    - account: the accounts.accounts row for xref acct_id (CVACT01Y); acct_id, curr_balance,
      credit_limit, curr_cycle_credit, curr_cycle_debit and expiration_date are used;
    - categoryBalance: the transactions.transaction_category_balances row for the posting key
      (CVTRA01Y); balance is used. Validation ignores it.
    now is the batch clock, a datetime without a time zone.
    """

    staged: Mapping[str, Any]
    xref: Mapping[str, Any] | None
    account: Mapping[str, Any] | None
    categoryBalance: Mapping[str, Any] | None
    now: datetime


@dataclass(frozen=True)
class PostingEffects:
    """The writes of 2000-POST-TRANSACTION, which the caller applies in this order.

    - categoryBalance: the full transaction_category_balances row (acct_id, tran_type_cd,
      tran_cat_cd, balance), inserted when isNewCategoryBalance (2700-A-CREATE-TCATBAL-REC) and
      updated by its key otherwise (2700-B-UPDATE-TCATBAL-REC);
    - account: acct_id and the absolute curr_balance, curr_cycle_credit and curr_cycle_debit to
      rewrite (2800-UPDATE-ACCOUNT-REC);
    - transaction: the transactions.transactions insert values, without the generated
      tran_id_sort (2900-WRITE-TRANSACTION-FILE).
    """

    categoryBalance: dict[str, Any]
    isNewCategoryBalance: bool
    account: dict[str, Any]
    transaction: dict[str, Any]


def ValidateDailyTransaction(postingInput: PostingInput) -> RejectOutcome | None:
    """Port of 1500-VALIDATE-TRAN (lines 370-378): the reject outcome, or None to post.

    The account is examined only when the cross-reference was found (line 372). A reject is a
    return value, never an exception.
    """
    if postingInput.xref is None:
        return REJECT_INVALID_CARD
    if postingInput.account is None:
        return REJECT_ACCOUNT_NOT_FOUND
    return _CheckFoundAccount(postingInput.staged, postingInput.account)


def ApplyPosting(postingInput: PostingInput) -> PostingEffects:
    """Port of 2000-POST-TRANSACTION (lines 424-444) for a record that passed validation.

    The caller writes the category balance (2700), then rewrites the account (2800), then inserts
    the transaction (2900), committing each. When the account rewrite matches no row, the caller
    records ACCOUNT_REWRITE_NOT_FOUND (109), still inserts the transaction, writes no DALYREJS
    record and counts no reject.
    """
    if postingInput.xref is None or postingInput.account is None:
        raise ValueError("ApplyPosting requires a validated record")
    return PostingEffects(
        categoryBalance=_BuildCategoryBalance(postingInput),
        isNewCategoryBalance=postingInput.categoryBalance is None,
        account=_BuildAccountUpdate(postingInput),
        transaction=_BuildTransactionRow(postingInput),
    )


def BuildValidationTrailer(outcome: RejectOutcome) -> str:
    """WS-VALIDATION-TRAILER (lines 180-182), the 80 characters that follow a DALYREJS record.

    The reason is moved into PIC 9(04); the description is left-justified in X(76), padded with
    spaces or cut at the right, as an alphanumeric MOVE does.
    """
    reasonCode = StoreInto(outcome.reasonCode, REASON_CODE_DIGITS, 0, signed=False)
    reasonText = f"{int(reasonCode):0{REASON_CODE_DIGITS}d}"
    descriptionText = outcome.reasonDescription[:REASON_DESCRIPTION_LENGTH]
    return reasonText + descriptionText.ljust(REASON_DESCRIPTION_LENGTH)


def _CheckFoundAccount(
    staged: Mapping[str, Any], account: Mapping[str, Any]
) -> RejectOutcome | None:
    """Port of the NOT INVALID KEY branch of 1500-B-LOOKUP-ACCT (lines 400-420).

    Both tests always run. Each rejects in the ELSE branch of a >= test, and the expiry test comes
    second, so a 103 overwrites a 102.
    """
    outcome = None
    if account["credit_limit"] < _TemporaryBalance(staged, account):
        outcome = REJECT_OVERLIMIT
    if _CollatingKey(_ExpirationText(account)) < _CollatingKey(_OrigDateText(staged)):
        outcome = REJECT_AFTER_EXPIRATION
    return outcome


def _TemporaryBalance(staged: Mapping[str, Any], account: Mapping[str, Any]) -> Decimal:
    """COMPUTE WS-TEMP-BAL = ACCT-CURR-CYC-CREDIT - ACCT-CURR-CYC-DEBIT + DALYTRAN-AMT (403-405).

    The receiver is S9(09)V99 (line 187), so an integer part beyond nine digits is dropped before
    the credit-limit comparison uses it.
    """
    with localcontext(COBOL_CONTEXT):
        cycleNet = account[CYCLE_CREDIT_COLUMN] - account[CYCLE_DEBIT_COLUMN]
        testedBalance = cycleNet + staged["tran_amt"]
    return StoreInto(testedBalance, *TRAN_AMOUNT_DIGITS)


def _ExpirationText(account: Mapping[str, Any]) -> str:
    """ACCT-EXPIRAION-DATE X(10) as text: YYYY-MM-DD, or 10 spaces for a blank (NULL) date."""
    expirationDate = account["expiration_date"]
    if expirationDate is None:
        return BLANK_DATE
    return f"{expirationDate.year:04d}-{expirationDate.month:02d}-{expirationDate.day:02d}"


def _OrigDateText(staged: Mapping[str, Any]) -> str:
    """DALYTRAN-ORIG-TS (1:10): the first 10 characters of the 26-character timestamp text."""
    return EncodeTimestamp(staged["orig_ts"], staged["orig_ts_format"])[:DATE_LENGTH]


def _CollatingKey(text: str) -> bytes:
    """The EBCDIC bytes of text, whose byte order is the COBOL alphanumeric comparison order."""
    return text.encode(EBCDIC_CODE_PAGE)


def _BuildCategoryBalance(postingInput: PostingInput) -> dict[str, Any]:
    """Port of 2700-UPDATE-TCATBAL (lines 467-542): the category balance after the amount.

    The key is XREF-ACCT-ID, DALYTRAN-TYPE-CD and DALYTRAN-CAT-CD (lines 469-471). A missing row
    starts from the initialised zero balance (2700-A-CREATE-TCATBAL-REC, lines 504-508); an
    existing row adds to its balance (2700-B-UPDATE-TCATBAL-REC, line 527).
    """
    staged = postingInput.staged
    existingRow = postingInput.categoryBalance
    startingBalance = INITIAL_CATEGORY_BALANCE if existingRow is None else existingRow["balance"]
    return {
        "acct_id": postingInput.xref["acct_id"],
        "tran_type_cd": staged["tran_type_cd"],
        "tran_cat_cd": staged["tran_cat_cd"],
        "balance": _AddInto(startingBalance, staged["tran_amt"], TRAN_AMOUNT_DIGITS),
    }


def _BuildAccountUpdate(postingInput: PostingInput) -> dict[str, Any]:
    """Port of 2800-UPDATE-ACCOUNT-REC (lines 545-552): the account money fields to rewrite.

    The amount is added to ACCT-CURR-BAL, and to ACCT-CURR-CYC-CREDIT when it is zero or more,
    else to ACCT-CURR-CYC-DEBIT, so a negative amount makes the debit more negative.
    """
    account = postingInput.account
    amount = postingInput.staged["tran_amt"]
    cycleColumn = CYCLE_CREDIT_COLUMN if amount >= 0 else CYCLE_DEBIT_COLUMN
    accountUpdate = {
        "acct_id": account["acct_id"],
        "curr_balance": _AddInto(account["curr_balance"], amount, ACCOUNT_AMOUNT_DIGITS),
        CYCLE_CREDIT_COLUMN: account[CYCLE_CREDIT_COLUMN],
        CYCLE_DEBIT_COLUMN: account[CYCLE_DEBIT_COLUMN],
    }
    accountUpdate[cycleColumn] = _AddInto(account[cycleColumn], amount, ACCOUNT_AMOUNT_DIGITS)
    return accountUpdate


def _BuildTransactionRow(postingInput: PostingInput) -> dict[str, Any]:
    """Port of the TRAN-RECORD moves of 2000-POST-TRANSACTION (lines 425-438).

    ORIG-TS and its form code are copied unchanged from the daily record (line 436); PROC-TS is
    the clock in the DB2 form of Z-GET-DB2-FORMAT-TIMESTAMP (lines 437-438).
    """
    staged = postingInput.staged
    transactionRow = {column: staged[column] for column in COPIED_TRANSACTION_COLUMNS}
    transactionRow["tran_amt"] = StoreInto(staged["tran_amt"], *TRAN_AMOUNT_DIGITS)
    transactionRow["proc_ts"] = _HundredthsOf(postingInput.now)
    transactionRow["proc_ts_format"] = PROC_TS_FORMAT
    return transactionRow


def _AddInto(receiverValue: Decimal, amount: Decimal, receiverDigits: tuple[int, int]) -> Decimal:
    """ADD amount TO a receiver of PIC S9(int)V9(dec), truncated as COBOL stores it."""
    with localcontext(COBOL_CONTEXT):
        total = receiverValue + amount
    return StoreInto(total, *receiverDigits)


def _HundredthsOf(now: datetime) -> datetime:
    """Port of Z-GET-DB2-FORMAT-TIMESTAMP (lines 692-705): the clock in whole hundredths.

    FUNCTION CURRENT-DATE carries hundredths of a second (COB-MIL), and the DB2 text ends in the
    literal '0000', so the microseconds below a hundredth are dropped.
    """
    if not isinstance(now, datetime):
        raise TypeError(f"the batch clock must be a datetime, not {type(now).__name__}")
    if now.tzinfo is not None:
        raise ValueError("the batch clock must be a datetime without a time zone")
    subHundredth = now.microsecond % MICROSECONDS_PER_HUNDREDTH
    return now.replace(microsecond=now.microsecond - subHundredth)
