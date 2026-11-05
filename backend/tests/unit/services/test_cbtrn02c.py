"""Unit tests for app.services.transactions.cbtrn02c, the CBTRN02C posting rules of POSTTRAN.

cbl/CBTRN02C.cbl, run by jcl/POSTTRAN.jcl, validates each DALYTRAN-RECORD (CVTRA06Y) against the
card cross-reference (CVACT03Y) and the account (CVACT01Y), then posts it to the category balance
(CVTRA01Y), the account and TRAN-RECORD (CVTRA05Y). The module is pure, so these tests call it
directly with no database. The 109 path also drives batch/jobs/posttran.py's per-record sequence
(PostAllRecords through InsertTransaction) against a stand-in engine whose account rewrite
matches no row, its staged-record read and validation unit replaced, again with no database.
Expected values are typed from the cited COBOL lines and worked by hand with Decimal literals,
never taken from the module:
- lines 370-399: a missing cross-reference rejects 100 and a missing account 101; the account is
    read only when the cross-reference was found (line 372);
- lines 403-412: WS-TEMP-BAL = ACCT-CURR-CYC-CREDIT - ACCT-CURR-CYC-DEBIT + DALYTRAN-AMT, stored
    in S9(09)V99 (line 187), rejects 102 when ACCT-CREDIT-LIMIT is below it;
- lines 414-419: ACCT-EXPIRAION-DATE below DALYTRAN-ORIG-TS (1:10), a text compare, rejects 103,
    overwriting a 102 set just before;
- lines 211-215: only a validation reason is counted and written to DALYREJS, so the 109 that
    2800-UPDATE-ACCOUNT-REC sets on a REWRITE INVALID KEY (lines 554-558) is neither, and
    2900-WRITE-TRANSACTION-FILE still writes the transaction (lines 441-442) before the next
    record is read;
- lines 425-438: TRAN-RECORD copies the daily fields and ORIG-TS; PROC-TS is the clock in the DB2
    form YYYY-MM-DD-HH.MM.SS.cc0000 (Z-GET-DB2-FORMAT-TIMESTAMP, lines 692-703);
- lines 503-527: TRAN-CAT-BAL S9(09)V99 starts at zero for a new key and adds the amount;
- lines 545-552: ACCT-CURR-BAL, and ACCT-CURR-CYC-CREDIT for an amount >= 0 or else
    ACCT-CURR-CYC-DEBIT, add the amount in S9(10)V99;
- lines 180-182 and 227-231: the DALYREJS trailer is the reason 9(04) and its description X(76);
    the run ends with two DISPLAY counters and RETURN-CODE 4 when any record was rejected.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from batch.jobs import posttran
from batch.orchestration.job_context import JobResult
from sqlalchemy import Insert, Update

from app.services.transactions.cbtrn02c import (
    ACCOUNT_REWRITE_NOT_FOUND,
    PROCESSED_COUNT_LABEL,
    REJECT_ACCOUNT_NOT_FOUND,
    REJECT_AFTER_EXPIRATION,
    REJECT_INVALID_CARD,
    REJECT_OVERLIMIT,
    REJECTED_COUNT_LABEL,
    REJECTS_RETURN_CODE,
    ApplyPosting,
    BuildValidationTrailer,
    PostingEffects,
    PostingInput,
    RejectOutcome,
    ValidateDailyTransaction,
)
from app.utils.record_layouts import EncodeTimestamp

# Card 2988091353094312 belongs to customer 000000004 and account 00000000004
# (data/ASCII/cardxref.txt record 13).
CARD_NUMBER = "2988091353094312"
CUSTOMER_ID = "000000004"
ACCOUNT_ID = "00000000004"
STAGED_TRAN_ID = "0000000000683580"
# DALYTRAN-ORIG-TS of every sample record: 2022-06-10 19:27:53.000000, the "S" form.
STAGED_ORIG_TS = datetime(2022, 6, 10, 19, 27, 53)
BATCH_NOW = datetime(2022, 7, 18, 0, 0, 0)

# One transactions.daily_transactions row (CVTRA06Y) with the sample record's descriptive fields.
STAGED_DEFAULTS = {
    "record_seq": 1,
    "tran_id": STAGED_TRAN_ID,
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "Purchase at Abshire-Lowe",
    "tran_amt": Decimal("10.00"),
    "merchant_id": "800000000",
    "merchant_name": "Abshire-Lowe",
    "merchant_city": "North Enoshaven",
    "merchant_zip": "72112",
    "card_num": CARD_NUMBER,
    "orig_ts": STAGED_ORIG_TS,
    "orig_ts_format": "S",
    "proc_ts": None,
    "proc_ts_format": " ",
}

# One accounts.accounts row (CVACT01Y), well inside its credit limit and expiry date.
ACCOUNT_DEFAULTS = {
    "acct_id": ACCOUNT_ID,
    "active_status": "Y",
    "curr_balance": Decimal("0.00"),
    "credit_limit": Decimal("1000.00"),
    "cash_credit_limit": Decimal("500.00"),
    "open_date": date(2012, 11, 17),
    "expiration_date": date(2025, 12, 31),
    "reissue_date": date(2023, 12, 16),
    "curr_cycle_credit": Decimal("0.00"),
    "curr_cycle_debit": Decimal("0.00"),
    "addr_zip": "A000000000",
    "group_id": "",
}

# The four validation reasons, typed from the MOVE statements of lines 385-419.
EXPECTED_INVALID_CARD = RejectOutcome(100, "INVALID CARD NUMBER FOUND")
EXPECTED_ACCOUNT_NOT_FOUND = RejectOutcome(101, "ACCOUNT RECORD NOT FOUND")
EXPECTED_OVERLIMIT = RejectOutcome(102, "OVERLIMIT TRANSACTION")
EXPECTED_AFTER_EXPIRATION = RejectOutcome(103, "TRANSACTION RECEIVED AFTER ACCT EXPIRATION")

# The transactions.transactions insert for the default staged record: lines 425-436 move the
# daily fields and ORIG-TS unchanged; lines 437-438 set PROC-TS from the clock in the "D" form.
EXPECTED_TRANSACTION_ROW = {
    "tran_id": STAGED_TRAN_ID,
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "Purchase at Abshire-Lowe",
    "tran_amt": Decimal("10.00"),
    "merchant_id": "800000000",
    "merchant_name": "Abshire-Lowe",
    "merchant_city": "North Enoshaven",
    "merchant_zip": "72112",
    "card_num": CARD_NUMBER,
    "orig_ts": STAGED_ORIG_TS,
    "orig_ts_format": "S",
    "proc_ts": BATCH_NOW,
    "proc_ts_format": "D",
}


def BuildStaged(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A staged DALYTRAN-RECORD for card 2988091353094312, with the given columns replaced."""
    return {**STAGED_DEFAULTS, **(overrides or {})}


