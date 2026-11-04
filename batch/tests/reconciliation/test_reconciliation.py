"""Chain parity of the batch port on the sample dataset (AAP 0.9.1 success item 3, rules 0.9.3).

Each test runs the system under test once, as the ``carddemo-batch`` console script in a
subprocess, against a freshly migrated test database:

    carddemo-batch run-all --new-cycle --as-of 2022-07-18T00:00:00

``--new-cycle`` abandons the cycle the Alembic seed opened, so all 18 chain steps run, the
loaders included (AAP 0.6.8); ``--as-of`` freezes every clock-generated field. The test then
compares every chain output with the oracle fixtures in ``batch/tests/fixtures/expected``, which
``build_expected.py`` wrote from ``legacy_oracle.py``, the stdlib-only reading of
``cbl/CBTRN02C.cbl`` (POSTTRAN), ``cbl/CBACT04C.cbl`` (INTCALC), ``jcl/TRANBKP.jcl``,
``jcl/COMBTRAN.jcl`` and ``cbl/CBSTM03A.CBL`` (CREASTMT):

- the tables no later step writes equal their loader rows; accounts equal the INTCALC result,
  category balances the POSTTRAN result, and transactions the COMBTRAN generation;
- the generated ``user_id_sort``, ``card_num_sort`` and ``tran_id_sort`` columns, which
  ``compare.TABLE_SPECS`` leaves out of those field comparisons, are compared separately with the
  code page 037 sort keys of the loaded users and cards and of the COMBTRAN transactions;
- the DALYREJS, SYSTRAN, TRANSACT.BKUP and TRANSACT.COMBINED generations and STATEMNT.PS and
  STATEMNT.HTML equal their fixtures byte for byte, terminators removed.

Report parity is proven in ``backend/tests/unit/services/test_cbtrn03c.py``, because the CLI has
no report operation. Ledger and catalog identifiers and timestamps, absolute generation numbers,
the users audit timestamps and password hashes are never compared: ``compare.TABLE_SPECS``
leaves those columns out, and generations are resolved relatively.

Independence (AAP 0.7.4): this module imports nothing from ``app``, ``batch`` or the batch
conftest. The database URL, the migration and the output directory come only from the conftest
fixtures ``testDatabaseUrl``, ``freshlyMigratedDatabase`` and ``outputDir``, and every SQL read
goes through ``compare``.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from . import compare

# --- Paths and the command under test ------------------------------------------------------------
EXPECTED_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "expected"
LOADER_ROWS_DIR = EXPECTED_DIR / "loader_rows"
REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = Path(sys.executable).parent / "carddemo-batch"
AS_OF_TEXT = "2022-07-18T00:00:00"
CHAIN_ARGUMENTS = ("run-all", "--new-cycle", "--as-of", AS_OF_TEXT)
# POSTTRAN rejects records of the sample daily file (legacy RC 4), every other step exits 0, and
# run-all exits with the highest status of the steps it ran.
EXPECTED_CHAIN_EXIT = 4
CHAIN_TIMEOUT_SECONDS = 1800
OUTPUT_TAIL_LINES = 40
MAX_SHOWN_DIFFERENCES = 20

# --- Environment of the subprocess ---------------------------------------------------------------
SYNC_URL_VARIABLE = "SYNC_DATABASE_URL"
OUTPUT_DIR_VARIABLE = "CARDDEMO_OUTPUT_DIR"
APP_ENV_VARIABLE = "APP_ENV"
TEST_APP_ENV = "test"

# --- Compared outputs ----------------------------------------------------------------------------
# (qualified table, loader-row CSV): the loaded tables no later chain step writes, so their
# post-chain rows are the IDCAMS REPRO loads of jcl/CARDFILE.jcl ... jcl/DUSRSECJ.jcl and the
# DALYTRAN staging load, unchanged.
LOADER_ONLY_TABLES = (
    ("security.users", "users.csv"),
    ("cards.cards", "cards.csv"),
    ("customers.customers", "customers.csv"),
    ("xref.card_xref", "card_xref.csv"),
    ("reference.transaction_types", "transaction_types.csv"),
    ("reference.transaction_categories", "transaction_categories.csv"),
    ("disclosure.disclosure_groups", "disclosure_groups.csv"),
    ("transactions.daily_transactions", "daily_transactions.csv"),
)
ACCOUNTS_TABLE = "accounts.accounts"
CATEGORY_BALANCES_TABLE = "transactions.transaction_category_balances"
TRANSACTIONS_TABLE = "transactions.transactions"
INTCALC_ACCOUNTS_FIXTURE = "intcalc_accounts.csv"
POSTTRAN_TCATBAL_FIXTURE = "posttran_tcatbal.csv"
COMBINED_FIXTURE = "combtran_combined.dat"
TRANSACTION_LAYOUT = "TRANSACTION"
# (GDG base, relative generation, fixture, oracle layout). TRANSACT.BKUP(0) is the chain step 15
# backup of the posted transactions; step 6 wrote the generation before it.
GENERATION_FIXTURES = (
    ("DALYREJS", 0, "posttran_dalyrejs.dat", "DALYREJS"),
    ("SYSTRAN", 0, "intcalc_systran.dat", TRANSACTION_LAYOUT),
    ("TRANSACT.BKUP", 0, "tranbkp_run2_bkup.dat", TRANSACTION_LAYOUT),
    ("TRANSACT.COMBINED", 0, COMBINED_FIXTURE, TRANSACTION_LAYOUT),
)
BACKUP_BASE_NAME = "TRANSACT.BKUP"
FIRST_BACKUP_RELATIVE = -1
# (statement file under <outputDir>/statements, fixture). The file name is also the oracle's
# line-only layout name, so a difference names the line.
STATEMENTS_SUBDIRECTORY = "statements"
STATEMENT_FIXTURES = (
    ("STATEMNT.PS", "creastmt_STATEMNT.PS"),
    ("STATEMNT.HTML", "creastmt_STATEMNT.HTML"),
)
# (table, fixture, oracle layout): the keys each generated EBCDIC sort column is checked against.
# No layout reads the key column of a loader-row CSV; a layout decodes the fixture's records.
SORT_KEY_FIXTURES = (
    pytest.param("security.users", LOADER_ROWS_DIR / "users.csv", None, id="user_id_sort"),
    pytest.param("cards.cards", LOADER_ROWS_DIR / "cards.csv", None, id="card_num_sort"),
    pytest.param(
        TRANSACTIONS_TABLE, EXPECTED_DIR / COMBINED_FIXTURE, TRANSACTION_LAYOUT, id="tran_id_sort"
    ),
)

# --- Messages ------------------------------------------------------------------------------------
CLI_MISSING_MESSAGE = (
    "carddemo-batch is not installed at {path}; install the editable packages with"
    " pip install -e backend -e batch"
)
CHAIN_TIMEOUT_MESSAGE = (
    "carddemo-batch {command} did not end within {timeout} s"
    "\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
)
CHAIN_STATUS_MESSAGE = (
    "carddemo-batch {command} exited {actual}, expected {expected}"
    "\n--- last {tailLines} stdout lines ---\n{stdout}"
    "\n--- last {tailLines} stderr lines ---\n{stderr}"
)
DIFFERENCES_MESSAGE = "{label}: {count} differences; the first {shown}:\n{differences}"
NO_GENERATION_MESSAGE = (
    "{baseName}({relative}) has no PUBLISHED generation in batch.gdg_generations"
)


# --- The chain run -------------------------------------------------------------------------------
@dataclass(frozen=True)
class ChainRunResult:
    """One run-all of the sample chain: its exit status, output and the database and directory.

    The database URL carries the password and the output can be long, so neither is in repr,
    which pytest prints when an assertion on this object fails.
    """

    returnCode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    databaseUrl: str = field(repr=False)
    outputDir: Path


def CapturedText(capturedOutput: str | bytes | None) -> str:
    """Return subprocess output as text; a TimeoutExpired may hold bytes or nothing."""
    if capturedOutput is None:
        return ""
    if isinstance(capturedOutput, bytes):
        return capturedOutput.decode(errors="replace")
    return capturedOutput


def TailText(text: str) -> str:
    """Return the last OUTPUT_TAIL_LINES lines of text, where a failing step logs its reason."""
    return "\n".join(text.splitlines()[-OUTPUT_TAIL_LINES:])


def RequireCliInstalled() -> None:
    """Fail with an install hint when the carddemo-batch console script is missing."""
    if not CLI_PATH.is_file():
        pytest.fail(CLI_MISSING_MESSAGE.format(path=CLI_PATH), pytrace=False)


def ChainEnvironment(databaseUrl: str, outputDir: Path) -> dict[str, str]:
    """Return this process's environment pointed at the test database and output directory."""
    return {
        **os.environ,
        SYNC_URL_VARIABLE: databaseUrl,
        OUTPUT_DIR_VARIABLE: str(outputDir),
        APP_ENV_VARIABLE: TEST_APP_ENV,
    }


