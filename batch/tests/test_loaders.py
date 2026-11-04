"""Replace-load parity, atomicity and reload rules of the nine dataset loader jobs.

Pins the IDCAMS DELETE/DEFINE/REPRO load jobs ACCTFILE, CARDFILE, CUSTFILE, XREFFILE, TRANFILE,
TRANTYPE, TRANCATG, DISCGRP and TCATBALF, chain steps 1-5 and 7-10, as `carddemo-batch load`
replaces them (Technical Specification, Section 0.6.8). DUSRSECJ and the DALYTRAN staging load
have their own modules, test_security_init.py and test_dalytran.py; the DALYTRAN load serves here
only as the atomic loader a SIGKILL interrupts.

- Parity (Section 0.9.1, success item 2): each EBCDIC load leaves rows equal, field for field, to
  batch/tests/fixtures/expected/loader_rows/*.csv: 50, 50, 50, 50, 7, 18, 51 and 50 rows.
  TRANFILE skips the DALYTRAN.PS.INIT priming record and loads no row. The Alembic seed already
  equals every CSV, so each positive EBCDIC, ASCII and TRANFILE case first disturbs the seeded
  rows: it adds one valid row no other row references and overwrites a non-key column in every
  row. A pass therefore proves the load deleted and replaced every row.
- Encodings (Section 0.9.3): the ASCII copies differ from the EBCDIC images the JCL loads in two
  fields only, ACCT-ADDR-ZIP of account 00000000049 and the DEFAULT/07/0001 DIS-INT-RATE.
- Atomicity: a malformed image, or a failure inside the load's own transaction, exits 12 and
  leaves the table as it was; a SIGKILLed load commits nothing and is retried to the clean result.
- History and financial reloads: every load adds a batch.job_runs row and moves its step's
  last_run_id. Once POSTTRAN has run in the OPEN cycle, the ACCTFILE, TCATBALF and TRANFILE
  reloads are refused and change nothing, while TRANTYPE still loads.
"""

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import click
import pytest
from app.core.paths import REPO_ROOT
from app.models import (
    JOB_RUNS,
    Account,
    Card,
    CardXref,
    Customer,
    DailyTransaction,
    DisclosureGroup,
    Transaction,
    TransactionCategory,
    TransactionCategoryBalance,
    TransactionType,
)
from app.seed.image_reader import ENCODING_ASCII, ENCODING_EBCDIC, SAMPLE_IMAGE_NAMES
from sqlalchemy import Engine, Select, Table, func, insert, select, text, update

from batch.loaders import (
    acctfile,
    cardfile,
    custfile,
    discgrp,
    tcatbalf,
    trancatg,
    tranfile,
    trantype,
    xreffile,
)
from batch.loaders.replace_loader import (
    IMAGE_NOT_FOUND_TEMPLATE,
    RECORDS_SKIPPED_KEY,
    ROWS_LOADED_KEY,
)
from batch.orchestration import run_ledger
from batch.orchestration.batch_chain import FindJobDefinition, StepsForJob
from batch.orchestration.job_context import EXIT_FAILURE, EXIT_OK, EXIT_REJECTS
from batch.tests.conftest import (
    INJECTED_ROLLBACK_TEMPLATE,
    LOADER_ROWS_DIR,
    SYNTHETIC_CARD_TABLES,
    BatchEnvironment,
    CountJobRuns,
    InjectAtomicRollbackFailure,
    InvokeCli,
    KillProcess,
    StartCli,
    StepRow,
    SyntheticAccountRow,
    SyntheticCardRow,
    SyntheticCustomerId,
    SyntheticCustomerRow,
    SyntheticTransactionRow,
    WaitUntil,
    WriteSyntheticDailyImage,
)
from batch.tests.reconciliation.compare import CompareTableWithCsv, ReadExpectedCsv


class LoaderCase(NamedTuple):
    """One loader: its `load` command name, table, expected CSV, row count and chain step."""

    cliName: str
    modelTable: Table
    csvName: str
    expectedCount: int
    stepNo: int


class TableDisturbance(NamedTuple):
    """Valid pre-load changes to a case's seeded table, which only a replace-load undoes.

    extraRows are inserted first, as (table, row) pairs, parents before children. No seeded row
    references them, so the deferred foreign keys still hold once the load has deleted them.
    changedValues then overwrite non-key columns in every row of the case's table.
    """

    extraRows: tuple[tuple[Table, Mapping[str, object]], ...]
    changedValues: Mapping[str, object]


