"""Plain-SQL and file comparators for the batch reconciliation (AAP 0.7.4, 0.9.3).

Checks the system's actual outputs against the oracle's expected fixtures in
``batch/tests/fixtures/expected``:

- Tables are read through SQLAlchemy Core over the caller's connection and compared as the
  normalised text of ``legacy_oracle.CsvValue`` (``Decimal`` at 2 places, ISO dates, timestamps
  to the microsecond beside their format codes, right-trimmed text) with the expected CSVs, or
  with records decoded at the oracle's copybook offsets.
- The generated EBCDIC sort columns ``user_id_sort``, ``card_num_sort`` and ``tran_id_sort``
  (``SORT_KEY_SPECS``), which ``TABLE_SPECS`` leaves out of the field comparison, are compared
  separately with the sort key derived from the code page 037 bytes of each expected key
  (AAP 0.6.5).
- GDG generations (TRANSACT.BKUP, DALYREJS, SYSTRAN, TRANSACT.COMBINED) and the CREASTMT
  statements (STATEMNT.PS, STATEMNT.HTML) are compared byte for byte after their ``\\n``
  terminators are removed. The oracle offsets serve only to name the field that differs.

Like the oracle, this module imports nothing from ``app`` or ``batch``, so a defect shared by the
production code cannot validate itself. Ledger and catalog identifiers, timestamps and absolute
generation numbers, the users audit timestamps and password hashes are never compared:
``TABLE_SPECS`` leaves those columns out, and generations are resolved relatively.
"""

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import column, select, table, text
from sqlalchemy.engine import Connection

from . import legacy_oracle

# The oracle's single definition of every compared table; the column lists live there only.
TABLE_SPECS = legacy_oracle.TABLE_SPECS
GDG_BASE_NAMES = ("TRANSACT.BKUP", "DALYREJS", "SYSTRAN", "TRANSACT.COMBINED")
PUBLISHED_STATE = "PUBLISHED"
# GDG files and STATEMNT.PS hold one byte per character, each line ended by "\n". Latin-1 maps
# every byte to one character, so a UTF-8 STATEMNT.HTML is still compared byte for byte.
FILE_ENCODING = "latin-1"
RECORD_TERMINATOR = "\n"
CSV_ENCODING = "utf-8"
# batch.gdg_generations.file_path is relative to BatchSettings.GdgDir(), which is
# <CARDDEMO_OUTPUT_DIR>/gdg, so a relative path resolves under outputDir / "gdg" (AAP 0.6.8).
GDG_DIRECTORY_NAME = "gdg"
CATALOG_PATH_OUTSIDE_ROOT_TEMPLATE = (
    "catalog file_path {filePath!r} resolves to {resolvedPath}, not inside the GDG root {gdgRoot}"
)
MAX_REPORTED_DIFFERENCES = 50
# Pseudo field kind of the CHAR(1) <timestamp>_format columns, beside the oracle's KIND_* codes.
FORMAT_CODE_KIND = "F"
LATEST_PUBLISHED_GENERATION_SQL = text(
    "SELECT file_path FROM batch.gdg_generations"
    " WHERE base_name = :baseName AND state = :state"
    " ORDER BY generation DESC OFFSET :skip LIMIT 1"
)


@dataclass(frozen=True)
class SortKeySpec:
    """One stored generated column ebcdic_sort_key(keyColumn), which ``TABLE_SPECS`` leaves out.

    tableName is the qualified table, keyColumn the key the column is generated from, and
    sortColumn the generated column.
    """

    tableName: str
    keyColumn: str
    sortColumn: str


# Every generated EBCDIC sort column (AAP 0.6.5, 0.6.7), by qualified table name.
SORT_KEY_SPECS: dict[str, SortKeySpec] = {
    spec.tableName: spec
    for spec in (
        SortKeySpec(legacy_oracle.USERS_TABLE, "user_id", "user_id_sort"),
        SortKeySpec(legacy_oracle.CARDS_TABLE, "card_num", "card_num_sort"),
        SortKeySpec(legacy_oracle.TRANSACTIONS_TABLE, "tran_id", "tran_id_sort"),
    )
}


