"""INTCALC interest posting: CBACT04C's account rewrites and its SYSTRAN(+1) records.

Pins cbl/CBACT04C.cbl lines 180-520 and jcl/INTCALC.jcl STEP15 (PARM='2022071800'), as the
Technical Specification transcribes them in Section 0.6.8 ("INTCALC") and the arithmetic table
of Section 0.6.6. Category balances are read in key order (account, type, category). On each
change of account after the first, 1050-UPDATE-ACCOUNT rewrites the previous account: the total
interest is added to its balance and both cycle fields become zero. Each balance's rate comes
from the account's disclosure group, retried with DEFAULT, whose absence abends (exit 16). A
rate of 0 is skipped; any other rate gives (balance * rate) / 1200 truncated into S9(09)V99 and
one SYSTRAN record whose TRAN-ID is PARM-DATE followed by the 9(06) suffix, both timestamps
being the clock in DB2 form. The final account is never rewritten.

The sample-chain tests compare the outputs with the committed oracle fixtures. Every synthetic
test reads the database it arranged into the oracle's state and runs legacy_oracle.RunIntcalc
on it, so each expectation comes from the independent oracle (Section 0.9.3). One of them gives
an account competing cards, whose SYSTRAN records must carry the first in EBCDIC card_num order.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.logging import PanMaskingFilter
from app.models import (
    GDG_GENERATIONS,
    Account,
    Card,
    CardXref,
    DisclosureGroup,
    TransactionCategoryBalance,
)
from sqlalchemy import ColumnElement, Engine, delete, func, insert, select, update
from sqlalchemy.engine import Connection

from batch.jobs import interest_calc
from batch.orchestration import gdg
from batch.orchestration.job_context import EXIT_ABEND, EXIT_OK, EXIT_REJECTS, JobContext
from batch.tests.conftest import (
    ACTIVE_STATUS,
    EXPECTED_DIR,
    FIXED_AS_OF_TEXT,
    SYNTHETIC_EXPIRY_DATE,
    BatchEnvironment,
    InvokeCli,
    StepRow,
)
from batch.tests.reconciliation import compare, legacy_oracle

DEFAULT_PARM_DATE = "2022071800"
OVERRIDE_PARM_DATE = "2022071900"
SYSTRAN_BASE_NAME = gdg.SYSTRAN.name
SYSTRAN_LAYOUT = "TRANSACTION"
INTCALC_STEP_NO = 14
STEP_FAILED_STATE = "FAILED"
POSTTRAN_ARGUMENTS = ("job", "post-tran", "--as-of", FIXED_AS_OF_TEXT)
INTCALC_ARGUMENTS = ("job", "interest-calc", "--as-of", FIXED_AS_OF_TEXT)
RUN_DATE_OPTION = "--run-date"
ORACLE_INPUT = legacy_oracle.IntcalcInput(DEFAULT_PARM_DATE, legacy_oracle.DB2_CLOCK_TEXT)

SYSTRAN_FIXTURE = EXPECTED_DIR / "intcalc_systran.dat"
INTCALC_ACCOUNTS_FIXTURE = EXPECTED_DIR / "intcalc_accounts.csv"
POSTTRAN_ACCOUNTS_FIXTURE = EXPECTED_DIR / "posttran_accounts.csv"
ACCOUNTS_TABLE = legacy_oracle.ACCOUNTS_TABLE
CATEGORY_BALANCES_TABLE = legacy_oracle.CATEGORY_BALANCES_TABLE
ACCT_ID_COLUMN = "acct_id"
BALANCE_COLUMN = "curr_balance"
CYCLE_COLUMNS = ("curr_cycle_credit", "curr_cycle_debit")
UNFLUSHED_COLUMNS = (BALANCE_COLUMN, *CYCLE_COLUMNS)

# CVTRA05Y offsets of a SYSTRAN record: TRAN-ID X(16) is PARM-DATE X(10) then the 9(06)
# suffix, TRAN-ORIG-TS X(26) starts at 278 and TRAN-PROC-TS X(26) at 304.
TRAN_ID_SLICE = slice(0, 16)
PARM_DATE_SLICE = slice(0, 10)
TRAN_ID_SUFFIX_SLICE = slice(10, 16)
ORIG_TS_SLICE = slice(278, 304)
PROC_TS_SLICE = slice(304, 330)
SUFFIX_DIGITS = 6

DEFAULT_GROUP = "DEFAULT"
SAMPLE_GROUP = "A000000000"
# A group no disclosure-group record has, so 1200-GET-INTEREST-RATE retries with DEFAULT.
ABSENT_GROUP = "NOSUCHGRP"
ZERO_RATE = Decimal("0.00")
ZERO_AMOUNT = Decimal("0.00")
# 1050-UPDATE-ACCOUNT moves 0 to ACCT-CURR-CYC-CREDIT and ACCT-CURR-CYC-DEBIT.
ZEROED_CYCLE_FIELDS = dict.fromkeys(CYCLE_COLUMNS, ZERO_AMOUNT)
ACCOUNT_IDS = tuple(f"{index:011d}" for index in range(1, 6))
SYNTHETIC_CARD_NUM = "9000000000000001"
SYNTHETIC_INTEREST = Decimal("1.23")
INTEREST_RATE_FUNCTION_NAME = "ReadInterestRate"
# 1300-B-WRITE-TX's TRAN-DESC: this prefix, then ACCT-ID.
INTEREST_DESCRIPTION_PREFIX = "Int. for a/c "

# Account 1's competing cards in insertion order, beside its all-digit seed card. Code page 037
# orders lower case before upper case before digits, so its first xref card is the k card,
# inserted last; ASCII order and both descending orders each put another card first.
FIRST_XREF_ACCT_ID = ACCOUNT_IDS[0]
FIRST_XREF_CARD_NUM = "k000000000000001"
COMPETING_CARD_NUMS = ("Q000000000000001", "x000000000000001", FIRST_XREF_CARD_NUM)
COMPETING_CVV_CD = "000"
COMPETING_EMBOSSED_NAME = "COMPETING CARD HOLDER"


@dataclass(frozen=True)
class AccountSetup:
    """The ACCTDATA fields an INTCALC test sets on one sample account."""

    acctId: str
    groupId: str
    currBalance: Decimal
    cycleCredit: Decimal
    cycleDebit: Decimal


@dataclass(frozen=True)
class CategoryBalance:
    """One TRAN-CAT-BAL-RECORD: TRAN-CAT-KEY and TRAN-CAT-BAL S9(09)V99."""

    acctId: str
    typeCd: str
    catCd: int
    balance: Decimal


@dataclass(frozen=True)
class DisclosureKey:
    """DIS-GROUP-KEY: DIS-ACCT-GROUP-ID, DIS-TRAN-TYPE-CD and DIS-TRAN-CAT-CD."""

    groupId: str
    typeCd: str
    catCd: int


@dataclass(frozen=True)
class SyntheticScenario:
    """The accounts, the whole TCATBALF and the disclosure rates one synthetic run reads."""

    accounts: tuple[AccountSetup, ...]
    balances: tuple[CategoryBalance, ...]
    rates: tuple[tuple[DisclosureKey, Decimal], ...] = ()


@dataclass(frozen=True)
class SyntheticRun:
    """One INTCALC run and the oracle's prediction from the same database state.

    systranRecords is the SYSTRAN(0) content, None when no generation was published.
    """

    before: legacy_oracle.OracleState
    expected: legacy_oracle.IntcalcOutcome
    exitCode: int
    output: str
    after: legacy_oracle.OracleState
    systranRecords: list[str] | None


class RecordingWriter:
    """Stands in for the SYSTRAN(+1) generation writer and keeps every record written."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def WriteRecord(self, record: str) -> None:
        self.records.append(record)


