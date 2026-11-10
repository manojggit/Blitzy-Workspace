# CardDemo Modernization

CardDemo is the AWS mainframe credit-card sample application. This repository re-implements its
base application: the 17 CICS transactions CC00, CM00, CA00, CAVW, CAUP, CCLI, CCDL, CCUP, CT00,
CT01, CT02, CR00, CB00, CU00, CU01, CU02 and CU03, and its 16 batch jobs. The target system is a
Python 3.13 FastAPI backend in `backend/`, a PostgreSQL 17 database built by the Alembic revisions
`0001` to `0009` (head `0009`), the Python batch CLI `carddemo-batch` in `batch/`, and a Next.js 16
frontend in `frontend/` built with Material UI and themed with Material Design 3 tokens.

The legacy sources stay in place, read-only, as the reference for every business rule, field edit
and message literal: `cbl/`, `cpy/`, `bms/`, `cpy-bms/`, `jcl/`, `proc/`, `ctl/`, `asm/`,
`maclib/`, `data/`, `scheduler/`, `catlg/` and `csd/`. The images in `data/EBCDIC/` and
`data/ASCII/` are also the seed input. Business rules are preserved exactly; no business-logic
change is made. The optional extension folders `app-authorization-ims-db2-mq/`,
`app-transaction-type-db2/` and `app-vsam-mq/` are not ported.

## Stack and versions

| Layer | Component | Version |
|---|---|---|
| Runtime | Python | 3.13.7 |
| Runtime | Node.js / npm | 22.23.1 / 11.18.0 |
| Database | PostgreSQL, Docker Compose image `postgres:17` | 17 |
| Backend | FastAPI | 0.136.1 |
| Backend | SQLAlchemy | 2.0.36 |
| Backend | Alembic | 1.14.0 |
| Backend | Pydantic | 2.10.4 |
| Batch | Typer | 0.27.0 |
| Frontend | Next.js | 16.2.11 |
| Frontend | React | 19.2.7 |
| Frontend | `@mui/material` | 9.2.0 |
| Tests | pytest | 9.1.1 |
| Tests | Jest | 30.5.2 |
| Tests | Playwright | 1.64.1 |

Every package pin is in `backend/requirements.txt`, `backend/requirements-dev.txt`,
`batch/requirements.txt` and `frontend/package.json`.

## Local development

The application runs as a local development application on macOS (Apple silicon or Intel) or on
Linux. PostgreSQL runs in Docker; the backend, the frontend and the batch CLI run natively on the
host. Every local URL uses the host `127.0.0.1`, never `localhost`:

| Component | URL |
|---|---|
| Frontend | `http://127.0.0.1:3000` |
| Backend API | `http://127.0.0.1:8000` |

## Getting started

Follow the run instructions in [docs/README.md](docs/README.md). They cover the prerequisites,
installation, the environment files, the database, migration and seeding, the backend and the
frontend, the batch chain and every test suite.

The sample users are non-production seed data, read from the legacy user-security image at load
time. They are described in [Sample users](docs/README.md#sample-users).

## Documentation

- [docs/README.md](docs/README.md): run instructions.
- [docs/data-model.md](docs/data-model.md): the COBOL-to-Python data-type and rounding table and
    the table-by-table schema design.
- [docs/batch.md](docs/batch.md): the batch dependency graph, CLI usage, exit statuses, GDG
    archives and reconciliation.
- [docs/api-reference.md](docs/api-reference.md): every `/api/v1` endpoint, with request and
    response samples.
- [docs/architecture.md](docs/architecture.md): the layers and the cross-cutting decisions.
- [docs/traceability.md](docs/traceability.md): each legacy artefact, its disposition and its
    target path.