# The eight loaders whose datasets have both an EBCDIC image and an ASCII copy, in chain order.
LOADER_CASES = (
    LoaderCase("accounts", Account.__table__, "accounts.csv", 50, 1),
    LoaderCase("cards", Card.__table__, "cards.csv", 50, 2),
    LoaderCase("customers", Customer.__table__, "customers.csv", 50, 3),
    LoaderCase("card-xref", CardXref.__table__, "card_xref.csv", 50, 4),
    LoaderCase("tran-types", TransactionType.__table__, "transaction_types.csv", 7, 7),
    LoaderCase(
        "tran-categories", TransactionCategory.__table__, "transaction_categories.csv", 18, 8
    ),
    LoaderCase("disclosure-groups", DisclosureGroup.__table__, "disclosure_groups.csv", 51, 9),
    LoaderCase(
        "tran-cat-balances",
        TransactionCategoryBalance.__table__,
        "transaction_category_balances.csv",
        50,
        10,
    ),
)
# TRANFILE reads DALYTRAN.PS.INIT, which has no ASCII copy and holds only the priming record.
TRANFILE_CASE = LoaderCase("transactions", Transaction.__table__, "transactions.csv", 0, 5)
ALL_LOADER_CASES = LOADER_CASES + (TRANFILE_CASE,)
CASES_BY_NAME = {loaderCase.cliName: loaderCase for loaderCase in ALL_LOADER_CASES}
ACCOUNTS_CASE = CASES_BY_NAME["accounts"]
DISCLOSURE_CASE = CASES_BY_NAME["disclosure-groups"]
TRAN_TYPES_CASE = CASES_BY_NAME["tran-types"]
CATEGORY_BALANCES_CASE = CASES_BY_NAME["tran-cat-balances"]
# The DALYTRAN staging load, chain step 12, is the atomic loader the SIGKILL case interrupts.
DAILY_CASE = LoaderCase(
    "daily-transactions", DailyTransaction.__table__, "daily_transactions.csv", 300, 12
)

# Each loader's module and the legacy JCL member it replaces.
LOADER_MODULES: dict[str, tuple[ModuleType, str]] = {
    "accounts": (acctfile, "ACCTFILE"),
    "cards": (cardfile, "CARDFILE"),
    "customers": (custfile, "CUSTFILE"),
    "card-xref": (xreffile, "XREFFILE"),
    "transactions": (tranfile, "TRANFILE"),
    "tran-types": (trantype, "TRANTYPE"),
    "tran-categories": (trancatg, "TRANCATG"),
    "disclosure-groups": (discgrp, "DISCGRP"),
    "tran-cat-balances": (tcatbalf, "TCATBALF"),
}

# The two fields in which the ASCII copies differ from the EBCDIC images [data/ASCII/acctdata.txt
# record 49; data/ASCII/discgrp.txt record 34], with each encoding's value.
PINNED_ACCOUNT_ID = "00000000049"
DEFAULT_RATE_KEY = ("DEFAULT", "07", 1)
EBCDIC_ACCOUNT_ZIP = "ZEROAPR"
ASCII_ACCOUNT_ZIP = "A000000000"
EBCDIC_DEFAULT_RATE = Decimal("15.00")
ASCII_DEFAULT_RATE = Decimal("0.00")
ACCOUNT_COLUMNS = Account.__table__.c
DISCLOSURE_COLUMNS = DisclosureGroup.__table__.c
PINNED_ACCOUNT_ZIP_STATEMENT = select(ACCOUNT_COLUMNS.addr_zip).where(
    ACCOUNT_COLUMNS.acct_id == PINNED_ACCOUNT_ID
)
DEFAULT_RATE_STATEMENT = select(DISCLOSURE_COLUMNS.interest_rate).where(
    DISCLOSURE_COLUMNS.group_id == DEFAULT_RATE_KEY[0],
    DISCLOSURE_COLUMNS.tran_type_cd == DEFAULT_RATE_KEY[1],
    DISCLOSURE_COLUMNS.tran_cat_cd == DEFAULT_RATE_KEY[2],
)
# CompareTableWithCsv overrides that turn the EBCDIC-derived CSVs into the ASCII expectation.
ASCII_OVERRIDES: dict[str, Mapping[tuple[object, str], object]] = {
    ACCOUNTS_CASE.modelTable.fullname: {(PINNED_ACCOUNT_ID, "addr_zip"): ASCII_ACCOUNT_ZIP},
    DISCLOSURE_CASE.modelTable.fullname: {(DEFAULT_RATE_KEY, "interest_rate"): ASCII_DEFAULT_RATE},
}
# The key and field CompareTableWithCsv names for each ASCII difference, in table order.
ASCII_DIFFERENCE_FRAGMENTS = (
    "accounts.accounts key=('00000000049',) field=addr_zip",
    "disclosure.disclosure_groups key=('DEFAULT', '07', '1') field=interest_rate",
)
PINNED_DATASET_CASES = (ACCOUNTS_CASE, DISCLOSURE_CASE)