# Five accounts of two or three balances each, the last never rewritten. Account 1 uses group
# A000000000, account 3 a group with no record (the DEFAULT retry) and the others a blank group
# (DEFAULT too). A000000000/02/0001 at rate 0 is skipped; DEFAULT/05/0001 on -0.07 truncates
# toward zero and still writes a 0.00 record.
MULTI_ACCOUNT_SCENARIO = SyntheticScenario(
    accounts=(
        AccountSetup(
            ACCOUNT_IDS[0], SAMPLE_GROUP, Decimal("1000.00"), Decimal("120.50"), Decimal("80.25")
        ),
        AccountSetup(ACCOUNT_IDS[1], "", Decimal("-45.10"), Decimal("33.33"), Decimal("12.00")),
        AccountSetup(
            ACCOUNT_IDS[2], ABSENT_GROUP, Decimal("250000.00"), Decimal("999.99"), Decimal("0.01")
        ),
        AccountSetup(
            ACCOUNT_IDS[3], SAMPLE_GROUP, Decimal("0.00"), Decimal("5.00"), Decimal("7.50")
        ),
        AccountSetup(ACCOUNT_IDS[4], "", Decimal("7777.77"), Decimal("64.00"), Decimal("128.00")),
    ),
    balances=(
        CategoryBalance(ACCOUNT_IDS[0], "01", 1, Decimal("1500.00")),
        CategoryBalance(ACCOUNT_IDS[0], "01", 2, Decimal("-250.75")),
        CategoryBalance(ACCOUNT_IDS[0], "02", 1, Decimal("300.00")),
        CategoryBalance(ACCOUNT_IDS[1], "01", 1, Decimal("2345.67")),
        CategoryBalance(ACCOUNT_IDS[1], "04", 3, Decimal("-1234.56")),
        CategoryBalance(ACCOUNT_IDS[2], "01", 4, Decimal("10000.00")),
        CategoryBalance(ACCOUNT_IDS[2], "05", 1, Decimal("-0.07")),
        CategoryBalance(ACCOUNT_IDS[2], "07", 1, Decimal("88.88")),
        CategoryBalance(ACCOUNT_IDS[3], "04", 2, Decimal("777.77")),
        CategoryBalance(ACCOUNT_IDS[3], "06", 1, Decimal("-999.99")),
        CategoryBalance(ACCOUNT_IDS[4], "01", 1, Decimal("999.99")),
        CategoryBalance(ACCOUNT_IDS[4], "01", 3, Decimal("4321.09")),
        CategoryBalance(ACCOUNT_IDS[4], "06", 2, Decimal("-50.00")),
    ),
    rates=(
        (DisclosureKey(SAMPLE_GROUP, "01", 2), Decimal("21.50")),
        (DisclosureKey(SAMPLE_GROUP, "02", 1), ZERO_RATE),
        (DisclosureKey(DEFAULT_GROUP, "04", 3), Decimal("9.99")),
        (DisclosureKey(DEFAULT_GROUP, "01", 4), Decimal("12.75")),
        (DisclosureKey(DEFAULT_GROUP, "06", 2), Decimal("3.33")),
    ),
)
MULTI_ACCOUNT_SKIPPED_RECORDS = 1
FLUSHED_ACCOUNT_IDS = ACCOUNT_IDS[:-1]
LAST_ACCOUNT_ID = ACCOUNT_IDS[-1]