def NormalizeValue(value: object) -> str:
    """Return the comparison text of one column value, the form the fixture CSVs hold.

    Delegates to ``legacy_oracle.CsvValue``: None is "", a Decimal has 2 places, a date is ISO,
    a datetime is YYYY-MM-DD HH:MM:SS.ffffff, an int is plain digits and text is right-trimmed,
    so a blank CHAR(1) format code is "".

    Raises:
        TypeError: The value is a bool, a float or another type no CardDemo column holds.
    """
    return legacy_oracle.CsvValue(value)


def ReadFixedWidthRecords(path: Path) -> list[str]:
    """Read a GDG or statement file into its records, terminators removed.

    The bytes are decoded as Latin-1 and split on "\\n". Only the final empty element left by
    the trailing terminator is dropped, so an empty file gives [] and an empty record survives.

    Raises:
        FileNotFoundError: The file does not exist.
    """
    content = path.read_bytes().decode(FILE_ENCODING)
    if not content:
        return []
    records = content.split(RECORD_TERMINATOR)
    if records[-1] == "":
        records.pop()
    return records


def ReadTableRows(connection: Connection, spec: legacy_oracle.TableSpec) -> list[dict[str, object]]:
    """Select the compared columns of one table as dicts of raw SQL values.

    The schema, table and column identifiers come only from ``TABLE_SPECS`` and are rendered by
    Core ``table()`` and ``column()`` constructs, never by string formatting.
    """
    schemaName, tableName = spec.name.split(".", 1)
    statement = select(*[column(columnName) for columnName in spec.columns]).select_from(
        table(tableName, schema=schemaName)
    )
    return [dict(row) for row in connection.execute(statement).mappings()]


def ResolveCatalogFilePath(outputDir: Path, filePath: str) -> Path:
    """Resolve a batch.gdg_generations file_path to its archive file, confined to outputDir/gdg.

    A relative file_path, the "<BASE>/G####V00.dat" the batch catalogs, is joined to the GDG
    root; an absolute one is taken as given. Both are canonicalised with ``Path.resolve``, which
    follows symlinks, a symlinked parent directory or final component included, so the
    containment check applies to the file a reader or a deletion would actually reach.

    Args:
        outputDir: The run's CARDDEMO_OUTPUT_DIR, whose gdg/ is the GDG root.
        filePath: The catalog file_path, for example "SYSTRAN/G0001V00.dat".

    Returns:
        The canonical path, strictly inside the canonical GDG root.

    Raises:
        ValueError: The path resolves to the GDG root itself or outside it, or holds a NUL.
    """
    gdgRoot = (outputDir / GDG_DIRECTORY_NAME).resolve()
    resolvedPath = (gdgRoot / filePath).resolve()
    if resolvedPath == gdgRoot or not resolvedPath.is_relative_to(gdgRoot):
        raise ValueError(
            CATALOG_PATH_OUTSIDE_ROOT_TEMPLATE.format(
                filePath=filePath, resolvedPath=resolvedPath, gdgRoot=gdgRoot
            )
        )
    return resolvedPath


