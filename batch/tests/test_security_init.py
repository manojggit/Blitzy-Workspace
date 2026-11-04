"""DUSRSECJ: the user-security load of security.users from the USRSEC image.

Pins jcl/DUSRSECJ.jcl, as ported to batch/loaders/security_init.py (chain step 11, the CLI's
`load init-users`): the AWS.M2.CARDDEMO.USRSEC.PS image, decoded in the CSUSR01Y layout
(SEC-USR-ID X(08), SEC-USR-FNAME X(20), SEC-USR-LNAME X(20), SEC-USR-PWD X(08), SEC-USR-TYPE
X(01)), replaces the table with each SEC-USR-PWD stored only as a bcrypt hash (Technical
Specification, Sections 0.6.8, 0.9.1 and 0.11.2).

The expected values are independent of the production decoder: the fixture users.csv and
legacy_oracle.ReadUsers, which reads the same image with its own copybook offsets. The image
passwords are read at test time and never written here, and wrong candidates come from
secrets.token_urlsafe, so this module holds no credential (MK_Modernization Rule, clause 3).
"""

import secrets
from collections import Counter

import pytest
from app.core.security import VerifyPassword
from app.models import User
from sqlalchemy import Engine, select

from batch import cli
from batch.loaders import security_init
from batch.tests.conftest import (
    APP_ENV_VARIABLE,
    LOADER_ROWS_DIR,
    REPO_ROOT,
    BatchEnvironment,
    CliOutcome,
    CountJobRuns,
    InvokeCli,
    StepRow,
)
from batch.tests.reconciliation import legacy_oracle
from batch.tests.reconciliation.compare import CompareTableWithCsv

USERS = User.__table__
EBCDIC_DIR = REPO_ROOT / "data" / "EBCDIC"
ASCII_DIR = REPO_ROOT / "data" / "ASCII"
USERS_FIXTURE = LOADER_ROWS_DIR / "users.csv"
# The SEC-USR-PWD field name the oracle decodes; it is never kept in a fixture.
IMAGE_SECRET_FIELD = legacy_oracle.CREDENTIAL_COLUMN

LOAD_COMMAND = "load"
SOURCE_DIR_OPTION = "--source-dir"
ENCODING_OPTION = "--encoding"
PRODUCTION_APP_ENV = "production"
# The replace loader's exit-12 text for an image absent from the directory it was pointed at.
IMAGE_NOT_FOUND_TEXT = "INPUT IMAGE NOT FOUND"
# Click's exit status for an option the command does not declare.
CLICK_USAGE_ERROR_EXIT = 2
DUSRSECJ_STEP_NO = 11
COMPLETED_STATE = "COMPLETED"
RERUN_LOAD_COUNT = 2

EXPECTED_USER_COUNT = 10
ADMIN_USER_TYPE = "A"
REGULAR_USER_TYPE = "U"
ADMIN_ID_PREFIX = "ADMIN"
EXPECTED_TYPE_COUNTS = {ADMIN_USER_TYPE: 5, REGULAR_USER_TYPE: 5}
# Every passlib bcrypt hash starts $2a$, $2b$ or $2y$.
BCRYPT_HASH_PREFIX = "$2"
WRONG_CANDIDATE_BYTES = 12

# The two code paths that hash SEC-USR-PWD: the DUSRSECJ loader and Alembic revision 0002.
LOADER_HASH_SOURCE = "init-users-load"
SEED_HASH_SOURCE = "alembic-0002-seed"
HASH_SOURCES = (LOADER_HASH_SOURCE, SEED_HASH_SOURCE)


def ImageUsers() -> list[dict[str, object]]:
    """Return the USRSEC image's users, plaintext included, decoded by the oracle at test time."""
    return legacy_oracle.ReadUsers(EBCDIC_DIR)


def ReadUserRows(engine: Engine) -> dict[str, dict[str, object]]:
    """Return every security.users row with all its columns, keyed by user_id."""
    with engine.connect() as connection:
        result = connection.execute(select(USERS).order_by(USERS.c.user_id))
        return {row["user_id"]: dict(row) for row in result.mappings()}


def StoredHashes(engine: Engine) -> dict[str, str]:
    """Return the stored password_hash of every user, keyed by user_id."""
    return {userId: row["password_hash"] for userId, row in ReadUserRows(engine).items()}


def CompareUsersWithFixture(engine: Engine) -> list[str]:
    """Compare security.users with users.csv field for field; [] when they match."""
    with engine.connect() as connection:
        return CompareTableWithCsv(connection, legacy_oracle.USERS_TABLE, USERS_FIXTURE)


def InvokeInitUsers(*extraArguments: str) -> CliOutcome:
    """Run `carddemo-batch load init-users` in this process, with any extra options."""
    return InvokeCli([LOAD_COMMAND, security_init.JOB_NAME, *extraArguments])


def RunInitUsersLoad() -> None:
    """Run `load init-users` and fail the test unless it exits 0."""
    outcome = InvokeInitUsers()
    assert outcome.exitCode == legacy_oracle.EXIT_OK, outcome.output


def ExpectedUserType(userId: str) -> str:
    """Return the USRSEC type the sample IDs carry: A for ADMIN001-005, U for USER0001-0005."""
    if userId.startswith(ADMIN_ID_PREFIX):
        return ADMIN_USER_TYPE
    return REGULAR_USER_TYPE