# StoreInto(1234.56 * 18.00 / 1200, 9, 2): 18.5184 truncated.
SINGLE_ACCOUNT_SCENARIO = SyntheticScenario(
    accounts=(
        AccountSetup(ACCOUNT_IDS[0], "", Decimal("500.00"), Decimal("42.00"), Decimal("17.25")),
    ),
    balances=(CategoryBalance(ACCOUNT_IDS[0], "01", 1, Decimal("1234.56")),),
    rates=((DisclosureKey(DEFAULT_GROUP, "01", 1), Decimal("18.00")),),
)
SINGLE_ACCOUNT_INTEREST = Decimal("18.51")

# Account 1 posts at DEFAULT/01/0002; account 2's group has no record and DEFAULT/01/0001 is
# deleted, so 1200-A-GET-DEFAULT-INT-RATE abends after account 1 has been rewritten. Account 3
# is never reached.
ABEND_SCENARIO = SyntheticScenario(
    accounts=(
        AccountSetup(ACCOUNT_IDS[0], "", Decimal("100.00"), Decimal("10.00"), Decimal("20.00")),
        AccountSetup(
            ACCOUNT_IDS[1], ABSENT_GROUP, Decimal("200.00"), Decimal("30.00"), Decimal("40.00")
        ),
    ),
    balances=(
        CategoryBalance(ACCOUNT_IDS[0], "01", 2, Decimal("1000.00")),
        CategoryBalance(ACCOUNT_IDS[1], "01", 1, Decimal("500.00")),
        CategoryBalance(ACCOUNT_IDS[2], "01", 1, Decimal("700.00")),
    ),
)
MISSING_DEFAULT_KEY = DisclosureKey(DEFAULT_GROUP, "01", 1)


# --- Arranging the synthetic input --------------------------------------------------------------
def SetAccounts(engine: Engine, setups: Iterable[AccountSetup]) -> None:
    """Set each sample account's group, balance and cycle fields, in one transaction."""
    with engine.begin() as connection:
        for setup in setups:
            statement = (
                update(Account)
                .where(Account.acct_id == setup.acctId)
                .values(
                    group_id=setup.groupId,
                    curr_balance=setup.currBalance,
                    curr_cycle_credit=setup.cycleCredit,
                    curr_cycle_debit=setup.cycleDebit,
                )
            )
            assert connection.execute(statement).rowcount == 1, setup.acctId


def SetCategoryBalances(engine: Engine, balances: Iterable[CategoryBalance]) -> None:
    """Replace the whole TCATBALF with balances; an empty iterable leaves it empty."""
    rows = [
        {
            "acct_id": balance.acctId,
            "tran_type_cd": balance.typeCd,
            "tran_cat_cd": balance.catCd,
            "balance": balance.balance,
        }
        for balance in balances
    ]
    balanceTable = TransactionCategoryBalance.__table__
    with engine.begin() as connection:
        connection.execute(delete(balanceTable))
        if rows:
            connection.execute(insert(balanceTable), rows)