def BuildAccount(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The ACCOUNT-RECORD of account 00000000004, with the given columns replaced."""
    return {**ACCOUNT_DEFAULTS, **(overrides or {})}


def BuildXref() -> dict[str, Any]:
    """The CARD-XREF-RECORD that links the staged card to its customer and account."""
    return {"card_num": CARD_NUMBER, "cust_id": CUSTOMER_ID, "acct_id": ACCOUNT_ID}


def BuildCategoryBalance(balance: Decimal) -> dict[str, Any]:
    """The TRAN-CAT-BAL-RECORD of the staged posting key (account, type 01, category 1)."""
    return {"acct_id": ACCOUNT_ID, "tran_type_cd": "01", "tran_cat_cd": 1, "balance": balance}


def BuildInput(
    staged: Mapping[str, Any],
    account: Mapping[str, Any] | None,
    xref: Mapping[str, Any] | None,
    categoryBalance: Mapping[str, Any] | None,
) -> PostingInput:
    """The records CBTRN02C holds for one daily record, None for a record not found."""
    return PostingInput(
        staged=staged,
        xref=xref,
        account=account,
        categoryBalance=categoryBalance,
        now=BATCH_NOW,
    )


def BuildPostableInput(
    stagedOverrides: Mapping[str, Any] | None = None,
    accountOverrides: Mapping[str, Any] | None = None,
) -> PostingInput:
    """An input whose cross-reference and account exist and whose category balance is new."""
    staged = BuildStaged(stagedOverrides)
    return BuildInput(staged, BuildAccount(accountOverrides), BuildXref(), None)


def BuildLookupInput(hasXref: bool, hasAccount: bool) -> PostingInput:
    """An input whose cross-reference or account read may have hit INVALID KEY."""
    xref = BuildXref() if hasXref else None
    account = BuildAccount() if hasAccount else None
    return BuildInput(BuildStaged(), account, xref, None)


def AccountMoney(balanceText: str, creditText: str, debitText: str) -> dict[str, Any]:
    """The account rewrite values 2800-UPDATE-ACCOUNT-REC produces for account 00000000004."""
    return {
        "acct_id": ACCOUNT_ID,
        "curr_balance": Decimal(balanceText),
        "curr_cycle_credit": Decimal(creditText),
        "curr_cycle_debit": Decimal(debitText),
    }


MISSING_RECORD_CASES = (
    pytest.param(False, True, EXPECTED_INVALID_CARD, id="100-xref-missing"),
    # 1500-B-LOOKUP-ACCT runs only when the cross-reference was found (line 372).
    pytest.param(False, False, EXPECTED_INVALID_CARD, id="100-account-not-examined"),
    pytest.param(True, False, EXPECTED_ACCOUNT_NOT_FOUND, id="101-account-missing"),
)

FOUND_ACCOUNT_CASES = (
    pytest.param({}, {}, None, id="accepted-within-limit-and-before-expiry"),
    # 95.00 - 0.00 + 10.00 = 105.00, above the 100.00 limit.
    pytest.param(
        {"credit_limit": Decimal("100.00"), "curr_cycle_credit": Decimal("95.00")},
        {},
        EXPECTED_OVERLIMIT,
        id="102-cycle-credit-plus-amount-over-limit",
    ),
    # The same 105.00 against a 105.00 limit passes IF ACCT-CREDIT-LIMIT >= WS-TEMP-BAL.
    pytest.param(
        {"credit_limit": Decimal("105.00"), "curr_cycle_credit": Decimal("95.00")},
        {},
        None,
        id="accepted-limit-equals-tested-balance",
    ),
    # 0.00 - (-95.00) + 10.00 = 105.00: a negative cycle debit raises the tested balance.
    pytest.param(
        {"credit_limit": Decimal("100.00"), "curr_cycle_debit": Decimal("-95.00")},
        {},
        EXPECTED_OVERLIMIT,
        id="102-negative-cycle-debit-raises-tested-balance",
    ),
    # 1000000000.00 - 0.00 + 0.00 = 1000000000.00, and S9(09)V99 keeps nine integer digits:
    # 0.00, which the 1000.00 limit covers.
    pytest.param(
        {"curr_cycle_credit": Decimal("1000000000.00")},
        {"tran_amt": Decimal("0.00")},
        None,
        id="accepted-tested-balance-loses-its-tenth-integer-digit",
    ),
    # '2022-06-09' is below '2022-06-10', the first 10 characters of ORIG-TS.
    pytest.param(
        {"expiration_date": date(2022, 6, 9)},
        {},
        EXPECTED_AFTER_EXPIRATION,
        id="103-expired-the-day-before-the-orig-date",
    ),
    pytest.param(
        {"expiration_date": date(2022, 6, 10)},
        {},
        None,
        id="accepted-expiry-equals-the-orig-date",
    ),
    # A blank X(10) date is ten spaces, which collate below every digit.
    pytest.param(
        {"expiration_date": None},
        {},
        EXPECTED_AFTER_EXPIRATION,
        id="103-blank-expiration-date",
    ),
    # Both tests run, and the expiry test, coming second, replaces the 102.
    pytest.param(
        {
            "credit_limit": Decimal("100.00"),
            "curr_cycle_credit": Decimal("95.00"),
            "expiration_date": date(2022, 6, 9),
        },
        {},
        EXPECTED_AFTER_EXPIRATION,
        id="103-overwrites-102",
    ),
)


@pytest.mark.parametrize("hasXref, hasAccount, expectedOutcome", MISSING_RECORD_CASES)
def test_missing_cross_reference_or_account_rejects(hasXref, hasAccount, expectedOutcome):
    postingInput = BuildLookupInput(hasXref, hasAccount)
    assert ValidateDailyTransaction(postingInput) == expectedOutcome


@pytest.mark.parametrize("accountOverrides, stagedOverrides, expectedOutcome", FOUND_ACCOUNT_CASES)
def test_found_account_credit_limit_and_expiry_tests(
    accountOverrides, stagedOverrides, expectedOutcome
):
    postingInput = BuildPostableInput(stagedOverrides, accountOverrides)
    assert ValidateDailyTransaction(postingInput) == expectedOutcome


def test_account_rewrite_not_found_109_is_not_a_validation_reject():
    postingInput = BuildPostableInput()
    assert ACCOUNT_REWRITE_NOT_FOUND == RejectOutcome(109, "ACCOUNT RECORD NOT FOUND")
    assert ACCOUNT_REWRITE_NOT_FOUND != REJECT_ACCOUNT_NOT_FOUND
    # A record reaching 2800 passed validation, so lines 211-215 neither count nor reject it,
    # and 2900-WRITE-TRANSACTION-FILE still writes its transaction (line 442).
    assert ValidateDailyTransaction(postingInput) is None
    assert ApplyPosting(postingInput).transaction == EXPECTED_TRANSACTION_ROW


# The 109 path through POSTTRAN's per-record sequence (batch/jobs/posttran.py). The second staged
# record is the default one with the next DALYTRAN-ID, so the run visibly goes on after the 109.
SECOND_STAGED_TRAN_ID = "0000000000683581"
POSTING_RUN_RECORDS = (
    BuildStaged(),
    BuildStaged({"record_seq": 2, "tran_id": SECOND_STAGED_TRAN_ID}),
)
# The first account REWRITE matches no row, the INVALID KEY of lines 554-558; the second
# matches its account.
REWRITE_ROWCOUNTS = (0, 1)
ACCOUNT_REWRITE_EVENT = "UPDATE accounts.accounts"
TRANSACTION_INSERT_EVENT = "INSERT transactions.transactions"
# 2000-POST-TRANSACTION, lines 441-442: 2800 rewrites the account, then 2900 writes the
# transaction whatever 2800 found, for each record in turn.
EXPECTED_POSTING_STATEMENTS = [ACCOUNT_REWRITE_EVENT, TRANSACTION_INSERT_EVENT] * 2
EXPECTED_POSTED_TRANSACTIONS = [
    EXPECTED_TRANSACTION_ROW,
    {**EXPECTED_TRANSACTION_ROW, "tran_id": SECOND_STAGED_TRAN_ID},
]
# Line 206 counts both records, lines 211-215 reject neither, and lines 229-231 leave
# RETURN-CODE 0, so both records are posted.
EXPECTED_POSTING_RESULT = JobResult(
    0,
    {posttran.PROCESSED_COUNT_KEY: 2, posttran.REJECTED_COUNT_KEY: 0, posttran.POSTED_COUNT_KEY: 2},
)
# The run's only warning, for record 1: MOVE 109 and 'ACCOUNT RECORD NOT FOUND' (lines
# 556-558), the reason shown as 9(04).
EXPECTED_REWRITE_NOT_FOUND_WARNING = (
    "DALYTRAN RECORD 1: REWRITE OF ACCOUNT 00000000004 FOUND NO RECORD, REASON 0109"
    " ACCOUNT RECORD NOT FOUND; THE TRANSACTION IS STILL WRITTEN AND NO REJECT IS COUNTED"
)


def StatementEvent(statement: object) -> str:
    """Name an executed statement by its verb and table, as UPDATE accounts.accounts."""
    if isinstance(statement, Update):
        return f"UPDATE {statement.table.fullname}"
    if isinstance(statement, Insert):
        return f"INSERT {statement.table.fullname}"
    raise AssertionError(f"POSTTRAN executed an unexpected statement {statement!r}")


@dataclass
class PostingDatabaseStub:
    """The account and transaction files as POSTTRAN's 2800 and 2900 units reach them.

    Each unit's connection records every statement as its verb and table, and the values of
    each transactions INSERT. The n-th accounts UPDATE matches rewriteRowcounts[n] rows, 0
    being the REWRITE INVALID KEY of reason 109.
    """

    rewriteRowcounts: tuple[int, ...]
    statements: list[str] = field(default_factory=list)
    insertedTransactions: list[dict[str, Any]] = field(default_factory=list)

    @contextmanager
    def BeginUnit(self) -> Iterator[SimpleNamespace]:
        """Stand in for Engine.begin(): one unit whose connection executes into this stub."""
        yield SimpleNamespace(execute=self.Execute)

    def Execute(self, statement: object) -> SimpleNamespace:
        """Record one Connection.execute call and return its result's rowcount."""
        event = StatementEvent(statement)
        self.statements.append(event)
        if event == ACCOUNT_REWRITE_EVENT:
            rewriteIndex = self.statements.count(ACCOUNT_REWRITE_EVENT) - 1
            return SimpleNamespace(rowcount=self.rewriteRowcounts[rewriteIndex])
        if event == TRANSACTION_INSERT_EVENT:
            self.insertedTransactions.append(statement.compile().params)
            return SimpleNamespace(rowcount=1)
        raise AssertionError(f"POSTTRAN executed an unexpected {event}")


@dataclass
class RecordingRejectWriter:
    """A DALYREJS(+1) writer that keeps each record 2500-WRITE-REJECT-REC writes."""

    records: list[str] = field(default_factory=list)

    def WriteRecord(self, record: str) -> None:
        """Keep one REJECT-RECORD."""
        self.records.append(record)


def StagedPostingRunRecords(context: object) -> Iterator[dict[str, Any]]:
    """Stand in for StagedRecordsInOrder: POSTING_RUN_RECORDS in record_seq order."""
    yield from POSTING_RUN_RECORDS


def ValidatedPostingEffects(context: object, stagedRow: Mapping[str, Any]) -> PostingEffects:
    """Stand in for RunValidationUnit: each record passes 1500-VALIDATE-TRAN.

    Its cross-reference and account 00000000004 are found and its category balance is new, so
    its effects are ApplyPosting's.
    """
    return ApplyPosting(BuildInput(stagedRow, BuildAccount(), BuildXref(), None))


def RunPostingOverRewriteStub(
    monkeypatch: pytest.MonkeyPatch, postingDatabase: PostingDatabaseStub
) -> tuple[posttran.PostingCounts, RecordingRejectWriter]:
    """Run the real posttran.PostAllRecords over POSTING_RUN_RECORDS against postingDatabase.

    Only the staged-record read and the validation unit, which need a database, are replaced:
    ProcessStagedRecord, PostValidRecord, RewriteAccount and InsertTransaction run unchanged.
    """
    monkeypatch.setattr(posttran, "StagedRecordsInOrder", StagedPostingRunRecords)
    monkeypatch.setattr(posttran, "RunValidationUnit", ValidatedPostingEffects)
    context = SimpleNamespace(engine=SimpleNamespace(begin=postingDatabase.BeginUnit))
    rejectWriter = RecordingRejectWriter()
    return posttran.PostAllRecords(context, rejectWriter), rejectWriter


def PostingWarnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The WARNING messages POSTTRAN's logger emitted."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == posttran.LOGGER.name and record.levelno == logging.WARNING
    ]