def LatestPublishedGeneration(
    connection: Connection, baseName: str, outputDir: Path, relative: int = 0
) -> Path | None:
    """Resolve a relative GDG reference, (0) or (-n), to its archive file.

    Only PUBLISHED catalog rows count, as in AAP 0.6.8, so a COMMITTED, LOST, ROLLED_OFF or
    ABANDONED generation is never resolved. This replaces the DSN=...(0) and (-1) references to
    the LIMIT(5) bases of jcl/DEFGDGB.jcl and jcl/DALYREJS.jcl (catalogued in catlg/LISTCAT.txt).
    Absolute generation numbers are never compared.

    Args:
        connection: An open connection to the database the batch run used.
        baseName: A GDG base, for example "TRANSACT.BKUP" (see ``GDG_BASE_NAMES``).
        outputDir: The run's CARDDEMO_OUTPUT_DIR; relative catalog paths sit under its gdg/.
        relative: 0 for the newest generation, -1 for the one before it, and so on.

    Returns:
        The archive path, canonical and inside outputDir/gdg (``ResolveCatalogFilePath``), or
        None when no PUBLISHED generation matches.

    Raises:
        ValueError: relative is greater than 0, or the catalog file_path resolves outside
            outputDir/gdg.
    """
    if relative > 0:
        raise ValueError(f"relative generation must be 0 or negative, got {relative!r}")
    parameters = {"baseName": baseName, "state": PUBLISHED_STATE, "skip": -relative}
    filePath = connection.execute(LATEST_PUBLISHED_GENERATION_SQL, parameters).scalar_one_or_none()
    if filePath is None:
        return None
    return ResolveCatalogFilePath(outputDir, filePath)


def FieldColumnKinds(fields: Iterable[legacy_oracle.FieldSpec]) -> dict[str, str]:
    """Map every column a layout yields to its field kind, so to its oracle-form conversion.

    A timestamp field also yields its <name>_format column, and record_seq is the position of a
    DALYTRAN record in its file (AAP 0.6.7).
    """
    kinds = {legacy_oracle.RECORD_SEQ_COLUMN: legacy_oracle.KIND_CODE}
    for fieldSpec in fields:
        kinds[fieldSpec.name] = fieldSpec.kind
        if fieldSpec.kind == legacy_oracle.KIND_TIMESTAMP:
            kinds[fieldSpec.name + legacy_oracle.TIMESTAMP_FORMAT_SUFFIX] = FORMAT_CODE_KIND
    return kinds


def OracleColumnValue(value: object, kind: str) -> object:
    """Convert one SQL value into the form ``legacy_oracle.ParseRecord`` gives that field.

    DATE becomes ISO text ("" for NULL), a CHAR(1) format code is one character (" " when
    blank), text is right-trimmed, and SMALLINT, NUMERIC and TIMESTAMP values stay int, Decimal
    and datetime or None.
    """
    if kind == legacy_oracle.KIND_DATE:
        return "" if value is None else legacy_oracle.DateText(value)
    if kind == FORMAT_CODE_KIND:
        return (value or legacy_oracle.BLANK_FORMAT_CODE)[:1]
    if isinstance(value, str):
        return value.rstrip(" ")
    return value


def ReadOracleRows(connection: Connection, tableName: str) -> list[dict[str, object]]:
    """Read one table's compared columns and convert every value into oracle form."""
    spec = legacy_oracle.TABLE_SPECS[tableName]
    kinds = FieldColumnKinds(legacy_oracle.TABLE_LAYOUT_FIELDS[tableName])
    return [
        {
            columnName: OracleColumnValue(value, kinds[columnName])
            for columnName, value in row.items()
        }
        for row in ReadTableRows(connection, spec)
    ]


def ReadIndexedState(connection: Connection, tableName: str) -> dict:
    """Read one keyed dataset as OracleState holds it: by its key value, or a tuple of key values.

    Category-balance and disclosure-group keys are (text, text, int category) tuples.
    """
    keyColumns = legacy_oracle.TABLE_SPECS[tableName].keyColumns
    return legacy_oracle.IndexRows(ReadOracleRows(connection, tableName), keyColumns)


def ReadStateTransactions(connection: Connection) -> dict[str, str]:
    """Read TRANSACT as OracleState holds it: the 16-character TRAN-ID to its 350-character record.

    Each row is re-encoded with the CVTRA05Y offsets, timestamps through EncodeTimestampText with
    their stored format codes.
    """
    records = [
        legacy_oracle.FormatRecord(
            row, legacy_oracle.TRANSACTION_FIELDS, legacy_oracle.TRANSACTION_LRECL
        )
        for row in ReadOracleRows(connection, legacy_oracle.TRANSACTIONS_TABLE)
    ]
    return {record[legacy_oracle.TRAN_ID_SLICE]: record for record in records}