def DisclosureKeyFilter(key: DisclosureKey) -> tuple[ColumnElement[bool], ...]:
    """Return the WHERE conditions that select one DISCGRP record by its key."""
    return (
        DisclosureGroup.group_id == key.groupId,
        DisclosureGroup.tran_type_cd == key.typeCd,
        DisclosureGroup.tran_cat_cd == key.catCd,
    )


def SetDisclosureRates(engine: Engine, rates: Iterable[tuple[DisclosureKey, Decimal]]) -> None:
    """Set DIS-INT-RATE of each existing DISCGRP record named by rates."""
    with engine.begin() as connection:
        for key, rate in rates:
            statement = (
                update(DisclosureGroup).where(*DisclosureKeyFilter(key)).values(interest_rate=rate)
            )
            assert connection.execute(statement).rowcount == 1, key


def SetEveryDisclosureRate(engine: Engine, rate: Decimal) -> None:
    """Set DIS-INT-RATE of every DISCGRP record to rate."""
    with engine.begin() as connection:
        connection.execute(update(DisclosureGroup).values(interest_rate=rate))


def DeleteDisclosureGroup(engine: Engine, key: DisclosureKey) -> None:
    """Delete one DISCGRP record, so a READ by its key is INVALID KEY (status 23)."""
    with engine.begin() as connection:
        result = connection.execute(delete(DisclosureGroup).where(*DisclosureKeyFilter(key)))
        assert result.rowcount == 1, key


def ArrangeScenario(engine: Engine, scenario: SyntheticScenario) -> None:
    """Write a scenario's accounts, category balances and disclosure rates."""
    SetAccounts(engine, scenario.accounts)
    SetCategoryBalances(engine, scenario.balances)
    SetDisclosureRates(engine, scenario.rates)


def AccountCardNums(engine: Engine, acctId: str) -> list[str]:
    """Return the card numbers of acctId's cross-references, in no defined order."""
    statement = select(CardXref.card_num).where(CardXref.acct_id == acctId)
    with engine.connect() as connection:
        return list(connection.execute(statement).scalars())


def CompetingCardRow(acctId: str, cardNum: str) -> dict[str, object]:
    """Return an active cards.cards row of acctId; CARD-NUM is X(16), so letters are valid."""
    return {
        "card_num": cardNum,
        "acct_id": acctId,
        "cvv_cd": COMPETING_CVV_CD,
        "embossed_name": COMPETING_EMBOSSED_NAME,
        "expiration_date": SYNTHETIC_EXPIRY_DATE,
        "active_status": ACTIVE_STATUS,
    }


def InsertCompetingCards(engine: Engine, acctId: str, cardNums: Sequence[str]) -> None:
    """Insert a card and a cross-reference of acctId for each of cardNums, in the order given.

    Each cross-reference names the customer of acctId's one seed cross-reference.
    """
    custIdStatement = select(CardXref.cust_id).where(CardXref.acct_id == acctId)
    with engine.begin() as connection:
        custId = connection.execute(custIdStatement).scalar_one()
        for cardNum in cardNums:
            connection.execute(insert(Card.__table__), CompetingCardRow(acctId, cardNum))
            xrefRow = {"card_num": cardNum, "cust_id": custId, "acct_id": acctId}
            connection.execute(insert(CardXref.__table__), xrefRow)


def WrongOrderFirstCards(
    seedCardNums: Sequence[str], addedCardNums: Sequence[str]
) -> dict[str, str]:
    """Return the card each read order other than ascending code page 037 would take first.

    ASCII order is card_num under C collation. A read with no ORDER BY tends to return rows as
    inserted: the seed cards, then addedCardNums.
    """
    insertionOrder = [*seedCardNums, *addedCardNums]
    return {
        "ascii-ascending": min(insertionOrder),
        "ascii-descending": max(insertionOrder),
        "cp037-descending": max(insertionOrder, key=legacy_oracle.EbcdicSortKey),
        "first-inserted": insertionOrder[0],
        "first-added": addedCardNums[0],
    }


# --- Running INTCALC and reading its outputs ----------------------------------------------------
def ReadOracleState(engine: Engine) -> legacy_oracle.OracleState:
    """Read every dataset of the database into the oracle's state, through plain SQL."""
    with engine.connect() as connection:
        return compare.ReadTableAsOracleState(connection)


def PublishedSystranPath(environment: BatchEnvironment) -> Path | None:
    """Resolve SYSTRAN(0), the newest PUBLISHED generation; None when there is none."""
    with environment.engine.connect() as connection:
        return compare.LatestPublishedGeneration(
            connection, SYSTRAN_BASE_NAME, environment.outputDir
        )


