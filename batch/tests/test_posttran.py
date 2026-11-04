"""POSTTRAN posting parity and per-write durability (chain step 13, program CBTRN02C).

Pins cbl/CBTRN02C.cbl lines 190-600 as jcl/POSTTRAN.jcl STEP15 runs them, and the write
durability of the Technical Specification, Sections 0.6.8 and 0.6.12:

- the mainline (lines 202-231) validates each staged DALYTRAN record in record_seq order with
  1500-VALIDATE-TRAN: reason 100 (no cross-reference), 101 (no account), 102 (over the credit
  limit) and 103 (received after the account expiry, which overwrites a 102);
- a reject is written to DALYREJS(+1) as the 350-byte daily record and its 80-byte trailer, the
  reason 9(04) and its description X(76); a valid record updates its category balance (2700),
  then its account (2800), then writes its transaction (2900), whose ORIG-TS is copied from the
  daily record and whose PROC-TS is the DB2-format clock;
- RC 4 when any record is rejected; an I/O error or a duplicate key abends 999 (exit 16), leaving
  the earlier records posted, as RECOVERY(NONE) files do, and cataloging no DALYREJS generation,
  as DISP=(NEW,CATLG,DELETE) does; the failed per-write step is refused without --force.

Every expected value comes from the independent oracle (batch/tests/reconciliation) or from its
committed fixtures, never from the code under test.
"""

import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.logging import PanMaskingFilter
from app.models import GDG_GENERATIONS, DailyTransaction, Transaction
from app.services.transactions import cbtrn02c
from sqlalchemy import Engine, Table, delete, func, insert, select
from sqlalchemy.engine import Row
from sqlalchemy.exc import OperationalError

from batch.jobs import posttran
from batch.orchestration import batch_chain, run_ledger
from batch.tests.conftest import (
    EXPECTED_DIR,
    LOADER_ROWS_DIR,
    SYNTHETIC_CREDIT_LIMIT,
    SYNTHETIC_EXPIRY_DATE,
    TESTS_DIR,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
    InsertSyntheticCards,
    InvokeCli,
    StepRow,
    SyntheticCardNum,
    SyntheticDailyValues,
)
from batch.tests.reconciliation import legacy_oracle
from batch.tests.reconciliation.compare import (
    CompareFixedWidthFiles,
    CompareTableWithCsv,
    CompareTableWithRecords,
    LatestPublishedGeneration,
    ReadFixedWidthRecords,
    ReadTableAsOracleState,
)

POSTTRAN_JOB = "post-tran"
POSTTRAN_LEGACY_NAME = "POSTTRAN"
POSTTRAN_STEP_NO = 13
AS_OF_OPTION = "--as-of"
FORCE_OPTION = "--force"
# Exit statuses: MOVE 4 TO RETURN-CODE (lines 229-230), abend 999, and a refused step.
EXIT_REJECTS = legacy_oracle.EXIT_REJECTS
EXIT_ABEND = legacy_oracle.EXIT_ABEND
EXIT_REFUSED = legacy_oracle.EXIT_UTILITY_FAILURE
# The recovery paths a refusal of a FAILED or INTERRUPTED per-write step names (AAP 0.6.8).
RECOVERY_PATH_FRAGMENTS = ("seed-all", "run-all --new-cycle", FORCE_OPTION)

# DISPLAY 'TRANSACTIONS PROCESSED :' WS-TRANSACTION-COUNT and 'TRANSACTIONS REJECTED  :'
# WS-REJECT-COUNT (lines 227-228); both counters are PIC 9(09).
PROCESSED_LABEL = "TRANSACTIONS PROCESSED :"
REJECTED_LABEL = "TRANSACTIONS REJECTED  :"
COUNTER_DIGITS = 9
DALYREJS_BASE = "DALYREJS"
DALYREJS_LAYOUT = "DALYREJS"
DALYREJS_LRECL = legacy_oracle.DALYREJS_LRECL
# WS-VALIDATION-FAIL-REASON 9(04) follows the 350-byte REJECT-TRAN-DATA (lines 176-182).
REASON_SLICE = slice(legacy_oracle.TRANSACTION_LRECL, legacy_oracle.TRANSACTION_LRECL + 4)
PART_FILE_PATTERN = "*.part"
# DISPLAY 'TCATBAL record not found for key : ' FD-TRAN-CAT-KEY '.. Creating.' (lines 476-477).
TCATBAL_CREATE_PREFIX = "TCATBAL record not found for key : "
TCATBAL_CREATE_SUFFIX = ".. Creating."