def ChainTimeoutText(error: subprocess.TimeoutExpired) -> str:
    """Describe a chain run that outlived CHAIN_TIMEOUT_SECONDS, with what it printed."""
    return CHAIN_TIMEOUT_MESSAGE.format(
        command=" ".join(CHAIN_ARGUMENTS),
        timeout=error.timeout,
        stdout=CapturedText(error.stdout),
        stderr=CapturedText(error.stderr),
    )


def RunSampleChain(databaseUrl: str, outputDir: Path) -> ChainRunResult:
    """Run ``carddemo-batch run-all --new-cycle --as-of AS_OF_TEXT`` from the repository root.

    The subprocess is the only way this module runs the system under test. A missing console
    script or a run past CHAIN_TIMEOUT_SECONDS fails the test; any exit status is returned.
    """
    RequireCliInstalled()
    try:
        completed = subprocess.run(
            [str(CLI_PATH), *CHAIN_ARGUMENTS],
            cwd=REPO_ROOT,
            env=ChainEnvironment(databaseUrl, outputDir),
            capture_output=True,
            text=True,
            timeout=CHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(ChainTimeoutText(error), pytrace=False)
    return ChainRunResult(
        returnCode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        databaseUrl=databaseUrl,
        outputDir=outputDir,
    )


# Function scope, not module scope: the conftest's freshlyMigratedDatabase and outputDir are
# function-scoped, so each test migrates its own database and runs its own chain. A module-scoped
# run would raise ScopeMismatch, or compare one test's outputs against a re-migrated database.
@pytest.fixture(name="reconciledChain")
def ReconciledChain(
    freshlyMigratedDatabase: object, outputDir: Path, testDatabaseUrl: str
) -> ChainRunResult:
    """Run the sample chain once on the freshly migrated test database and return the result.

    freshlyMigratedDatabase is requested for its effect only: the dropped and re-migrated test
    database, seeded by Alembic revision 0002.
    """
    return RunSampleChain(testDatabaseUrl, outputDir)


# --- Comparison helpers --------------------------------------------------------------------------
@contextmanager
def OpenConnection(databaseUrl: str) -> Iterator[Any]:
    """Yield a SQLAlchemy connection to the database the chain ran on; dispose of it afterwards.

    The connection only carries the reads inside ``compare``.
    """
    engine = create_engine(databaseUrl)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def ChainStatusText(chainRun: ChainRunResult) -> str:
    """Describe the chain's exit status with the last lines of its stdout and stderr."""
    return CHAIN_STATUS_MESSAGE.format(
        command=" ".join(CHAIN_ARGUMENTS),
        actual=chainRun.returnCode,
        expected=EXPECTED_CHAIN_EXIT,
        tailLines=OUTPUT_TAIL_LINES,
        stdout=TailText(chainRun.stdout),
        stderr=TailText(chainRun.stderr),
    )


def RequireChainCompleted(chainRun: ChainRunResult) -> None:
    """Fail with the chain's own output unless it ended with the expected status.

    Every comparison test calls it first, so a chain that stopped early fails for one clear
    reason instead of a list of missing outputs.
    """
    if chainRun.returnCode != EXPECTED_CHAIN_EXIT:
        pytest.fail(ChainStatusText(chainRun), pytrace=False)


def AssertNoDifferences(differences: list[str], label: str) -> None:
    """Fail when differences is not empty, showing the label, the count and the first 20."""
    if differences:
        shownDifferences = differences[:MAX_SHOWN_DIFFERENCES]
        failureText = DIFFERENCES_MESSAGE.format(
            label=label,
            count=len(differences),
            shown=len(shownDifferences),
            differences="\n".join(shownDifferences),
        )
        pytest.fail(failureText, pytrace=False)


def AssertTableEqualsCsv(chainRun: ChainRunResult, tableName: str, csvPath: Path) -> None:
    """Fail unless the table's compared columns equal the expected CSV, field for field."""
    RequireChainCompleted(chainRun)
    with OpenConnection(chainRun.databaseUrl) as connection:
        differences = compare.CompareTableWithCsv(connection, tableName, csvPath)
    AssertNoDifferences(differences, tableName)


def GenerationDifferences(connection: Any, chainRun: ChainRunResult) -> list[str]:
    """Compare each GENERATION_FIXTURES generation with its fixture; prefix each message.

    The prefix names the base and its relative generation, for example ``SYSTRAN(0)``.
    """
    differences = []
    for baseName, relative, fixtureName, layoutName in GENERATION_FIXTURES:
        generationPath = compare.LatestPublishedGeneration(
            connection, baseName, chainRun.outputDir, relative
        )
        assert generationPath is not None, NO_GENERATION_MESSAGE.format(
            baseName=baseName, relative=relative
        )
        fileDifferences = compare.CompareFixedWidthFiles(
            generationPath, EXPECTED_DIR / fixtureName, layoutName
        )
        differences.extend(f"{baseName}({relative}): {message}" for message in fileDifferences)
    return differences


def ExpectedSortKeyTexts(
    sortSpec: compare.SortKeySpec, fixturePath: Path, layoutName: str | None
) -> tuple[list[str | None], list[str]]:
    """Return the keys a fixture gives the table, with one message per record that fails to decode.

    A CSV (no layout) gives its key column. A record fixture is decoded at the oracle's copybook
    offsets for layoutName, so the TRAN-IDs come from the oracle's CVTRA05Y layout.
    """
    if layoutName is None:
        _header, csvRows = compare.ReadExpectedCsv(fixturePath)
        return [row[sortSpec.keyColumn] for row in csvRows], []
    records = compare.ReadFixedWidthRecords(fixturePath)
    tableSpec = compare.TABLE_SPECS[sortSpec.tableName]
    rows, messages = compare.ParseExpectedRecords(tableSpec, records, layoutName)
    return [row[sortSpec.keyColumn] for row in rows], messages


# --- Tests ---------------------------------------------------------------------------------------
def test_run_all_new_cycle_exits_with_posttran_reject_status(
    reconciledChain: ChainRunResult,
) -> None:
    """The 18-step chain ends with status 4: POSTTRAN's reject RC (jcl/POSTTRAN.jcl STEP15).

    Every step ran and none stopped the chain, which run-all does at status 8 or above, as the
    legacy COND=(4,LT) steps do.
    """
    assert reconciledChain.returnCode == EXPECTED_CHAIN_EXIT, ChainStatusText(reconciledChain)


def test_loader_only_tables_equal_loader_row_fixtures(reconciledChain: ChainRunResult) -> None:
    """Tables no later step writes keep their loaded rows (IDCAMS REPRO loads, DALYTRAN staging).

    Users are compared without their password hash and audit timestamps.
    """
    RequireChainCompleted(reconciledChain)
    differences = []
    with OpenConnection(reconciledChain.databaseUrl) as connection:
        for tableName, csvName in LOADER_ONLY_TABLES:
            differences.extend(
                compare.CompareTableWithCsv(connection, tableName, LOADER_ROWS_DIR / csvName)
            )
    AssertNoDifferences(differences, "loader-only tables")


def test_accounts_equal_intcalc_accounts_fixture(reconciledChain: ChainRunResult) -> None:
    """Accounts equal the INTCALC (cbl/CBACT04C.cbl) rewrite of the POSTTRAN balances.

    The last account in category-balance key order keeps its post-POSTTRAN balance and cycle
    values, because CBACT04C never flushes the final account.
    """
    AssertTableEqualsCsv(reconciledChain, ACCOUNTS_TABLE, EXPECTED_DIR / INTCALC_ACCOUNTS_FIXTURE)


def test_category_balances_equal_posttran_fixture(reconciledChain: ChainRunResult) -> None:
    """Category balances equal the POSTTRAN (cbl/CBTRN02C.cbl) result after the whole chain.

    INTCALC opens TCATBALF for input only, so no later step changes them.
    """
    AssertTableEqualsCsv(
        reconciledChain, CATEGORY_BALANCES_TABLE, EXPECTED_DIR / POSTTRAN_TCATBAL_FIXTURE
    )


def test_transactions_table_equals_combined_generation_records(
    reconciledChain: ChainRunResult,
) -> None:
    """TRANSACT holds exactly the TRANSACT.COMBINED records COMBTRAN STEP10 reloads.

    The records are decoded with the CVTRA05Y offsets, and timestamps are compared to the
    microsecond together with their format codes (jcl/COMBTRAN.jcl).
    """
    RequireChainCompleted(reconciledChain)
    combinedRecords = compare.ReadFixedWidthRecords(EXPECTED_DIR / COMBINED_FIXTURE)
    with OpenConnection(reconciledChain.databaseUrl) as connection:
        differences = compare.CompareTableWithRecords(
            connection, TRANSACTIONS_TABLE, combinedRecords, TRANSACTION_LAYOUT
        )
    AssertNoDifferences(differences, TRANSACTIONS_TABLE)


@pytest.mark.parametrize(("tableName", "fixturePath", "layoutName"), SORT_KEY_FIXTURES)
def test_generated_sort_columns_equal_cp037_sort_keys(
    reconciledChain: ChainRunResult, tableName: str, fixturePath: Path, layoutName: str | None
) -> None:
    """Each generated EBCDIC sort column holds the code page 037 sort key of its row's key.

    ``compare.TABLE_SPECS`` leaves user_id_sort, card_num_sort and tran_id_sort out of the field
    comparisons, so each is checked against the keys of its fixture: the loaded users and cards,
    and the transactions COMBTRAN STEP10 reloads (jcl/COMBTRAN.jcl).
    """
    RequireChainCompleted(reconciledChain)
    sortSpec = compare.SORT_KEY_SPECS[tableName]
    expectedKeys, differences = ExpectedSortKeyTexts(sortSpec, fixturePath, layoutName)
    with OpenConnection(reconciledChain.databaseUrl) as connection:
        differences.extend(compare.CompareSortKeys(connection, sortSpec, expectedKeys))
    AssertNoDifferences(differences, f"{tableName}.{sortSpec.sortColumn}")


def test_gdg_generations_equal_expected_records(reconciledChain: ChainRunResult) -> None:
    """The newest generations equal the oracle's records byte for byte, terminators removed.

    POSTTRAN DALYREJS(+1), INTCALC SYSTRAN(+1), the second TRANBKP's TRANSACT.BKUP(+1) and
    COMBTRAN's TRANSACT.COMBINED(+1), each resolved as (0) among the PUBLISHED generations.
    """
    RequireChainCompleted(reconciledChain)
    with OpenConnection(reconciledChain.databaseUrl) as connection:
        differences = GenerationDifferences(connection, reconciledChain)
    AssertNoDifferences(differences, "GDG generations")


def test_first_backup_generation_is_empty(reconciledChain: ChainRunResult) -> None:
    """TRANSACT.BKUP(-1), the first TRANBKP's backup (jcl/TRANBKP.jcl), holds no record.

    Chain step 6 archives TRANSACT right after TRANFILE, which skips the one priming record.
    """
    RequireChainCompleted(reconciledChain)
    with OpenConnection(reconciledChain.databaseUrl) as connection:
        backupPath = compare.LatestPublishedGeneration(
            connection, BACKUP_BASE_NAME, reconciledChain.outputDir, FIRST_BACKUP_RELATIVE
        )
    assert backupPath is not None, NO_GENERATION_MESSAGE.format(
        baseName=BACKUP_BASE_NAME, relative=FIRST_BACKUP_RELATIVE
    )
    assert compare.ReadFixedWidthRecords(backupPath) == []


def test_statement_files_equal_creastmt_fixtures(reconciledChain: ChainRunResult) -> None:
    """STATEMNT.PS and STATEMNT.HTML equal the CREASTMT (cbl/CBSTM03A.CBL) statements.

    Both are compared byte for byte, line by line, after the terminators are removed.
    """
    RequireChainCompleted(reconciledChain)
    statementsDir = reconciledChain.outputDir / STATEMENTS_SUBDIRECTORY
    differences = []
    for fileName, fixtureName in STATEMENT_FIXTURES:
        differences.extend(
            compare.CompareFixedWidthFiles(
                statementsDir / fileName, EXPECTED_DIR / fixtureName, fileName
            )
        )
    AssertNoDifferences(differences, "CREASTMT statements")