def ReadTableAsOracleState(connection: Connection) -> legacy_oracle.OracleState:
    """Build the oracle's dataset state from the database, so the oracle can run from it.

    For example, the POSTTRAN partial state left by an abend is read here before the oracle
    predicts a --force rerun (AAP 0.6.8, 0.6.12). Users come without a password, because
    ``TABLE_SPECS`` leaves the hash out.

    Args:
        connection: An open connection to the database to read.

    Returns:
        An OracleState with the keys its docstring defines.

    Raises:
        ValueError: Two rows share a key, or a transaction value cannot be encoded.
    """
    tables = {
        attribute: ReadIndexedState(connection, tableName)
        for attribute, tableName, _image, _layout in legacy_oracle.STATE_TABLE_SOURCES
    }
    return legacy_oracle.OracleState(
        **tables,
        transactions=ReadStateTransactions(connection),
        users=ReadIndexedState(connection, legacy_oracle.USERS_TABLE),
    )


def NormalizeRow(values: Mapping[str, object], columns: Sequence[str]) -> dict[str, str]:
    """Return the comparison text of the named columns of one row, in column order."""
    return {columnName: NormalizeValue(values[columnName]) for columnName in columns}


def NormalizedTableRows(
    connection: Connection, spec: legacy_oracle.TableSpec
) -> list[dict[str, str]]:
    """Read one table through plain SQL and normalise every compared column."""
    return [NormalizeRow(row, spec.columns) for row in ReadTableRows(connection, spec)]


def IndexByRowKey(
    spec: legacy_oracle.TableSpec, rows: Iterable[dict[str, str]], side: str
) -> tuple[dict[tuple[str, ...], dict[str, str]], list[str]]:
    """Index normalised rows by ``legacy_oracle.RowKey``, reporting each repeated key.

    Returns:
        The rows by key (a repeated key keeps its last row) and one message per repetition.
    """
    indexed: dict[tuple[str, ...], dict[str, str]] = {}
    duplicates = []
    for row in rows:
        key = legacy_oracle.RowKey(row, spec)
        if key in indexed:
            duplicates.append(f"{spec.name} key={key!r}: duplicate key in {side}")
        indexed[key] = row
    return indexed, duplicates


def CompareRowFields(
    label: str, key: tuple[str, ...], expectedRow: Mapping[str, str], actualRow: Mapping[str, str]
) -> list[str]:
    """Return one message per column whose actual text differs from the expected text."""
    differences = []
    for columnName, actualText in actualRow.items():
        expectedText = expectedRow.get(columnName)
        if expectedText != actualText:
            differences.append(
                f"{label} key={key!r} field={columnName}: "
                f"expected {expectedText!r}, actual {actualText!r}"
            )
    return differences


def CompareIndexedRows(
    spec: legacy_oracle.TableSpec,
    expectedRows: Mapping[tuple[str, ...], dict[str, str]],
    actualRows: Mapping[tuple[str, ...], dict[str, str]],
) -> list[str]:
    """Compare expected keys in input order, then report unexpected keys in sorted order."""
    differences = []
    for key, expectedRow in expectedRows.items():
        actualRow = actualRows.get(key)
        if actualRow is None:
            differences.append(f"{spec.name} key={key!r}: expected row is missing")
        else:
            differences.extend(CompareRowFields(spec.name, key, expectedRow, actualRow))
    for key in sorted(set(actualRows).difference(expectedRows)):
        differences.append(f"{spec.name} key={key!r}: unexpected row")
    return differences