EBCDIC_DIR = TESTS_DIR.parents[1] / "data" / "EBCDIC"
DAILY_CSV = LOADER_ROWS_DIR / "daily_transactions.csv"
SAMPLE_DAILY_RECORD_COUNT = 300
# Z-GET-DB2-FORMAT-TIMESTAMP of --as-of 2022-07-18T00:00:00, and the ORIG-TS form of DALYTRAN.
FROZEN_CLOCK = datetime(2022, 7, 18)
FORMAT_DB2 = legacy_oracle.FORMAT_DB2
FORMAT_SPACE = legacy_oracle.FORMAT_SPACE
# The 150th staged record fails before it changes anything (AAP 0.6.12): records 1-149 stay.
FAILURE_RECORD_NUMBER = 150
VALIDATION_FUNCTION_NAME = "ValidateDailyTransaction"
CREATE_TCATBAL_FUNCTION_NAME = "CreateCategoryBalance"
INJECTED_STATEMENT = "INJECTED"
INJECTED_IO_MESSAGE = "injected I/O error"

# InsertSyntheticCards numbers its cards from 1, so card index 0 has no cross-reference (100).
UNKNOWN_CARD_NUM = SyntheticCardNum(0)
# One cent over the synthetic credit limit with a zero cycle: WS-TEMP-BAL exceeds it (102).
OVERLIMIT_AMOUNT = SYNTHETIC_CREDIT_LIMIT + Decimal("0.01")
# The day after the synthetic account expiry: the expiry test overwrites the 102 (103).
AFTER_EXPIRY_ORIG_TS = datetime.combine(SYNTHETIC_EXPIRY_DATE + timedelta(days=1), time())
EXPECTED_REJECT_REASONS = ["0100", "0102", "0103"]

# (table, oracle layout) of the datasets CBTRN02C writes: ACCTFILE, TCATBALF and TRANFILE.
POSTED_TABLE_LAYOUTS = (
    (legacy_oracle.ACCOUNTS_TABLE, "ACCOUNT"),
    (legacy_oracle.CATEGORY_BALANCES_TABLE, "TCATBAL"),
    (legacy_oracle.TRANSACTIONS_TABLE, "TRANSACTION"),
)
# (table, CSV fixture) of the same datasets after the sample POSTTRAN.
POSTED_TABLE_FIXTURES = (
    (legacy_oracle.ACCOUNTS_TABLE, EXPECTED_DIR / "posttran_accounts.csv"),
    (legacy_oracle.CATEGORY_BALANCES_TABLE, EXPECTED_DIR / "posttran_tcatbal.csv"),
    (legacy_oracle.TRANSACTIONS_TABLE, EXPECTED_DIR / "posttran_transactions.csv"),
)


@dataclass(frozen=True)
class OracleInputs:
    """The datasets the loaders leave and the DALYTRAN records, as the oracle reads the images."""

    state: legacy_oracle.OracleState
    dailyRecords: list[str]


@pytest.fixture(name="sampleInputs", scope="module")
def SampleInputs() -> OracleInputs:
    """Read the sample state and the 300 DALYTRAN records from the EBCDIC images, once."""
    dailyPath = legacy_oracle.ImagePath(EBCDIC_DIR, legacy_oracle.DALYTRAN_IMAGE)
    return OracleInputs(
        state=legacy_oracle.LoadSampleState(EBCDIC_DIR),
        dailyRecords=legacy_oracle.ReadDailyRecords(dailyPath),
    )


@pytest.fixture(name="samplePosting", scope="module")
def SamplePosting(sampleInputs: OracleInputs) -> legacy_oracle.PosttranOutcome:
    """Return the oracle's POSTTRAN over the whole sample daily file at the frozen clock."""
    return legacy_oracle.RunPosttran(
        sampleInputs.state, sampleInputs.dailyRecords, legacy_oracle.DB2_CLOCK_TEXT
    )


@pytest.fixture(name="postedSample")
def PostedSample(batchEnvironment: BatchEnvironment) -> CliOutcome:
    """Run job post-tran on the seeded sample data, as step 13 of the seed cycle."""
    return RunPosttranCli(batchEnvironment)