def PublishedSystranRecords(environment: BatchEnvironment) -> list[str] | None:
    """Return the SYSTRAN(0) records, terminators removed; None when none is published."""
    systranPath = PublishedSystranPath(environment)
    if systranPath is None:
        return None
    return compare.ReadFixedWidthRecords(systranPath)


def CatalogedSystranCount(engine: Engine) -> int:
    """Return the number of SYSTRAN catalog rows, in any state."""
    statement = (
        select(func.count())
        .select_from(GDG_GENERATIONS)
        .where(GDG_GENERATIONS.c.base_name == SYSTRAN_BASE_NAME)
    )
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def SystranFiles(outputDir: Path) -> list[Path]:
    """Return every file in the SYSTRAN generation directory, .part files included."""
    systranDir = outputDir / compare.GDG_DIRECTORY_NAME / SYSTRAN_BASE_NAME
    if not systranDir.is_dir():
        return []
    return sorted(systranDir.iterdir())


def RunSyntheticIntcalc(environment: BatchEnvironment) -> SyntheticRun:
    """Predict INTCALC with the oracle from the arranged database, then run the job command."""
    before = ReadOracleState(environment.engine)
    expected = legacy_oracle.RunIntcalc(before, ORACLE_INPUT)
    outcome = InvokeCli(INTCALC_ARGUMENTS)
    return SyntheticRun(
        before=before,
        expected=expected,
        exitCode=outcome.exitCode,
        output=outcome.output,
        after=ReadOracleState(environment.engine),
        systranRecords=PublishedSystranRecords(environment),
    )


def RunSampleChainThroughIntcalc(intcalcArguments: Sequence[str] = INTCALC_ARGUMENTS) -> None:
    """Post the staged sample day (exit 4, its rejects), then run INTCALC (exit 0)."""
    posttran = InvokeCli(POSTTRAN_ARGUMENTS)
    assert posttran.exitCode == EXIT_REJECTS, posttran.output
    intcalc = InvokeCli(intcalcArguments)
    assert intcalc.exitCode == EXIT_OK, intcalc.output


def AccountFields(state: legacy_oracle.OracleState, acctId: str) -> dict[str, object]:
    """Return an account's balance and both cycle fields."""
    return {column: state.accounts[acctId][column] for column in UNFLUSHED_COLUMNS}


def CycleFields(state: legacy_oracle.OracleState, acctId: str) -> dict[str, object]:
    """Return an account's ACCT-CURR-CYC-CREDIT and ACCT-CURR-CYC-DEBIT."""
    return {column: state.accounts[acctId][column] for column in CYCLE_COLUMNS}


def LastCategoryBalanceAccount(connection: Connection) -> str:
    """Return TRANCAT-ACCT-ID of the last TCATBALF record in TRAN-CAT-KEY order."""
    spec = legacy_oracle.TABLE_SPECS[CATEGORY_BALANCES_TABLE]
    rows = compare.ReadOracleRows(connection, CATEGORY_BALANCES_TABLE)
    return str(max(rows, key=lambda row: legacy_oracle.RowSortKey(row, spec))[ACCT_ID_COLUMN])


def TableAccountTexts(connection: Connection, acctId: str) -> dict[str, str]:
    """Return the normalised balance and cycle fields of one accounts.accounts row."""
    rows = compare.ReadOracleRows(connection, ACCOUNTS_TABLE)
    row = next(row for row in rows if row[ACCT_ID_COLUMN] == acctId)
    return {column: compare.NormalizeValue(row[column]) for column in UNFLUSHED_COLUMNS}


def FixtureAccountTexts(csvPath: Path, acctId: str) -> dict[str, str]:
    """Return the balance and cycle fields of one account row of an expected accounts CSV."""
    _header, rows = compare.ReadExpectedCsv(csvPath)
    row = next(row for row in rows if row[ACCT_ID_COLUMN] == acctId)
    return {column: str(row[column]) for column in UNFLUSHED_COLUMNS}


def AccountRecordCards(records: Iterable[str], acctId: str) -> set[str]:
    """Return the TRAN-CARD-NUM of every SYSTRAN record whose TRAN-DESC names acctId."""
    description = INTEREST_DESCRIPTION_PREFIX + acctId
    return {
        record[legacy_oracle.TRAN_CARD_SLICE]
        for record in records
        if record[legacy_oracle.TRAN_DESC_SLICE].rstrip(" ") == description
    }