# The positive load cases disturb the seeded table first, because the Alembic seed already equals
# every loader_rows CSV. The disturbed values differ from every seeded value: the seed's rates are
# 0.00, 15.00 and 25.00, and every seeded category balance is 0.00.
DISTURBED_TEXT = "DISTURBED"
DISTURBED_RATE = Decimal("99.99")
DISTURBED_BALANCE = Decimal("123.45")
EXTRA_TRAN_AMOUNT = Decimal("1.00")
EXTRA_ROW_INDEX = 1
EXTRA_TYPE_CD = "99"
EXTRA_CAT_CD = 9999
SEED_TYPE_CD = "01"
SEED_CAT_CD = 1
# The lowest seeded account and card keys, read by the INSERT that adds an extra child row.
FIRST_SEED_ACCOUNT_ID = select(func.min(ACCOUNT_COLUMNS.acct_id)).scalar_subquery()
FIRST_SEED_CARD_NUM = select(func.min(Card.__table__.c.card_num)).scalar_subquery()
EXTRA_CARD_ROW = {**SyntheticCardRow(EXTRA_ROW_INDEX), "acct_id": FIRST_SEED_ACCOUNT_ID}
EXTRA_TYPE_ROW = {"tran_type": EXTRA_TYPE_CD, "description": DISTURBED_TEXT}
EXTRA_CATEGORY_ROW = {
    "tran_type_cd": SEED_TYPE_CD,
    "tran_cat_cd": EXTRA_CAT_CD,
    "description": DISTURBED_TEXT,
}
EXTRA_GROUP_ROW = {
    "group_id": DISTURBED_TEXT,
    "tran_type_cd": SEED_TYPE_CD,
    "tran_cat_cd": SEED_CAT_CD,
    "interest_rate": DISTURBED_RATE,
}
EXTRA_BALANCE_ROW = {
    "acct_id": FIRST_SEED_ACCOUNT_ID,
    "tran_type_cd": SEED_TYPE_CD,
    "tran_cat_cd": EXTRA_CAT_CD,
    "balance": DISTURBED_BALANCE,
}
EXTRA_TRANSACTION_ROW = SyntheticTransactionRow(
    EXTRA_ROW_INDEX, FIRST_SEED_CARD_NUM, EXTRA_TRAN_AMOUNT
)
# The extra cross-reference comes with its own synthetic account, customer and card.
EXTRA_XREF_ROWS = tuple(
    (modelTable, rowBuilder(EXTRA_ROW_INDEX)) for modelTable, rowBuilder in SYNTHETIC_CARD_TABLES
)
DISTURBANCES: dict[str, TableDisturbance] = {
    "accounts": TableDisturbance(
        ((Account.__table__, SyntheticAccountRow(EXTRA_ROW_INDEX)),), {"addr_zip": DISTURBED_TEXT}
    ),
    "cards": TableDisturbance(
        ((Card.__table__, EXTRA_CARD_ROW),), {"embossed_name": DISTURBED_TEXT}
    ),
    "customers": TableDisturbance(
        ((Customer.__table__, SyntheticCustomerRow(EXTRA_ROW_INDEX)),),
        {"first_name": DISTURBED_TEXT},
    ),
    # Every column of card_xref is a key, so each row is pointed at the synthetic customer.
    "card-xref": TableDisturbance(
        EXTRA_XREF_ROWS, {"cust_id": SyntheticCustomerId(EXTRA_ROW_INDEX)}
    ),
    "tran-types": TableDisturbance(
        ((TransactionType.__table__, EXTRA_TYPE_ROW),), {"description": DISTURBED_TEXT}
    ),
    "tran-categories": TableDisturbance(
        ((TransactionCategory.__table__, EXTRA_CATEGORY_ROW),), {"description": DISTURBED_TEXT}
    ),
    "disclosure-groups": TableDisturbance(
        ((DisclosureGroup.__table__, EXTRA_GROUP_ROW),), {"interest_rate": DISTURBED_RATE}
    ),
    "tran-cat-balances": TableDisturbance(
        ((TransactionCategoryBalance.__table__, EXTRA_BALANCE_ROW),), {"balance": DISTURBED_BALANCE}
    ),
    # transactions is empty after the seed, so the extra posted row is the whole disturbance.
    "transactions": TableDisturbance(
        ((Transaction.__table__, EXTRA_TRANSACTION_ROW),), {"tran_desc": DISTURBED_TEXT}
    ),
}

# A size that is not a multiple of the CVACT01Y LRECL 300 fails the EBCDIC framing check.
ACCOUNT_LRECL = 300
TRUNCATED_BYTES = 7
TRUNCATED_IMAGE_DIR_NAME = "truncated-images"
EBCDIC_DIR = REPO_ROOT / "data" / "EBCDIC"
ASCII_DIR = REPO_ROOT / "data" / "ASCII"