@pytest.fixture(name="failedPosttran")
def FailedPosttran(batchEnvironment: BatchEnvironment) -> CliOutcome:
    """Run job post-tran with an I/O error injected at record 150, then remove the injection.

    The injection lives on a MonkeyPatch of its own, so removing it leaves the test's monkeypatch,
    which holds this test's CARDDEMO_OUTPUT_DIR, in place for the reruns.
    """
    with pytest.MonkeyPatch.context() as injection:
        InjectIoErrorAtRecord(injection, FAILURE_RECORD_NUMBER)
        return RunPosttranCli(batchEnvironment)


def RunPosttranCli(environment: BatchEnvironment, force: bool = False) -> CliOutcome:
    """Run carddemo-batch job post-tran in this process at the frozen clock, forced on request."""
    arguments = ["job", POSTTRAN_JOB, AS_OF_OPTION, environment.asOfText]
    if force:
        arguments.append(FORCE_OPTION)
    return InvokeCli(arguments)


def InjectIoErrorAtRecord(monkeypatch: pytest.MonkeyPatch, recordNumber: int) -> None:
    """Make the recordNumber-th 1500-VALIDATE-TRAN raise an I/O error before it changes anything.

    The validation runs once per staged record, inside the unit that has only read and locked
    the record's cross-reference and account, so the failing record writes nothing. The error is
    an OperationalError, which POSTTRAN turns into 9999-ABEND-PROGRAM (exit 16).
    """
    originalValidate = cbtrn02c.ValidateDailyTransaction
    callNumbers = itertools.count(1)

    def FailingValidate(postingInput: cbtrn02c.PostingInput) -> cbtrn02c.RejectOutcome | None:
        if next(callNumbers) == recordNumber:
            raise OperationalError(INJECTED_STATEMENT, {}, OSError(INJECTED_IO_MESSAGE))
        return originalValidate(postingInput)

    monkeypatch.setattr(cbtrn02c, VALIDATION_FUNCTION_NAME, FailingValidate, raising=False)
    monkeypatch.setattr(posttran, VALIDATION_FUNCTION_NAME, FailingValidate, raising=False)


def CounterDisplay(label: str, count: int) -> str:
    """Return the DISPLAY of a label and its PIC 9(09) counter, for example ...:000000300."""
    return f"{label}{count:0{COUNTER_DIGITS}d}"


def ReadState(engine: Engine) -> legacy_oracle.OracleState:
    """Read every dataset of the test database as the oracle's state, through plain SQL."""
    with engine.connect() as connection:
        return ReadTableAsOracleState(connection)


def StateRecords(state: legacy_oracle.OracleState, tableName: str, layoutName: str) -> list[str]:
    """Return one dataset of an oracle state as fixed-width records of the named layout."""
    fields, lrecl = legacy_oracle.LAYOUTS[layoutName]
    return [
        legacy_oracle.FormatRecord(row, fields, lrecl)
        for row in legacy_oracle.StateRows(state, tableName)
    ]


def PostedTableDifferences(engine: Engine, state: legacy_oracle.OracleState) -> list[str]:
    """Compare the accounts, category balances and transactions with an oracle state."""
    with engine.connect() as connection:
        return [
            difference
            for tableName, layoutName in POSTED_TABLE_LAYOUTS
            for difference in CompareTableWithRecords(
                connection, tableName, StateRecords(state, tableName, layoutName), layoutName
            )
        ]


def PublishedRejects(environment: BatchEnvironment) -> list[str] | None:
    """Return the records of DALYREJS(0), or None when no generation is PUBLISHED."""
    with environment.engine.connect() as connection:
        path = LatestPublishedGeneration(connection, DALYREJS_BASE, environment.outputDir)
    if path is None:
        return None
    return ReadFixedWidthRecords(path)


def ExpectedRejects(outcome: legacy_oracle.PosttranOutcome) -> list[str] | None:
    """Return the DALYREJS(+1) records the oracle catalogs: none after an abend (DISP DELETE)."""
    if outcome.exitCode == EXIT_ABEND:
        return None
    return outcome.rejectRecords


def CatalogedDalyrejsCount(engine: Engine) -> int:
    """Return the DALYREJS catalog rows that are COMMITTED or PUBLISHED."""
    statement = (
        select(func.count())
        .select_from(GDG_GENERATIONS)
        .where(
            GDG_GENERATIONS.c.base_name == DALYREJS_BASE,
            GDG_GENERATIONS.c.state.in_((run_ledger.GDG_COMMITTED, run_ledger.GDG_PUBLISHED)),
        )
    )
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def CountRows(engine: Engine, sourceTable: Table) -> int:
    """Return the number of rows of one table."""
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(sourceTable)).scalar_one()