# --- Sample chain parity ------------------------------------------------------------------------
def test_intcalc_systran_generation_equals_expected_fixture(
    batchEnvironment: BatchEnvironment,
) -> None:
    """POSTTRAN then INTCALC on the sample day write the oracle's SYSTRAN(+1), byte for byte."""
    RunSampleChainThroughIntcalc()
    systranPath = PublishedSystranPath(batchEnvironment)
    assert compare.CompareFixedWidthFiles(systranPath, SYSTRAN_FIXTURE, SYSTRAN_LAYOUT) == []


def test_intcalc_accounts_equal_expected_fixture_with_last_account_unflushed(
    batchEnvironment: BatchEnvironment,
) -> None:
    """The accounts equal the oracle's, and the last account keeps its post-POSTTRAN values.

    The end-of-file READ ends the PERFORM UNTIL before its outer ELSE performs
    1050-UPDATE-ACCOUNT, so the last account in TRAN-CAT-KEY order is never rewritten.
    """
    RunSampleChainThroughIntcalc()
    with batchEnvironment.engine.connect() as connection:
        differences = compare.CompareTableWithCsv(
            connection, ACCOUNTS_TABLE, INTCALC_ACCOUNTS_FIXTURE
        )
        lastAcctId = LastCategoryBalanceAccount(connection)
        lastAccountTexts = TableAccountTexts(connection, lastAcctId)
    assert differences == []
    assert lastAccountTexts == FixtureAccountTexts(POSTTRAN_ACCOUNTS_FIXTURE, lastAcctId)


def test_systran_ids_use_fixed_parm_prefix_and_d_format_clock(
    batchEnvironment: BatchEnvironment,
) -> None:
    """TRAN-ID is the PARM 2022071800 then the suffix from 000001; both timestamps are form D."""
    RunSampleChainThroughIntcalc()
    records = PublishedSystranRecords(batchEnvironment)
    assert records
    expectedSuffixes = [f"{suffix:0{SUFFIX_DIGITS}d}" for suffix in range(1, len(records) + 1)]
    clockText = legacy_oracle.DB2_CLOCK_TEXT
    assert {record[PARM_DATE_SLICE] for record in records} == {DEFAULT_PARM_DATE}
    assert [record[TRAN_ID_SUFFIX_SLICE] for record in records] == expectedSuffixes
    assert {(record[ORIG_TS_SLICE], record[PROC_TS_SLICE]) for record in records} == {
        (clockText, clockText)
    }


def test_run_date_override_changes_tran_id_prefix(batchEnvironment: BatchEnvironment) -> None:
    """--run-date replaces PARM-DATE, the TRAN-ID prefix, and nothing else of any record."""
    RunSampleChainThroughIntcalc((*INTCALC_ARGUMENTS, RUN_DATE_OPTION, OVERRIDE_PARM_DATE))
    records = PublishedSystranRecords(batchEnvironment)
    fixtureRecords = compare.ReadFixedWidthRecords(SYSTRAN_FIXTURE)
    assert records == [
        OVERRIDE_PARM_DATE + record[PARM_DATE_SLICE.stop :] for record in fixtureRecords
    ]


# --- Synthetic edge cases, predicted by the oracle ----------------------------------------------
def test_empty_category_balance_input_exits_0_and_rewrites_nothing(
    batchEnvironment: BatchEnvironment,
) -> None:
    """An empty TCATBALF ends the loop at its first READ: no rewrite, an empty SYSTRAN, exit 0."""
    SetCategoryBalances(batchEnvironment.engine, ())
    run = RunSyntheticIntcalc(batchEnvironment)
    assert run.exitCode == run.expected.exitCode == EXIT_OK, run.output
    assert run.expected.state.accounts == run.before.accounts
    assert run.after.accounts == run.before.accounts
    assert run.systranRecords == run.expected.systranRecords == []


def test_single_account_input_rewrites_nothing(batchEnvironment: BatchEnvironment) -> None:
    """One account's interest is written to SYSTRAN, but that last account is never rewritten."""
    ArrangeScenario(batchEnvironment.engine, SINGLE_ACCOUNT_SCENARIO)
    run = RunSyntheticIntcalc(batchEnvironment)
    assert run.exitCode == run.expected.exitCode == EXIT_OK, run.output
    assert run.systranRecords == run.expected.systranRecords
    amounts = [legacy_oracle.RecordAmount(record) for record in run.expected.systranRecords]
    assert amounts == [SINGLE_ACCOUNT_INTEREST]
    assert run.after.accounts == run.expected.state.accounts == run.before.accounts


