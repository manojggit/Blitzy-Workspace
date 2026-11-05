"""DALYTRAN staging load: chain step 12, `carddemo-batch load daily-transactions`.

Pins batch/loaders/dalytran.py, which stages the sequential file AWS.M2.CARDDEMO.DALYTRAN.PS
that jcl/POSTTRAN.jcl reads through its DALYTRAN DD, in the 350-byte DALYTRAN-RECORD layout of
copybook CVTRA06Y, into transactions.daily_transactions (Technical Specification, Sections
0.6.7, 0.6.8, 0.7.3 and 0.9.1). The parity bar: 300 rows, numbered record_seq 1 to 300 in file
order, equal to the oracle fixture loader_rows/daily_transactions.csv, every ORIG-TS kept in
format S and the blank PROC-TS stored as NULL. Also pinned: --daily-file resolved against the
repository root, the ASCII copy staging the same rows, exit 12 on a malformed image with the
staged rows unchanged, no financial-reload refusal after POSTTRAN, and the step 12 history row.

Each test starts from the restored seed state (batchEnvironment), whose staging table already
holds the 300 sample rows, so the parity tests empty the table first to prove the load wrote.
"""

import csv
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest
from app.models import JOB_RUNS, DailyTransaction
from sqlalchemy import Engine, delete, select

from batch.loaders.dalytran import JOB_NAME
from batch.tests.conftest import (
    LOADER_ROWS_DIR,
    REPO_ROOT,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
    InvokeCli,
    StepRow,
)
from batch.tests.reconciliation.compare import CompareTableWithCsv

DAILY_TABLE_NAME = "transactions.daily_transactions"
DAILY_CSV_PATH = LOADER_ROWS_DIR / "daily_transactions.csv"
CSV_ENCODING = "utf-8"
# The image jcl/POSTTRAN.jcl allocates to DD DALYTRAN, as the repository-relative path and the
# absolute path the default --encoding ebcdic stages.
RELATIVE_DAILY_IMAGE = "data/EBCDIC/AWS.M2.CARDDEMO.DALYTRAN.PS"
EBCDIC_DAILY_IMAGE = REPO_ROOT / RELATIVE_DAILY_IMAGE
DAILY_LRECL = 350
DAILY_RECORD_COUNT = 300
PREFIX_RECORD_COUNT = 5
DAILY_STEP_NO = 12
LOAD_DAILY_ARGUMENTS = ("load", JOB_NAME)
DAILY_FILE_OPTION = "--daily-file"
ENCODING_OPTION = "--encoding"
ASCII_ENCODING = "ascii"
POST_TRAN_ARGUMENTS = ("job", "post-tran")
AS_OF_OPTION = "--as-of"
EXIT_OK = 0
# CBTRN02C ends with RETURN-CODE 4 when any daily record is rejected; the sample file has rejects.
EXIT_POSTTRAN_REJECTS = 4
EXIT_FAILURE = 12
RELOAD_REFUSED_TEXT = "RELOAD REFUSED"
# DALYTRAN-ORIG-TS of every sample record, 2022-06-10 19:27:53.000000 (format S), and the
# 26-space DALYTRAN-PROC-TS, stored as NULL with the blank format code.
SAMPLE_ORIG_TS = datetime(2022, 6, 10, 19, 27, 53)
ORIG_TS_FORMAT_SPACE = "S"
BLANK_FORMAT_CODE = " "
STEP_COMPLETED = "COMPLETED"
RUN_COMPLETED = "COMPLETED"
ROWS_LOADED_KEY = "rows_loaded"


def LoadDailyTransactions(extraArguments: Sequence[str] = ()) -> CliOutcome:
    """Run `load daily-transactions` in process with the given options."""
    return InvokeCli([*LOAD_DAILY_ARGUMENTS, *extraArguments])


def ClearStagedRows(engine: Engine) -> None:
    """Empty transactions.daily_transactions, so only a load can put rows back."""
    with engine.begin() as connection:
        connection.execute(delete(DailyTransaction.__table__))


