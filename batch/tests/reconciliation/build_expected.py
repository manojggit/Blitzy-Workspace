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

Every fixture is rendered in memory first, and the printed counts come from that rendering.
Each one is then staged beside its target as a hidden ``.<name>.<random>.tmp`` file, and only
when all 22 are staged is each renamed into place with ``os.replace``. A reader therefore sees
a whole fixture, old or new, never a partial one, and a target that is a symlink is replaced
rather than written through.

A missing or malformed image, an oracle abend, a target that exists and is neither a regular
file nor a symlink, or a failure while staging leaves every existing fixture untouched. The
builder then prints one line, ``build_expected: <reason>``, to stderr; an image error reads
``<image path>: record <n>: <reason>``. It exits with the oracle's status when the chain stops
(16 for an abend) and 12 for any other failure. A failure during the final renames, or a hard
kill, can leave some fixtures new and some old. Whether publication succeeds or fails, the
builder attempts to delete every staged file it created; a hard kill or a failed cleanup can
leave a ``.tmp`` file behind. Rerunning the command restores the complete fixture set, and a
leftover ``.tmp`` file is safe to delete.

To delete leftover staged files, run the following command from the repository root:

```bash
find batch/tests/fixtures/expected -name '.*.tmp' -type f -delete
```
"""

import csv
import errno
import io
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import legacy_oracle

REPO_ROOT_DEPTH = 3
EXPECTED_RELATIVE_DIR = Path("batch/tests/fixtures/expected")
LOADER_ROWS_DIR_NAME = "loader_rows"
EBCDIC_RELATIVE_DIR = Path("data/EBCDIC")

LINE_TERMINATOR = "\n"
CSV_ENCODING = "utf-8"
RECORD_ENCODING = "latin-1"
# STATEMNT.HTML is UTF-8, the charset its HTML-L04 meta line declares (cbl/CBSTM03A.CBL:153).
HTML_ENCODING = "utf-8"
REPORT_ENCODING = "utf-8"
CSV_COUNT_UNIT = "rows"
RECORD_COUNT_UNIT = "records"

# Staged files are hidden, named after their target, and created in the target's directory, so
# os.replace publishes them atomically on the same file system.
STAGED_PREFIX = "."
STAGED_SUFFIX = ".tmp"
# A published fixture gets the mode a plain open() would give it (0o666 less the umask), not the
# owner-only mode of a mkstemp file.
NEW_FILE_MODE = 0o666
NOT_REPLACEABLE_REASON = "exists and is not a regular file; remove it, then rerun the builder"

MESSAGE_PREFIX = "build_expected: "
# Exit status for an unreadable or malformed image or an unwritable fixture: the batch loaders'
# status for an image format error. An oracle abend exits with its own status instead.
FAILURE_EXIT = 12

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


@dataclass(frozen=True)
class RenderedFixture:
    """One fixture rendered in memory, ready to publish.

    Attributes:
        path: The target file under EXPECTED_RELATIVE_DIR.
        content: The exact bytes of the file.
        count: The data rows of a CSV, header excluded, or the records or lines of any other file.
        unit: CSV_COUNT_UNIT for a CSV, RECORD_COUNT_UNIT for any other file.
    """

    path: Path
    content: bytes
    count: int
    unit: str


def RenderCsvFixture(
    path: Path, tableName: str, rows: Iterable[Mapping[str, object]]
) -> RenderedFixture:
    """Render one table as CSV: the TableSpec columns as header, then one CsvValue row per row.

    Rows are rendered in the order given, which StateRows and DailyRows already make the
    fixture order (VSAM key order, or record_seq for daily rows).

    Raises:
        UnicodeEncodeError: A value has no CSV_ENCODING form.
    """
    columns = legacy_oracle.ResolveTableSpec(tableName).columns
    csvRows = [[legacy_oracle.CsvValue(row[column]) for column in columns] for row in rows]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator=LINE_TERMINATOR)
    writer.writerow(columns)
    writer.writerows(csvRows)
    content = buffer.getvalue().encode(CSV_ENCODING)
    return RenderedFixture(path, content, len(csvRows), CSV_COUNT_UNIT)


def RequireSingleLineRecords(path: Path, records: Sequence[str]) -> None:
    """Refuse a record holding CR or LF, which would split it when the terminators are removed.

    Raises:
        ValueError: A record contains a line-break character.
    """
    for recordNumber, record in enumerate(records, start=1):
        if "\r" in record or LINE_TERMINATOR in record:
            raise ValueError(f"{path}: record {recordNumber} contains a line-break character")


def RenderRecordFixture(path: Path, records: Sequence[str], encoding: str) -> RenderedFixture:
    """Render fixed-width records or lines, each followed by one LF; no records give 0 bytes.

    Raises:
        ValueError: A record contains a line-break character.
        UnicodeEncodeError: A record has no form in encoding.
    """
    RequireSingleLineRecords(path, records)
    content = "".join(record + LINE_TERMINATOR for record in records).encode(encoding)
    return RenderedFixture(path, content, len(records), RECORD_COUNT_UNIT)


def LoaderTableRows(chain: legacy_oracle.ChainOutcome, tableName: str) -> list[dict[str, object]]:
    """Return one table as the loaders of chain steps 1-5 and 7-12 leave it."""
    if tableName == legacy_oracle.DAILY_TRANSACTIONS_TABLE:
        return legacy_oracle.DailyRows(chain.dailyRecords)
    return legacy_oracle.StateRows(chain.loadedState, tableName)


def RenderLoaderRows(directory: Path, chain: legacy_oracle.ChainOutcome) -> list[RenderedFixture]:
    """Render the eleven loader_rows CSVs; users.csv carries no password or hash column."""
    return [
        RenderCsvFixture(directory / fileName, tableName, LoaderTableRows(chain, tableName))
        for fileName, tableName in LOADER_ROW_FIXTURES
    ]


def RenderStateCsvFixtures(
    expectedDir: Path, state: legacy_oracle.OracleState, fixtures: Iterable[tuple[str, str]]
) -> list[RenderedFixture]:
    """Render one CSV per (file name, table) pair from a job's output state, in VSAM key order."""
    return [
        RenderCsvFixture(
            expectedDir / fileName, tableName, legacy_oracle.StateRows(state, tableName)
        )
        for fileName, tableName in fixtures
    ]


def RenderPosttranFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render the POSTTRAN (CBTRN02C) tables and its 430-byte DALYREJS(+1) generation."""
    fixtures = RenderStateCsvFixtures(expectedDir, chain.posttran.state, POSTTRAN_TABLE_FIXTURES)
    rejectsPath = expectedDir / "posttran_dalyrejs.dat"
    fixtures.append(RenderRecordFixture(rejectsPath, chain.posttran.rejectRecords, RECORD_ENCODING))
    return fixtures


def RenderIntcalcFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render the INTCALC (CBACT04C) SYSTRAN(+1) generation and the accounts it rewrote."""
    systranPath = expectedDir / "intcalc_systran.dat"
    fixtures = [RenderRecordFixture(systranPath, chain.intcalc.systranRecords, RECORD_ENCODING)]
    fixtures.extend(
        RenderStateCsvFixtures(expectedDir, chain.intcalc.state, INTCALC_TABLE_FIXTURES)
    )
    return fixtures


def RenderTranbkpFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render the TRANSACT.BKUP(+1) generation of the second TRANBKP (chain step 15)."""
    return [
        RenderRecordFixture(
            expectedDir / "tranbkp_run2_bkup.dat", chain.bkupRun2.bkupRecords, RECORD_ENCODING
        ),
    ]


def RenderCombtranFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render the TRANSACT.COMBINED(+1) generation: BKUP(0) and SYSTRAN(0) sorted by TRAN-ID."""
    return [
        RenderRecordFixture(
            expectedDir / "combtran_combined.dat", chain.combinedRecords, RECORD_ENCODING
        ),
    ]


def RenderCreastmtFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render the CREASTMT (CBSTM03A) STATEMNT.PS (80) and STATEMNT.HTML (100) lines."""
    statements = chain.statements
    return [
        RenderRecordFixture(
            expectedDir / "creastmt_STATEMNT.PS", statements.psLines, RECORD_ENCODING
        ),
        RenderRecordFixture(
            expectedDir / "creastmt_STATEMNT.HTML", statements.htmlLines, HTML_ENCODING
        ),
    ]


def RenderTranreptFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render the 133-column CBTRN03C report lines for 2022-07-18 to 2022-07-18."""
    return [
        RenderRecordFixture(expectedDir / "tranrept_lines.txt", chain.reportLines, REPORT_ENCODING),
    ]


