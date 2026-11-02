"""Build the committed reconciliation fixtures from the independent legacy oracle.

The repository holds no legacy batch outputs, so the expected outputs of the sample chain are
derived once by ``legacy_oracle`` and committed under ``batch/tests/fixtures/expected``: the
loader rows (ACCTFILE ... DUSRSECJ and the DALYTRAN staging load), POSTTRAN
(``jcl/POSTTRAN.jcl``), INTCALC (``jcl/INTCALC.jcl``), the second TRANBKP (``jcl/TRANBKP.jcl``),
COMBTRAN (``jcl/COMBTRAN.jcl``), CREASTMT (``jcl/CREASTMT.JCL``) and the 2022-07-18 TRANREPT
report (``proc/TRANREPT.prc``).

To rebuild every fixture, run the following command from the repository root:

```bash
.venv/bin/python -m batch.tests.reconciliation.build_expected
```

The input is the EBCDIC images in ``data/EBCDIC``, which are what the JCL loads. The fixed clock
``2022-07-18-00.00.00.000000`` (``--as-of 2022-07-18T00:00:00``) applies only to clock-generated
fields. The builder needs no database, settings or environment variables, and imports only the
standard library and ``legacy_oracle``, never ``app`` or ``batch`` production code. A second run
writes byte-identical files. A human review of the committed fixture diff is the gate for every
later change.
"""

import csv
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from . import legacy_oracle

REPO_ROOT_DEPTH = 3
EXPECTED_RELATIVE_DIR = Path("batch/tests/fixtures/expected")
LOADER_ROWS_DIR_NAME = "loader_rows"
EBCDIC_RELATIVE_DIR = Path("data/EBCDIC")

LINE_TERMINATOR = "\n"
CSV_ENCODING = "utf-8"
RECORD_ENCODING = "latin-1"
REPORT_ENCODING = "utf-8"
CSV_SUFFIX = ".csv"

# (file name, table) of the loader_rows CSVs, in output order. Daily rows come from the staged
# DALYTRAN records; every other table comes from the state the loaders leave.
LOADER_ROW_FIXTURES = (
    ("accounts.csv", legacy_oracle.ACCOUNTS_TABLE),
    ("cards.csv", legacy_oracle.CARDS_TABLE),
    ("customers.csv", legacy_oracle.CUSTOMERS_TABLE),
    ("card_xref.csv", legacy_oracle.CARD_XREF_TABLE),
    ("transactions.csv", legacy_oracle.TRANSACTIONS_TABLE),
    ("daily_transactions.csv", legacy_oracle.DAILY_TRANSACTIONS_TABLE),
    ("transaction_types.csv", legacy_oracle.TRANSACTION_TYPES_TABLE),
    ("transaction_categories.csv", legacy_oracle.TRANSACTION_CATEGORIES_TABLE),
    ("disclosure_groups.csv", legacy_oracle.DISCLOSURE_GROUPS_TABLE),
    ("transaction_category_balances.csv", legacy_oracle.CATEGORY_BALANCES_TABLE),
    ("users.csv", legacy_oracle.USERS_TABLE),
)
# (file name, table) of the CSVs taken from the state POSTTRAN and INTCALC leave.
POSTTRAN_TABLE_FIXTURES = (
    ("posttran_accounts.csv", legacy_oracle.ACCOUNTS_TABLE),
    ("posttran_tcatbal.csv", legacy_oracle.CATEGORY_BALANCES_TABLE),
    ("posttran_transactions.csv", legacy_oracle.TRANSACTIONS_TABLE),
)
INTCALC_TABLE_FIXTURES = (("intcalc_accounts.csv", legacy_oracle.ACCOUNTS_TABLE),)