def StagedRows(engine: Engine) -> list[dict[str, object]]:
    """Return every staged row as a column-name dict, in record_seq order."""
    statement = select(DailyTransaction.__table__).order_by(DailyTransaction.record_seq)
    with engine.connect() as connection:
        return [dict(rowMapping) for rowMapping in connection.execute(statement).mappings()]


def StagedRecordSeqs(engine: Engine) -> list[int]:
    """Return the staged record_seq values in ascending order."""
    statement = select(DailyTransaction.record_seq).order_by(DailyTransaction.record_seq)
    with engine.connect() as connection:
        return list(connection.execute(statement).scalars())


def CompareStagedRows(engine: Engine, csvPath: Path) -> list[str]:
    """Compare the staging table with an expected CSV; [] when every field matches."""
    with engine.connect() as connection:
        return CompareTableWithCsv(connection, DAILY_TABLE_NAME, csvPath)


def WriteImagePrefix(targetPath: Path, byteCount: int) -> Path:
    """Write the first byteCount bytes of the EBCDIC daily image to targetPath."""
    targetPath.write_bytes(EBCDIC_DAILY_IMAGE.read_bytes()[:byteCount])
    return targetPath


def WriteCsvPrefix(targetPath: Path, rowCount: int) -> Path:
    """Write the fixture CSV's header and its first rowCount rows to targetPath."""
    with DAILY_CSV_PATH.open(encoding=CSV_ENCODING, newline="") as sourceFile:
        csvLines = list(csv.reader(sourceFile))
    with targetPath.open("w", encoding=CSV_ENCODING, newline="") as targetFile:
        csv.writer(targetFile).writerows(csvLines[: rowCount + 1])
    return targetPath


def LatestStepRun(engine: Engine, stepNo: int) -> dict[str, object]:
    """Return the batch.job_runs row with the highest run_id among those of chain step stepNo."""
    statement = (
        select(JOB_RUNS)
        .where(JOB_RUNS.c.step_no == stepNo)
        .order_by(JOB_RUNS.c.run_id.desc())
        .limit(1)
    )
    with engine.connect() as connection:
        return dict(connection.execute(statement).mappings().one())


def ExpectedRecordSeqs(recordCount: int) -> list[int]:
    """Return record_seq 1 to recordCount, the file order POSTTRAN reads the records in."""
    return list(range(1, recordCount + 1))


