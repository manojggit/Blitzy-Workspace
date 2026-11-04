# CardDemo traceability: legacy artefacts, dispositions and target paths

This document traces every artefact of the CardDemo base application to its disposition and, where
it is ported, to the file that replaces it. It merges three parts of the Technical Specification:
the disposition of every base artefact (Section 0.2.3.4), the online transaction traceability
(Section 0.6.9) and the file-by-file plan (Section 0.8.1). Every path is repository-relative and
spelt exactly as the plan defines it.

`backend/tests/test_code_standards.py` reads the [Program-to-module map](#program-to-module-map)
to check that every ported COBOL program has exactly one Python module, and that the module's
header names the program. Keep the three map tables in their two-column form: a bare upper-case
name in the first column, one backticked path in the second, and no blank line inside a table.

Related documents:

- [README.md](README.md): install, configuration and run instructions.
- [architecture.md](architecture.md): layers and cross-cutting decisions.
- [data-model.md](data-model.md): COBOL-to-Python types, rounding, the schema and the seed counts.
- [batch.md](batch.md): the job chain, cycles, GDG archives and reconciliation.
- [api-reference.md](api-reference.md): every `/api/v1` endpoint and the error contract.

The legacy sources sit at the repository root (`cbl/`, `cpy/`, `bms/`, `cpy-bms/`, `jcl/`,
`proc/`, `ctl/`, `asm/`, `maclib/`, `data/`, `scheduler/`, `catlg/`, `csd/`), not under `app/`.
No legacy file is updated or deleted.

## Disposition rule

Every base-application artefact gets exactly one disposition, decided from the repository alone:

- **PORTED**: the request names it, a ported artefact `CALL`s or `COPY`s it, or the ported flow
    reads or writes its dataset. Its target path is listed in this document.
- **SUPERSEDED**: it is purely mainframe infrastructure (file open and close, waits, GDG
    definitions, print and sort procedures, scheduling), and a named target mechanism provides
    its effect.
- **OUT OF SCOPE**: everything else. The entry cites the request clause that excludes it, or the
    absence of any caller.

For an online program, map or menu the rule applies per behaviour. Every paragraph path of a
ported program, and every option a ported menu shows, is carried over. An option that reaches an
out-of-scope program answers with that program's legacy "not installed" message
([Menu options](#menu-options)).

Legacy inputs that the new code reads in place without porting them, the data images and the
catalog, are marked **REFERENCE**.

## Program-to-module map

| Program | Module |
|---|---|
| COSGN00C | `backend/app/services/auth/cosgn00c.py` |
| COMEN01C | `backend/app/services/menu/comen01c.py` |
| COADM01C | `backend/app/services/menu/coadm01c.py` |
| COACTVWC | `backend/app/services/accounts/coactvwc.py` |
| COACTUPC | `backend/app/services/accounts/coactupc.py` |
| COCRDLIC | `backend/app/services/cards/cocrdlic.py` |
| COCRDSLC | `backend/app/services/cards/cocrdslc.py` |
| COCRDUPC | `backend/app/services/cards/cocrdupc.py` |
| COTRN00C | `backend/app/services/transactions/cotrn00c.py` |
| COTRN01C | `backend/app/services/transactions/cotrn01c.py` |
| COTRN02C | `backend/app/services/transactions/cotrn02c.py` |
| CORPT00C | `backend/app/services/reports/corpt00c.py` |
| COBIL00C | `backend/app/services/billpay/cobil00c.py` |
| COUSR00C | `backend/app/services/users/cousr00c.py` |
| COUSR01C | `backend/app/services/users/cousr01c.py` |
| COUSR02C | `backend/app/services/users/cousr02c.py` |
| COUSR03C | `backend/app/services/users/cousr03c.py` |
| CBTRN02C | `backend/app/services/transactions/cbtrn02c.py` |
| CBTRN03C | `backend/app/services/reports/cbtrn03c.py` |
| CBACT04C | `batch/jobs/interest_calc.py` |
| CBSTM03A | `batch/jobs/statement_gen/cbstm03a.py` |
| CBSTM03B | `batch/jobs/statement_gen/cbstm03b.py` |
| CSUTLDTC | `backend/app/utils/date_validation.py` |

Each module opens with a `Replaces:` header naming its program, copybooks and BMS map. Module
file names are the lower-cased program names, or the job names the request fixes.

- CBTRN02C's posting rules (`ValidateDailyTransaction`, `ApplyPosting`) live in the backend
    package so that the POSTTRAN job module, `batch/jobs/posttran.py`, imports them.
- CBTRN03C has no batch job. The TRANREPT job is superseded by the online CR00 report, which
    calls `BuildReport`.
- CBACT04C's module is also the INTCALC job module.
- CBSTM03A and CBSTM03B run inside the CREASTMT job module,
    `batch/jobs/statement_gen/creastmt.py`.
- `backend/app/utils/date_validation.py` also carries the copybooks CSUTLDPY and CSUTLDWY.

## Map-to-page map

| BMS map | Page |
|---|---|
| COSGN00 | `frontend/src/app/signon/page.tsx` |
| COMEN01 | `frontend/src/app/menu/page.tsx` |
| COADM01 | `frontend/src/app/admin/page.tsx` |
| COACTVW | `frontend/src/app/accounts/view/page.tsx` |
| COACTUP | `frontend/src/app/accounts/update/page.tsx` |
| COCRDLI | `frontend/src/app/cards/page.tsx` |
| COCRDSL | `frontend/src/app/cards/view/page.tsx` |
| COCRDUP | `frontend/src/app/cards/update/page.tsx` |
| COTRN00 | `frontend/src/app/transactions/page.tsx` |
| COTRN01 | `frontend/src/app/transactions/view/page.tsx` |
| COTRN02 | `frontend/src/app/transactions/add/page.tsx` |
| CORPT00 | `frontend/src/app/reports/transactions/page.tsx` |
| COBIL00 | `frontend/src/app/billpay/page.tsx` |
| COUSR00 | `frontend/src/app/admin/users/page.tsx` |
| COUSR01 | `frontend/src/app/admin/users/add/page.tsx` |
| COUSR02 | `frontend/src/app/admin/users/update/page.tsx` |
| COUSR03 | `frontend/src/app/admin/users/delete/page.tsx` |

Each map's source is `bms/<MAP>.bms`, with its symbolic map in `cpy-bms/<MAP>.CPY`. Every page
opens with a header comment naming its BMS map and program, which
`frontend/src/__tests__/sourceReferences.test.ts` checks. The parts common to every map (the
header, the message line, the PF-key legend, selection lists and the Y/N confirm field) are the
shared components in `frontend/src/components/`.

## Job-to-module map

| Job | Module |
|---|---|
| ACCTFILE | `batch/loaders/acctfile.py` |
| CARDFILE | `batch/loaders/cardfile.py` |
| CUSTFILE | `batch/loaders/custfile.py` |
| XREFFILE | `batch/loaders/xreffile.py` |
| TRANFILE | `batch/loaders/tranfile.py` |
| TRANTYPE | `batch/loaders/trantype.py` |
| TRANCATG | `batch/loaders/trancatg.py` |
| DISCGRP | `batch/loaders/discgrp.py` |
| TCATBALF | `batch/loaders/tcatbalf.py` |
| DUSRSECJ | `batch/loaders/security_init.py` |
| DALYTRAN | `batch/loaders/dalytran.py` |
| TRANBKP | `batch/jobs/tranbkp.py` |
| POSTTRAN | `batch/jobs/posttran.py` |
| INTCALC | `batch/jobs/interest_calc.py` |
| COMBTRAN | `batch/jobs/combtran.py` |
| CREASTMT | `batch/jobs/statement_gen/creastmt.py` |
| TRANIDX | `batch/jobs/tranidx.py` |

- DALYTRAN is the new staging step 12, with no legacy JCL. It loads
    `transactions.daily_transactions` and replaces the precondition that `DALYTRAN.PS` exists
    before `jcl/POSTTRAN.jcl` runs.
- The other 16 rows are the legacy jobs. Their members are `jcl/<JOB>.jcl`, except
    `jcl/CREASTMT.JCL`.
- TRANBKP runs twice in the chain, as steps 6 and 15, from the one module.
- All eleven loaders share the replace-load routine `batch/loaders/replace_loader.py`.
- `batch/orchestration/batch_chain.py` runs the jobs in chain order. The order, step numbers and
    CLI names are in [batch.md](batch.md#chain-order).

## Online transaction traceability

The 17 base transactions are the `DEFINE TRANSACTION` entries of `csd/CARDDEMO.CSD`, lines
306–488. `backend/app/core/dependencies.py` assigns each endpoint its role: **admin** is user type
exactly `A`, and **regular** is every other type, as COSGN00C routes them.

| Transaction | Program | Mapset | Role | CSD line |
|---|---|---|---|---|
| CC00 | COSGN00C | COSGN00 | Public sign-on | 378 |
| CM00 | COMEN01C | COMEN01 | Regular | 399 |
| CA00 | COADM01C | COADM01 | Admin | 327 |
| CAVW | COACTVWC | COACTVW | Regular | 317 |
| CAUP | COACTUPC | COACTUP | Regular | 306 |
| CCLI | COCRDLIC | COCRDLI | Regular | 357 |
| CCDL | COCRDSLC | COCRDSL | Regular | 347 |
| CCUP | COCRDUPC | COCRDUP | Regular | 367 |
| CT00 | COTRN00C | COTRN00 | Regular | 419 |
| CT01 | COTRN01C | COTRN01 | Regular | 429 |
| CT02 | COTRN02C | COTRN02 | Regular | 439 |
| CR00 | CORPT00C | CORPT00 | Regular | 409 |
| CB00 | COBIL00C | COBIL00 | Regular | 337 |
| CU00 | COUSR00C | COUSR00 | Admin | 449 |
| CU01 | COUSR01C | COUSR01 | Admin | 459 |
| CU02 | COUSR02C | COUSR02 | Admin | 469 |
| CU03 | COUSR03C | COUSR03 | Admin | 479 |

The eighteenth entry, `CDV1` (line 388), names program COCRDSEC, which has no source in the
repository. It is OUT OF SCOPE and has no route.

### Endpoints, modules and pages

Paths are relative to `/api/v1`. Modules, routers and pages are listed per transaction rather than
as table columns, so that every line stays within the 100-column limit of the MK_Modernization
Rule.

- **CC00** (COSGN00C)
    - Service: `backend/app/services/auth/cosgn00c.py`, with the lockout in
        `backend/app/services/auth/login_throttle.py`
    - Router: `backend/app/api/v1/auth.py`
    - Endpoints: `POST /auth/login` (public), `POST /auth/logout` (no session needed),
        `GET /auth/me` (any signed-in user)
    - Page: `frontend/src/app/signon/page.tsx`
- **CM00** (COMEN01C)
    - Service: `backend/app/services/menu/comen01c.py`
    - Router: `backend/app/api/v1/menu.py`
    - Endpoints: `GET /menu/main`, `POST /menu/main/selection` (regular)
    - Page: `frontend/src/app/menu/page.tsx`
- **CA00** (COADM01C)
    - Service: `backend/app/services/menu/coadm01c.py`
    - Router: `backend/app/api/v1/menu.py`
    - Endpoints: `GET /menu/admin`, `POST /menu/admin/selection` (admin)
    - Page: `frontend/src/app/admin/page.tsx`
- **CAVW** (COACTVWC)
    - Service: `backend/app/services/accounts/coactvwc.py`
    - Router: `backend/app/api/v1/accounts.py`
    - Endpoints: `GET /accounts/{acctId}` (regular)
    - Page: `frontend/src/app/accounts/view/page.tsx`
- **CAUP** (COACTUPC)
    - Service: `backend/app/services/accounts/coactupc.py`
    - Router: `backend/app/api/v1/accounts.py`
    - Endpoints: `GET /accounts/{acctId}/edit`, `PUT /accounts/{acctId}` (regular)
    - Page: `frontend/src/app/accounts/update/page.tsx`
- **CCLI** (COCRDLIC)
    - Service: `backend/app/services/cards/cocrdlic.py`
    - Router: `backend/app/api/v1/cards.py`
    - Endpoints: `GET /cards`, `POST /cards/selection` (regular)
    - Page: `frontend/src/app/cards/page.tsx`
- **CCDL** (COCRDSLC)
    - Service: `backend/app/services/cards/cocrdslc.py`
    - Router: `backend/app/api/v1/cards.py`
    - Endpoints: `GET /cards/{cardNum}` (regular)
    - Page: `frontend/src/app/cards/view/page.tsx`
- **CCUP** (COCRDUPC)
    - Service: `backend/app/services/cards/cocrdupc.py`
    - Router: `backend/app/api/v1/cards.py`
    - Endpoints: `GET /cards/{cardNum}/edit`, `PUT /cards/{cardNum}` (regular)
    - Page: `frontend/src/app/cards/update/page.tsx`
- **CT00** (COTRN00C)
    - Service: `backend/app/services/transactions/cotrn00c.py`
    - Router: `backend/app/api/v1/transactions.py`
    - Endpoints: `GET /transactions`, `POST /transactions/selection` (regular)
    - Page: `frontend/src/app/transactions/page.tsx`
- **CT01** (COTRN01C)
    - Service: `backend/app/services/transactions/cotrn01c.py`
    - Router: `backend/app/api/v1/transactions.py`
    - Endpoints: `GET /transactions/{tranId}` (regular)
    - Page: `frontend/src/app/transactions/view/page.tsx`
- **CT02** (COTRN02C)
    - Service: `backend/app/services/transactions/cotrn02c.py`
    - Router: `backend/app/api/v1/transactions.py`
    - Endpoints: `POST /transactions` (regular)
    - Page: `frontend/src/app/transactions/add/page.tsx`
- **CR00** (CORPT00C)
    - Service: `backend/app/services/reports/corpt00c.py`, with the CBTRN03C line builder
        `backend/app/services/reports/cbtrn03c.py` and the CSV and PDF renderers
        `backend/app/services/reports/report_export.py`
    - Router: `backend/app/api/v1/reports.py`
    - Endpoints: `GET /reports/transactions`, `GET /reports/transactions/export?format=csv|pdf`
        (regular)
    - Page: `frontend/src/app/reports/transactions/page.tsx`
- **CB00** (COBIL00C)
    - Service: `backend/app/services/billpay/cobil00c.py`
    - Router: `backend/app/api/v1/billpay.py`
    - Endpoints: `POST /billpay` (regular)
    - Page: `frontend/src/app/billpay/page.tsx`
- **CU00** (COUSR00C)
    - Service: `backend/app/services/users/cousr00c.py`
    - Router: `backend/app/api/v1/admin_users.py`
    - Endpoints: `GET /admin/users`, `POST /admin/users/selection` (admin)
    - Page: `frontend/src/app/admin/users/page.tsx`
- **CU01** (COUSR01C)
    - Service: `backend/app/services/users/cousr01c.py`
    - Router: `backend/app/api/v1/admin_users.py`
    - Endpoints: `POST /admin/users` (admin)
    - Page: `frontend/src/app/admin/users/add/page.tsx`
- **CU02** (COUSR02C)
    - Service: `backend/app/services/users/cousr02c.py`
    - Router: `backend/app/api/v1/admin_users.py`
    - Endpoints: `GET /admin/users/{userId}?screen=update`, `PUT /admin/users/{userId}` (admin)
    - Page: `frontend/src/app/admin/users/update/page.tsx`
- **CU03** (COUSR03C)
    - Service: `backend/app/services/users/cousr03c.py`
    - Router: `backend/app/api/v1/admin_users.py`
    - Endpoints: `GET /admin/users/{userId}?screen=delete`, `DELETE /admin/users/{userId}`
        (admin)
    - Page: `frontend/src/app/admin/users/delete/page.tsx`

`backend/app/api/v1/router.py` aggregates the eight routers under `/api/v1`. CU02 and CU03 share
the fetch endpoint; its `screen` parameter selects the COUSR02C or the COUSR03C prompt.

## Menu options

The menus list every option of their copybook, with the legacy number and label. Labels are the
`X(35)` values right-trimmed. Each route is the page of the same path under `frontend/src/app/`,
and `backend/app/services/menu/menu_options.py` holds both tables.

### Main menu (CM00, COMEN01C)

Options of `cpy/COMEN02Y.cpy`, lines 25–90 (`CDEMO-MENU-OPT-COUNT` 11):

| Option | Label | Program | Route or outcome |
|---|---|---|---|
| 1 | Account View | COACTVWC | `/accounts/view` |
| 2 | Account Update | COACTUPC | `/accounts/update` |
| 3 | Credit Card List | COCRDLIC | `/cards` |
| 4 | Credit Card View | COCRDSLC | `/cards/view` |
| 5 | Credit Card Update | COCRDUPC | `/cards/update` |
| 6 | Transaction List | COTRN00C | `/transactions` |
| 7 | Transaction View | COTRN01C | `/transactions/view` |
| 8 | Transaction Add | COTRN02C | `/transactions/add` |
| 9 | Transaction Reports | CORPT00C | `/reports/transactions` |
| 10 | Bill Payment | COBIL00C | `/billpay` |
| 11 | Pending Authorization View | COPAUS0C | Not installed |

- Option 8's active label is `Transaction Add`. The `Transaction Add (Admin Only)` text on line 69
    is a comment.
- Every option is flagged user type `U`, so COMEN01C's `No access - Admin Only option... `
    refusal is never set by this table.
- Option 11 targets COPAUS0C in the excluded `app-authorization-ims-db2-mq/` module. Its
    `INQUIRE PROGRAM` fails, and COMEN01C (lines 163–167) answers with this message:

```text
This option Pending Authorization View is not installed...
```

### Admin menu (CA00, COADM01C)

Options of `cpy/COADM02Y.cpy`, lines 26–53 (`CDEMO-ADMIN-OPT-COUNT` 6):

| Option | Label | Program | Route or outcome |
|---|---|---|---|
| 1 | User List (Security) | COUSR00C | `/admin/users` |
| 2 | User Add (Security) | COUSR01C | `/admin/users/add` |
| 3 | User Update (Security) | COUSR02C | `/admin/users/update` |
| 4 | User Delete (Security) | COUSR03C | `/admin/users/delete` |
| 5 | Transaction Type List/Update (Db2) | COTRTLIC | Not installed |
| 6 | Transaction Type Maintenance (Db2) | COTRTUPC | Not installed |

Options 5 and 6 target COTRTLIC and COTRTUPC in the excluded `app-transaction-type-db2/`
module. Their `XCTL` raises `PGMIDERR`, and COADM01C (lines 152–156 and 273–277) answers with
this message:

```text
This option is not installed ...
```

The option-name operand of both `STRING` statements is commented out, so the admin literal names
no option, unlike the main-menu one. `backend/app/services/menu/coadm01c.py` returns the active
literal as written.

Neither active table has a `DUMMY` program entry, so COMEN01C's `is coming soon ...` builder
(lines 169–176) is never reached with the shipped tables.

## Artefact dispositions

Each table covers one legacy folder, and every artefact in it appears exactly once.

### Programs (`cbl/`)

The 23 programs of the [Program-to-module map](#program-to-module-map) are PORTED. The other
eight are:

| Program | Disposition | Target, replacement or excluding clause |
|---|---|---|
| CBACT01C | OUT OF SCOPE | Read/print utility run by READACCT; not in the request's job list |
| CBACT02C | OUT OF SCOPE | Read/print utility run by READCARD; not in the request's job list |
| CBACT03C | OUT OF SCOPE | Read/print utility run by READXREF; not in the request's job list |
| CBCUS01C | OUT OF SCOPE | Read/print utility run by READCUST; not in the request's job list |
| CBEXPORT | OUT OF SCOPE | Branch migration export; not in the request's job list |
| CBIMPORT | OUT OF SCOPE | Branch migration import; not in the request's job list |
| CBTRN01C | OUT OF SCOPE | No JCL runs it; CBTRN02C is the posting program the request names |
| COBSWAIT | SUPERSEDED | WAITSTEP's timed wait; the sequential chain runner needs none |

`cbl/CBSTM03A.CBL` and `cbl/CBSTM03B.CBL` are the two members with an upper-case extension.

### Copybooks (`cpy/`)

Every record copybook is also transcribed field by field in
`backend/app/utils/record_layouts.py`, which the seed reader, the loaders and the GDG writers use.

| Copybook | Disposition | Target, replacement or excluding clause |
|---|---|---|
| CVACT01Y | PORTED | `backend/app/models/account.py` |
| CVACT02Y | PORTED | `backend/app/models/card.py` |
| CVACT03Y | PORTED | `backend/app/models/card_xref.py` |
| CVCUS01Y | PORTED | `backend/app/models/customer.py` |
| CUSTREC | PORTED | `backend/app/models/customer.py`; `batch/jobs/statement_gen/cbstm03a.py` |
| CVTRA01Y | PORTED | `backend/app/models/transaction_category_balance.py` |
| CVTRA02Y | PORTED | `backend/app/models/disclosure_group.py` |
| CVTRA03Y | PORTED | `backend/app/models/transaction_type.py` |
| CVTRA04Y | PORTED | `backend/app/models/transaction_category.py` |
| CVTRA05Y | PORTED | `backend/app/models/transaction.py` |
| CVTRA06Y | PORTED | `backend/app/models/daily_transaction.py` |
| CVTRA07Y | PORTED | Report lines of `backend/app/services/reports/cbtrn03c.py` |
| CSUSR01Y | PORTED | `backend/app/models/user.py` |
| COSTM01 | PORTED | TRXFL record in `backend/app/utils/record_layouts.py` |
| COCOM01Y | PORTED | `backend/app/core/security.py`, `backend/app/core/dependencies.py` |
| COMEN02Y | PORTED | `backend/app/services/menu/menu_options.py` |
| COADM02Y | PORTED | `backend/app/services/menu/menu_options.py` |
| COTTL01Y | PORTED | `backend/app/core/messages.py`, `frontend/src/components/ScreenShell.tsx` |
| CSMSG01Y | PORTED | `backend/app/core/messages.py`, `frontend/src/components/MessageLine.tsx` |
| CSMSG02Y | PORTED | `backend/app/core/messages.py`, `frontend/src/components/MessageLine.tsx` |
| CSDAT01Y | PORTED | Timestamps in `backend/app/utils/record_layouts.py`; `ScreenShell` date |
| CSSTRPFY | PORTED | `frontend/src/lib/useFunctionKeys.ts` |
| CSSETATY | PORTED | `field_errors` in responses and the MUI field error state on the pages |
| CVCRD01Y | PORTED | Request models in `backend/app/schemas/cards.py` |
| CSLKPCDY | PORTED | `backend/app/utils/lookup_codes.py` |
| CSUTLDPY | PORTED | `backend/app/utils/date_validation.py` |
| CSUTLDWY | PORTED | `backend/app/utils/date_validation.py` |
| CODATECN | OUT OF SCOPE | Used only by CBACT01C |
| CVEXPORT | OUT OF SCOPE | Used only by CBEXPORT and CBIMPORT |
| UNUSED1Y | OUT OF SCOPE | No program copies it |

- COCOM01Y's COMMAREA fields become session-token claims (user ID and type) and explicit
    request keys (account, card, transaction and user IDs).
- `cpy/COSTM01.CPY` is the one member with an upper-case extension.
- DFHAID and DFHBMSCA are IBM-supplied, not in `cpy/`, and copied by all 17 online programs.
    They are PORTED: the attention keys become `frontend/src/lib/useFunctionKeys.ts` and
    `frontend/src/components/FunctionKeyBar.tsx`, and the colour and attribute constants become
    the `MessageLine` severities and read-only fields.

### BMS mapsets and symbolic maps (`bms/`, `cpy-bms/`)

The 17 mapsets and their 17 symbolic maps are PORTED. Each map becomes the page of the
[Map-to-page map](#map-to-page-map), and its field lengths become the `max_length` limits of the
request models and the types of the typed endpoint functions:

| BMS map | Request and response models | Typed endpoint functions |
|---|---|---|
| COSGN00 | `backend/app/schemas/auth.py` | `frontend/src/lib/api/auth.ts` |
| COMEN01 | `backend/app/schemas/menu.py` | `frontend/src/lib/api/menu.ts` |
| COADM01 | `backend/app/schemas/menu.py` | `frontend/src/lib/api/menu.ts` |
| COACTVW | `backend/app/schemas/accounts.py` | `frontend/src/lib/api/accounts.ts` |
| COACTUP | `backend/app/schemas/accounts.py` | `frontend/src/lib/api/accounts.ts` |
| COCRDLI | `backend/app/schemas/cards.py` | `frontend/src/lib/api/cards.ts` |
| COCRDSL | `backend/app/schemas/cards.py` | `frontend/src/lib/api/cards.ts` |
| COCRDUP | `backend/app/schemas/cards.py` | `frontend/src/lib/api/cards.ts` |
| COTRN00 | `backend/app/schemas/transactions.py` | `frontend/src/lib/api/transactions.ts` |
| COTRN01 | `backend/app/schemas/transactions.py` | `frontend/src/lib/api/transactions.ts` |
| COTRN02 | `backend/app/schemas/transactions.py` | `frontend/src/lib/api/transactions.ts` |
| CORPT00 | `backend/app/schemas/reports.py` | `frontend/src/lib/api/reports.ts` |
| COBIL00 | `backend/app/schemas/billpay.py` | `frontend/src/lib/api/billpay.ts` |
| COUSR00 | `backend/app/schemas/users.py` | `frontend/src/lib/api/users.ts` |
| COUSR01 | `backend/app/schemas/users.py` | `frontend/src/lib/api/users.ts` |
| COUSR02 | `backend/app/schemas/users.py` | `frontend/src/lib/api/users.ts` |
| COUSR03 | `backend/app/schemas/users.py` | `frontend/src/lib/api/users.ts` |

### JCL, procedures and control cards (`jcl/`, `proc/`, `ctl/`)

The 16 legacy jobs of the [Job-to-module map](#job-to-module-map) are PORTED. The other members
are:

| Member | Disposition | Target, replacement or excluding clause |
|---|---|---|
| `jcl/CLOSEFIL.jcl` | SUPERSEDED | Batch window (`batch/orchestration/batch_window.py`) |
| `jcl/OPENFIL.jcl` | SUPERSEDED | Batch window (`batch/orchestration/batch_window.py`) |
| `jcl/WAITSTEP.jcl` | SUPERSEDED | Sequential chain runner (`batch/orchestration/batch_chain.py`) |
| `jcl/DEFGDGB.jcl` | SUPERSEDED | `batch/orchestration/gdg.py` and `batch.gdg_generations` |
| `jcl/DEFGDGD.jcl` | SUPERSEDED | `batch/orchestration/gdg.py` and `batch.gdg_generations` |
| `jcl/DALYREJS.jcl` | SUPERSEDED | `batch/orchestration/gdg.py` and `batch.gdg_generations` |
| `jcl/REPTFILE.jcl` | SUPERSEDED | `batch/orchestration/gdg.py` and `batch.gdg_generations` |
| `jcl/TRANREPT.jcl` | SUPERSEDED | Online CR00 report (`backend/app/services/reports/`) |
| `proc/TRANREPT.prc` | SUPERSEDED | Online CR00 report (`backend/app/services/reports/`) |
| `proc/REPROC.prc` | SUPERSEDED | Fixed-width writers of TRANBKP and COMBTRAN |
| `ctl/REPROCT.ctl` | SUPERSEDED | Fixed-width writers of TRANBKP and COMBTRAN |
| `jcl/READACCT.jcl` | OUT OF SCOPE | Runs CBACT01C; not in the request's job list |
| `jcl/READCARD.jcl` | OUT OF SCOPE | Runs CBACT02C; not in the request's job list |
| `jcl/READCUST.jcl` | OUT OF SCOPE | Runs CBCUS01C; not in the request's job list |
| `jcl/READXREF.jcl` | OUT OF SCOPE | Runs CBACT03C; not in the request's job list |
| `jcl/CBEXPORT.jcl` | OUT OF SCOPE | Branch migration; not in the request's job list |
| `jcl/CBIMPORT.jcl` | OUT OF SCOPE | Branch migration; not in the request's job list |
| `jcl/PRTCATBL.jcl` | OUT OF SCOPE | Not in the request's job list |
| `jcl/FTPJCL.JCL` | OUT OF SCOPE | Excluded "Additional JCL Utilities" (FTP) |
| `jcl/TXT2PDF1.JCL` | OUT OF SCOPE | Excluded "Additional JCL Utilities" (statement PDF) |
| `jcl/INTRDRJ1.JCL` | OUT OF SCOPE | Excluded "Additional JCL Utilities" (Internal Reader) |
| `jcl/INTRDRJ2.JCL` | OUT OF SCOPE | Excluded "Additional JCL Utilities" (Internal Reader) |
| `jcl/CBADMCDJ.jcl` | OUT OF SCOPE | CICS CSD definition job: mainframe infrastructure |
| `jcl/DEFCUST.jcl` | OUT OF SCOPE | Defines a cluster no program uses |
| `jcl/ESDSRRDS.jcl` | OUT OF SCOPE | Defines clusters no program uses |

The online side of the batch window, which refuses access to the CLOSEFIL files while a batch
command runs, is `backend/app/core/locks.py`.

### Assembler, macros, scheduler, catalog and CICS definitions

| Artefact | Disposition | Target, replacement or excluding clause |
|---|---|---|
| `asm/MVSWAIT.asm` | SUPERSEDED | The sequential chain runner needs no timed wait |
| `maclib/ASMWAIT.mac` | SUPERSEDED | The sequential chain runner needs no timed wait |
| `asm/COBDATFT.asm` | OUT OF SCOPE | Called only by CBACT01C |
| `maclib/COCDATFT.mac` | OUT OF SCOPE | Serves COBDATFT, which only CBACT01C calls |
| `scheduler/CardDemo.ca7` | SUPERSEDED | `batch/orchestration/batch_chain.py` |
| `scheduler/CardDemo.controlm` | SUPERSEDED | `batch/orchestration/batch_chain.py` |
| `catlg/LISTCAT.txt` | REFERENCE | Keys, AIX and GDG limits behind the schema and `gdg.py` |
| `csd/CARDDEMO.CSD` | OUT OF SCOPE | CICS CSD infrastructure; read for the transaction inventory |

### Data images (`data/`)

The images are REFERENCE inputs, read in place and never modified. `batch/loaders/*` read them
through `backend/app/seed/image_reader.py`, as does the Alembic seed revision
`backend/alembic/versions/0002_seed_sample_data.py`. The EBCDIC images are the default input, as
the JCL loads them; the ASCII copies are the `--encoding ascii` alternative. Image names below omit
the `AWS.M2.CARDDEMO.` prefix.

| Image (`data/EBCDIC/`) | ASCII copy (`data/ASCII/`) | Target table | Loader (`batch/loaders/`) |
|---|---|---|---|
| `ACCTDATA.PS` | `acctdata.txt` | `accounts.accounts` | `acctfile.py` |
| `CARDDATA.PS` | `carddata.txt` | `cards.cards` | `cardfile.py` |
| `CUSTDATA.PS` | `custdata.txt` | `customers.customers` | `custfile.py` |
| `CARDXREF.PS` | `cardxref.txt` | `xref.card_xref` | `xreffile.py` |
| `DALYTRAN.PS.INIT` | None | `transactions.transactions` | `tranfile.py` |
| `TRANTYPE.PS` | `trantype.txt` | `reference.transaction_types` | `trantype.py` |
| `TRANCATG.PS` | `trancatg.txt` | `reference.transaction_categories` | `trancatg.py` |
| `DISCGRP.PS` | `discgrp.txt` | `disclosure.disclosure_groups` | `discgrp.py` |
| `TCATBALF.PS` | `tcatbal.txt` | `transactions.transaction_category_balances` | `tcatbalf.py` |
| `USRSEC.PS` | None | `security.users` | `security_init.py` |
| `DALYTRAN.PS` | `dailytran.txt` | `transactions.daily_transactions` | `dalytran.py` |

- `DALYTRAN.PS.INIT` holds only the TRANFILE priming record, which the loader skips, so
    `transactions.transactions` starts empty.
- `USRSEC.PS` has no ASCII copy. Its passwords are bcrypt-hashed as they are read.
- `data/EBCDIC/AWS.M2.CARDDEMO.ACCDATA.PS` is OUT OF SCOPE: no JCL reads it, and it is a
    byte-identical duplicate of `ACCTDATA.PS`.
- `data/EBCDIC/AWS.M2.CARDDEMO.EXPORT.DATA.PS` is OUT OF SCOPE: it belongs to CBEXPORT.

### Optional extension modules

The three extension folders are OUT OF SCOPE under the request's System Boundaries exclusions:

- `app-authorization-ims-db2-mq/`: transactions CPVS, CPVD and CP00; programs COPAUS0C,
    COPAUS1C, COPAUS2C and COPAUA0C; batch job CBPAUP0J. Main-menu option 11 answers "not
    installed".
- `app-transaction-type-db2/`: transactions CTTU and CTLI; programs COTRTUPC, COTRTLIC and
    COBTUPDT; batch jobs CREADB21, TRANEXTR and MNTTRDB2. Admin options 5 and 6 answer "not
    installed".
- `app-vsam-mq/`: transactions CDRD and CDRA; programs CODATE01 and COACCT01.

## Target path inventory

Every path of the file-by-file plan (Section 0.8.1), with each brace list expanded: 342 paths in
eight groups. Every path is created, except `README.md`, which is updated. No file is deleted.

### Root and local run

- `README.md` (UPDATE)
- `.gitignore`
- `.editorconfig`
- `.python-version`
- `.dockerignore`
- `docker-compose.yml`
- `docker/postgres/init-test-db.sql`

### Backend project, core and API

- `backend/pyproject.toml`
- `backend/requirements.txt`
- `backend/requirements-dev.txt`
- `backend/alembic.ini`
- `backend/.env.example`
- `backend/Dockerfile`
- `backend/app/__init__.py`
- `backend/app/main.py`
- `backend/app/core/__init__.py`
- `backend/app/core/config.py`
- `backend/app/core/database.py`
- `backend/app/core/dependencies.py`
- `backend/app/core/middleware.py`
- `backend/app/core/errors.py`
- `backend/app/core/messages.py`
- `backend/app/core/security.py`
- `backend/app/core/locks.py`
- `backend/app/core/paths.py`
- `backend/app/core/logging.py`
- `backend/app/api/__init__.py`
- `backend/app/api/health.py`
- `backend/app/api/v1/__init__.py`
- `backend/app/api/v1/router.py`
- `backend/app/api/v1/auth.py`
- `backend/app/api/v1/menu.py`
- `backend/app/api/v1/accounts.py`
- `backend/app/api/v1/cards.py`
- `backend/app/api/v1/transactions.py`
- `backend/app/api/v1/reports.py`
- `backend/app/api/v1/billpay.py`
- `backend/app/api/v1/admin_users.py`

### Backend data layer

- `backend/app/models/__init__.py`
- `backend/app/models/base.py`
- `backend/app/models/user.py`
- `backend/app/models/account.py`
- `backend/app/models/customer.py`
- `backend/app/models/card.py`
- `backend/app/models/card_xref.py`
- `backend/app/models/transaction.py`
- `backend/app/models/daily_transaction.py`
- `backend/app/models/transaction_category_balance.py`
- `backend/app/models/disclosure_group.py`
- `backend/app/models/transaction_type.py`
- `backend/app/models/transaction_category.py`
- `backend/app/models/support_tables.py`
- `backend/app/schemas/__init__.py`
- `backend/app/schemas/common.py`
- `backend/app/schemas/auth.py`
- `backend/app/schemas/menu.py`
- `backend/app/schemas/accounts.py`
- `backend/app/schemas/cards.py`
- `backend/app/schemas/transactions.py`
- `backend/app/schemas/reports.py`
- `backend/app/schemas/billpay.py`
- `backend/app/schemas/users.py`
- `backend/app/repositories/__init__.py`
- `backend/app/repositories/user_repository.py`
- `backend/app/repositories/account_repository.py`
- `backend/app/repositories/customer_repository.py`
- `backend/app/repositories/card_repository.py`
- `backend/app/repositories/card_xref_repository.py`
- `backend/app/repositories/transaction_repository.py`
- `backend/app/repositories/reference_repository.py`
- `backend/app/utils/__init__.py`
- `backend/app/utils/decimal_utils.py`
- `backend/app/utils/cobol_intrinsics.py`
- `backend/app/utils/cobol_format.py`
- `backend/app/utils/date_validation.py`
- `backend/app/utils/lookup_codes.py`
- `backend/app/utils/record_layouts.py`
- `backend/app/seed/__init__.py`
- `backend/app/seed/image_reader.py`
- `backend/app/seed/sample_seed.py`
- `backend/alembic/env.py`
- `backend/alembic/script.py.mako`
- `backend/alembic/versions/0001_create_dataset_schemas_and_tables.py`
- `backend/alembic/versions/0002_seed_sample_data.py`
- `backend/alembic/versions/0003_alternate_index_replacements.py`
- `backend/alembic/versions/0004_relationship_foreign_keys.py`
- `backend/alembic/versions/0005_pic9_digit_checks.py`
- `backend/alembic/versions/0006_users_audit_timestamps.py`
- `backend/alembic/versions/0007_security_login_attempts.py`
- `backend/alembic/versions/0008_batch_job_run_ledger.py`
- `backend/alembic/versions/0009_batch_gdg_catalog.py`

### Backend services

- `backend/app/services/__init__.py`
- `backend/app/services/auth/__init__.py`
- `backend/app/services/menu/__init__.py`
- `backend/app/services/accounts/__init__.py`
- `backend/app/services/cards/__init__.py`
- `backend/app/services/transactions/__init__.py`
- `backend/app/services/reports/__init__.py`
- `backend/app/services/billpay/__init__.py`
- `backend/app/services/users/__init__.py`
- `backend/app/services/auth/cosgn00c.py`
- `backend/app/services/auth/login_throttle.py`
- `backend/app/services/menu/comen01c.py`
- `backend/app/services/menu/coadm01c.py`
- `backend/app/services/menu/menu_options.py`
- `backend/app/services/accounts/coactvwc.py`
- `backend/app/services/accounts/coactupc.py`
- `backend/app/services/cards/cocrdlic.py`
- `backend/app/services/cards/cocrdslc.py`
- `backend/app/services/cards/cocrdupc.py`
- `backend/app/services/transactions/cotrn00c.py`
- `backend/app/services/transactions/cotrn01c.py`
- `backend/app/services/transactions/cotrn02c.py`
- `backend/app/services/transactions/cbtrn02c.py`
- `backend/app/services/reports/corpt00c.py`
- `backend/app/services/reports/cbtrn03c.py`
- `backend/app/services/reports/report_export.py`
- `backend/app/services/billpay/cobil00c.py`
- `backend/app/services/users/cousr00c.py`
- `backend/app/services/users/cousr01c.py`
- `backend/app/services/users/cousr02c.py`
- `backend/app/services/users/cousr03c.py`

### Batch

- `batch/pyproject.toml`
- `batch/requirements.txt`
- `batch/__init__.py`
- `batch/cli.py`
- `batch/config.py`
- `batch/db.py`
- `batch/orchestration/__init__.py`
- `batch/orchestration/batch_chain.py`
- `batch/orchestration/run_ledger.py`
- `batch/orchestration/gdg.py`
- `batch/orchestration/job_context.py`
- `batch/orchestration/clock.py`
- `batch/orchestration/batch_window.py`
- `batch/loaders/__init__.py`
- `batch/loaders/replace_loader.py`
- `batch/loaders/acctfile.py`
- `batch/loaders/cardfile.py`
- `batch/loaders/custfile.py`
- `batch/loaders/xreffile.py`
- `batch/loaders/tranfile.py`
- `batch/loaders/trantype.py`
- `batch/loaders/trancatg.py`
- `batch/loaders/discgrp.py`
- `batch/loaders/tcatbalf.py`
- `batch/loaders/security_init.py`
- `batch/loaders/dalytran.py`
- `batch/jobs/__init__.py`
- `batch/jobs/tranbkp.py`
- `batch/jobs/posttran.py`
- `batch/jobs/interest_calc.py`
- `batch/jobs/combtran.py`
- `batch/jobs/tranidx.py`
- `batch/jobs/statement_gen/__init__.py`
- `batch/jobs/statement_gen/creastmt.py`
- `batch/jobs/statement_gen/cbstm03a.py`
- `batch/jobs/statement_gen/cbstm03b.py`
- `batch/jobs/statement_gen/templates/statement.html.j2`

### Tests

- `backend/tests/__init__.py`
- `backend/tests/conftest.py`
- `backend/tests/unit/utils/test_decimal_utils.py`
- `backend/tests/unit/utils/test_cobol_intrinsics.py`
- `backend/tests/unit/utils/test_cobol_format.py`
- `backend/tests/unit/utils/test_date_validation.py`
- `backend/tests/unit/utils/test_lookup_codes.py`
- `backend/tests/unit/utils/test_record_layouts.py`
- `backend/tests/unit/seed/test_image_reader.py`
- `backend/tests/unit/core/test_config.py`
- `backend/tests/unit/core/test_security.py`
- `backend/tests/unit/core/test_middleware.py`
- `backend/tests/unit/core/test_errors.py`
- `backend/tests/unit/core/test_logging.py`
- `backend/tests/unit/core/test_paths.py`
- `backend/tests/unit/core/test_locks.py`
- `backend/tests/unit/services/test_cosgn00c.py`
- `backend/tests/unit/services/test_login_throttle.py`
- `backend/tests/unit/services/test_comen01c.py`
- `backend/tests/unit/services/test_coadm01c.py`
- `backend/tests/unit/services/test_coactvwc.py`
- `backend/tests/unit/services/test_coactupc.py`
- `backend/tests/unit/services/test_cocrdlic.py`
- `backend/tests/unit/services/test_cocrdslc.py`
- `backend/tests/unit/services/test_cocrdupc.py`
- `backend/tests/unit/services/test_cotrn00c.py`
- `backend/tests/unit/services/test_cotrn01c.py`
- `backend/tests/unit/services/test_cotrn02c.py`
- `backend/tests/unit/services/test_cbtrn02c.py`
- `backend/tests/unit/services/test_corpt00c.py`
- `backend/tests/unit/services/test_cbtrn03c.py`
- `backend/tests/unit/services/test_report_export.py`
- `backend/tests/unit/services/test_cobil00c.py`
- `backend/tests/unit/services/test_cousr00c.py`
- `backend/tests/unit/services/test_cousr01c.py`
- `backend/tests/unit/services/test_cousr02c.py`
- `backend/tests/unit/services/test_cousr03c.py`
- `backend/tests/integration/test_api_auth.py`
- `backend/tests/integration/test_api_menu.py`
- `backend/tests/integration/test_api_accounts.py`
- `backend/tests/integration/test_api_cards.py`
- `backend/tests/integration/test_api_transactions.py`
- `backend/tests/integration/test_api_reports.py`
- `backend/tests/integration/test_api_billpay.py`
- `backend/tests/integration/test_api_admin_users.py`
- `backend/tests/integration/test_health.py`
- `backend/tests/integration/test_concurrency.py`
- `backend/tests/integration/test_durability.py`
- `backend/tests/integration/test_migrations.py`
- `backend/tests/contract/test_openapi_contract.py`
- `backend/tests/test_code_standards.py`
- `batch/tests/__init__.py`
- `batch/tests/conftest.py`
- `batch/tests/test_loaders.py`
- `batch/tests/test_dalytran.py`
- `batch/tests/test_security_init.py`
- `batch/tests/test_tranbkp.py`
- `batch/tests/test_posttran.py`
- `batch/tests/test_interest_calc.py`
- `batch/tests/test_combtran.py`
- `batch/tests/test_creastmt.py`
- `batch/tests/test_tranidx.py`
- `batch/tests/test_batch_chain.py`
- `batch/tests/test_gdg.py`
- `batch/tests/test_run_ledger.py`
- `batch/tests/test_batch_window.py`
- `batch/tests/test_cli.py`
- `batch/tests/reconciliation/__init__.py`
- `batch/tests/reconciliation/legacy_oracle.py`
- `batch/tests/reconciliation/build_expected.py`
- `batch/tests/reconciliation/compare.py`
- `batch/tests/reconciliation/test_reconciliation.py`
- `batch/tests/fixtures/expected/loader_rows/accounts.csv`
- `batch/tests/fixtures/expected/loader_rows/cards.csv`
- `batch/tests/fixtures/expected/loader_rows/customers.csv`
- `batch/tests/fixtures/expected/loader_rows/card_xref.csv`
- `batch/tests/fixtures/expected/loader_rows/transactions.csv`
- `batch/tests/fixtures/expected/loader_rows/daily_transactions.csv`
- `batch/tests/fixtures/expected/loader_rows/transaction_types.csv`
- `batch/tests/fixtures/expected/loader_rows/transaction_categories.csv`
- `batch/tests/fixtures/expected/loader_rows/disclosure_groups.csv`
- `batch/tests/fixtures/expected/loader_rows/transaction_category_balances.csv`
- `batch/tests/fixtures/expected/loader_rows/users.csv`
- `batch/tests/fixtures/expected/posttran_dalyrejs.dat`
- `batch/tests/fixtures/expected/posttran_accounts.csv`
- `batch/tests/fixtures/expected/posttran_tcatbal.csv`
- `batch/tests/fixtures/expected/posttran_transactions.csv`
- `batch/tests/fixtures/expected/intcalc_systran.dat`
- `batch/tests/fixtures/expected/intcalc_accounts.csv`
- `batch/tests/fixtures/expected/tranbkp_run2_bkup.dat`
- `batch/tests/fixtures/expected/combtran_combined.dat`
- `batch/tests/fixtures/expected/creastmt_STATEMNT.PS`
- `batch/tests/fixtures/expected/creastmt_STATEMNT.HTML`
- `batch/tests/fixtures/expected/tranrept_lines.txt`

### Frontend

- `frontend/package.json`
- `frontend/package-lock.json`
- `frontend/.nvmrc`
- `frontend/tsconfig.json`
- `frontend/next-env.d.ts`
- `frontend/next.config.ts`
- `frontend/eslint.config.mjs`
- `frontend/.prettierrc.json`
- `frontend/jest.config.mjs`
- `frontend/jest.setup.ts`
- `frontend/playwright.config.ts`
- `frontend/.env.local.example`
- `frontend/.env.e2e.example`
- `frontend/Dockerfile`
- `frontend/.dockerignore`
- `frontend/public/favicon.svg`
- `frontend/src/app/layout.tsx`
- `frontend/src/app/providers.tsx`
- `frontend/src/app/theme.ts`
- `frontend/src/theme/m3.d.ts`
- `frontend/src/app/signon/page.tsx`
- `frontend/src/app/menu/page.tsx`
- `frontend/src/app/admin/page.tsx`
- `frontend/src/app/accounts/view/page.tsx`
- `frontend/src/app/accounts/update/page.tsx`
- `frontend/src/app/cards/page.tsx`
- `frontend/src/app/cards/view/page.tsx`
- `frontend/src/app/cards/update/page.tsx`
- `frontend/src/app/transactions/page.tsx`
- `frontend/src/app/transactions/view/page.tsx`
- `frontend/src/app/transactions/add/page.tsx`
- `frontend/src/app/reports/transactions/page.tsx`
- `frontend/src/app/billpay/page.tsx`
- `frontend/src/app/admin/users/page.tsx`
- `frontend/src/app/admin/users/add/page.tsx`
- `frontend/src/app/admin/users/update/page.tsx`
- `frontend/src/app/admin/users/delete/page.tsx`
- `frontend/src/components/ScreenShell.tsx`
- `frontend/src/components/MessageLine.tsx`
- `frontend/src/components/FunctionKeyBar.tsx`
- `frontend/src/components/SelectionTable.tsx`
- `frontend/src/components/ConfirmField.tsx`
- `frontend/src/lib/apiClient.ts`
- `frontend/src/lib/session.tsx`
- `frontend/src/lib/useFunctionKeys.ts`
- `frontend/src/lib/api/auth.ts`
- `frontend/src/lib/api/menu.ts`
- `frontend/src/lib/api/accounts.ts`
- `frontend/src/lib/api/cards.ts`
- `frontend/src/lib/api/transactions.ts`
- `frontend/src/lib/api/reports.ts`
- `frontend/src/lib/api/billpay.ts`
- `frontend/src/lib/api/users.ts`
- `frontend/src/__tests__/screens/SignOn.test.tsx`
- `frontend/src/__tests__/screens/MainMenu.test.tsx`
- `frontend/src/__tests__/screens/AdminMenu.test.tsx`
- `frontend/src/__tests__/screens/AccountView.test.tsx`
- `frontend/src/__tests__/screens/AccountUpdate.test.tsx`
- `frontend/src/__tests__/screens/CardList.test.tsx`
- `frontend/src/__tests__/screens/CardView.test.tsx`
- `frontend/src/__tests__/screens/CardUpdate.test.tsx`
- `frontend/src/__tests__/screens/TransactionList.test.tsx`
- `frontend/src/__tests__/screens/TransactionView.test.tsx`
- `frontend/src/__tests__/screens/TransactionAdd.test.tsx`
- `frontend/src/__tests__/screens/TransactionReports.test.tsx`
- `frontend/src/__tests__/screens/BillPay.test.tsx`
- `frontend/src/__tests__/screens/AdminUsersList.test.tsx`
- `frontend/src/__tests__/screens/AdminUsersAdd.test.tsx`
- `frontend/src/__tests__/screens/AdminUsersUpdate.test.tsx`
- `frontend/src/__tests__/screens/AdminUsersDelete.test.tsx`
- `frontend/src/__tests__/components/ScreenShell.test.tsx`
- `frontend/src/__tests__/components/MessageLine.test.tsx`
- `frontend/src/__tests__/components/FunctionKeyBar.test.tsx`
- `frontend/src/__tests__/components/SelectionTable.test.tsx`
- `frontend/src/__tests__/components/ConfirmField.test.tsx`
- `frontend/src/__tests__/lib/apiClient.test.ts`
- `frontend/src/__tests__/lib/useFunctionKeys.test.ts`
- `frontend/src/__tests__/lib/session.test.tsx`
- `frontend/src/__tests__/theme.test.ts`
- `frontend/src/__tests__/sourceReferences.test.ts`
- `frontend/e2e/global-setup.ts`
- `frontend/e2e/day-in-the-life.spec.ts`
- `frontend/e2e/role-navigation.spec.ts`

### Documentation

- `docs/README.md`
- `docs/data-model.md`
- `docs/batch.md`
- `docs/api-reference.md`
- `docs/architecture.md`
- `docs/traceability.md`

### Legacy reference inputs

These globs are REFERENCE inputs, read in place; no legacy file is updated or deleted:

- `cbl/*.cbl`
- `cbl/*.CBL`
- `cpy/*.cpy`
- `cpy/COSTM01.CPY`
- `bms/*.bms`
- `cpy-bms/*.CPY`
- `jcl/*.jcl`
- `jcl/*.JCL`
- `proc/*.prc`
- `ctl/REPROCT.ctl`
- `scheduler/CardDemo.ca7`
- `scheduler/CardDemo.controlm`
- `catlg/LISTCAT.txt`
- `csd/CARDDEMO.CSD`
- `data/EBCDIC/*`
- `data/ASCII/*`