@pytest.fixture(name="hashedUsersEngine", params=HASH_SOURCES)
def HashedUsersEngine(request: pytest.FixtureRequest, batchEnvironment: BatchEnvironment) -> Engine:
    """Return the engine once security.users holds the hashes of the parametrized code path.

    init-users-load runs `load init-users` over the restored seed; alembic-0002-seed keeps the
    post-migration rows that revision 0002 hashed.
    """
    if request.param == LOADER_HASH_SOURCE:
        RunInitUsersLoad()
    return batchEnvironment.engine


def test_init_users_loads_ten_users_equal_to_fixture(batchEnvironment: BatchEnvironment) -> None:
    RunInitUsersLoad()
    engine = batchEnvironment.engine
    typesByUser = {userId: row["user_type"] for userId, row in ReadUserRows(engine).items()}
    assert len(typesByUser) == EXPECTED_USER_COUNT
    assert CompareUsersWithFixture(engine) == []
    assert Counter(typesByUser.values()) == EXPECTED_TYPE_COUNTS
    for userId, userType in typesByUser.items():
        assert userType == ExpectedUserType(userId), userId


def test_every_stored_hash_verifies_against_image_password(hashedUsersEngine: Engine) -> None:
    storedHashes = StoredHashes(hashedUsersEngine)
    imageUsers = ImageUsers()
    assert len(imageUsers) == EXPECTED_USER_COUNT
    for record in imageUsers:
        storedHash = storedHashes[record["user_id"]]
        assert VerifyPassword(record[IMAGE_SECRET_FIELD], storedHash), record["user_id"]


def test_no_plaintext_password_is_stored(hashedUsersEngine: Engine) -> None:
    userRows = ReadUserRows(hashedUsersEngine)
    for record in ImageUsers():
        imageSecret = record[IMAGE_SECRET_FIELD]
        storedRow = userRows[record["user_id"]]
        assert imageSecret, record["user_id"]
        assert storedRow["password_hash"] != imageSecret
        assert storedRow["password_hash"].startswith(BCRYPT_HASH_PREFIX)
        for columnName, value in storedRow.items():
            if isinstance(value, str):
                assert imageSecret not in value, (record["user_id"], columnName)


def test_wrong_password_does_not_verify(hashedUsersEngine: Engine) -> None:
    for userId, storedHash in StoredHashes(hashedUsersEngine).items():
        wrongCandidate = secrets.token_urlsafe(WRONG_CANDIDATE_BYTES)
        assert not VerifyPassword(wrongCandidate, storedHash), userId


def test_init_users_refused_under_production_with_table_unchanged(
    batchEnvironment: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = batchEnvironment.engine
    rowsBefore = ReadUserRows(engine)
    monkeypatch.setenv(APP_ENV_VARIABLE, PRODUCTION_APP_ENV)
    outcome = InvokeInitUsers()
    assert outcome.exitCode == legacy_oracle.EXIT_UTILITY_FAILURE, outcome.output
    assert security_init.PRODUCTION_REFUSED_MESSAGE in outcome.output
    assert ReadUserRows(engine) == rowsBefore


def test_init_users_rejects_encoding_option_as_usage_error(
    batchEnvironment: BatchEnvironment,
) -> None:
    # USRSEC.PS exists only in EBCDIC, so the CLI declares no --encoding for init-users: Click
    # refuses the option with its usage-error status before any batch work starts.
    engine = batchEnvironment.engine
    rowsBefore = ReadUserRows(engine)
    runCountBefore = CountJobRuns(engine)
    outcome = InvokeInitUsers(ENCODING_OPTION, cli.EncodingChoice.ASCII.value)
    assert outcome.exitCode == CLICK_USAGE_ERROR_EXIT, outcome.output
    assert ENCODING_OPTION in outcome.output
    assert CountJobRuns(engine) == runCountBefore
    assert ReadUserRows(engine) == rowsBefore


def test_init_users_has_no_ascii_image_and_exits_12(batchEnvironment: BatchEnvironment) -> None:
    engine = batchEnvironment.engine
    rowsBefore = ReadUserRows(engine)
    assert not legacy_oracle.ImagePath(ASCII_DIR, legacy_oracle.USRSEC_IMAGE).exists()
    outcome = InvokeInitUsers(SOURCE_DIR_OPTION, str(ASCII_DIR))
    assert outcome.exitCode == legacy_oracle.EXIT_UTILITY_FAILURE, outcome.output
    assert IMAGE_NOT_FOUND_TEXT in outcome.output
    assert ReadUserRows(engine) == rowsBefore


def test_init_users_rerun_adds_history_row_for_step_11(batchEnvironment: BatchEnvironment) -> None:
    engine = batchEnvironment.engine
    seenRunIds = {StepRow(engine, DUSRSECJ_STEP_NO)["last_run_id"]}
    for _ in range(RERUN_LOAD_COUNT):
        runCountBefore = CountJobRuns(engine, stepNo=DUSRSECJ_STEP_NO)
        completedBefore = CountJobRuns(engine, DUSRSECJ_STEP_NO, COMPLETED_STATE)
        RunInitUsersLoad()
        stepRow = StepRow(engine, DUSRSECJ_STEP_NO)
        assert CountJobRuns(engine, stepNo=DUSRSECJ_STEP_NO) == runCountBefore + 1
        assert CountJobRuns(engine, DUSRSECJ_STEP_NO, COMPLETED_STATE) == completedBefore + 1
        assert stepRow["state"] == COMPLETED_STATE
        assert stepRow["last_run_id"] not in seenRunIds
        seenRunIds.add(stepRow["last_run_id"])
    assert CompareUsersWithFixture(engine) == []