def PartFiles(outputDir: Path) -> list[Path]:
    """Return every uncataloged .part generation file left under the batch output root."""
    return sorted(outputDir.rglob(PART_FILE_PATTERN))


def ReadTimestampRows(engine: Engine, sourceTable: Table) -> dict[str, Row]:
    """Return each row's TRAN-ID with its ORIG-TS and PROC-TS values and format codes."""
    columns = sourceTable.c
    statement = select(
        columns.tran_id,
        columns.orig_ts,
        columns.orig_ts_format,
        columns.proc_ts,
        columns.proc_ts_format,
    )
    with engine.connect() as connection:
        return {row.tran_id: row for row in connection.execute(statement)}


def SyntheticRejectRows(cardNum: str) -> list[dict[str, object]]:
    """Return staged rows 1-3, each a reject, in a form both the table and the oracle accept.

    Row 1 names a card with no cross-reference (100); rows 2 and 3 name cardNum with an amount
    over its credit limit (102), and row 3 arrives after the account expiry (103 overwrites it).
    """
    overrides = (
        {"card_num": UNKNOWN_CARD_NUM},
        {"tran_amt": OVERLIMIT_AMOUNT},
        {"tran_amt": OVERLIMIT_AMOUNT, "orig_ts": AFTER_EXPIRY_ORIG_TS},
    )
    return [
        {"record_seq": recordSeq, **SyntheticDailyValues(recordSeq - 1, cardNum), **override}
        for recordSeq, override in enumerate(overrides, start=1)
    ]


def InsertDailyRows(engine: Engine, rows: list[dict[str, object]]) -> None:
    """Replace the staged DALYTRAN file with rows, in one transaction."""
    stagedTable = DailyTransaction.__table__
    with engine.begin() as connection:
        connection.execute(delete(stagedTable))
        connection.execute(insert(stagedTable), rows)


def DailyRecords(rows: list[dict[str, object]]) -> list[str]:
    """Return staged rows as the 350-byte CVTRA06Y records the oracle reads, in row order."""
    return [
        legacy_oracle.FormatRecord(row, legacy_oracle.DAILY_FIELDS, legacy_oracle.TRANSACTION_LRECL)
        for row in rows
    ]


def test_posttran_on_sample_data_exits_4_with_rejects(
    postedSample: CliOutcome, samplePosting: legacy_oracle.PosttranOutcome
) -> None:
    """RC 4 when WS-REJECT-COUNT > 0, with both counters displayed as the oracle counts them."""
    assert samplePosting.exitCode == EXIT_REJECTS
    assert postedSample.exitCode == samplePosting.exitCode, postedSample.output
    assert CounterDisplay(PROCESSED_LABEL, samplePosting.processedCount) in postedSample.output
    assert CounterDisplay(REJECTED_LABEL, samplePosting.rejectCount) in postedSample.output


def test_posttran_dalyrejs_generation_equals_expected_fixture(
    postedSample: CliOutcome, batchEnvironment: BatchEnvironment
) -> None:
    """DALYREJS(+1) holds the oracle's 430-byte rejects: daily record, reason and description."""
    with batchEnvironment.engine.connect() as connection:
        generationPath = LatestPublishedGeneration(
            connection, DALYREJS_BASE, batchEnvironment.outputDir
        )
    expectedPath = EXPECTED_DIR / "posttran_dalyrejs.dat"
    assert postedSample.exitCode == EXIT_REJECTS, postedSample.output
    assert CompareFixedWidthFiles(generationPath, expectedPath, DALYREJS_LAYOUT) == []
    recordLengths = {len(record) for record in ReadFixedWidthRecords(generationPath)}
    assert recordLengths == {DALYREJS_LRECL}


def test_posttran_tables_equal_expected_fixtures(
    postedSample: CliOutcome, batchEnvironment: BatchEnvironment
) -> None:
    """Accounts, category balances and transactions equal the oracle's post-POSTTRAN fixtures."""
    assert postedSample.exitCode == EXIT_REJECTS, postedSample.output
    with batchEnvironment.engine.connect() as connection:
        differences = [
            difference
            for tableName, csvPath in POSTED_TABLE_FIXTURES
            for difference in CompareTableWithCsv(connection, tableName, csvPath)
        ]
    assert differences == []