def test_account_rewrite_not_found_109_still_writes_the_transaction_and_counts_no_reject(
    monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger=posttran.LOGGER.name)
    postingDatabase = PostingDatabaseStub(REWRITE_ROWCOUNTS)
    counts, rejectWriter = RunPostingOverRewriteStub(monkeypatch, postingDatabase)
    assert postingDatabase.statements == EXPECTED_POSTING_STATEMENTS
    assert postingDatabase.insertedTransactions == EXPECTED_POSTED_TRANSACTIONS
    assert (counts.processed, counts.rejected, counts.Posted()) == (2, 0, 2)
    assert rejectWriter.records == []
    assert posttran.BuildPostingResult(counts) == EXPECTED_POSTING_RESULT
    assert PostingWarnings(caplog) == [EXPECTED_REWRITE_NOT_FOUND_WARNING]


def test_end_of_run_display_labels_and_return_code():
    assert PROCESSED_COUNT_LABEL == "TRANSACTIONS PROCESSED :"
    assert REJECTED_COUNT_LABEL == "TRANSACTIONS REJECTED  :"
    assert REJECTS_RETURN_CODE == 4


@pytest.mark.parametrize(
    "hasXref, hasAccount", ((False, True), (True, False)), ids=("xref-missing", "account-missing")
)
def test_apply_posting_refuses_a_record_whose_lookup_failed(hasXref, hasAccount):
    with pytest.raises(ValueError, match="validated record"):
        ApplyPosting(BuildLookupInput(hasXref, hasAccount))