def test_daily_load_stages_300_rows_in_record_seq_order_equal_to_fixture(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine = batchEnvironment.engine
    ClearStagedRows(engine)
    outcome = LoadDailyTransactions()
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert StagedRecordSeqs(engine) == ExpectedRecordSeqs(DAILY_RECORD_COUNT)
    assert CompareStagedRows(engine, DAILY_CSV_PATH) == []


def test_staged_orig_ts_keeps_s_format_and_proc_ts_is_null(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine = batchEnvironment.engine
    ClearStagedRows(engine)
    outcome = LoadDailyTransactions()
    assert outcome.exitCode == EXIT_OK, outcome.output
    stagedRows = StagedRows(engine)
    assert len(stagedRows) == DAILY_RECORD_COUNT
    timestampForms = {
        (row["orig_ts"], row["orig_ts_format"], row["proc_ts"], row["proc_ts_format"])
        for row in stagedRows
    }
    assert timestampForms == {(SAMPLE_ORIG_TS, ORIG_TS_FORMAT_SPACE, None, BLANK_FORMAT_CODE)}


def test_daily_file_override_with_absolute_path_loads_that_file(
    batchEnvironment: BatchEnvironment, tmp_path: Path
) -> None:
    engine = batchEnvironment.engine
    imagePath = WriteImagePrefix(tmp_path / "five.PS", PREFIX_RECORD_COUNT * DAILY_LRECL)
    expectedCsvPath = WriteCsvPrefix(tmp_path / "five.csv", PREFIX_RECORD_COUNT)
    outcome = LoadDailyTransactions([DAILY_FILE_OPTION, str(imagePath.resolve())])
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert StagedRecordSeqs(engine) == ExpectedRecordSeqs(PREFIX_RECORD_COUNT)
    assert CompareStagedRows(engine, expectedCsvPath) == []


def test_daily_file_override_with_repo_relative_path_resolves_against_repo_root(
    batchEnvironment: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The working directory holds no data/ tree, so only repository-root resolution finds the
    # image.
    engine = batchEnvironment.engine
    ClearStagedRows(engine)
    monkeypatch.chdir(tmp_path)
    outcome = LoadDailyTransactions([DAILY_FILE_OPTION, RELATIVE_DAILY_IMAGE])
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert StagedRecordSeqs(engine) == ExpectedRecordSeqs(DAILY_RECORD_COUNT)
    assert CompareStagedRows(engine, DAILY_CSV_PATH) == []


def test_ascii_daily_image_loads_identical_rows(batchEnvironment: BatchEnvironment) -> None:
    # Only the accounts and disclosure-group images differ between the encodings.
    engine = batchEnvironment.engine
    ClearStagedRows(engine)
    outcome = LoadDailyTransactions([ENCODING_OPTION, ASCII_ENCODING])
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert StagedRecordSeqs(engine) == ExpectedRecordSeqs(DAILY_RECORD_COUNT)
    assert CompareStagedRows(engine, DAILY_CSV_PATH) == []


def test_malformed_daily_file_exits_12_and_keeps_staged_rows(
    batchEnvironment: BatchEnvironment, tmp_path: Path
) -> None:
    engine = batchEnvironment.engine
    completedBefore = CountJobRuns(engine, stepNo=DAILY_STEP_NO, status=RUN_COMPLETED)
    imagePath = WriteImagePrefix(tmp_path / "short.PS", DAILY_LRECL - 1)
    outcome = LoadDailyTransactions([DAILY_FILE_OPTION, str(imagePath.resolve())])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert StagedRecordSeqs(engine) == ExpectedRecordSeqs(DAILY_RECORD_COUNT)
    assert CompareStagedRows(engine, DAILY_CSV_PATH) == []
    assert CountJobRuns(engine, stepNo=DAILY_STEP_NO, status=RUN_COMPLETED) == completedBefore


def test_daily_load_is_not_a_financial_reload_and_runs_after_posttran(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine = batchEnvironment.engine
    postOutcome = InvokeCli([*POST_TRAN_ARGUMENTS, AS_OF_OPTION, batchEnvironment.asOfText])
    assert postOutcome.exitCode == EXIT_POSTTRAN_REJECTS, postOutcome.output
    # POSTTRAN reads the staged records and never updates or deletes them.
    assert CompareStagedRows(engine, DAILY_CSV_PATH) == []
    outcome = LoadDailyTransactions()
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert RELOAD_REFUSED_TEXT not in outcome.output
    assert CompareStagedRows(engine, DAILY_CSV_PATH) == []


def test_daily_load_adds_step_12_history_row_in_open_cycle(
    batchEnvironment: BatchEnvironment,
) -> None:
    engine = batchEnvironment.engine
    runsBefore = CountJobRuns(engine, stepNo=DAILY_STEP_NO)
    stepBefore = StepRow(engine, DAILY_STEP_NO)
    outcome = LoadDailyTransactions()
    assert outcome.exitCode == EXIT_OK, outcome.output
    assert CountJobRuns(engine, stepNo=DAILY_STEP_NO) == runsBefore + 1
    stepAfter = StepRow(engine, DAILY_STEP_NO)
    newRun = LatestStepRun(engine, DAILY_STEP_NO)
    assert stepAfter["last_run_id"] == newRun["run_id"] != stepBefore["last_run_id"]
    assert stepAfter["cycle_id"] == stepBefore["cycle_id"] == newRun["cycle_id"]
    assert stepAfter["state"] == STEP_COMPLETED
    assert newRun["status"] == RUN_COMPLETED
    assert newRun["counts"][ROWS_LOADED_KEY] == DAILY_RECORD_COUNT