POSTTRAN_STEP = 13
# `load transactions` takes no --encoding and no load command takes --force, so Click rejects
# either option as a usage error.
USAGE_ERROR_EXIT_CODE = click.UsageError.exit_code

# The AAP 0.6.8 atomic-interruption case: a 20,000-record synthetic daily file.
SYNTHETIC_RECORD_COUNT = 20000
SYNTHETIC_IMAGE_NAME = "synthetic-daily.PS"
LOCK_WAIT_SECONDS = 120
LOCK_WAIT_DESCRIPTION = "the daily load to hold its row lock on transactions.daily_transactions"
# pg_locks lists every database of the shared server, so only the current database counts, and
# the probing session itself is left out.
ROW_EXCLUSIVE_LOCK_STATEMENT = text(
    "SELECT count(*) FROM pg_locks"
    " WHERE locktype = 'relation' AND mode = 'RowExclusiveLock' AND granted"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND relation = CAST(:tableName AS regclass) AND pid <> pg_backend_pid()"
)


def CaseId(loaderCase: LoaderCase) -> str:
    """Return the pytest id of a loader case: its `load` command name."""
    return loaderCase.cliName


def SelectValue(engine: Engine, statement: Select) -> object:
    """Return the single value a one-row, one-column select reads."""
    with engine.connect() as connection:
        return connection.execute(statement).scalar_one()


def CountRows(engine: Engine, modelTable: Table) -> int:
    """Return the number of rows in modelTable."""
    return SelectValue(engine, select(func.count()).select_from(modelTable))


def TableContents(engine: Engine, modelTable: Table) -> list[tuple]:
    """Return every row of modelTable, generated sort columns left out, in primary-key order."""
    storedColumns = [
        tableColumn for tableColumn in modelTable.columns if tableColumn.computed is None
    ]
    statement = select(*storedColumns).order_by(*modelTable.primary_key.columns)
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(statement)]


def TableDifferences(
    engine: Engine,
    loaderCase: LoaderCase,
    overrides: Mapping[tuple[object, str], object] | None = None,
) -> list[str]:
    """Compare the case's table with its loader_rows CSV; [] when they match field for field."""
    csvPath = LOADER_ROWS_DIR / loaderCase.csvName
    with engine.connect() as connection:
        return CompareTableWithCsv(connection, loaderCase.modelTable.fullname, csvPath, overrides)


def PrimaryKeys(engine: Engine, modelTable: Table) -> set[tuple]:
    """Return the primary key of every row of modelTable, each as a tuple."""
    statement = select(*modelTable.primary_key.columns)
    with engine.connect() as connection:
        return {tuple(row) for row in connection.execute(statement)}


def DisturbTable(engine: Engine, loaderCase: LoaderCase) -> set[tuple]:
    """Apply the case's DISTURBANCES entry in one transaction; return the keys it added."""
    disturbance = DISTURBANCES[loaderCase.cliName]
    keysBefore = PrimaryKeys(engine, loaderCase.modelTable)
    with engine.begin() as connection:
        for modelTable, extraRow in disturbance.extraRows:
            connection.execute(insert(modelTable).values(extraRow))
        connection.execute(update(loaderCase.modelTable).values(disturbance.changedValues))
    return PrimaryKeys(engine, loaderCase.modelTable) - keysBefore


def DisturbBeforeLoad(
    engine: Engine,
    loaderCase: LoaderCase,
    overrides: Mapping[tuple[object, str], object] | None = None,
) -> set[tuple]:
    """Disturb the case's seeded table, assert it no longer matches its CSV; return the extra key.

    The seed already equals the CSV, so only a load that deletes and reinserts every row can then
    pass AssertTableReplaced.
    """
    extraKeys = DisturbTable(engine, loaderCase)
    assert len(extraKeys) == 1, extraKeys
    assert TableDifferences(engine, loaderCase, overrides) != []
    return extraKeys


def AssertTableReplaced(
    engine: Engine,
    loaderCase: LoaderCase,
    extraKeys: set[tuple],
    overrides: Mapping[tuple[object, str], object] | None = None,
) -> None:
    """Assert the table holds exactly its CSV's rows: the count, every field, no extra key."""
    assert CountRows(engine, loaderCase.modelTable) == loaderCase.expectedCount
    assert extraKeys & PrimaryKeys(engine, loaderCase.modelTable) == set()
    assert TableDifferences(engine, loaderCase, overrides) == []


def LatestRunId(engine: Engine, stepNo: int) -> int | None:
    """Return the newest batch.job_runs id recorded against a chain step, of any cycle."""
    statement = select(func.max(JOB_RUNS.c.run_id)).where(JOB_RUNS.c.step_no == stepNo)
    return SelectValue(engine, statement)