def WriteCsvFixture(path: Path, tableName: str, rows: Iterable[Mapping[str, object]]) -> Path:
    """Write one table as CSV: the TableSpec columns as header, then one CsvValue row per row.

    Rows are written in the order given, which StateRows and DailyRows already make the
    fixture order (VSAM key order, or record_seq for daily rows).
    """
    columns = legacy_oracle.ResolveTableSpec(tableName).columns
    with path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle, lineterminator=LINE_TERMINATOR)
        writer.writerow(columns)
        writer.writerows(
            [legacy_oracle.CsvValue(row[column]) for column in columns] for row in rows
        )
    return path


def RequireSingleLineRecords(path: Path, records: Sequence[str]) -> None:
    """Refuse a record holding CR or LF, which would split it when the terminators are removed.

    Raises:
        ValueError: A record contains a line-break character.
    """
    for recordNumber, record in enumerate(records, start=1):
        if "\r" in record or LINE_TERMINATOR in record:
            raise ValueError(f"{path}: record {recordNumber} contains a line-break character")


def WriteRecordFixture(path: Path, records: Sequence[str], encoding: str) -> Path:
    """Write fixed-width records or lines, each followed by one LF; no records give 0 bytes."""
    RequireSingleLineRecords(path, records)
    content = "".join(record + LINE_TERMINATOR for record in records)
    path.write_bytes(content.encode(encoding))
    return path


def LoaderTableRows(chain: legacy_oracle.ChainOutcome, tableName: str) -> list[dict[str, object]]:
    """Return one table as the loaders of chain steps 1-5 and 7-12 leave it."""
    if tableName == legacy_oracle.DAILY_TRANSACTIONS_TABLE:
        return legacy_oracle.DailyRows(chain.dailyRecords)
    return legacy_oracle.StateRows(chain.loadedState, tableName)


