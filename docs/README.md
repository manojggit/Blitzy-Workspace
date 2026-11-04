# CardDemo: getting started

CardDemo is the AWS mainframe credit-card sample application, re-implemented as a Python 3.13
FastAPI backend, a PostgreSQL 17 database, a Python batch CLI (`carddemo-batch`) and a Next.js 16
frontend styled with Material UI. The legacy COBOL, JCL, BMS and VSAM sources stay at the
repository root as read-only reference inputs.

This guide runs the application locally on a developer's Mac (macOS on Apple silicon or Intel;
iOS cannot host the development servers) or on Linux. PostgreSQL runs in Docker; the backend, the
frontend and the batch CLI run natively on the host. It covers
installation, configuration, the database, every service, the batch chain, every test suite, the
sample users and troubleshooting.

## Documentation index

- [data-model.md](data-model.md): COBOL-to-Python data types, rounding and truncation, timestamps,
    the table-by-table schema design, the Alembic revisions and the seed counts.
- [batch.md](batch.md): the 18-step job chain, CLI usage, exit statuses, cycles and reruns, GDG
    archives, the batch window and the reconciliation procedure.
- [api-reference.md](api-reference.md): every `/api/v1` endpoint with request and response
    samples, the error contract, the session cookie and the CSRF `Origin` requirement.
- [architecture.md](architecture.md): layers, module layout, the component diagram and the
    cross-cutting decisions.
- [traceability.md](traceability.md): each legacy artefact, its disposition and its target path.

## Conventions in this guide

- Commands run from the repository root. A block that starts with `cd backend` or `cd frontend`
    runs in that directory and leaves the shell there; run `cd ..` before the next step, or use
    a new shell.
- Every Python command calls `.venv/bin/` explicitly, so no step depends on an activated virtual
    environment.
- Upper-case names with underscores, such as `SECRET_KEY_VALUE`, are placeholders. Each is
    explained after its first use.

## Prerequisites

- **Python 3.13.7**, the version in `.python-version`. Install it with uv, pyenv or the
    python.org installer.
- **Node.js 22.23.1 with npm 11.18.0**, the version in `frontend/.nvmrc`. `engines.node` in
    `frontend/package.json` requires at least `20.9.0`.
- **Docker Engine with the Compose plugin**, such as Docker Desktop on macOS. It runs the
    multi-architecture `postgres:17` image.
- **OpenSSL**, which generates the backend signing key.

To check the installed versions:

```bash
python3.13 --version
node --version
npm --version
docker compose version
```

The backend wheels, including `asyncpg` and `psycopg2-binary`, are published for macOS arm64 and
x86_64 and for Linux, so no compiler is needed.

Without Docker, any PostgreSQL 17 server meets the same contract: it listens on `127.0.0.1:5432`,
has the `carddemo` role and the `carddemo` database, and has the test database that
`docker/postgres/init-test-db.sql` creates. To create the test database with `psql` 15 or later,
which the script's `\getenv` command needs:

```bash
psql -h 127.0.0.1 -U carddemo -d carddemo \
    -f docker/postgres/init-test-db.sql
```

