"""Integration tests of the connection-level test-database guard of tests/conftest.py.

The reset's DROP and the restore's TRUNCATE each first read current_database() inside their own
transaction and refuse a database other than the validated ``_test`` one; migratedDatabase
first needs asyncpg and psycopg2 to report one database of one running server. The first three
cases run both checks against the configured test database, read-only. The last requests
migratedDatabase alone and proves the driver comparison is part of its setup, so it precedes
the reset. The guard comes from the databaseGuard fixture.
"""

import pytest
from sqlalchemy import Engine

OTHER_DATABASE_SUFFIX = "_other_test"
TEST_DATABASE_SUFFIX = "_test"
REFUSAL_PREFIX = "Refusing to use database"
IDENTITY_FIXTURE_NAME = "verifiedDatabaseIdentity"


def test_connected_database_check_accepts_the_test_database(databaseGuard, syncEngine: Engine):
    """A connection open on the validated test database passes the check."""
    with syncEngine.connect() as connection:
        databaseGuard.requireConnectedTestDatabase(connection, syncEngine.url.database)


def test_connected_database_check_refuses_another_expected_database(
    databaseGuard, syncEngine: Engine
):
    """A connection open on any database but the expected one is refused, naming both."""
    connectedName = syncEngine.url.database
    expectedName = connectedName + OTHER_DATABASE_SUFFIX
    with syncEngine.connect() as connection, pytest.raises(pytest.fail.Exception) as refusal:
        databaseGuard.requireConnectedTestDatabase(connection, expectedName)
    failureText = str(refusal.value)
    assert failureText.startswith(f"{REFUSAL_PREFIX} {connectedName!r}")
    assert repr(expectedName) in failureText


def test_async_and_sync_drivers_reach_one_test_database(
    verifiedDatabaseIdentity, syncEngine: Engine
):
    """The session check found asyncpg and psycopg2 on the configured ``_test`` database."""
    assert verifiedDatabaseIdentity.databaseName == syncEngine.url.database
    assert verifiedDatabaseIdentity.databaseName.endswith(TEST_DATABASE_SUFFIX)
    assert verifiedDatabaseIdentity.startedAt is not None


@pytest.mark.usefixtures("migratedDatabase")
def test_migrated_database_sets_up_the_driver_comparison_first(request: pytest.FixtureRequest):
    """A test needing only migratedDatabase still gets verifiedDatabaseIdentity set up.

    Neither databaseUrls nor syncEngine requests it, so it is in this closure only because
    migratedDatabase does: pytest therefore finishes the comparison before the reset starts.
    """
    assert IDENTITY_FIXTURE_NAME in request.fixturenames