def CapDifferences(differences: list[str]) -> list[str]:
    """Keep the first MAX_REPORTED_DIFFERENCES messages and count the rest in a final entry."""
    if len(differences) <= MAX_REPORTED_DIFFERENCES:
        return differences
    hiddenCount = len(differences) - MAX_REPORTED_DIFFERENCES
    return differences[:MAX_REPORTED_DIFFERENCES] + [f"… and {hiddenCount} more"]


def ReadExpectedCsv(csvPath: Path) -> tuple[list[str], list[dict[str, str | None]]]:
    """Read a fixture CSV with ``csv.DictReader``: its header and its rows.

    Raises:
        FileNotFoundError: The CSV does not exist.
    """
    with csvPath.open(encoding=CSV_ENCODING, newline="") as csvFile:
        reader = csv.DictReader(csvFile)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def MalformedCsvRows(csvPath: Path, rows: Sequence[Mapping[object, object]]) -> list[str]:
    """Report every CSV row with fewer or more fields than its header (DictReader's None keys)."""
    messages = []
    for lineNumber, row in enumerate(rows, start=2):
        if None in row or None in row.values():
            messages.append(f"{csvPath} line {lineNumber}: field count differs from the header")
    return messages


def ApplyOverrides(
    spec: legacy_oracle.TableSpec,
    expectedRows: dict[tuple[str, ...], dict[str, str]],
    overrides: Mapping[tuple[object, str], object],
) -> list[str]:
    """Replace expected values: overrides maps (key tuple, column) to the expected value.

    Key parts and values are normalised like table values, so ("00000000004",) and a Decimal
    are accepted. An override naming no expected row or no compared column is reported.
    """
    messages = []
    for (keyValues, columnName), expectedValue in overrides.items():
        keyParts = (keyValues,) if isinstance(keyValues, str) else keyValues
        key = tuple(NormalizeValue(part) for part in keyParts)
        if key not in expectedRows or columnName not in spec.columns:
            messages.append(
                f"{spec.name} key={key!r} field={columnName}: override matches no expected value"
            )
            continue
        expectedRows[key] = {**expectedRows[key], columnName: NormalizeValue(expectedValue)}
    return messages


def CompareTableWithCsv(
    connection: Connection,
    tableName: str,
    csvPath: Path,
    overrides: Mapping[tuple[object, str], object] | None = None,
) -> list[str]:
    """Compare a table with its expected CSV, field for field by key (AAP 0.9.3 table rows).

    Both sides are normalised text: Decimal exact at 2 places, text right-trimmed, dates as ISO
    strings, timestamps to the microsecond beside their format codes. This reconciles the
    IDCAMS REPRO loads (jcl/ACCTFILE.jcl ... jcl/DUSRSECJ.jcl) and the account and category
    balances POSTTRAN (cbl/CBTRN02C.cbl) and INTCALC (cbl/CBACT04C.cbl) rewrite.

    Args:
        connection: An open connection to the database the batch run used.
        tableName: A qualified ("accounts.accounts") or bare ("accounts") table name.
        csvPath: The expected CSV, whose header must equal the table's ``TABLE_SPECS`` columns.
        overrides: (key tuple, column) to the expected value, replacing the CSV value.

    Returns:
        Difference messages naming the table, key and field; [] when the table matches.

    Raises:
        KeyError: The table name is unknown.
    """
    spec = legacy_oracle.ResolveTableSpec(tableName)
    try:
        header, csvRows = ReadExpectedCsv(csvPath)
    except FileNotFoundError:
        return [f"missing file {csvPath}"]
    if header != list(spec.columns):
        return [f"{spec.name} {csvPath}: header {header!r}, expected {list(spec.columns)!r}"]
    differences = MalformedCsvRows(csvPath, csvRows)
    expectedList = [NormalizeRow(row, spec.columns) for row in csvRows]
    expectedRows, duplicates = IndexByRowKey(spec, expectedList, str(csvPath))
    differences.extend(duplicates + ApplyOverrides(spec, expectedRows, overrides or {}))
    actualRows, actualDuplicates = IndexByRowKey(
        spec, NormalizedTableRows(connection, spec), "the table"
    )
    differences.extend(actualDuplicates + CompareIndexedRows(spec, expectedRows, actualRows))
    return CapDifferences(differences)