JOB_FIXTURE_RENDERERS: tuple[
    Callable[[Path, legacy_oracle.ChainOutcome], list[RenderedFixture]], ...
] = (
    RenderPosttranFixtures,
    RenderIntcalcFixtures,
    RenderTranbkpFixtures,
    RenderCombtranFixtures,
    RenderCreastmtFixtures,
    RenderTranreptFixtures,
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


def RenderAllFixtures(
    expectedDir: Path, chain: legacy_oracle.ChainOutcome
) -> list[RenderedFixture]:
    """Render all 22 fixtures in memory: the loader_rows CSVs, then the job fixtures.

    Raises:
        ValueError: A record contains a line-break character, or a value cannot be encoded.
    """
    fixtures = RenderLoaderRows(expectedDir / LOADER_ROWS_DIR_NAME, chain)
    for renderJobFixtures in JOB_FIXTURE_RENDERERS:
        fixtures.extend(renderJobFixtures(expectedDir, chain))
    return fixtures


def IsReplaceableTarget(path: Path) -> bool:
    """Return True when os.replace can publish to path: it is absent, a regular file or a symlink.

    Raises:
        OSError: path cannot be examined, for example because a parent is a regular file.
    """
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return True
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def RequireReplaceableTargets(fixtures: Iterable[RenderedFixture]) -> None:
    """Refuse, before staging, a target that exists and is neither a regular file nor a symlink.

    Raises:
        FileExistsError: A target is a directory, a FIFO, a socket or a device; it names the path.
    """
    for fixture in fixtures:
        if not IsReplaceableTarget(fixture.path):
            raise FileExistsError(errno.EEXIST, NOT_REPLACEABLE_REASON, str(fixture.path))


def NewFileMode() -> int:
    """Return the mode open() gives a new file: NEW_FILE_MODE less the process umask."""
    processUmask = os.umask(0)
    os.umask(processUmask)
    return NEW_FILE_MODE & ~processUmask


def StageFixture(fixture: RenderedFixture, fileMode: int, stagedPaths: list[Path]) -> None:
    """Write a fixture's bytes to a new hidden file beside its target, and record that file.

    The staged path is appended to stagedPaths before any byte is written, so the caller can
    delete a partly written file.

    Raises:
        OSError: The directory cannot be created, or the staged file cannot be written.
    """
    fixture.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, stagedName = tempfile.mkstemp(
        suffix=STAGED_SUFFIX,
        prefix=f"{STAGED_PREFIX}{fixture.path.name}.",
        dir=fixture.path.parent,
    )
    stagedPaths.append(Path(stagedName))
    with os.fdopen(descriptor, "wb") as stagedFile:
        os.fchmod(stagedFile.fileno(), fileMode)
        stagedFile.write(fixture.content)


def PublishFixtures(fixtures: Sequence[RenderedFixture]) -> None:
    """Stage every fixture beside its target, then rename each into place with os.replace.

    No target changes until every fixture is staged, so a failure up to then leaves every
    existing fixture untouched. Each os.replace is atomic: a reader sees the old file or the new
    one, never a partial one. Before returning or raising, it attempts to delete every staged file
    still present; a hard kill or a failed cleanup can leave a staged file behind.

    Raises:
        FileExistsError: A target exists and is neither a regular file nor a symlink; nothing is
            staged.
        OSError: A directory or a staged file cannot be created, written or deleted, or a rename
            fails.
    """
    RequireReplaceableTargets(fixtures)
    fileMode = NewFileMode()
    stagedPaths: list[Path] = []
    try:
        for fixture in fixtures:
            StageFixture(fixture, fileMode, stagedPaths)
        for fixture, stagedPath in zip(fixtures, stagedPaths, strict=True):
            os.replace(stagedPath, fixture.path)
    finally:
        for stagedPath in stagedPaths:
            stagedPath.unlink(missing_ok=True)


def BuildAllFixtures(repoRoot: Path) -> list[RenderedFixture]:
    """Run the oracle's sample chain, render all 22 fixtures, then publish them together.

    Returns:
        The published fixtures: the loader_rows CSVs, then the job fixtures, in chain order.

    Raises:
        FileNotFoundError: A sample image is missing from data/EBCDIC; no fixture is written.
        legacy_oracle.ImageRecordError: An image is malformed; it names the image and the
            record, and no fixture is written.
        legacy_oracle.OracleAbend: The chain abended, so no fixture is written.
        ValueError: A fixture cannot be rendered, so no fixture is written.
        OSError: A target exists and is neither a regular file nor a symlink, or staging, a
            rename or the cleanup of a staged file failed.
    """
    chain = legacy_oracle.RunSampleChain(
        repoRoot / EBCDIC_RELATIVE_DIR, legacy_oracle.DB2_CLOCK_TEXT
    )
    RequireCleanChain(chain)
    fixtures = RenderAllFixtures(repoRoot / EXPECTED_RELATIVE_DIR, chain)
    PublishFixtures(fixtures)
    return fixtures


def ReportFailure(message: str, exitCode: int) -> int:
    """Print one diagnostic line, MESSAGE_PREFIX then message, to stderr and return exitCode."""
    print(MESSAGE_PREFIX + message, file=sys.stderr)
    return exitCode


def Main() -> int:
    """Build every fixture and print each published path with its row or record count.

    Returns:
        0 when every fixture is published; the abend status when the oracle chain stops; and
        FAILURE_EXIT when an image, a record or a fixture cannot be read, decoded or written.
    """
    repoRoot = Path(__file__).resolve().parents[REPO_ROOT_DEPTH]
    try:
        fixtures = BuildAllFixtures(repoRoot)
    except legacy_oracle.OracleAbend as abend:
        return ReportFailure(abend.message, abend.exitCode)
    except (OSError, ValueError) as error:
        return ReportFailure(str(error), FAILURE_EXIT)
    for fixture in fixtures:
        print(f"{fixture.path.relative_to(repoRoot).as_posix()}: {fixture.count} {fixture.unit}")
    return legacy_oracle.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(Main())