The script creates `carddemo_test`, owned by `carddemo`. The connecting role needs the
`CREATEDB` privilege; otherwise, run the script as a superuser role. The database URLs in
`backend/.env.example` name the local example credentials. If your server uses other ones, change
the URLs in `backend/.env` once you have created it
([Configure the environment](#configure-the-environment)).

## Use the host 127.0.0.1

Every local URL uses the host `127.0.0.1`, never `localhost`:

| Component | URL |
|---|---|
| Frontend | `http://127.0.0.1:3000` |
| Backend API | `http://127.0.0.1:8000` |
| Allowed frontend origin, `BACKEND_CORS_ORIGINS` in `backend/.env` | `http://127.0.0.1:3000` |
| Database | `127.0.0.1:5432` |

There are two reasons:

- A browser treats `localhost` and `127.0.0.1` as different sites. The session cookie is
    `SameSite=Lax`, so a cookie set for one host is not sent on API calls from a page served by
    the other, and the CSRF guard accepts only the exact origin in `BACKEND_CORS_ORIGINS`.
- `localhost` may resolve to the IPv6 address `::1` first, while the servers listen on IPv4.

The environment templates already carry these values.

## Install the dependencies

To create the virtual environment and install the pinned backend, development and batch packages:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt \
    -r backend/requirements-dev.txt -r batch/requirements.txt
.venv/bin/pip install -e backend -e batch
```

The editable installs register the `carddemo-backend` package, whose top-level package is `app`,
and the `carddemo-batch` package with its `carddemo-batch` console script.

To check that the installed set is consistent:

```bash
.venv/bin/pip check
```

The output is:

```text
No broken requirements found.
```

To install the frontend packages exactly as `frontend/package-lock.json` pins them:

```bash
cd frontend
npm ci
```

## Configure the environment

The backend, Alembic, the batch CLI and the test suites read `backend/.env`. The frontend build
reads `frontend/.env.local`. Both copies are git-ignored; never commit them.

To create the backend environment file and generate a signing key:

```bash
cp backend/.env.example backend/.env
openssl rand -hex 32
```

Open `backend/.env` and set the `SECRET_KEY` line to the generated key:

```text
SECRET_KEY=SECRET_KEY_VALUE
```

Replace `SECRET_KEY_VALUE` with the 64-character hexadecimal string that `openssl` printed. The
key must be at least 32 characters long: the backend refuses to start while `SECRET_KEY` is blank
or shorter. Batch and Alembic do not need it. Every other value in the template works as it stands
for local development, and `backend/.env.example` documents each variable.

To create the frontend environment file:

```bash
cp frontend/.env.local.example frontend/.env.local
```

`NEXT_PUBLIC_API_URL` in `frontend/.env.local` is the backend origin, `http://127.0.0.1:8000`,
with no `/api/v1` path; the API client appends it. Next.js inlines the value into the browser
bundle when the frontend is built, so a change takes effect only after a rebuild.

Both templates already carry the `127.0.0.1` URLs. The data and output directories in
`backend/.env` are relative paths, which resolve against the repository root from any working
directory:

| Variable | Default | Content |
|---|---|---|
| `CARDDEMO_EBCDIC_DIR` | `data/EBCDIC` | EBCDIC seed images for the seed and the loaders |
| `CARDDEMO_ASCII_DIR` | `data/ASCII` | ASCII seed images for loaders run with `--encoding ascii` |
| `CARDDEMO_OUTPUT_DIR` | `var/batch-output` | Batch GDG archives and statements; git-ignored |

`APP_ENV` is `local` in the template. The test suites set `test`. `production` makes the session
cookie `Secure`, turns the API documentation off unless `ENABLE_API_DOCS` is true, skips the
sample-data seed and refuses `carddemo-batch load init-users`.

## Start the database

To start PostgreSQL 17 from Docker Compose:

```bash
docker compose up -d db
```

The `db` service publishes `127.0.0.1:5432` and keeps its data in the `pgdata` volume. On the
first start with a new volume it runs `docker/postgres/init-test-db.sql`, which creates the
`carddemo_test` database for the test suites. `POSTGRES_USER`, `POSTGRES_PASSWORD` and
`POSTGRES_DB` are read from the shell environment and default to `carddemo`, matching the URLs in
`backend/.env.example`.

To check the database state:

```bash
docker compose ps db
```

Wait until the `STATUS` column shows `(healthy)`; the health check runs `pg_isready` every five
seconds.

## Migrate and seed the database

Migrations run with the backend stopped: before its first start, and after stopping it for any
later upgrade.

To apply every Alembic revision and check the result:

```bash
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/alembic current
```

The last line that `alembic current` prints is the head revision:

```text
0009 (head)
```

Revision `0002` seeds the sample data from the EBCDIC images in `CARDDEMO_EBCDIC_DIR`. It runs
only when `APP_ENV` is `local` or `test`; under `production` it loads nothing. Revision `0008`
records the seed as an open batch cycle, with the loader steps completed. After the upgrade, the
tables hold these rows:

| Table | Rows |
|---|---|
| `security.users` | 10 |
| `accounts.accounts` | 50 |
| `customers.customers` | 50 |
| `cards.cards` | 50 |
| `xref.card_xref` | 50 |
| `reference.transaction_types` | 7 |
| `reference.transaction_categories` | 18 |
| `disclosure.disclosure_groups` | 51 |
| `transactions.transaction_category_balances` | 50 |
| `transactions.daily_transactions` | 300 |
| `transactions.transactions` | 0 |

`transactions.transactions` stays empty until the batch chain posts the daily file. The reasons
for each count are in [Expected rows after migration](data-model.md#expected-rows-after-migration).

To count the rows of one table, here `accounts.accounts`:

```bash
docker compose exec db psql -U carddemo -d carddemo -At \
    -c 'SELECT count(*) FROM accounts.accounts'
```

The output is:

```text
50
```

## Start the backend

To start the FastAPI backend in its own shell:

```bash
cd backend
../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API is served under `http://127.0.0.1:8000/api/v1`, and the health probes at `/health` and
`/health/ready`. While `APP_ENV` is not `production`, the interactive API documentation is at
`http://127.0.0.1:8000/docs` and `http://127.0.0.1:8000/redoc`.

## Start the frontend

`npm run build` reads `NEXT_PUBLIC_API_URL` from `frontend/.env.local`, so create that file first.
The build writes a standalone server to `.next/standalone`, without the static assets and public
files, so copy them into it after every build.

To build the frontend and start it in a second shell:

```bash
cd frontend
npm run build
cp -R .next/static .next/standalone/.next/
cp -R public .next/standalone/
HOSTNAME=127.0.0.1 PORT=3000 node .next/standalone/server.js
```

The two `cp -R` commands work with both the BSD `cp` of macOS and the GNU `cp` of Linux. Each
build regenerates `.next/standalone` without `static` or `public`, so the copies never nest.

For development with hot reload, instead of the build and the standalone server:

```bash
cd frontend
npm run dev -- --hostname 127.0.0.1
```

The `dev` script serves port 3000.

## Sign on

Open `http://127.0.0.1:3000/signon` in a browser; `http://127.0.0.1:3000/` redirects there. Sign on
with one of the [sample users](#sample-users). An admin user opens the admin menu at `/admin`, and
every other user opens the main menu at `/menu`.

## Run the batch jobs

The batch CLI uses the database in `SYNC_DATABASE_URL` of `backend/.env`, from any working
directory.

To list the command groups:

```bash
.venv/bin/carddemo-batch --help
```

The help lists four commands: `run-all`, `seed-all`, `job` and `load`.

To run the chain, or resume the open cycle, in the legacy order:

```bash
.venv/bin/carddemo-batch run-all
```

After a fresh migration, `run-all` resumes the cycle that the seed opened and runs the remaining
steps, POSTTRAN, INTCALC, COMBTRAN and CREASTMT among them. On the sample data it exits with
status 4, because POSTTRAN rejects some daily records.

To reload every loaded table in a new cycle, abandoning the open one:

```bash
.venv/bin/carddemo-batch seed-all
```

`seed-all` replaces the data of the configured database.

To run one processing job, or one loader:

```bash
.venv/bin/carddemo-batch job JOB_NAME
.venv/bin/carddemo-batch load LOADER_NAME
```

Replace the following placeholders:

- `JOB_NAME`: a processing job, such as `post-tran`, `interest-calc` or `statement-gen`.
- `LOADER_NAME`: a loader, such as `accounts`, `tran-types` or `init-users`.

Legacy JCL names, such as `POSTTRAN`, are accepted as aliases. Batch output goes to
`CARDDEMO_OUTPUT_DIR`, by default `var/batch-output`: the GDG archives under `gdg/` and the
statements under `statements/`. Every command holds the batch window, so while one runs, online
transactions that read the files the legacy CLOSEFIL job closed answer with their file-error
messages. For every name, option, exit status and recovery recipe, see
[CLI usage](batch.md#cli-usage) and [Exit statuses](batch.md#exit-statuses).

## Run the full stack in Docker

As an alternative to the native backend and frontend, the Compose `full` profile containerises
both. It reads `backend/.env`, so create it with its `SECRET_KEY` first, and stop the native
servers, because the containers publish the same `127.0.0.1` ports.

To build the images and start the stack:

```bash
docker compose --profile full up -d --build
```

The `migrator` service applies `alembic upgrade head` once the `db` service is healthy. Then the
`backend` service starts on `127.0.0.1:8000`, and the `frontend` service, built with
`NEXT_PUBLIC_API_URL=http://127.0.0.1:8000`, starts on `127.0.0.1:3000`. Inside Compose the
database host is `db`; the profile overrides the database URLs of `backend/.env` accordingly.

To run the batch chain inside the backend image:

```bash
docker compose --profile full run --rm backend carddemo-batch run-all
```

Its output is written to `var/batch-output` on the host.

Migrations run with the backend stopped. To apply a later upgrade:

```bash
docker compose --profile full stop backend
docker compose --profile full up -d --build
```

## Verify the running stack

With the database migrated and the backend and frontend running, each check below has this
expected result:

| Check | Command or action | Expected result |
|---|---|---|
| Liveness | `curl -s http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| Readiness | `curl -s http://127.0.0.1:8000/health/ready` | `{"status":"ready"}` |
| Migrations | `cd backend && ../.venv/bin/alembic current` | `0009 (head)` |
| Seed counts | The `psql` count in [Migrate](#migrate-and-seed-the-database) | The table there |
| Sign-on | The login sample below | Status 200 and an HttpOnly `carddemo_session` cookie |
| Card list | `GET /api/v1/cards` with the cookie | At most 7 rows, card numbers masked, no CVV |
| Authentication | `GET /api/v1/cards` without the cookie | Status 401 |
| CSRF guard | A `PUT` with the cookie and no allowed `Origin` | Status 403 |
| Batch CLI | `.venv/bin/carddemo-batch --help` | Commands `run-all`, `seed-all`, `job`, `load` |
| User interface | Open `http://127.0.0.1:3000/signon` | The sign-on screen |

To sign on as a regular user and store the session cookie in `cookies.txt`:

```bash
curl -s -i -c cookies.txt -X POST \
    http://127.0.0.1:8000/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -H 'Origin: http://127.0.0.1:3000' \
    -d '{"user_id": "USER_ID", "password": "PASSWORD"}'
```

Replace the following placeholders:

- `USER_ID`: the ID of a regular user, whose user type is not `A`.
- `PASSWORD`: that user's password.

The response starts with this status line:

```text
HTTP/1.1 200 OK
```

Its `set-cookie` header carries these cookie attributes, joined by `; `:

```text
carddemo_session=SESSION_TOKEN
HttpOnly
Max-Age=3600
Path=/
SameSite=lax
```

`SESSION_TOKEN` stands for the signed token that the server issues; you never type it. The card
list and every other regular-user endpoint need a regular user; an admin user gets status 403
there.

To list the first page of cards with the session cookie:

```bash
curl -s -b cookies.txt http://127.0.0.1:8000/api/v1/cards
```

To check that a request without the cookie is refused:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
    http://127.0.0.1:8000/api/v1/cards
```

The output is:

```text
401
```

To check that a write without an allowed `Origin` header is refused before it reaches the
endpoint:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -b cookies.txt -X PUT \
    -H 'Content-Type: application/json' -d '{}' \
    http://127.0.0.1:8000/api/v1/accounts/00000000004
```

The output is:

```text
403
```

Every endpoint, with its request and response samples, is in [api-reference.md](api-reference.md).

## Run the tests

The backend and batch suites need only the database: they migrate and use the test database,
`carddemo_test`. They read `TEST_DATABASE_URL` and `TEST_SYNC_DATABASE_URL` when set, and
otherwise derive them from `DATABASE_URL` and `SYNC_DATABASE_URL` with the database name
replaced by `carddemo_test`. Run the backend and batch suites one after the other, never at the
same time, because both use that database. The frontend component and screen suites need no
database.

### Backend

To run the backend unit, integration, API contract and code-standards tests:

```bash
cd backend
../.venv/bin/pytest
```

To run only the API contract tests, which check every operation of the generated OpenAPI
document with schemathesis:

```bash
cd backend
../.venv/bin/pytest tests/contract
```

### Lint and format

To lint the backend and batch code and check its formatting, without changing any file:

```bash
.venv/bin/ruff check backend batch
.venv/bin/ruff format --check backend batch
```

### Batch and reconciliation

To run the batch job tests, reconciliation included:

```bash
.venv/bin/pytest batch/tests
```

The reconciliation compares every batch output with expected fixtures in
`batch/tests/fixtures/expected/`, which an independent oracle derives from the COBOL rules. To
regenerate the fixtures:

```bash
.venv/bin/python -m batch.tests.reconciliation.build_expected
```

Review the fixture diff before you commit it: that review is the gate for any change to the
expected outputs. The procedure and comparison rules are in
[Reconciliation](batch.md#reconciliation).

### Frontend

To run the frontend tests once, without watch mode, then the type check, the lint and the format
check:

```bash
cd frontend
CI=true npm test -- --watchAll=false --ci
npm run type-check
npm run lint
npm run format:check
```

### End to end

The Playwright suite drives the running application through a regular user's day and through
both menus. It reads its settings from `frontend/.env.e2e`, which is git-ignored.

To create the settings file:

```bash
cp frontend/.env.e2e.example frontend/.env.e2e
```

Fill in the four blank sign-on values: `CARDDEMO_E2E_USER_ID` and `CARDDEMO_E2E_PASSWORD` for a
regular user, and `CARDDEMO_E2E_ADMIN_ID` and `CARDDEMO_E2E_ADMIN_PASSWORD` for a user of type `A`.
The other values are prefilled: `CARDDEMO_E2E_BASE_URL` is `http://127.0.0.1:3000`,
`CARDDEMO_E2E_API_URL` is `http://127.0.0.1:8000`, `CARDDEMO_BATCH_BIN` is
`.venv/bin/carddemo-batch` and `CARDDEMO_OUTPUT_DIR` is `var/batch-output`. The two relative paths
resolve against the repository root.

> **Warning:** the suite's global setup runs `carddemo-batch seed-all` against the database in
> `backend/.env`, normally the local `carddemo` database. It reloads every loaded table and
> abandons the open batch cycle, so it destroys any data you entered there.

With the backend and frontend running as described above, to install the Playwright browser and
run the suite:

```bash
cd frontend
npx playwright install chromium
CI=true npm run test:e2e
```

## Sample users

The sample users are non-production seed data. They reach a database only by being read from
`data/EBCDIC/AWS.M2.CARDDEMO.USRSEC.PS`, which the legacy `jcl/DUSRSECJ.jcl` job built: by
Alembic revision `0002`, which runs only when `APP_ENV` is `local` or `test`, or by
`.venv/bin/carddemo-batch load init-users`, which exits with status 12 when `APP_ENV` is
`production`. Each password is bcrypt-hashed as it is read; no plaintext is stored.

| User IDs | User type | Menu after sign-on |
|---|---|---|
| `ADMIN001` to `ADMIN005` | `A` (admin) | The admin menu, `/admin` |
| `USER0001` to `USER0005` | `U` (regular) | The main menu, `/menu` |

Every sample user's password is the legacy sample value held in the password field of the
USRSEC image. Sign-on upper-cases both the user ID and the password before it checks them, as the
legacy sign-on program did.

## Troubleshooting

- **The backend refuses to start with a `SECRET_KEY` error.** `SECRET_KEY` in `backend/.env` is
    blank or shorter than 32 characters. Generate a key with `openssl rand -hex 32` and set it,
    as in [Configure the environment](#configure-the-environment).
- **A container cannot reach the database.** Inside Compose the database host is `db`, not
    `127.0.0.1`. The `full` profile overrides `DATABASE_URL` and `SYNC_DATABASE_URL` for its
    services, so leave the `127.0.0.1` URLs in `backend/.env` for the native processes.
- **The frontend calls the wrong API address.** `NEXT_PUBLIC_API_URL` is inlined when the
    frontend is built. Rebuild after changing it: rerun `npm run build` and the two `cp -R`
    commands, or rebuild the `frontend` image, whose build argument carries the value.
- **Writes are refused with status 403.** Every `POST`, `PUT` and `DELETE` except sign-on needs
    an `Origin` header that exactly equals an entry of `BACKEND_CORS_ORIGINS`. The browser sends
    it; for `curl`, add `-H 'Origin: http://127.0.0.1:3000'`.
- **Requests fail with schema or relation errors.** The database is not at the head revision.
    Stop the backend, run `../.venv/bin/alembic upgrade head` from `backend/`, and check that
    `../.venv/bin/alembic current` prints `0009 (head)`.
- **The session is lost after sign-on.** The browser opened `localhost:3000` instead of
    `127.0.0.1:3000`, so the session cookie is not sent. Open `http://127.0.0.1:3000/signon`.
- **A batch command exits with `BATCH WINDOW NOT AVAILABLE`.** Another batch command holds the
    batch window, or an online request did not finish within 60 seconds. Wait for it to end and
    rerun; see [Batch window](batch.md#batch-window).
- **The test suites cannot connect to `carddemo_test`.** The init script runs only when the
    `pgdata` volume is new. To apply it to the running `db` service:

    ```bash
    docker compose exec -T db psql -U carddemo -d carddemo \
        < docker/postgres/init-test-db.sql
    ```

    Without Docker, apply it with `psql` as in [Prerequisites](#prerequisites).
- **A port is already in use.** The database, the backend and the frontend use the ports 5432,
    8000 and 3000 on `127.0.0.1`. Stop the process that holds the port, or the native servers
    before you start the `full` profile.