def WriteLoaderRows(directory: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the eleven loader_rows CSVs; users.csv carries no password or hash column."""
    return [
        WriteCsvFixture(directory / fileName, tableName, LoaderTableRows(chain, tableName))
        for fileName, tableName in LOADER_ROW_FIXTURES
    ]


def WriteStateCsvFixtures(
    expectedDir: Path, state: legacy_oracle.OracleState, fixtures: Iterable[tuple[str, str]]
) -> list[Path]:
    """Write one CSV per (file name, table) pair from a job's output state, in VSAM key order."""
    return [
        WriteCsvFixture(
            expectedDir / fileName, tableName, legacy_oracle.StateRows(state, tableName)
        )
        for fileName, tableName in fixtures
    ]


def WritePosttranFixtures(expectedDir: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the POSTTRAN (CBTRN02C) tables and its 430-byte DALYREJS(+1) generation."""
    paths = WriteStateCsvFixtures(expectedDir, chain.posttran.state, POSTTRAN_TABLE_FIXTURES)
    rejectsPath = expectedDir / "posttran_dalyrejs.dat"
    paths.append(WriteRecordFixture(rejectsPath, chain.posttran.rejectRecords, RECORD_ENCODING))
    return paths


def WriteIntcalcFixtures(expectedDir: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the INTCALC (CBACT04C) SYSTRAN(+1) generation and the accounts it rewrote."""
    systranPath = expectedDir / "intcalc_systran.dat"
    paths = [WriteRecordFixture(systranPath, chain.intcalc.systranRecords, RECORD_ENCODING)]
    paths.extend(WriteStateCsvFixtures(expectedDir, chain.intcalc.state, INTCALC_TABLE_FIXTURES))
    return paths


def WriteTranbkpFixtures(expectedDir: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the TRANSACT.BKUP(+1) generation of the second TRANBKP (chain step 15)."""
    return [
        WriteRecordFixture(
            expectedDir / "tranbkp_run2_bkup.dat", chain.bkupRun2.bkupRecords, RECORD_ENCODING
        ),
    ]


def WriteCombtranFixtures(expectedDir: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the TRANSACT.COMBINED(+1) generation: BKUP(0) and SYSTRAN(0) sorted by TRAN-ID."""
    return [
        WriteRecordFixture(
            expectedDir / "combtran_combined.dat", chain.combinedRecords, RECORD_ENCODING
        ),
    ]


def WriteCreastmtFixtures(expectedDir: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the CREASTMT (CBSTM03A) STATEMNT.PS (80) and STATEMNT.HTML (100) lines."""
    statements = chain.statements
    return [
        WriteRecordFixture(
            expectedDir / "creastmt_STATEMNT.PS", statements.psLines, RECORD_ENCODING
        ),
        WriteRecordFixture(
            expectedDir / "creastmt_STATEMNT.HTML", statements.htmlLines, RECORD_ENCODING
        ),
    ]


def WriteTranreptFixtures(expectedDir: Path, chain: legacy_oracle.ChainOutcome) -> list[Path]:
    """Write the 133-column CBTRN03C report lines for 2022-07-18 to 2022-07-18."""
    return [
        WriteRecordFixture(expectedDir / "tranrept_lines.txt", chain.reportLines, REPORT_ENCODING),
    ]


# The job fixture writers in output order, after the loader_rows CSVs.
JOB_FIXTURE_WRITERS: tuple[Callable[[Path, legacy_oracle.ChainOutcome], list[Path]], ...] = (
    WritePosttranFixtures,
    WriteIntcalcFixtures,
    WriteTranbkpFixtures,
    WriteCombtranFixtures,
    WriteCreastmtFixtures,
    WriteTranreptFixtures,
)


def RequireCleanChain(chain: legacy_oracle.ChainOutcome) -> None:
    """Refuse a chain that stopped or a CREASTMT that failed, before any fixture is written.

    Raises:
        legacy_oracle.OracleAbend: The chain status is 8 or more, or CREASTMT did not end at 0.
    """
    if chain.exitCode >= legacy_oracle.CHAIN_STOP_EXIT:
        raise legacy_oracle.OracleAbend(
            f"sample chain ended with status {chain.exitCode}; no fixture written",
            chain.exitCode,
        )
    if chain.statements.exitCode != legacy_oracle.EXIT_OK:
        raise legacy_oracle.OracleAbend(
            f"CREASTMT ended with status {chain.statements.exitCode}; no fixture written",
            chain.statements.exitCode,
        )


def BuildAllFixtures(repoRoot: Path) -> list[Path]:
    """Run the oracle's sample chain and write all 22 fixtures under EXPECTED_RELATIVE_DIR.

    Returns:
        The written paths: the loader_rows CSVs, then the job fixtures, in chain order.

    Raises:
        FileNotFoundError: A sample image is missing from data/EBCDIC.
        legacy_oracle.OracleAbend: The chain abended, so no fixture is written.
    """
    chain = legacy_oracle.RunSampleChain(
        repoRoot / EBCDIC_RELATIVE_DIR, legacy_oracle.DB2_CLOCK_TEXT
    )
    RequireCleanChain(chain)
    expectedDir = repoRoot / EXPECTED_RELATIVE_DIR
    loaderRowsDir = expectedDir / LOADER_ROWS_DIR_NAME
    loaderRowsDir.mkdir(parents=True, exist_ok=True)
    writtenPaths = WriteLoaderRows(loaderRowsDir, chain)
    for writeJobFixtures in JOB_FIXTURE_WRITERS:
        writtenPaths.extend(writeJobFixtures(expectedDir, chain))
    return writtenPaths


def CountFixtureRecords(path: Path) -> int:
    """Return the data rows of a CSV fixture, header excluded, or the lines of any other one."""
    if path.suffix == CSV_SUFFIX:
        with path.open(encoding=CSV_ENCODING, newline="") as handle:
            return len(list(csv.reader(handle))) - 1
    return path.read_bytes().count(LINE_TERMINATOR.encode(RECORD_ENCODING))


def Main() -> int:
    """Build every fixture and print each written path with its row or record count."""
    repoRoot = Path(__file__).resolve().parents[REPO_ROOT_DEPTH]
    for path in BuildAllFixtures(repoRoot):
        unit = "rows" if path.suffix == CSV_SUFFIX else "records"
        print(f"{path.relative_to(repoRoot).as_posix()}: {CountFixtureRecords(path)} {unit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