def RunRow(engine: Engine, runId: int) -> dict[str, object]:
    """Return the status and counts of one batch.job_runs row."""
    statement = select(JOB_RUNS.c.status, JOB_RUNS.c.counts).where(JOB_RUNS.c.run_id == runId)
    with engine.connect() as connection:
        return dict(connection.execute(statement).mappings().one())


def AssertLoadSucceeds(arguments: list[str]) -> None:
    """Run a carddemo-batch command in process and assert it exits 0."""
    outcome = InvokeCli(arguments)
    assert outcome.exitCode == EXIT_OK, outcome.output


def AssertLoadAddsHistoryRow(engine: Engine, loaderCase: LoaderCase) -> int:
    """Run `load <case>` and assert one more history row and a COMPLETED step pointing at it.

    Returns the new run's id, which is the step's last_run_id.
    """
    historyCountBefore = CountJobRuns(engine, stepNo=loaderCase.stepNo)
    lastRunIdBefore = StepRow(engine, loaderCase.stepNo)["last_run_id"]
    AssertLoadSucceeds(["load", loaderCase.cliName])
    stepRow = StepRow(engine, loaderCase.stepNo)
    assert CountJobRuns(engine, stepNo=loaderCase.stepNo) == historyCountBefore + 1
    assert stepRow["state"] == run_ledger.STEP_COMPLETED
    assert stepRow["last_run_id"] == LatestRunId(engine, loaderCase.stepNo)
    assert stepRow["last_run_id"] != lastRunIdBefore
    assert RunRow(engine, stepRow["last_run_id"])["status"] == run_ledger.RUN_COMPLETED
    return stepRow["last_run_id"]


def LoadInEncoding(loaderCases: tuple[LoaderCase, ...], encoding: str) -> None:
    """Run `load <case> --encoding <encoding>` for each case, asserting each exits 0."""
    for loaderCase in loaderCases:
        AssertLoadSucceeds(["load", loaderCase.cliName, "--encoding", encoding])


def TruncatedImageCopy(directory: Path) -> Path:
    """Copy the ACCTDATA image into a new directory, TRUNCATED_BYTES short; return the directory."""
    imageName = SAMPLE_IMAGE_NAMES[acctfile.LOADER_SPEC.dataset].ebcdicName
    imageBytes = (EBCDIC_DIR / imageName).read_bytes()[:-TRUNCATED_BYTES]
    assert len(imageBytes) % ACCOUNT_LRECL != 0
    directory.mkdir()
    (directory / imageName).write_bytes(imageBytes)
    return directory


def RunPostTran(environment: BatchEnvironment) -> int:
    """Run POSTTRAN as step 13 of the seed cycle with the frozen clock; return the cycle's id.

    The sample daily file holds records CBTRN02C rejects, so the job ends with RC 4.
    """
    outcome = InvokeCli(["job", "post-tran", "--as-of", environment.asOfText])
    assert outcome.exitCode == EXIT_REJECTS, outcome.output
    return StepRow(environment.engine, POSTTRAN_STEP)["cycle_id"]


def ExpectedRefusal(modelTable: Table, cycleId: int) -> str:
    """Return the RELOAD REFUSED literal for a table POSTTRAN, step 13 alone, has changed."""
    return run_ledger.RELOAD_REFUSED_TEMPLATE.format(
        table=modelTable.fullname, steps=str(POSTTRAN_STEP), cycleId=cycleId
    )


def AssertLoadRefused(engine: Engine, loaderCase: LoaderCase, cycleId: int) -> None:
    """Assert `load <case>` exits 12 with the refusal literal, changing no row and no history."""
    contentsBefore = TableContents(engine, loaderCase.modelTable)
    historyCountBefore = CountJobRuns(engine)
    outcome = InvokeCli(["load", loaderCase.cliName])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert ExpectedRefusal(loaderCase.modelTable, cycleId) in outcome.output
    assert TableContents(engine, loaderCase.modelTable) == contentsBefore
    assert CountJobRuns(engine) == historyCountBefore


def SampleCardNumbers(environment: BatchEnvironment) -> list[str]:
    """Return the card numbers of the seeded cards.cards rows."""
    seedCards = environment.snapshot.tableRows[Card.__table__.fullname]
    return [cardRow["card_num"] for cardRow in seedCards]


def HoldsDailyRowLock(engine: Engine) -> bool:
    """Return whether another session holds RowExclusiveLock on transactions.daily_transactions."""
    parameters = {"tableName": DailyTransaction.__table__.fullname}
    with engine.connect() as connection:
        return connection.execute(ROW_EXCLUSIVE_LOCK_STATEMENT, parameters).scalar_one() > 0