def ParseExpectedRecords(
    spec: legacy_oracle.TableSpec, records: Iterable[str], layoutName: str
) -> tuple[list[dict[str, str]], list[str]]:
    """Decode records with the oracle layout and normalise the table's compared columns.

    record_seq is the record's position in the list, from 1. A record that does not decode is
    reported, never raised.
    """
    fields, _lrecl = legacy_oracle.LAYOUTS[layoutName]
    rows = []
    messages = []
    for recordNumber, record in enumerate(records, start=1):
        try:
            values = legacy_oracle.ParseRecord(record, fields)
        except ValueError as error:
            messages.append(
                f"{spec.name} record {recordNumber}: not a {layoutName} record: {error}"
            )
            continue
        values.setdefault(legacy_oracle.RECORD_SEQ_COLUMN, recordNumber)
        rows.append(NormalizeRow(values, spec.columns))
    return rows, messages


def CompareTableWithRecords(
    connection: Connection, tableName: str, records: Sequence[str], layoutName: str
) -> list[str]:
    """Compare a table with fixed-width records, the count first, then every field by key.

    The records are decoded at the oracle's copybook offsets, and timestamps are compared to the
    microsecond together with their format codes (AAP 0.9.3). It reconciles, for example, the
    TRANSACT table COMBTRAN STEP10 reloads (jcl/COMBTRAN.jcl) with combtran_combined.dat under
    layout "TRANSACTION" (CVTRA05Y).

    Args:
        connection: An open connection to the database the batch run used.
        tableName: A qualified or bare table name.
        records: The expected record texts, for example from ``ReadFixedWidthRecords``.
        layoutName: A ``legacy_oracle.LAYOUTS`` key that yields every compared column.

    Returns:
        Difference messages; [] when the table holds exactly these records.

    Raises:
        KeyError: The table or layout name is unknown.
    """
    spec = legacy_oracle.ResolveTableSpec(tableName)
    fields, _lrecl = legacy_oracle.LAYOUTS[layoutName]
    missingColumns = sorted(set(spec.columns).difference(FieldColumnKinds(fields)))
    if missingColumns:
        return [f"{spec.name}: layout {layoutName} has no field for {missingColumns!r}"]
    expectedList, differences = ParseExpectedRecords(spec, records, layoutName)
    actualList = NormalizedTableRows(connection, spec)
    if len(records) != len(actualList):
        differences.append(
            f"{spec.name}: expected {len(records)} records, actual {len(actualList)}"
        )
    expectedRows, duplicates = IndexByRowKey(spec, expectedList, "the records")
    actualRows, actualDuplicates = IndexByRowKey(spec, actualList, "the table")
    differences.extend(duplicates + actualDuplicates)
    differences.extend(CompareIndexedRows(spec, expectedRows, actualRows))
    return CapDifferences(differences)


def ExpectedSortKey(keyText: str) -> str:
    """Return the ebcdic_sort_key value of a key: each character as the code point of its byte.

    The byte is the character's code page 037 byte, the oracle's VSAM collating key, so a space
    is U+0040, "a" U+0081, "A" U+00C1 and "0" U+00F0 (AAP 0.6.5). Under COLLATE "C" the text
    therefore orders as the EBCDIC key bytes do.

    Raises:
        UnicodeEncodeError: A character has no code page 037 byte.
    """
    return "".join(chr(byteValue) for byteValue in legacy_oracle.EbcdicSortKey(keyText))


