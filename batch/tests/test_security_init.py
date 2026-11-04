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

The restored seed already holds the image's users with valid hashes, so every positive load
first disturbs security.users: one user's last name changes, another's hash becomes that of a
random secret, and a generated user the image lacks is added. A pass therefore proves the load
replaced every row: exactly the image's users remain, the extra user is gone, and each stored
hash is new and verifies its image password.
"""

import secrets
from collections import Counter

import pytest
from app.core.security import HashPassword, VerifyPassword
from app.models import User
from sqlalchemy import Engine, insert, select, update

from batch import cli
from batch.loaders import security_init
from batch.tests.conftest import (
    APP_ENV_VARIABLE,
    LOADER_ROWS_DIR,
    REPO_ROOT,
    TEST_BCRYPT_ROUNDS,
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

# The restored seed already holds the image's users, so a positive load first makes the table
# stale: a changed last name, a hash of a random secret and a generated user the image lacks,
# whose "ZZ" ID prefix no USRSEC ID (ADMIN..., USER...) shares.
DISTURBED_LAST_NAME = "DISTURBED"
EXTRA_USER_ID_PREFIX = "ZZ"
EXTRA_USER_ID_HEX_BYTES = 3
EXTRA_USER_FIRST_NAME = "EXTRA"
EXTRA_USER_LAST_NAME = "GENERATED"
STALE_SECRET_BYTES = 12
TEST_HASH_ROUNDS = int(TEST_BCRYPT_ROUNDS)

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


def RandomSecretHash() -> str:
    """Return the bcrypt hash of a fresh secrets.token_urlsafe value at the test BCRYPT_ROUNDS."""
    return HashPassword(secrets.token_urlsafe(STALE_SECRET_BYTES), TEST_HASH_ROUNDS)


def ExtraUserRow() -> dict[str, object]:
    """Return a type U security.users row whose generated 8-character ID the image lacks."""
    return {
        "user_id": EXTRA_USER_ID_PREFIX + secrets.token_hex(EXTRA_USER_ID_HEX_BYTES).upper(),
        "first_name": EXTRA_USER_FIRST_NAME,
        "last_name": EXTRA_USER_LAST_NAME,
        "password_hash": RandomSecretHash(),
        "user_type": REGULAR_USER_TYPE,
    }


def DisturbUsers(engine: Engine, imageUsers: list[dict[str, object]]) -> str:
    """Make security.users stale against the image in one transaction; return the extra user ID.

    The first image user's last name changes, the second one's hash becomes that of a random
    secret, and a generated user the image lacks is added.
    """
    renamedId, rehashedId = imageUsers[0]["user_id"], imageUsers[1]["user_id"]
    extraRow = ExtraUserRow()
    staleHash = RandomSecretHash()
    with engine.begin() as connection:
        connection.execute(
            update(USERS).where(USERS.c.user_id == renamedId).values(last_name=DISTURBED_LAST_NAME)
        )
        connection.execute(
            update(USERS).where(USERS.c.user_id == rehashedId).values(password_hash=staleHash)
        )
        connection.execute(insert(USERS).values(extraRow))
    return extraRow["user_id"]


def AssertUsersStale(
    engine: Engine, imageUsers: list[dict[str, object]], extraUserId: str
) -> dict[str, str]:
    """Assert DisturbUsers took effect and return every stored hash, keyed by user_id.

    Results are asserted as booleans, so a failure names user IDs and never a password or hash.
    """
    storedHashes = StoredHashes(engine)
    storedIds = set(storedHashes)
    rehashedId = imageUsers[1]["user_id"]
    staleHashVerifies = VerifyPassword(imageUsers[1][IMAGE_SECRET_FIELD], storedHashes[rehashedId])
    assert CompareUsersWithFixture(engine) != []
    assert extraUserId in storedIds
    assert not staleHashVerifies, rehashedId
    return storedHashes


def AssertUsersReplacedFromImage(
    engine: Engine, imageUsers: list[dict[str, object]], hashesBefore: dict[str, str]
) -> None:
    """Assert only the image's users remain, equal to users.csv, each with a fresh valid hash.

    Results are asserted as booleans, so a failure names user IDs and never a password or hash.
    """
    storedHashes = StoredHashes(engine)
    storedIds = sorted(storedHashes)
    imageIds = sorted(record["user_id"] for record in imageUsers)
    assert storedIds == imageIds
    assert CompareUsersWithFixture(engine) == []
    for record in imageUsers:
        userId = record["user_id"]
        hashRewritten = storedHashes[userId] != hashesBefore[userId]
        hashVerifies = VerifyPassword(record[IMAGE_SECRET_FIELD], storedHashes[userId])
        assert hashRewritten, userId
        assert hashVerifies, userId


def RunDisturbedInitUsersLoad(engine: Engine) -> None:
    """Disturb security.users, run `load init-users`, and assert the image replaced every row.

    Bcrypt salts are random, so a hash the load rewrote always differs from the one before it.
    """
    imageUsers = ImageUsers()
    assert len(imageUsers) == EXPECTED_USER_COUNT
    extraUserId = DisturbUsers(engine, imageUsers)
    hashesBefore = AssertUsersStale(engine, imageUsers, extraUserId)
    RunInitUsersLoad()
    remainingIds = set(StoredHashes(engine))
    assert extraUserId not in remainingIds
    AssertUsersReplacedFromImage(engine, imageUsers, hashesBefore)


@pytest.fixture(name="hashedUsersEngine", params=HASH_SOURCES)
def HashedUsersEngine(request: pytest.FixtureRequest, batchEnvironment: BatchEnvironment) -> Engine:
    """Return the engine once security.users holds the hashes of the parametrized code path.

    init-users-load disturbs the restored seed and runs `load init-users` over it;
    alembic-0002-seed keeps the post-migration rows that revision 0002 hashed.
    """
    if request.param == LOADER_HASH_SOURCE:
        RunDisturbedInitUsersLoad(batchEnvironment.engine)
    return batchEnvironment.engine


def test_init_users_loads_ten_users_equal_to_fixture(batchEnvironment: BatchEnvironment) -> None:
    engine = batchEnvironment.engine
    RunDisturbedInitUsersLoad(engine)
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
        RunDisturbedInitUsersLoad(engine)
        stepRow = StepRow(engine, DUSRSECJ_STEP_NO)
        assert CountJobRuns(engine, stepNo=DUSRSECJ_STEP_NO) == runCountBefore + 1
        assert CountJobRuns(engine, DUSRSECJ_STEP_NO, COMPLETED_STATE) == completedBefore + 1
        assert stepRow["state"] == COMPLETED_STATE
        assert stepRow["last_run_id"] not in seenRunIds
        seenRunIds.add(stepRow["last_run_id"])
    assert CompareUsersWithFixture(engine) == []