def KillDailyLoadInsideTransaction(engine: Engine, imagePath: Path) -> int:
    """Start `load daily-transactions --daily-file imagePath` and SIGKILL it mid-transaction.

    The kill comes once the load's DELETE holds its row lock, before the load commits. Returns
    the killed run's id, still RUNNING because no later command has classified it.
    """
    process = StartCli(["load", DAILY_CASE.cliName, "--daily-file", str(imagePath)])
    try:
        WaitUntil(
            lambda: HoldsDailyRowLock(engine) or process.poll() is not None,
            LOCK_WAIT_SECONDS,
            LOCK_WAIT_DESCRIPTION,
        )
        assert process.poll() is None, f"the daily load ended with {process.returncode} unkilled"
    finally:
        KillProcess(process)
    return LatestRunId(engine, DAILY_CASE.stepNo)


@pytest.mark.parametrize("loaderCase", ALL_LOADER_CASES, ids=CaseId)
def test_loader_module_declares_its_jcl_job_table_and_chain_step(loaderCase: LoaderCase) -> None:
    """Each loader module names its command, its JCL member and its table, at its chain step."""
    loaderModule, jclName = LOADER_MODULES[loaderCase.cliName]
    definition = FindJobDefinition(loaderModule.JOB_NAME)
    assert loaderModule.JOB_NAME == loaderCase.cliName
    assert loaderModule.JCL_NAME == jclName == definition.LegacyName()
    assert definition.runner.moduleName == loaderModule.__name__
    assert loaderModule.LOADER_SPEC.table is loaderCase.modelTable
    assert [step.stepNo for step in StepsForJob(definition)] == [loaderCase.stepNo]


@pytest.mark.parametrize("loaderCase", LOADER_CASES, ids=CaseId)
def test_ebcdic_loader_rows_equal_expected_fixture(
    batchEnvironment: BatchEnvironment, loaderCase: LoaderCase
) -> None:
    """The EBCDIC load REPROs every record: the table equals its CSV, row count included.

    The seeded table is disturbed first, so the load must delete the extra row and restore every
    changed field. Steps 13 and 14 are PENDING after the Alembic seed, so the financial loaders
    run too.
    """
    engine = batchEnvironment.engine
    extraKeys = DisturbBeforeLoad(engine, loaderCase)
    newRunId = AssertLoadAddsHistoryRow(engine, loaderCase)
    expectedCounts = {ROWS_LOADED_KEY: loaderCase.expectedCount, RECORDS_SKIPPED_KEY: 0}
    assert RunRow(engine, newRunId)["counts"] == expectedCounts
    AssertTableReplaced(engine, loaderCase, extraKeys)


@pytest.mark.parametrize("loaderCase", LOADER_CASES, ids=CaseId)
def test_ascii_loader_rows_equal_expected_fixture_except_pinned_differences(
    batchEnvironment: BatchEnvironment, loaderCase: LoaderCase
) -> None:
    """The ASCII copy loads the same rows as the EBCDIC image, apart from the two pinned fields.

    The seeded table is disturbed first, so a pass proves the ASCII load replaced every row.
    """
    engine = batchEnvironment.engine
    overrides = ASCII_OVERRIDES.get(loaderCase.modelTable.fullname)
    extraKeys = DisturbBeforeLoad(engine, loaderCase, overrides)
    LoadInEncoding((loaderCase,), ENCODING_ASCII)
    AssertTableReplaced(engine, loaderCase, extraKeys, overrides)