def ReadSortKeys(connection: Connection, sortSpec: SortKeySpec) -> tuple[dict[str, str], list[str]]:
    """Read every row's key and stored sort value, both normalised, reporting each repeated key.

    The identifiers come only from sortSpec, for example a ``SORT_KEY_SPECS`` entry, and are
    rendered by Core ``table()`` and ``column()`` constructs, never by string formatting.

    Returns:
        The stored sort value by key text (a repeated key keeps its last row) and one message
        per repetition.
    """
    schemaName, tableName = sortSpec.tableName.split(".", 1)
    statement = select(column(sortSpec.keyColumn), column(sortSpec.sortColumn)).select_from(
        table(tableName, schema=schemaName)
    )
    sortValues: dict[str, str] = {}
    duplicates = []
    for keyValue, sortValue in connection.execute(statement):
        keyText = NormalizeValue(keyValue)
        if keyText in sortValues:
            duplicates.append(f"{sortSpec.tableName} key={keyText!r}: duplicate key in the table")
        sortValues[keyText] = NormalizeValue(sortValue)
    return sortValues, duplicates


def SortValueDifference(sortSpec: SortKeySpec, keyText: str, actualSort: str | None) -> str | None:
    """Describe one expected key the table lacks, or whose stored sort value is not its own."""
    if actualSort is None:
        return f"{sortSpec.tableName} key={keyText!r}: expected row is missing"
    expectedSort = ExpectedSortKey(keyText)
    if actualSort == expectedSort:
        return None
    return (
        f"{sortSpec.tableName} key={keyText!r} field={sortSpec.sortColumn}: "
        f"expected {expectedSort!r}, actual {actualSort!r}"
    )


def CompareSortKeys(
    connection: Connection, sortSpec: SortKeySpec, expectedKeys: Iterable[str | None]
) -> list[str]:
    """Compare a generated EBCDIC sort column with the code page 037 keys of the expected keys.

    ``TABLE_SPECS`` leaves the generated columns out of the field comparison, so they are
    checked here: the table holds exactly the expected keys, and each row's stored sort value
    equals ``ExpectedSortKey`` of its key. Keys and stored values are normalised like every
    table value, so text is right-trimmed (AAP 0.9.3).

    Args:
        connection: An open connection to the database the batch run used.
        sortSpec: The generated column, one of ``SORT_KEY_SPECS``.
        expectedKeys: The key values the table must hold, for example the user_id column of
            loader_rows/users.csv or the TRAN-IDs of combtran_combined.dat.

    Returns:
        Difference messages naming the table, key and sort column: each missing key and wrong
        sort value in expected order, then each unexpected key; [] when the column matches.

    Raises:
        UnicodeEncodeError: An expected key has a character with no code page 037 byte.
    """
    actualSorts, differences = ReadSortKeys(connection, sortSpec)
    expectedTexts = [NormalizeValue(keyValue) for keyValue in expectedKeys]
    for keyText in expectedTexts:
        difference = SortValueDifference(sortSpec, keyText, actualSorts.get(keyText))
        if difference is not None:
            differences.append(difference)
    for keyText in sorted(set(actualSorts).difference(expectedTexts)):
        differences.append(f"{sortSpec.tableName} key={keyText!r}: unexpected row")
    return CapDifferences(differences)


def ReadFileRecords(paths: Iterable[Path | None]) -> tuple[list[list[str]], list[str]]:
    """Read each file's records, reporting every absent file (None: no generation resolved)."""
    contents = []
    missing = []
    for path in paths:
        if path is None:
            missing.append("missing file: no PUBLISHED generation was resolved")
            continue
        try:
            contents.append(ReadFixedWidthRecords(Path(path)))
        except FileNotFoundError:
            missing.append(f"missing file {path}")
    return contents, missing


