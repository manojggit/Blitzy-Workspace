-- Test database for the backend and batch pytest suites (run by the postgres init hook).
-- Its owner is the POSTGRES_USER role the init hook runs psql as, or carddemo when that variable
-- is unset or blank, as when the file is applied with native psql.
\getenv testDbOwner POSTGRES_USER
\if :{?testDbOwner}
\else
    \set testDbOwner ''
\endif
SELECT format(
    'CREATE DATABASE carddemo_test OWNER %I',
    COALESCE(NULLIF(:'testDbOwner', ''), 'carddemo')
)
\gexec