def test_ebcdic_and_ascii_images_differ_only_in_two_pinned_fields(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Account 00000000049 ZIP and the DEFAULT/07/0001 rate follow the image's encoding."""
    engine = batchEnvironment.engine
    LoadInEncoding(PINNED_DATASET_CASES, ENCODING_EBCDIC)
    assert SelectValue(engine, PINNED_ACCOUNT_ZIP_STATEMENT) == EBCDIC_ACCOUNT_ZIP
    assert SelectValue(engine, DEFAULT_RATE_STATEMENT) == EBCDIC_DEFAULT_RATE
    LoadInEncoding(PINNED_DATASET_CASES, ENCODING_ASCII)
    assert SelectValue(engine, PINNED_ACCOUNT_ZIP_STATEMENT) == ASCII_ACCOUNT_ZIP
    assert SelectValue(engine, DEFAULT_RATE_STATEMENT) == ASCII_DEFAULT_RATE
    differences = [
        difference
        for loaderCase in PINNED_DATASET_CASES
        for difference in TableDifferences(engine, loaderCase)
    ]
    assert len(differences) == len(ASCII_DIFFERENCE_FRAGMENTS), differences
    for fragment, difference in zip(ASCII_DIFFERENCE_FRAGMENTS, differences, strict=True):
        assert fragment in difference


def test_tranfile_skips_priming_record_and_loads_no_rows(
    batchEnvironment: BatchEnvironment,
) -> None:
    """TRANFILE's DALYTRAN.PS.INIT priming record is skipped, so transactions ends up empty.

    The seed leaves transactions empty, so a posted row is added first: the load must delete it.
    """
    engine = batchEnvironment.engine
    extraKeys = DisturbBeforeLoad(engine, TRANFILE_CASE)
    newRunId = AssertLoadAddsHistoryRow(engine, TRANFILE_CASE)
    assert RunRow(engine, newRunId)["counts"] == {ROWS_LOADED_KEY: 0, RECORDS_SKIPPED_KEY: 1}
    _header, expectedRows = ReadExpectedCsv(LOADER_ROWS_DIR / TRANFILE_CASE.csvName)
    assert expectedRows == []
    AssertTableReplaced(engine, TRANFILE_CASE, extraKeys)


def test_tranfile_has_no_ascii_image_and_exits_12(batchEnvironment: BatchEnvironment) -> None:
    """No ASCII copy of DALYTRAN.PS.INIT exists, so TRANFILE cannot load one.

    `load transactions` offers no --encoding, so asking for ASCII is a usage error, and pointing
    --source-dir at the ASCII images finds no TRANFILE image: exit 12. Neither touches the table.
    """
    engine = batchEnvironment.engine
    usageOutcome = InvokeCli(["load", TRANFILE_CASE.cliName, "--encoding", ENCODING_ASCII])
    assert usageOutcome.exitCode == USAGE_ERROR_EXIT_CODE, usageOutcome.output
    missingOutcome = InvokeCli(["load", TRANFILE_CASE.cliName, "--source-dir", str(ASCII_DIR)])
    assert missingOutcome.exitCode == EXIT_FAILURE, missingOutcome.output
    missingPath = ASCII_DIR / SAMPLE_IMAGE_NAMES[tranfile.LOADER_SPEC.dataset].ebcdicName
    expectedMessage = IMAGE_NOT_FOUND_TEMPLATE.format(
        dataset=tranfile.LOADER_SPEC.dataset, path=missingPath
    )
    assert expectedMessage in missingOutcome.output
    assert CountRows(engine, Transaction.__table__) == 0


def test_malformed_image_exits_12_and_leaves_table_unchanged(
    batchEnvironment: BatchEnvironment, tmp_path: Path
) -> None:
    """An ACCTDATA image cut short of a whole record exits 12 and rolls the load back.

    run_ledger.RecordFailure records the run FAILED and its step FAILED; the atomic step has
    rolled back completely, so the next load retries it to COMPLETED.
    """
    engine = batchEnvironment.engine
    sourceDir = TruncatedImageCopy(tmp_path / TRUNCATED_IMAGE_DIR_NAME)
    outcome = InvokeCli(["load", ACCOUNTS_CASE.cliName, "--source-dir", str(sourceDir)])
    assert outcome.exitCode == EXIT_FAILURE, outcome.output
    assert TableDifferences(engine, ACCOUNTS_CASE) == []
    stepRow = StepRow(engine, ACCOUNTS_CASE.stepNo)
    assert stepRow["state"] == run_ledger.STEP_FAILED
    assert stepRow["last_run_id"] == LatestRunId(engine, ACCOUNTS_CASE.stepNo)
    assert RunRow(engine, stepRow["last_run_id"])["status"] == run_ledger.RUN_FAILED
    AssertLoadAddsHistoryRow(engine, ACCOUNTS_CASE)
    assert TableDifferences(engine, ACCOUNTS_CASE) == []


def test_atomic_rollback_inside_loader_transaction_restores_clean_table(
    batchEnvironment: BatchEnvironment,
) -> None:
    """A failure at the load's final ledger write rolls back its DELETE and inserts with it."""
    engine = batchEnvironment.engine
    # A scoped MonkeyPatch undoes only the injection: undoing the test's own monkeypatch would
    # also undo the outputDir fixture's CARDDEMO_OUTPUT_DIR.
    with pytest.MonkeyPatch.context() as failurePatch:
        InjectAtomicRollbackFailure(failurePatch, ACCOUNTS_CASE.stepNo)
        failedOutcome = InvokeCli(["load", ACCOUNTS_CASE.cliName])
    assert failedOutcome.exitCode == EXIT_FAILURE, failedOutcome.output
    injectedMessage = INJECTED_ROLLBACK_TEMPLATE.format(stepNo=ACCOUNTS_CASE.stepNo)
    assert injectedMessage in failedOutcome.output
    assert TableDifferences(engine, ACCOUNTS_CASE) == []
    AssertLoadAddsHistoryRow(engine, ACCOUNTS_CASE)
    assert TableDifferences(engine, ACCOUNTS_CASE) == []


@pytest.mark.slow
def test_sigkilled_atomic_loader_is_retried_to_clean_result(
    batchEnvironment: BatchEnvironment, tmp_path: Path
) -> None:
    """A load killed inside its transaction commits nothing; the next load reclassifies and reruns.

    The killed run becomes INTERRUPTED, step 12 returns to PENDING and the retry completes it.
    """
    engine = batchEnvironment.engine
    imagePath = WriteSyntheticDailyImage(
        tmp_path / SYNTHETIC_IMAGE_NAME, SYNTHETIC_RECORD_COUNT, SampleCardNumbers(batchEnvironment)
    )
    killedRunId = KillDailyLoadInsideTransaction(engine, imagePath)
    assert CountRows(engine, DailyTransaction.__table__) == DAILY_CASE.expectedCount
    assert RunRow(engine, killedRunId)["status"] == run_ledger.RUN_RUNNING
    retryRunId = AssertLoadAddsHistoryRow(engine, DAILY_CASE)
    assert retryRunId > killedRunId
    assert RunRow(engine, killedRunId)["status"] == run_ledger.RUN_INTERRUPTED
    assert TableDifferences(engine, DAILY_CASE) == []


def test_load_accounts_after_alembic_seed_adds_history_row_and_moves_last_run_id(
    batchEnvironment: BatchEnvironment,
) -> None:
    """Straight after the Alembic seed, ACCTFILE runs as step 1 of the SEED cycle."""
    engine = batchEnvironment.engine
    seedStepRow = StepRow(engine, ACCOUNTS_CASE.stepNo)
    assert seedStepRow["state"] == run_ledger.STEP_COMPLETED
    assert seedStepRow["last_run_id"] is not None
    newRunId = AssertLoadAddsHistoryRow(engine, ACCOUNTS_CASE)
    assert newRunId > seedStepRow["last_run_id"]
    assert StepRow(engine, ACCOUNTS_CASE.stepNo)["cycle_id"] == seedStepRow["cycle_id"]


def test_load_accounts_twice_in_succession_succeeds_each_time(
    batchEnvironment: BatchEnvironment,
) -> None:
    """A replace-load gives the same state every time, so a repeated load runs again."""
    engine = batchEnvironment.engine
    firstRunId = AssertLoadAddsHistoryRow(engine, ACCOUNTS_CASE)
    secondRunId = AssertLoadAddsHistoryRow(engine, ACCOUNTS_CASE)
    assert secondRunId > firstRunId
    assert TableDifferences(engine, ACCOUNTS_CASE) == []


def test_load_accounts_refused_after_posttran_and_balances_unchanged(
    batchEnvironment: BatchEnvironment,
) -> None:
    """After POSTTRAN, ACCTFILE would undo the posted balances, so it is refused.

    The load command has no --force option, so --force cannot override the refusal either.
    """
    engine = batchEnvironment.engine
    cycleId = RunPostTran(batchEnvironment)
    assert TableDifferences(engine, ACCOUNTS_CASE) != []
    AssertLoadRefused(engine, ACCOUNTS_CASE, cycleId)
    postedAccounts = TableContents(engine, ACCOUNTS_CASE.modelTable)
    forcedOutcome = InvokeCli(["load", ACCOUNTS_CASE.cliName, "--force"])
    assert forcedOutcome.exitCode == USAGE_ERROR_EXIT_CODE, forcedOutcome.output
    assert TableContents(engine, ACCOUNTS_CASE.modelTable) == postedAccounts


@pytest.mark.parametrize("loaderCase", (CATEGORY_BALANCES_CASE, TRANFILE_CASE), ids=CaseId)
def test_load_tran_cat_balances_and_transactions_refused_after_posttran(
    batchEnvironment: BatchEnvironment, loaderCase: LoaderCase
) -> None:
    """POSTTRAN writes category balances and transactions, so TCATBALF and TRANFILE are refused."""
    engine = batchEnvironment.engine
    cycleId = RunPostTran(batchEnvironment)
    assert TableDifferences(engine, loaderCase) != []
    AssertLoadRefused(engine, loaderCase, cycleId)


def test_load_tran_types_accepted_after_posttran(batchEnvironment: BatchEnvironment) -> None:
    """No later step writes the transaction types, so TRANTYPE loads in any cycle state."""
    engine = batchEnvironment.engine
    RunPostTran(batchEnvironment)
    AssertLoadAddsHistoryRow(engine, TRAN_TYPES_CASE)
    assert TableDifferences(engine, TRAN_TYPES_CASE) == []