# Account cases start from a balance of 100.00, a cycle credit of 20.00 and a cycle debit of
# -5.00, so the cycle field the amount skips is visibly kept.
SPLIT_START = {
    "curr_balance": Decimal("100.00"),
    "curr_cycle_credit": Decimal("20.00"),
    "curr_cycle_debit": Decimal("-5.00"),
}

ACCOUNT_UPDATE_CASES = (
    # 100.00 + 10.00 = 110.00 and 20.00 + 10.00 = 30.00; the debit keeps -5.00.
    pytest.param(
        SPLIT_START, Decimal("10.00"), AccountMoney("110.00", "30.00", "-5.00"), id="credit"
    ),
    # 100.00 - 10.00 = 90.00 and -5.00 - 10.00 = -15.00; the credit keeps 20.00.
    pytest.param(
        SPLIT_START, Decimal("-10.00"), AccountMoney("90.00", "20.00", "-15.00"), id="debit"
    ),
    # The smallest negative amount is already below the >= 0 test: -5.00 - 0.01 = -5.01.
    pytest.param(
        SPLIT_START,
        Decimal("-0.01"),
        AccountMoney("99.99", "20.00", "-5.01"),
        id="debit-smallest-negative-amount",
    ),
    # IF DALYTRAN-AMT >= 0 sends zero to the cycle credit, which, like every field, keeps its value.
    pytest.param(
        SPLIT_START, Decimal("0.00"), AccountMoney("100.00", "20.00", "-5.00"), id="credit-zero"
    ),
    # 9999999999.99 + 0.01 = 10000000000.00; S9(10)V99 keeps ten integer digits: 0.00.
    pytest.param(
        {"curr_balance": Decimal("9999999999.99"), "curr_cycle_credit": Decimal("9999999999.99")},
        Decimal("0.01"),
        AccountMoney("0.00", "0.00", "0.00"),
        id="credit-drops-high-order-digit-of-s9-10-v99",
    ),
    # -9999999999.99 - 0.01 = -10000000000.00, which keeps 0.00, a zero with no sign.
    pytest.param(
        {"curr_balance": Decimal("-9999999999.99"), "curr_cycle_debit": Decimal("-9999999999.99")},
        Decimal("-0.01"),
        AccountMoney("0.00", "0.00", "0.00"),
        id="debit-drops-high-order-digit-of-s9-10-v99",
    ),
)