def test_posted_orig_ts_is_copied_from_staged_record_and_proc_ts_is_frozen_clock(
    postedSample: CliOutcome, batchEnvironment: BatchEnvironment
) -> None:
    """ORIG-TS moves unchanged from DALYTRAN (line 436); PROC-TS is the DB2 clock (format D).

    POSTTRAN never updates a staged row: the daily table still equals the loaded image.
    """
    engine = batchEnvironment.engine
    postedRows = ReadTimestampRows(engine, Transaction.__table__)
    stagedRows = ReadTimestampRows(engine, DailyTransaction.__table__)
    with engine.connect() as connection:
        stagedDifferences = CompareTableWithCsv(
            connection, legacy_oracle.DAILY_TRANSACTIONS_TABLE, DAILY_CSV
        )
    assert postedSample.exitCode == EXIT_REJECTS, postedSample.output
    assert postedRows
    for tranId, postedRow in postedRows.items():
        stagedRow = stagedRows[tranId]
        assert (stagedRow.orig_ts_format, postedRow.orig_ts_format) == (FORMAT_SPACE, FORMAT_SPACE)
        assert postedRow.orig_ts == stagedRow.orig_ts
        assert (postedRow.proc_ts, postedRow.proc_ts_format) == (FROZEN_CLOCK, FORMAT_DB2)
    assert stagedDifferences == []
    assert CountRows(engine, DailyTransaction.__table__) == SAMPLE_DAILY_RECORD_COUNT


def test_failure_at_record_150_leaves_records_1_to_149_posted_and_exits_16(
    failedPosttran: CliOutcome,
    batchEnvironment: BatchEnvironment,
    sampleInputs: OracleInputs,
    samplePosting: legacy_oracle.PosttranOutcome,
) -> None:
    """An abend mid-file keeps every earlier write, catalogs no DALYREJS and fails step 13."""
    engine = batchEnvironment.engine
    expected = legacy_oracle.RunPosttran(
        sampleInputs.state,
        sampleInputs.dailyRecords[: FAILURE_RECORD_NUMBER - 1],
        legacy_oracle.DB2_CLOCK_TEXT,
    )
    assert 0 < len(expected.state.transactions) < len(samplePosting.state.transactions)
    assert failedPosttran.exitCode == EXIT_ABEND, failedPosttran.output
    assert PostedTableDifferences(engine, expected.state) == []
    assert CatalogedDalyrejsCount(engine) == 0
    assert PartFiles(batchEnvironment.outputDir) == []
    assert StepRow(engine, POSTTRAN_STEP_NO)["state"] == run_ledger.STEP_FAILED


def test_rerun_after_failed_posttran_is_refused_with_exit_12(
    failedPosttran: CliOutcome, batchEnvironment: BatchEnvironment
) -> None:
    """A FAILED per-write step is not rerun without --force; the refusal names the way out."""
    engine = batchEnvironment.engine
    stateBefore = ReadState(engine)
    runsBefore = CountJobRuns(engine, stepNo=POSTTRAN_STEP_NO)
    cycleId = StepRow(engine, POSTTRAN_STEP_NO)["cycle_id"]
    outcome = RunPosttranCli(batchEnvironment)
    refusal = batch_chain.PER_WRITE_REFUSED_TEMPLATE.format(
        stepNo=POSTTRAN_STEP_NO,
        jobName=POSTTRAN_LEGACY_NAME,
        cycleId=cycleId,
        state=run_ledger.STEP_FAILED,
    )
    assert failedPosttran.exitCode == EXIT_ABEND, failedPosttran.output
    assert outcome.exitCode == EXIT_REFUSED, outcome.output
    assert refusal in outcome.output
    for fragment in (f"STEP {POSTTRAN_STEP_NO}", f"CYCLE {cycleId}", *RECOVERY_PATH_FRAGMENTS):
        assert fragment in outcome.output
    assert ReadState(engine) == stateBefore
    assert CountJobRuns(engine, stepNo=POSTTRAN_STEP_NO) == runsBefore