def FirstFieldDifference(
    location: str, pair: tuple[str, str], fields: Sequence[legacy_oracle.FieldSpec]
) -> str | None:
    """Name the first field, in offset order, whose bytes differ between expected and actual."""
    expected, actual = pair
    for fieldSpec in fields:
        fieldSlice = slice(fieldSpec.offset, fieldSpec.offset + fieldSpec.length)
        if expected[fieldSlice] != actual[fieldSlice]:
            return (
                f"{location} field {fieldSpec.name}: "
                f"expected {expected[fieldSlice]!r}, actual {actual[fieldSlice]!r}"
            )
    return None


def ByteRangeDifference(location: str, pair: tuple[str, str]) -> str:
    """Describe a difference outside every field: the length, or the FILLER byte range.

    The range is inclusive, in 0-based offsets as the copybook comments give them.
    """
    expected, actual = pair
    if len(expected) != len(actual):
        return f"{location} length: expected {len(expected)}, actual {len(actual)}"
    positions = [
        index
        for index, (expectedByte, actualByte) in enumerate(zip(expected, actual, strict=True))
        if expectedByte != actualByte
    ]
    start, end = positions[0], positions[-1]
    expectedBytes, actualBytes = expected[start : end + 1], actual[start : end + 1]
    return f"{location} bytes {start}-{end}: expected {expectedBytes!r}, actual {actualBytes!r}"


def DescribeRecordDifference(
    filePath: Path | None,
    recordNumber: int,
    pair: tuple[str, str],
    fields: Sequence[legacy_oracle.FieldSpec],
) -> str:
    """Describe one unequal record: by field for a record layout, by line for a statement file."""
    expected, actual = pair
    if not fields:
        return f"{filePath} line {recordNumber}: expected {expected!r}, actual {actual!r}"
    location = f"{filePath} record {recordNumber}"
    return FirstFieldDifference(location, pair, fields) or ByteRangeDifference(location, pair)


def CompareFixedWidthFiles(
    actualPath: Path | None, expectedPath: Path, layoutName: str
) -> list[str]:
    """Compare two fixed-width files byte for byte after removing the "\\n" terminators.

    This is the AAP 0.6.11 and 0.9.3 basis for the GDG generations: DALYREJS(+1), F 430 with
    DISP=(NEW,CATLG,DELETE) (jcl/POSTTRAN.jcl, cbl/CBTRN02C.cbl), SYSTRAN(+1) (jcl/INTCALC.jcl),
    TRANSACT.BKUP(+1) (jcl/TRANBKP.jcl) and TRANSACT.COMBINED(+1) (jcl/COMBTRAN.jcl), all
    CVTRA05Y/CVTRA06Y records; and for STATEMNT.PS (80) and STATEMNT.HTML (100), which
    jcl/CREASTMT.JCL STEP040 writes from the TRXFL work file.

    Args:
        actualPath: The file the run wrote; None (no generation resolved) is reported missing.
        expectedPath: The oracle fixture.
        layoutName: A ``legacy_oracle.LAYOUTS`` key. Record layouts such as "TRANSACTION",
            "DALYREJS" and "DAILY" name the first differing field, then a length or FILLER byte
            range; the line-only "STATEMNT.PS" and "STATEMNT.HTML" name the line.

    Returns:
        Difference messages, at most MAX_REPORTED_DIFFERENCES plus a count of the rest; [] when
        the files hold the same records.

    Raises:
        KeyError: The layout name is unknown.
    """
    fields, _lrecl = legacy_oracle.LAYOUTS[layoutName]
    contents, missing = ReadFileRecords((actualPath, expectedPath))
    if missing:
        return missing
    actualRecords, expectedRecords = contents
    differences = []
    if len(actualRecords) != len(expectedRecords):
        differences.append(
            f"{actualPath}: expected {len(expectedRecords)} records, actual {len(actualRecords)}"
        )
    for recordNumber, pair in enumerate(zip(expectedRecords, actualRecords, strict=False), 1):
        if pair[0] != pair[1]:
            differences.append(DescribeRecordDifference(actualPath, recordNumber, pair, fields))
    return CapDifferences(differences)