CATEGORY_BALANCE_CASES = (
    # INITIALIZE TRAN-CAT-BAL-RECORD, then ADD 10.00: 0.00 + 10.00 = 10.00 (lines 504-508).
    pytest.param(None, Decimal("10.00"), True, Decimal("10.00"), id="created-from-zero"),
    # ADD 10.00 TO an existing 5.00 gives 15.00 (line 527).
    pytest.param(Decimal("5.00"), Decimal("10.00"), False, Decimal("15.00"), id="updated"),
    pytest.param(Decimal("5.00"), Decimal("-7.50"), False, Decimal("-2.50"), id="updated-debit"),
    # 999999999.99 + 0.01 = 1000000000.00; S9(09)V99 keeps nine integer digits: 0.00.
    pytest.param(
        Decimal("999999999.99"),
        Decimal("0.01"),
        False,
        Decimal("0.00"),
        id="drops-high-order-digit-of-s9-09-v99",
    ),
)

# COB-MIL of FUNCTION CURRENT-DATE holds hundredths of a second, and DB2-REST is '0000', so the
# clock keeps whole hundredths in the "D" text of Z-GET-DB2-FORMAT-TIMESTAMP (lines 692-703).
CLOCK_CASES = (
    pytest.param(BATCH_NOW, BATCH_NOW, "2022-07-18-00.00.00.000000", id="batch-now"),
    pytest.param(
        datetime(2022, 7, 18, 13, 45, 6, 123456),
        datetime(2022, 7, 18, 13, 45, 6, 120000),
        "2022-07-18-13.45.06.120000",
        id="drops-sub-hundredths",
    ),
    pytest.param(
        datetime(2022, 12, 31, 23, 59, 59, 999999),
        datetime(2022, 12, 31, 23, 59, 59, 990000),
        "2022-12-31-23.59.59.990000",
        id="never-rounds-up",
    ),
)