def test_forced_rerun_restarts_from_partial_state_like_legacy_restart(
    failedPosttran: CliOutcome, batchEnvironment: BatchEnvironment, sampleInputs: OracleInputs
) -> None:
    """--force rereads DALYTRAN from record 1 over the partial state, as a legacy restart does.

    CBTRN02C keeps no checkpoint, so every outcome, the abend on the first already-posted
    TRAN-ID included, is the oracle's prediction from the state the failure left.
    """
    engine = batchEnvironment.engine
    expected = legacy_oracle.RunPosttran(
        ReadState(engine), sampleInputs.dailyRecords, legacy_oracle.DB2_CLOCK_TEXT
    )
    runsBefore = CountJobRuns(engine, stepNo=POSTTRAN_STEP_NO)
    failedRunId = StepRow(engine, POSTTRAN_STEP_NO)["last_run_id"]
    outcome = RunPosttranCli(batchEnvironment, force=True)
    assert failedPosttran.exitCode == EXIT_ABEND, failedPosttran.output
    assert outcome.exitCode == expected.exitCode, outcome.output
    assert CountJobRuns(engine, stepNo=POSTTRAN_STEP_NO) == runsBefore + 1
    assert StepRow(engine, POSTTRAN_STEP_NO)["last_run_id"] != failedRunId
    assert PostedTableDifferences(engine, expected.state) == []
    assert PublishedRejects(batchEnvironment) == ExpectedRejects(expected)


def test_posttran_rejects_reach_dalyrejs_with_reason_trailer(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Reasons 100, 102 and 103 (overwriting 102) reach DALYREJS(+1); none posts a transaction."""
    engine = batchEnvironment.engine
    dailyRows = SyntheticRejectRows(InsertSyntheticCards(engine, 1)[0])
    InsertDailyRows(engine, dailyRows)
    expected = legacy_oracle.RunPosttran(
        ReadState(engine), DailyRecords(dailyRows), legacy_oracle.DB2_CLOCK_TEXT
    )
    outcome = RunPosttranCli(batchEnvironment)
    rejects = PublishedRejects(batchEnvironment)
    assert outcome.exitCode == expected.exitCode == EXIT_REJECTS, outcome.output
    assert rejects == expected.rejectRecords
    assert [record[REASON_SLICE] for record in rejects] == EXPECTED_REJECT_REASONS
    assert PostedTableDifferences(engine, expected.state) == []
    assert CountRows(engine, Transaction.__table__) == 0


def MaskedPosttranMessages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return each captured posttran record's message after the batch log masking filter."""
    maskingFilter = PanMaskingFilter()
    posttranRecords = [record for record in caplog.records if record.name == posttran.LOGGER.name]
    for record in posttranRecords:
        maskingFilter.filter(record)
    return [record.getMessage() for record in posttranRecords]


@pytest.mark.parametrize(
    ("balanceKey", "expectedKeyText"),
    [
        pytest.param(
            ("00000000001", "01", 1),
            "TRANCAT-ACCT-ID=00000000001 TRANCAT-TYPE-CD=01 TRANCAT-CD=0001",
            id="first-sample-key",
        ),
        pytest.param(
            ("99999999999", "1", 9999),
            "TRANCAT-ACCT-ID=99999999999 TRANCAT-TYPE-CD=1  TRANCAT-CD=9999",
            id="widest-digits-space-padded-type",
        ),
    ],
)
def test_tcatbal_create_display_keeps_its_key_readable_through_log_masking(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    balanceKey: tuple[str, str, int],
    expectedKeyText: str,
) -> None:
    """2700-UPDATE-TCATBAL's INVALID KEY display names each key field and survives masking.

    Run together, the 17 key digits would be one chain the masking filter hides but its last
    four, so every account would log alike. WriteCategoryBalance runs with the create stubbed
    out, so no database is needed; its line must pass the filter whole, the account 9(11), type
    X(02) and category 9(04) each after its CVTRA01Y name, between the legacy literals.
    """
    acctId, typeCd, catCd = balanceKey
    balanceRow = {"acct_id": acctId, "tran_type_cd": typeCd, "tran_cat_cd": catCd}
    effects = cbtrn02c.PostingEffects(balanceRow, True, {}, {})
    monkeypatch.setattr(posttran, CREATE_TCATBAL_FUNCTION_NAME, lambda *arguments: None)
    caplog.set_level(logging.INFO, logger=posttran.LOGGER.name)
    posttran.WriteCategoryBalance(None, effects, 1)
    expectedLine = TCATBAL_CREATE_PREFIX + expectedKeyText + TCATBAL_CREATE_SUFFIX
    assert MaskedPosttranMessages(caplog) == [expectedLine]