def test_multi_account_input_matches_oracle_including_unflushed_last_account(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Each account change rewrites the previous account; the last one keeps its fields.

    Covers negative balances, varied rates, a rate-0 balance that writes nothing and a group
    with no disclosure record that falls back to DEFAULT.
    """
    ArrangeScenario(batchEnvironment.engine, MULTI_ACCOUNT_SCENARIO)
    run = RunSyntheticIntcalc(batchEnvironment)
    assert run.exitCode == run.expected.exitCode == EXIT_OK, run.output
    assert run.systranRecords == run.expected.systranRecords
    expectedCount = len(MULTI_ACCOUNT_SCENARIO.balances) - MULTI_ACCOUNT_SKIPPED_RECORDS
    assert len(run.expected.systranRecords) == expectedCount
    assert run.after.accounts == run.expected.state.accounts
    for acctId in FLUSHED_ACCOUNT_IDS:
        assert CycleFields(run.after, acctId) == ZEROED_CYCLE_FIELDS
    assert AccountFields(run.after, LAST_ACCOUNT_ID) == AccountFields(run.before, LAST_ACCOUNT_ID)


def test_account_change_reads_first_xref_card_in_ebcdic_card_num_order(
    batchEnvironment: BatchEnvironment,
) -> None:
    """1110-GET-XREF-DATA takes the account's first card in code page 037 card_num order.

    Account 1 gets three more cards, inserted out of key order with its EBCDIC-first card last.
    ASCII order, both descending orders and insertion order each pick another card, so only the
    CXACAIX prime-key order puts FIRST_XREF_CARD_NUM on account 1's SYSTRAN records, as the
    oracle's FirstXrefForAccount predicts.
    """
    engine = batchEnvironment.engine
    ArrangeScenario(engine, MULTI_ACCOUNT_SCENARIO)
    seedCardNums = AccountCardNums(engine, FIRST_XREF_ACCT_ID)
    InsertCompetingCards(engine, FIRST_XREF_ACCT_ID, COMPETING_CARD_NUMS)
    cardNums = AccountCardNums(engine, FIRST_XREF_ACCT_ID)
    assert sorted(cardNums) == sorted([*seedCardNums, *COMPETING_CARD_NUMS])
    assert min(cardNums, key=legacy_oracle.EbcdicSortKey) == FIRST_XREF_CARD_NUM
    wrongFirstCards = WrongOrderFirstCards(seedCardNums, COMPETING_CARD_NUMS)
    assert FIRST_XREF_CARD_NUM not in wrongFirstCards.values(), wrongFirstCards
    run = RunSyntheticIntcalc(batchEnvironment)
    assert run.exitCode == run.expected.exitCode == EXIT_OK, run.output
    assert run.systranRecords == run.expected.systranRecords
    for records in (run.systranRecords, run.expected.systranRecords):
        assert AccountRecordCards(records, FIRST_XREF_ACCT_ID) == {FIRST_XREF_CARD_NUM}


def test_missing_default_disclosure_group_exits_16(batchEnvironment: BatchEnvironment) -> None:
    """No DEFAULT record abends with 999 (exit 16): earlier rewrites stay, SYSTRAN is deleted.

    The per-write rewrite of the account processed before the abend persists, as the oracle's
    partial state predicts, and DISP=(NEW,CATLG,DELETE) catalogs no SYSTRAN generation.
    """
    engine = batchEnvironment.engine
    ArrangeScenario(engine, ABEND_SCENARIO)
    DeleteDisclosureGroup(engine, MISSING_DEFAULT_KEY)
    run = RunSyntheticIntcalc(batchEnvironment)
    assert run.exitCode == run.expected.exitCode == EXIT_ABEND, run.output
    assert StepRow(engine, INTCALC_STEP_NO)["state"] == STEP_FAILED_STATE
    assert run.after.accounts == run.expected.state.accounts
    assert run.after.accounts[ACCOUNT_IDS[0]] != run.before.accounts[ACCOUNT_IDS[0]]
    assert run.systranRecords is None
    assert CatalogedSystranCount(engine) == 0
    assert SystranFiles(batchEnvironment.outputDir) == []


def test_zero_rate_records_write_no_systran_record(batchEnvironment: BatchEnvironment) -> None:
    """Rate 0 skips 1300-COMPUTE-INTEREST, yet every account but the last is still rewritten.

    The rewrite adds a zero total interest and zeroes both cycle fields.
    """
    engine = batchEnvironment.engine
    ArrangeScenario(engine, MULTI_ACCOUNT_SCENARIO)
    SetEveryDisclosureRate(engine, ZERO_RATE)
    run = RunSyntheticIntcalc(batchEnvironment)
    assert run.exitCode == run.expected.exitCode == EXIT_OK, run.output
    assert run.systranRecords == run.expected.systranRecords == []
    assert run.after.accounts == run.expected.state.accounts
    for acctId in FLUSHED_ACCOUNT_IDS:
        expectedFields = {**AccountFields(run.before, acctId), **ZEROED_CYCLE_FIELDS}
        assert AccountFields(run.after, acctId) == expectedFields
    assert AccountFields(run.after, LAST_ACCOUNT_ID) == AccountFields(run.before, LAST_ACCOUNT_ID)


@pytest.mark.parametrize(
    ("startSuffix", "expectedSuffixText"),
    [
        pytest.param(0, "000001", id="first-record"),
        pytest.param(999998, "999999", id="highest-suffix"),
        pytest.param(999999, "000000", id="wraps-to-zero"),
    ],
)
def test_tran_id_suffix_wraps_from_999999_to_000000(
    jobContext: JobContext, startSuffix: int, expectedSuffixText: str
) -> None:
    """ADD 1 TO WS-TRANID-SUFFIX 9(06) has no ON SIZE ERROR, so 999999 wraps to 000000.

    interest_calc exposes 1300-B-WRITE-TX as WriteInterestTransaction, and WS-TRANID-SUFFIX is
    InterestState.tranIdSuffix, a dataclass default rather than a module-level value. The test
    therefore calls that helper with the suffix at startSuffix and a recording writer in place
    of SYSTRAN(+1), and checks the oracle's NextTranIdSuffix against the same cases.
    """
    writer = RecordingWriter()
    state = interest_calc.InterestState(
        account={ACCT_ID_COLUMN: ACCOUNT_IDS[0]},
        xrefCardNum=SYNTHETIC_CARD_NUM,
        tranIdSuffix=startSuffix,
    )
    files = interest_calc.InterestFiles(jobContext, None, writer)
    interest_calc.WriteInterestTransaction(files, state, SYNTHETIC_INTEREST)
    assert legacy_oracle.NextTranIdSuffix(startSuffix) == int(expectedSuffixText)
    assert state.tranIdSuffix == int(expectedSuffixText)
    assert [record[TRAN_ID_SLICE] for record in writer.records] == [
        DEFAULT_PARM_DATE + expectedSuffixText
    ]


# --- The TRAN-CAT-BAL-RECORD display through log masking ----------------------------------------
def MaskedIntcalcMessages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return each captured interest_calc record's message after the batch log masking filter."""
    maskingFilter = PanMaskingFilter()
    intcalcRecords = [
        record for record in caplog.records if record.name == interest_calc.LOGGER.name
    ]
    for record in intcalcRecords:
        maskingFilter.filter(record)
    return [record.getMessage() for record in intcalcRecords]


@pytest.mark.parametrize(
    ("balance", "expectedLine"),
    [
        pytest.param(
            CategoryBalance("00000000001", "01", 1, Decimal("1164.87")),
            "TRAN-CAT-BAL-RECORD TRANCAT-ACCT-ID=00000000001 TRANCAT-TYPE-CD=01 TRANCAT-CD=0001"
            " TRAN-CAT-BAL=1164.87",
            id="first-sample-key",
        ),
        pytest.param(
            CategoryBalance("99999999999", "02", 9999, Decimal("-999999999.99")),
            "TRAN-CAT-BAL-RECORD TRANCAT-ACCT-ID=99999999999 TRANCAT-TYPE-CD=02 TRANCAT-CD=9999"
            " TRAN-CAT-BAL=-999999999.99",
            id="widest-fields-negative-balance",
        ),
    ],
)
def test_record_display_keeps_key_and_balance_readable_through_log_masking(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    balance: CategoryBalance,
    expectedLine: str,
) -> None:
    """DISPLAY TRAN-CAT-BAL-RECORD (line 193) names each field and survives log masking.

    Joined by spaces, the key and balance digits would be one chain of 16 or more digits, which
    the masking filter hides but its last four. ProcessCategoryBalance runs with its account
    already current and the rate read stubbed to 0, so no database is needed; its DEBUG line
    must pass the filter whole, each field after its CVTRA01Y name.
    """
    monkeypatch.setattr(interest_calc, INTEREST_RATE_FUNCTION_NAME, lambda *arguments: ZERO_RATE)
    caplog.set_level(logging.DEBUG, logger=interest_calc.LOGGER.name)
    balanceRow = {
        "acct_id": balance.acctId,
        "tran_type_cd": balance.typeCd,
        "tran_cat_cd": balance.catCd,
        "balance": balance.balance,
    }
    state = interest_calc.InterestState(lastAcctId=balance.acctId)
    files = interest_calc.InterestFiles(None, None, RecordingWriter())
    interest_calc.ProcessCategoryBalance(files, state, balanceRow)
    assert MaskedIntcalcMessages(caplog) == [expectedLine]