INVALID_CLOCK_CASES = (
    pytest.param(date(2022, 7, 18), TypeError, id="date-not-datetime"),
    pytest.param(datetime(2022, 7, 18, tzinfo=UTC), ValueError, id="zone-aware"),
)

# WS-VALIDATION-FAIL-REASON 9(04) followed by WS-VALIDATION-FAIL-REASON-DESC X(76).
TRAILER_CASES = (
    pytest.param(REJECT_INVALID_CARD, "0100" + "INVALID CARD NUMBER FOUND".ljust(76), id="100"),
    pytest.param(REJECT_ACCOUNT_NOT_FOUND, "0101" + "ACCOUNT RECORD NOT FOUND".ljust(76), id="101"),
    pytest.param(REJECT_OVERLIMIT, "0102" + "OVERLIMIT TRANSACTION".ljust(76), id="102"),
    pytest.param(
        REJECT_AFTER_EXPIRATION,
        "0103" + "TRANSACTION RECEIVED AFTER ACCT EXPIRATION".ljust(76),
        id="103",
    ),
    # An alphanumeric MOVE cuts a description longer than X(76) at the right.
    pytest.param(RejectOutcome(103, "X" * 80), "0103" + "X" * 76, id="description-cut-at-76"),
)


@pytest.mark.parametrize("accountOverrides, amount, expectedAccount", ACCOUNT_UPDATE_CASES)
def test_account_update_adds_amount_by_sign(accountOverrides, amount, expectedAccount):
    postingInput = BuildPostableInput({"tran_amt": amount}, accountOverrides)
    assert ApplyPosting(postingInput).account == expectedAccount


@pytest.mark.parametrize(
    "existingBalance, amount, expectedIsNew, expectedBalance", CATEGORY_BALANCE_CASES
)
def test_category_balance_created_or_updated(
    existingBalance, amount, expectedIsNew, expectedBalance
):
    categoryBalance = None if existingBalance is None else BuildCategoryBalance(existingBalance)
    staged = BuildStaged({"tran_amt": amount})
    effects = ApplyPosting(BuildInput(staged, BuildAccount(), BuildXref(), categoryBalance))
    assert effects.isNewCategoryBalance is expectedIsNew
    assert effects.categoryBalance == BuildCategoryBalance(expectedBalance)


def test_transaction_row_copies_daily_fields_and_orig_ts():
    transactionRow = ApplyPosting(BuildPostableInput()).transaction
    assert transactionRow == EXPECTED_TRANSACTION_ROW
    origText = EncodeTimestamp(transactionRow["orig_ts"], transactionRow["orig_ts_format"])
    assert origText == "2022-06-10 19:27:53.000000"


@pytest.mark.parametrize("now, expectedProcTs, expectedProcText", CLOCK_CASES)
def test_proc_ts_is_the_clock_in_whole_hundredths(now, expectedProcTs, expectedProcText):
    transactionRow = ApplyPosting(replace(BuildPostableInput(), now=now)).transaction
    assert transactionRow["proc_ts"] == expectedProcTs
    assert transactionRow["proc_ts_format"] == "D"
    procText = EncodeTimestamp(transactionRow["proc_ts"], transactionRow["proc_ts_format"])
    assert procText == expectedProcText


@pytest.mark.parametrize("invalidNow, expectedError", INVALID_CLOCK_CASES)
def test_apply_posting_refuses_a_clock_that_is_not_a_naive_datetime(invalidNow, expectedError):
    with pytest.raises(expectedError):
        ApplyPosting(replace(BuildPostableInput(), now=invalidNow))


@pytest.mark.parametrize("outcome, expectedTrailer", TRAILER_CASES)
def test_validation_trailer_is_reason_then_description(outcome, expectedTrailer):
    trailer = BuildValidationTrailer(outcome)
    assert len(trailer) == 80
    assert trailer == expectedTrailer
