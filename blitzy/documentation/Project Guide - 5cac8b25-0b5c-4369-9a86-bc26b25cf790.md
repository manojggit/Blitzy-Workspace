# 1. Executive Summary

## 1.1 Project Overview

CardDemo's mainframe credit-card application is rebuilt as a FastAPI REST API over PostgreSQL 17, a Next.js 16 / MUI 9 web UI, and a Python `carddemo-batch` CLI that replaces the JCL chain. Regular users, administrators and batch operators keep the legacy literals, edits, page sizes and posting rules across all 17 online transactions. The scope covers an 11-table schema with nine Alembic revisions, session security, an 18-step batch chain, CSV/PDF reporting, local and containerised run paths, and six guides. Legacy sources are unchanged; only README.md was modified.

## 1.2 Completion Status

```mermaid
%%{init: {"theme": "base", "themeVariables": {"pie1": "#5B39F3", "pie2": "#FFFFFF", "pieStrokeColor": "#B23AF2", "pieOuterStrokeColor": "#B23AF2", "pieSectionTextColor": "#1A1A1A", "pieTitleTextColor": "#B23AF2"}}}%%
pie showData title 96.7% Complete
    "Completed Work" : 2540
    "Remaining Work" : 88
```

| Metric | Value |
|---|---|
| Total Hours | 2,628 |
| Completed Hours (AI + Manual) | 2,540 (2,540 AI + 0 manual) |
| Remaining Hours | 88 |
| Percent Complete | 96.7% |

2,540 hours completed out of 2,628 total hours = 96.7% complete.

## 1.3 Key Accomplishments

- ✅ All 17 transactions (CC00–CU03) have an API and a screen; both roles were driven in a browser.
- ✅ 11-table PostgreSQL schema; Alembic `0009 (head)` seeds the exact AAP row counts from the EBCDIC images.
- ✅ 18-step batch chain runs through `carddemo-batch`, with run ledger, archive generations, recovery and a batch window.
- ✅ Session security: HttpOnly cookie, bcrypt, login throttle, Origin CSRF guard, role gates, masked PAN/SSN, no CVV.
- ✅ CR00 report renders on screen and exports CSV (formula guard) and PDF, matching the sample report line for line.
- ✅ Zero failures: 3,718 backend, 564 batch, 2,764 jest and 53 Playwright tests; backend coverage 97%.
- ✅ Every MK_Modernization Rule clause is enforced by a gate (AST checks, ESLint, ruff, Prettier).
- ✅ Native run, Compose topology, both Docker images and six guides under `docs/` are in place.

## 1.4 Critical Unresolved Issues

6 items are open. 5 of them fall against 4 of the 10 requested capabilities (R1 online transactions, R3 batch, R4 business rules, R7 local run); the sixth, production configuration, is path-to-production work. None of the 10 capabilities fails a test. A further 11 LOW usability and parity observations are listed in Section 2.2.

| Issue | Impact | Owner | ETA |
|---|---|---|---|
| CAVW shows unread account fields as blank or zero when the account read fails but the customer read succeeds; this is not in the AAP 0.6.12 inventory (Section 5.2) | An operator could read zero balances during an account-file outage | Product owner + backend lead | 4 h |
| `next` 16.2.11, pinned by the AAP, carries 1 critical and 2 high advisories (`next`, `postcss`, `sharp`); 16.4.5 fixes all three | Exposure of the frontend server in production | Frontend lead | 8 h |
| Production configuration has never run: `APP_ENV=production` (Secure cookie, docs off), secret storage, TLS | Without production mode and TLS, the session cookie lacks `Secure` | DevOps | 8 h |
| The AAP 0.9.3 human review of the reconciliation fixtures, and business UAT, are outstanding. No legacy outputs exist, so parity is proven against an oracle derived from the COBOL | A business-rule mismatch shared by oracle and port would go undetected | Business SMEs + QA | 24 h |
| `docker compose --profile full up` has never run end to end | Migrator ordering and health-check wiring are unverified | DevOps | 4 h |
| Outage paths built but never exercised at runtime: database failure during a rewrite, throttle-write failure, ROLLBACK failure | Behaviour under real outages is unproven | Backend lead + QA | 8 h |

## 1.5 Access Issues

No access issues identified. The database, the Docker daemon and image registry, the npm registry and the Playwright browser were all reachable.

## 1.6 Recommended Next Steps

1. [High] Decide the CAVW account-read-failure display, and sign off `isCardRef` and `card_ref`.
2. [High] Lift the `next` pin to 16.4.5 or later and rerun the frontend gate and Playwright.
3. [High] Configure production: `APP_ENV=production`, stored secrets, TLS and the CORS allow-list.
4. [Medium] Review the reconciliation fixtures with business SMEs and run UAT.
5. [Medium] Run the full Compose profile on a dedicated host; provision backups and the batch schedule.

# 2. Project Hours Breakdown

## 2.1 Completed Work Detail

| Component | Hours | Description |
|---|---|---|
| Legacy analysis and visibility artefacts | 60 | Program, map, copybook and JCL inventory; `docs/data-model.md`, `docs/batch.md`, `docs/traceability.md` (AAP R9) |
| Data layer | 170 | 8 dataset schemas and 11 tables with NUMERIC money and EBCDIC sort keys; 10 ORM models; Alembic `0001`–`0009`; EBCDIC seed reader; repositories (R2) |
| Core platform and security | 150 | Fail-closed settings, session JWT cookie, bcrypt, login throttle, Fernet `card_ref`/`screen_token`, Origin CSRF guard, CORS, security headers, legacy error contract, log masking, health/readiness, DB timeouts and admission (R6) |
| Sign-on and menus (CC00, CM00, CA00) | 50 | `cosgn00c`, `comen01c`, `coadm01c`, role routing and option tables |
| Accounts (CAVW, CAUP) | 120 | `coactvwc`, `coactupc`: the full COACTUPC edit chain, action state machine, row locking |
| Cards (CCLI, CCDL, CCUP) | 100 | `cocrdlic`, `cocrdslc`, `cocrdupc`: 7-row paging, opaque card references, update edits |
| Transactions (CT00, CT01, CT02) and posting rules | 90 | `cotrn00c`–`cotrn02c`, `cbtrn02c`: 10-row paging, add with `copy_last`, TRAN-ID allocation |
| Reports (CR00) | 80 | `corpt00c`, `cbtrn03c`, `report_export`: date ranges, 20-line pages, streamed CSV/PDF (R5) |
| Bill pay (CB00) | 40 | `cobil00c`: confirm flow, width-boundary payment, unconditional step sequence |
| User administration (CU00–CU03) | 70 | `cousr00c`–`cousr03c`: paging, add/update/delete, password semantics under hashing |
| Batch platform | 160 | Typer CLI, 18-step chain, cycles and run ledger, archive-generation catalog, retention and recovery, batch window (R3) |
| Batch loaders and jobs | 150 | 12 loaders, plus POSTTRAN, INTCALC, COMBTRAN, TRANBKP, TRANIDX and CREASTMT (PS/HTML statements) (R3, R4) |
| Frontend foundation | 120 | M3 theme tokens, ScreenShell, MessageLine, FunctionKeyBar, SelectionTable, ConfirmField, API client, session provider, function-key hook |
| Frontend screens | 260 | 17 `page.tsx` routes, one per BMS map, with in-flight locks, focus management and responsive layout (R1) |
| Backend test suite | 260 | 3,718 unit, integration, concurrency, durability, migration and OpenAPI contract tests (R8) |
| Batch tests and legacy oracle | 150 | 564 tests, including an independent oracle derived from the COBOL and full-chain reconciliation (R8) |
| Frontend and end-to-end tests | 180 | 2,764 jest tests in 41 suites; 53 Playwright tests including both role journeys (R8) |
| Code-standards enforcement and tooling | 40 | `backend/tests/test_code_standards.py`, local ESLint rules, ruff, Prettier, `.editorconfig` (Rule) |
| Containerisation and local run | 30 | `backend/Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml` (db plus `full` profile), test-database init, env templates (R7) |
| Documentation | 60 | `docs/README.md`, `api-reference.md`, `architecture.md`, `batch.md`, `data-model.md`, `traceability.md`; root README link |
| Hardening and runtime verification | 200 | Security, performance, accessibility and responsive hardening; browser and API runtime verification of both roles |
| **Total** | **2,540** | |

## 2.2 Remaining Work Detail

| Category | Hours | Priority |
|---|---|---|
| Divergence decisions and API contract sign-off: CAVW unread fields, `isCardRef`, `card_ref`, client-held first-entry prompts (H1) | 4 | High |
| Next.js advisory upgrade: lift the pin to `next` ≥ 16.4.5 with `eslint-config-next`, refresh `package-lock.json`, rerun the frontend gate and Playwright (H2) | 8 | High |
| Production configuration and secrets: `APP_ENV=production`, secret store for `SECRET_KEY` and DB credentials, TLS, CORS allow-list, docs off (H3) | 8 | High |
| Reconciliation fixture review (AAP 0.9.3 human gate, `batch/tests/fixtures/expected/`) and business UAT / parallel run (M1) | 24 | Medium |
| Full-stack Compose verification on a dedicated host (M2) | 4 | Medium |
| Production runtime provisioning: PostgreSQL 17 backups and a restore test, scheduled `run-all`, reverse proxy (M3) | 12 | Medium |
| Fault-injection verification of outage paths never exercised at runtime (M4) | 8 | Medium |
| Batch operator safeguard: refuse `seed-all` and loader reloads under `APP_ENV=production` (M5) | 2 | Medium |
| Usability and accessibility, 4 observations (L1): the sticky message line covers the lowest field at 1280×900 and on mobile, and `/menu` loads scrolled 16 px; a destination screen's first load leaves focus on the page body; the CR00 report region has no top-edge scroll cue; Back after sign-off shows the static shell for about 28 ms | 8 | Low |
| Legacy parity, 6 observations (L2): the CCLI deep link `/cards?acctId=` does not apply the filter; CT01 date boxes carry trailing spaces; CT01 F4 leaves `?tranId` in the URL and the key lacks an autocomplete attribute; the CCLI 7-row page returns identical first and last keys; a zero `acctId` typed into a CCDL URL is seeded as typed; CAUP 401 teardown timing | 8 | Low |
| Logging and repository hygiene, 1 observation (L3): synchronous stdout logging becomes a non-blocking handler; decide whether to keep the 702 PNGs under `blitzy/screenshots/` | 2 | Low |
| **Total** | **88** | |

## 2.3 Completion Calculation

- **Completed hours: 2,540.** Every AAP requirement group is complete except local run (R7), which is about 90% complete; the full Compose verification above finishes it.
- **Remaining hours: 88.** High 20, Medium 50, Low 18.
- **Total project hours:** 2,540 + 88 = 2,628.
- **Completion:** 2,540 ÷ 2,628 × 100 = **96.7%**.
- **Confidence:** high for the engineering items, medium for UAT (24 h), whose duration depends on SME availability and on any mismatch it finds.

# 3. Test Results

Every suite was run against HEAD `cf6ba3e` on a freshly migrated PostgreSQL 17 database. All 7,099 tests passed, with 0 failures and 0 skips. The static gates are also clean: `ruff check` and `ruff format --check` (265 files), ESLint, `tsc --noEmit`, Prettier, `pip check` and `next build` (17 routes).

| Area / Category | Framework | Tests | Passed | Failed | Coverage | What This Proves |
|---|---|---|---|---|---|---|
| Online services, API layer, schemas, repositories (`backend/tests/unit/{services,api,schemas,repositories}`) | pytest 9.1.1 | 1,420 | 1,420 | 0 | 97% of `backend/app` (whole package) | Every COBOL procedure path, literal and edit order of the 17 transactions is reproduced, including where a write happens |
| Core platform, utilities, seed, migrations, deployment (`backend/tests/unit/{core,utils,seed,migrations,deployment}`, DB guard) | pytest | 1,451 | 1,451 | 0 | (included above) | Security primitives, zoned-decimal and record-layout codecs, settings fail-closed, seed parsing and the image contract are correct |
| API integration and OpenAPI contract (`backend/tests/integration`, `backend/tests/contract`) | pytest + httpx + schemathesis | 712 | 712 | 0 | (included above) | Both roles get the right answers and 403 refusals against live PostgreSQL; concurrency, durability, outage and migration behaviour hold; the OpenAPI contract has no violations |
| Code standards (`backend/tests/test_code_standards.py`) | pytest + AST | 135 | 135 | 0 | n/a | Every module meets the MK_Modernization naming, length, parameter and source-header rules |
| Batch jobs, loaders, chain, run ledger, archive generations, batch window (`batch/tests`) | pytest | 534 | 534 | 0 | Not measured | Each job matches its expected records; reruns, recovery, retention and window fencing behave as specified |
| Batch reconciliation (`batch/tests/reconciliation`) | pytest | 30 | 30 | 0 | Not measured | The full 18-step chain on the sample data equals an independent oracle derived from the COBOL |
| Frontend screens, components, API client, theme, config (`frontend/src/__tests__`, 41 suites) | Jest 30 + Testing Library | 2,764 | 2,764 | 0 | Not measured | All 17 screens render their BMS labels and lengths, show every literal, bind every PF key and lock input while busy; the theme and source headers are enforced |
| End-to-end (`frontend/e2e`) | Playwright 1.64.1 (Chromium) | 53 | 53 | 0 | n/a | The regular-user day-in-the-life (sign on, add transaction, batch, statement) and both role menus work end to end; 49 of the 53 are amount-parser cases |

**Not Covered**

- **Rewrite-phase database failure.** No test reaches the database-failure outcome between the locked read and the commit in CCUP and CAUP (`StepInProgress`, `backend/app/services/cards/cocrdupc.py:981`, `backend/app/services/accounts/coactupc.py:1456`), or CB00's rewrite-failure log (`backend/app/services/billpay/cobil00c.py:149`). Before release, inject a failure between the read and the commit.
- **Unit tests only, never against a live failure.** Response-validation redaction; the asyncpg `InternalClientError` mapping; the session-invalidate fallback after a failed ROLLBACK; the sign-on 503 when the throttle write fails; the CT02 transaction-lookup failure; and the CAUP F12 re-read failure (jest only). Stage a real database outage in a non-shared environment.
- **Reachable through API and jest only.** CU00 and CT00 invalid-selection redisplay (the UI offers only valid codes), and a CU00 second page (the seed has exactly 10 users).
- **Theme rules with no screen using them.** Outlined multiline field variants and the disabled stacked-segment colour are covered only by `theme.test.ts`.
- **Coverage measurement.** Coverage is measured for the backend only (97%). Batch and frontend coverage is not measured.
- **Containers.** No test exercises the Compose `full` profile or the frontend container serving the UI.

# 4. Runtime Validation & UI Verification

The stack ran natively: PostgreSQL 17, uvicorn, and the Next.js standalone production build. It was driven with curl, Playwright and headless Chrome at 1280×900 and 375×812.

- ✅ **Start-up.** `alembic upgrade head` reaches `0009 (head)` and seeds exactly users 10; accounts, customers, cards and card_xref 50 each; transaction types 7; categories 18; disclosure groups 51; category balances 50; daily transactions 300; transactions 0. `/health` returns `{"status":"ok"}`, `/health/ready` returns `{"status":"ready"}`, and `/signon` returns 200.
- ✅ **Authentication.** Login returns 200 and sets `carddemo_session` (HttpOnly, SameSite=Lax, Max-Age 3600), with `nosniff`, `X-Frame-Options: DENY` and `frame-ancestors 'none'`. A wrong password returns 401. Logout clears the cookie, and `/auth/me` then returns 401.
- ✅ **Authorization and CSRF.** `/cards` without a cookie returns 401. A cookie mutation with no `Origin`, or a foreign one, returns 403; an allow-listed `Origin` returns 200. A regular user opening `/admin/users` gets 403 and returns to CM00 with `No access - Admin Only option... `. An admin opening `/menu` gets 403 and returns to CA00.
- ✅ **Regular-user journey (browser).** CC00 → CM00 (11 options) → CAVW account `00000000001` (SSN shown as `***-**-3888`) → CCLI (7 rows, PAN `************5740`, opaque `card_ref`, no CVV; F8 to page 2) → CT00 (10 rows) → CR00 → CB00 → F3 sign-off. Across 96 requests there were 0 JS exceptions and 0 5xx responses.
- ✅ **Admin journey (browser).** CA00 (6 options) → CU00 (10 users; F8 answers the bottom-of-page literal) → CU02 for USER0001 (no password returned; an unchanged F3 Save&Exit answers `Please modify to update ...` and writes nothing) → CU01 empty form. At 375 px the user table scrolls inside its region (311 px of 688 px visible) without page overflow. Back after sign-off shows no data before returning to `/signon`.
- ✅ **End-to-end flows and batch.** Playwright ran 53 of 53: sign on, view account 4, add transaction `0000000000000001`, `run-all` from POSTTRAN, and the statement lines checked; both role-navigation journeys also passed. The backend log showed 0 tracebacks and 0 5xx responses. `carddemo-batch --help` lists `run-all`, `seed-all`, `job` and `load`.
- ✅ **Containers.** `docker compose config` validates `db`, plus `migrator`, `backend` and `frontend` under the `full` profile. Both images build. The backend container served `/health`, `/health/ready` and login 200 against PostgreSQL.
- ⚠ **Layout.** The sticky message line covers the lowest input at 1280×900 and on mobile, and `/menu` loads scrolled by 16 px. Both are cosmetic, and the field stays focused and usable.
- ⚠ **Covered by API integration tests, not driven in a browser.**
  - Writes: CAUP and CCUP saves, the CB00 payment, CU01 add, CU02 save, CU03 delete.
  - CR00 report submission and CSV/PDF downloads.
- ⚠ **Never exercised at runtime.**
  - `docker compose --profile full up` end to end (the host's fixed ports 5432, 8000 and 3000 are shared).
  - The frontend container serving the UI.
  - Production mode (`APP_ENV=production`: Secure cookie, docs off, seed skipped), which is unit-tested only.

# 5. Compliance & Quality Review

## 5.1 Compliance Matrix

| Deliverable (AAP) | Benchmark | Evidence | Status | Progress |
|---|---|---|---|---|
| 17 online transactions (R1, 0.6.9) | One API group and one page per transaction; literals, edits, page sizes preserved | 22 service modules, 8 routers, 17 `page.tsx`; 1,420 service/API tests, 1,631 screen tests | ✅ Pass | 100% |
| Schema, migrations, seed (R2, 0.6.7) | 11 tables in 8 schemas, NUMERIC money, `0009 (head)`, exact seed counts | `backend/alembic/versions/0001`–`0009`; live row counts | ✅ Pass | 100% |
| Batch chain and jobs (R3, 0.6.8) | 18 steps, exit statuses 0/4/12/16, per-job parity, reconciliation | `batch/`; 564 batch tests | ✅ Pass | 100% |
| Business rules (R4, 0.6.6, 0.6.12) | `StoreInto` truncation, POSTTRAN rejects, pinned defect-looking behaviours | `test_cbtrn02c.py`, `test_decimal_utils.py`, reconciliation oracle | ✅ Pass | 100% |
| Reports (R5, 0.6.11) | On-screen table, CSV formula guard, 133-column PDF | `report_export.py`; sample-report parity case | ✅ Pass | 100% |
| Security (R6, 0.7.1) | HttpOnly session, bcrypt, throttle, Origin guard, role gates, masking, fail-closed key | Live curl checks; `test_security.py`, `test_middleware.py` | ✅ Pass (⚠ `next` advisories) | 100% |
| Local run and containers (R7, 0.11.1) | Native sequence; Compose `db` and `full` profile | Native run verified; both images build | ⚠ Partial | 90% |
| Test suites (R8, 0.9.2) | Zero failures and zero errors | 7,099 passed, 0 failed | ✅ Pass | 100% |
| Visibility artefacts and docs (R9, 0.8.4) | Data model and batch docs first; six guides; README link | `docs/` | ✅ Pass | 100% |
| Minimal change and traceability (R10, 0.11.3) | Only README.md modified; one module per program; source headers | Branch diff; `sourceReferences.test.ts`; code-standards gate | ✅ Pass | 100% |
| Design system (0.5) | MUI 9 M3 tokens only in `theme.ts`; no raw HTML controls | `frontend/src/app/theme.ts`; ESLint; `theme.test.ts` (218 tests) | ✅ Pass | 100% |
| MK_Modernization Rule (0.10) | Naming, formatting, ≤20 lines, ≤4 params, no secrets, specific exceptions, docs style | 135 code-standards tests; ruff (1 unrelated `noqa`); 0 `eslint-disable` | ✅ Pass | 100% |

## 5.2 AAP & Rule Divergences and Gaps

| What the AAP/Rule Required | What Was Delivered Instead | Why It Diverged | Impact | Remediation |
|---|---|---|---|---|
| AAP 0.6.6: `StoreInto` runs in `COBOL_CONTEXT` at precision 31 | It runs at precision 31 plus the receiver's decimal places (`backend/app/utils/decimal_utils.py:59`) | Quantizing a 31-digit integer part to the receiver's scale needs that headroom to avoid `InvalidOperation`; truncation stays explicit | None: results are identical and pinned by tests | None needed |
| AAP 0.6.9: `GET /cards/{cardNum}?acctId`, where `cardNum` is a PAN or a `card_ref` | An optional `isCardRef` query flag declares a short value to be a token (`backend/app/api/v1/cards.py:100`; documented at `docs/api-reference.md:1482`) | A `card_ref` shorter than 16 characters cannot be told apart from a typed key by its length | Additive: callers that omit it behave as specified | Sign off, or remove (H1) |
| AAP 0.6.9: `CardDetailResponse` carries a masked PAN, no CVV and `info_message` | Card detail and card update bodies also return the opaque `card_ref` (`backend/app/schemas/cards.py:282`, `:355`) | The screen needs a key for the next F5/F12 and navigation without holding the PAN | Additive; no PAN exposure | Sign off (H1) |
| AAP 0.6.12: every observable defect-looking behaviour is listed and pinned | CAVW shows unread account fields as blank or zero when the account read fails but the customer read succeeds (`backend/app/services/accounts/coactvwc.py:274`–`298`, `:439`). This is not in the inventory | Faithful port: the legacy guard is dead because its `SET` is commented out (`cbl/COACTVWC.cbl:792`) | Misleading zero balances on screen during an account-file outage | **Human decision** (H1) |
| AAP 0.6.3: message texts come from the server | The CAVW and CAUP first-entry prompts are held in the pages (`frontend/src/app/accounts/view/page.tsx:52`, `frontend/src/app/accounts/update/page.tsx:234`) | AAP 0.6.9 defines no first-entry endpoint to serve them before an account is keyed | Two literals duplicated client-side; drift risk | Sign off, or add an endpoint (H1) |
| AAP 0.11.1: `cp -rT .next/static .next/standalone/.next/static && cp -rT public …` | `docs/README.md:396`–`397` uses `cp -R .next/static .next/standalone/.next/` and `cp -R public .next/standalone/` | macOS BSD `cp` has no `-T`; the macOS target needs a portable form | None: same result on Linux and macOS | None needed |
| AAP 0.5.5: no colour literal outside `theme.ts` | `frontend/public/favicon.svg` holds `#6750A4` and `#FFFFFF` | AAP 0.8.1 requires the icon, and an SVG asset cannot import the theme | None: the values equal the theme's primary pair | None needed |
| AAP 0.4.1 pins `next` 16.2.11 (known issue accepted) | The pin is kept (`frontend/package.json:28`). `npm audit --omit=dev` reports 1 critical (`next`) and 2 high (`postcss`, `sharp`) | The fix needs `next` 16.4.5, outside the AAP pin | Exposure of the frontend server | **Lift the pin and re-gate** (H2) |
| AAP 0.11.1: full stack via `docker compose --profile full up -d --build` | The Compose file validates, both images build, and the backend container serves health and login. The `full` profile was never brought up end to end | The verification host's ports 5432, 8000 and 3000 are shared and could not be bound | Migrator ordering and health-check wiring are unverified | **Run on a dedicated host** (M2) |
| Rule clause 1: PascalCase, camelCase and UPPER_CASE names | Database columns, ORM attributes and JSON keys are snake_case (`acct_id`, `curr_balance`, `{"user_id"}`) | **Sanctioned** by AAP 0.10.1, which fixes these names from the schema mapping; the AAP outranks the Rule | None | None needed |
| Rule clause 1: method and variable casing | Framework-mandated names keep their form: Alembic `upgrade`/`downgrade`, pytest `test_*`, React `use*` hooks, Next.js `metadata` and `dynamic` exports, logging `filter`/`emit` overrides, dunders | **Sanctioned** by AAP 0.10.1 ("identifiers fixed outside the rule keep their form") | None; each is commented at its site | None needed |
| Rule clause 2: lines within 80–120 characters | Reconciliation data fixtures exceed 120 columns: `batch/tests/fixtures/expected/tranrept_lines.txt` (519 lines of 133 columns) and the customer, daily-transaction and posted-transaction CSVs. They are exempted in `.editorconfig` | **Sanctioned** by AAP 0.9.1/0.6.11, which require byte-for-byte legacy record and 133-column report widths | None; no code file has a line over 100 | None needed |

**CAVW unread account fields.** The CAVW read chain keeps going after an account-file failure, because COACTVWC's `DID-NOT-FIND-ACCT-IN-ACCTDAT` test is dead: its `SET` at `cbl/COACTVWC.cbl:792` is commented out. The port therefore still reads the customer and shows the account section with blank text and zero amounts beside the file-error message (`backend/app/services/accounts/coactvwc.py:274`–`298`). This is legacy-faithful, but AAP 0.6.12 lists every pinned defect-looking behaviour, and this one is missing. The product owner must decide whether to keep it, and add it to the inventory and `docs/traceability.md`, or blank the account section on that path. Either way, a test should pin the chosen outcome.

**`next` 16.2.11 advisories.** `npm audit --omit=dev` reports one critical advisory against `next` (affected range up to 16.4.0) and two high advisories against `postcss` and `sharp` pulled in by it. All three are fixed by `next` 16.4.5. The AAP pins 16.2.11 exactly, so the pin was kept (`frontend/package.json:28`) and the risk stands for any internet-facing deployment. Before release, decide to lift the pin. Then update `next` and `eslint-config-next` together, regenerate `frontend/package-lock.json`, and rerun `npm run lint`, `type-check`, `format:check`, jest, `npm run build` and the Playwright suite.

**Compose `full` profile.** `docker-compose.yml` defines `db` and, under the `full` profile, a one-shot `migrator`, `backend` and `frontend`, with health-gated start order. `docker compose config` validates. `backend/Dockerfile` and `frontend/Dockerfile` both build, and the backend image served `/health`, `/health/ready` and login against PostgreSQL. The full profile binds the fixed ports 5432, 8000 and 3000, which were in use on the verification host, so the orchestrated start was never observed. On a dedicated host, run `docker compose --profile full up -d --build`, then confirm the migrator exits after `0009 (head)`, both health checks pass and `/signon` loads.

**`isCardRef` query flag.** AAP 0.6.9 lets `cardNum` be a 16-digit PAN or a server-issued `card_ref`, and tells them apart by length. A `card_ref` coming back from a frontend URL can be shorter than a PAN, so `GET /cards/{cardNum}` accepts an optional `isCardRef=true` that treats the value as a token whatever its length (`backend/app/api/v1/cards.py:100`). Omitting it gives exactly the specified behaviour. It is documented in `docs/api-reference.md:1482` and covered by the contract test. Architecture should approve it as an additive contract element, or require the frontend to rely on length alone.

**`card_ref` on card screens.** `CardDetailResponse` and `CardScreen` both return the opaque `card_ref` (`backend/app/schemas/cards.py:282`, `:355`), which the AAP 0.6.9 declaration does not list. The field lets CCDL and CCUP carry the card key into the next F5, F12 or navigation without the browser ever holding the PAN. It also matches the token CCLI already returns per row. Nothing is removed from the declared contract, and the PAN stays masked. Approve it as part of the published contract in `docs/api-reference.md`, or ask for it to be removed and the page to keep the token from its URL.

**Client-held first-entry prompts.** AAP 0.6.3 says message texts come from the server. On first entry, before any account is keyed, CAVW and CAUP show `Enter or update id of account to display` and `… to update` from constants in their pages (`frontend/src/app/accounts/view/page.tsx:52`, `frontend/src/app/accounts/update/page.tsx:234`). AAP 0.6.9 defines no endpoint that answers before a key exists, so no server source was available. Every later message comes from the API. Accept the two duplicated literals, which jest asserts, or add a first-entry endpoint and remove the constants.

Not divergences: F3 on CCDL, CCUP and CT01 returns to the owning menu rather than the calling list, as AAP 0.6.3 requires. Protected fields stay focusable, per AAP 0.5. `AUTH_MODE` is absent, as session-only auth requires (AAP 0.7.1).

# 6. Risk Assessment

| Risk | Category | Severity | Probability | Mitigation | Status |
|---|---|---|---|---|---|
| `next` 16.2.11 carries a critical advisory, plus high advisories in `postcss` and `sharp` | Security | High | Medium | Upgrade to `next` ≥ 16.4.5 and re-gate (H2); until then, keep the frontend off the public internet | Open |
| Parity is proven against an oracle derived from the COBOL, because no legacy output files exist. A misreading shared by oracle and port would pass every test | Technical | High | Low | AAP 0.9.3 fixture review by SMEs, then a parallel run against mainframe output (M1) | Open |
| `seed-all` and the loaders reload tables even under `APP_ENV=production`; only `load init-users` refuses (`batch/loaders/security_init.py:72`) | Operational | High | Low | Refuse destructive reloads in production before the first step (M5); restrict batch credentials | Open |
| CAVW shows zero balances beside the file-error message when the account read fails | Technical | Medium | Low | Product decision and a pinning test (H1) | Open |
| The session JWT is not revoked server-side on logout, by AAP 0.7.1 design; a copied cookie stays valid until it expires (60 min) | Security | Medium | Low | TLS with `APP_ENV=production` (Secure cookie); keep `ACCESS_TOKEN_EXPIRE_MINUTES` short; add a denylist if policy requires | Accepted by design |
| Logging writes synchronously to stdout, and monitoring and alerting are outside the AAP scope | Operational | Medium | Medium | Move to a queue-based handler and ship logs; add health-probe alerting at deployment (L3, M3) | Open |
| The Compose `full` profile (migrator ordering, health-gated start) has never run end to end | Operational | Medium | Medium | Bring it up on a dedicated host (M2) | Open |
| Outage paths (database failure mid-rewrite, throttle-write 503, ROLLBACK failure) are proven by unit tests only | Integration | Medium | Low | Fault-injection runs against a disposable PostgreSQL (M4) | Open |

# 7. Visual Project Status

```mermaid
%%{init: {"theme": "base", "themeVariables": {"pie1": "#5B39F3", "pie2": "#FFFFFF", "pieStrokeColor": "#B23AF2", "pieOuterStrokeColor": "#B23AF2", "pieSectionTextColor": "#1A1A1A", "pieTitleTextColor": "#B23AF2"}}}%%
pie showData title Project Hours Breakdown
    "Completed Work" : 2540
    "Remaining Work" : 88
```

Remaining work by priority (88 hours):

```mermaid
%%{init: {"theme": "base", "themeVariables": {"pie1": "#5B39F3", "pie2": "#B23AF2", "pie3": "#A8FDD9", "pieStrokeColor": "#1A1A1A", "pieSectionTextColor": "#1A1A1A", "pieTitleTextColor": "#B23AF2"}}}%%
pie showData title Remaining Hours by Priority
    "High" : 20
    "Medium" : 50
    "Low" : 18
```

| Remaining category (Section 2.2) | Hours | Priority |
|---|---|---|
| Fixture review and business UAT (M1) | 24 | Medium |
| Production runtime provisioning (M3) | 12 | Medium |
| Next.js advisory upgrade (H2) | 8 | High |
| Production configuration and secrets (H3) | 8 | High |
| Fault-injection verification (M4) | 8 | Medium |
| Usability and accessibility follow-ups (L1) | 8 | Low |
| Legacy-parity follow-ups (L2) | 8 | Low |
| Divergence decisions and contract sign-off (H1) | 4 | High |
| Full-stack Compose verification (M2) | 4 | Medium |
| Batch operator safeguard (M5) | 2 | Medium |
| Logging and repository hygiene (L3) | 2 | Low |
| **Total** | **88** | |

# 8. Summary & Recommendations

CardDemo is 96.7% complete against the AAP scope: 2,540 of 2,628 hours. Every AAP requirement group is delivered. The one exception is local run (R7), which is about 90% complete: the native run is verified, but the Compose `full` profile has not been brought up end to end. All 17 online transactions run as FastAPI services behind Next.js screens, with the legacy literals, edits, page sizes and PF-key behaviour. The 11-table schema migrates to `0009 (head)` and seeds the exact AAP counts. The 18-step batch chain runs through `carddemo-batch` with a run ledger, archive generations, recovery and a batch window. Existing legacy sources were not modified; README.md is the only changed file.

Verification is broad and first-hand. 7,099 tests pass with no failures: 3,718 backend (97% coverage), 564 batch including full-chain reconciliation against an independent oracle, 2,764 jest and 53 Playwright. Live checks confirmed the HttpOnly session cookie, the 401 and 403 gates, the Origin CSRF guard, masked PAN and SSN with no CVV, and both role journeys in a browser with zero JS exceptions and zero 5xx responses. Both Docker images build, and the backend container serves health and login. Every MK_Modernization Rule clause is enforced by an automated gate. The only departures from it are those the AAP itself sanctions.

The remaining 88 hours are release work, not missing features. The critical path:
1. Decide the CAVW account-read-failure display, and sign off the three additive contract elements (4 h).
2. Lift the `next` pin to clear one critical and two high advisories (8 h).
3. Stand up production configuration: `APP_ENV=production`, secrets, TLS and CORS (8 h).
4. Run the AAP's human review of the reconciliation fixtures together with business UAT (24 h). This is the largest item, and the only one whose duration depends on what it finds.
5. Verify the full Compose profile, provision the runtime, run fault injection, and add the production guard on destructive batch reloads (26 h).

| Readiness indicator | Status |
|---|---|
| Functional parity (17 transactions, 18 batch steps) | ✅ Verified by tests and runtime |
| Test suites | ✅ 7,099 / 7,099 passing |
| Security controls | ✅ Verified live; ⚠ framework advisories open |
| Deployment | ⚠ Images build; full Compose profile and production mode not yet run |
| Business acceptance | ⚠ Fixture review and UAT outstanding |

**Assessment:** the codebase is ready for staging and UAT. It is ready for production once the three High items are closed and UAT signs off parity. Success means zero parity differences in UAT, a clean `npm audit --omit=dev`, the `full` profile healthy on first start, and a Secure session cookie over TLS.

# 9. Development Guide

All commands run from the repository root unless a step says otherwise. Every Python command calls `.venv/bin/…` explicitly, so no shell activation is needed. Use the host `127.0.0.1` everywhere: `localhost` is a different cookie site and may resolve to IPv6 first.

## System Prerequisites

- Python **3.13.7** (`.python-version`); install with uv, pyenv or python.org.
- Node.js **22.23.1** (`frontend/.nvmrc`) with npm 11.
- Docker Engine with the Compose plugin, for `postgres:17`. Alternatively, any PostgreSQL 17 server at `127.0.0.1:5432` with role and database `carddemo`, with `docker/postgres/init-test-db.sql` applied.
- Linux or macOS. About 2 GB free disk for `.venv`, `node_modules` and the build.

## Environment Setup

Create the two env files. Both templates already carry the `127.0.0.1` values:

```bash
cp backend/.env.example backend/.env
openssl rand -hex 32        # paste the output into SECRET_KEY= in backend/.env
cp frontend/.env.local.example frontend/.env.local
```

- `SECRET_KEY` must be at least 32 characters, or the backend refuses to start.
- `NEXT_PUBLIC_API_URL` is the backend origin only (`http://127.0.0.1:8000`). It is inlined at build time.

## Dependency Installation

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt -r batch/requirements.txt
.venv/bin/pip install -e backend -e batch
.venv/bin/pip check          # expect: No broken requirements found.
cd frontend && npm ci && cd ..
```

Keep `sqlalchemy[asyncio]==2.0.36`. On Python 3.13 the extra is what installs greenlet for the async engine.

## Database: Start, Migrate, Seed

```bash
docker compose up -d db                      # postgres:17 on 127.0.0.1:5432; also creates carddemo_test
cd backend && ../.venv/bin/alembic upgrade head && ../.venv/bin/alembic current && cd ..
```

Expected output is `0009 (head)`. The seeded rows are: users 10; accounts, customers, cards and card_xref 50 each; transaction types 7; categories 18; disclosure groups 51; category balances 50; daily transactions 300; transactions 0. Run migrations with the backend stopped.

## Application Startup

Backend, in shell 1:

```bash
cd backend && ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Frontend (production build), in shell 2:

```bash
cd frontend
npm run build
cp -rT .next/static .next/standalone/.next/static && cp -rT public .next/standalone/public
HOSTNAME=127.0.0.1 PORT=3000 node .next/standalone/server.js
```

- On macOS, use `cp -R .next/static .next/standalone/.next/ && cp -R public .next/standalone/` instead.
- Development mode instead: `npm run dev` (binds `127.0.0.1:3000`).
- Full stack in containers: `docker compose --profile full up -d --build`. A one-shot `migrator` runs `alembic upgrade head` before `backend` starts.

## Verification Steps

```bash
curl -s http://127.0.0.1:8000/health            # {"status":"ok"}
curl -s http://127.0.0.1:8000/health/ready      # {"status":"ready"}
mkdir -p var
curl -s -i -c var/session.cookies -X POST http://127.0.0.1:8000/api/v1/auth/login \
    -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:3000' \
    -d '{"user_id":"USER0001","password":"PASSWORD"}'   # 200 + Set-Cookie carddemo_session; HttpOnly
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/cards     # ≤ 7 rows, card_num_masked, no CVV
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/cards   # 401
```

- Open `http://127.0.0.1:3000/signon`.
- The demo users come only from the legacy USRSEC image: `ADMIN001`–`ADMIN005` (type A) and `USER0001`–`USER0005` (type U), each with the legacy sample password `PASSWORD`. They are seed data, never production accounts.
- `GET /cards` is a regular-user function, so an admin session gets 403 there.

## Example Usage: Batch CLI

```bash
.venv/bin/carddemo-batch --help                 # run-all, seed-all, job, load
.venv/bin/carddemo-batch seed-all               # reload every table (destructive to the target database)
.venv/bin/carddemo-batch run-all --new-cycle --as-of 2022-07-18T00:00:00
.venv/bin/carddemo-batch run-all --start-at POSTTRAN --as-of 2022-07-18T00:00:00
.venv/bin/carddemo-batch job interest-calc --run-date 2022071800   # one step of the OPEN cycle
.venv/bin/carddemo-batch load init-users        # refuses with exit 12 under APP_ENV=production
```

- Statements and archive generations go to `var/batch-output`.
- Exit statuses:
  - 0: success.
  - 4: POSTTRAN rejected records; a chain continues.
  - 12: refusal or recoverable failure; a chain stops.
  - 16: abend; a chain stops.
  - 2: usage error.
- `python -m batch.cli` is equivalent to `carddemo-batch`.

## Running the Test Suites

```bash
cd backend && ../.venv/bin/pytest -q --cov=app && cd ..      # 3,718 passed, 97% coverage (needs carddemo_test)
.venv/bin/pytest -q batch/tests                              # 564 passed; run after, never alongside, the backend suite
.venv/bin/ruff check backend batch && .venv/bin/ruff format --check backend batch
cd frontend && npm run lint && npm run type-check && npm run format:check
CI=true npm test -- --watchAll=false --ci --maxWorkers=2     # 41 suites, 2,764 passed
cp .env.e2e.example .env.e2e                                 # fill in the four credential values
npx playwright install chromium && CI=true npm run test:e2e  # 53 passed; backend and frontend must be running
```

- The Playwright global setup runs `carddemo-batch seed-all` against the app database before the specs.
- It refuses quoted or commented lines in `backend/.env`, and requires the same host in the base and API URLs.

## Troubleshooting

- **Backend exits on start with a `SECRET_KEY` error.** The key is blank or shorter than 32 characters. Generate one with `openssl rand -hex 32`.
- **Login works but the next call returns 401.** The page is on `localhost` and the API on `127.0.0.1`, or the other way round. Use `127.0.0.1` for both.
- **CORS preflight fails, or the UI calls the wrong host.** `NEXT_PUBLIC_API_URL` must be the backend origin with no `/api/v1`. Rebuild after changing it, and list the frontend origin in `BACKEND_CORS_ORIGINS`.
- **A cookie request that changes data returns 403.** The CSRF guard needs an allow-listed `Origin` header; only login is exempt.
- **Connection refused inside Compose.** The database host is `db`, not `127.0.0.1`. Compose overrides the URLs for the `full` profile.
- **`alembic current` is not `0009 (head)`.** Stop the backend and run `alembic upgrade head` again.
- **Health probe hangs.** You are probing `localhost`, which resolved to `::1`. Probe `127.0.0.1`.
- **Online calls answer the batch-window literal.** A batch job holds the window lock. Retry once the job finishes.
- **`seed-all` or Playwright wiped your data.** Both reload every table. Point `backend/.env` at a disposable database before running them.

# 10. Appendices

## A. Command Reference

| Purpose | Command |
|---|---|
| Dependency health | `.venv/bin/pip check` |
| Start database | `docker compose up -d db` |
| Migrate and seed | `cd backend && ../.venv/bin/alembic upgrade head` |
| Migration status | `cd backend && ../.venv/bin/alembic current` (expect `0009 (head)`) |
| Start backend | `cd backend && ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| Build frontend | `cd frontend && npm run build` |
| Start frontend | `cd frontend && HOSTNAME=127.0.0.1 PORT=3000 node .next/standalone/server.js` |
| Full stack | `docker compose --profile full up -d --build` |
| Batch chain in the container | `docker compose --profile full run --rm backend carddemo-batch run-all` |
| Backend tests | `cd backend && ../.venv/bin/pytest -q --cov=app` |
| Batch tests | `.venv/bin/pytest -q batch/tests` |
| Python lint and format | `.venv/bin/ruff check backend batch && .venv/bin/ruff format --check backend batch` |
| Frontend gate | `cd frontend && npm run lint && npm run type-check && npm run format:check && CI=true npm test -- --watchAll=false --ci` |
| End-to-end | `cd frontend && CI=true npm run test:e2e` |
| Dependency audit | `cd frontend && npm audit --omit=dev` |

## B. Port Reference

| Service | Port | Bind | Notes |
|---|---|---|---|
| PostgreSQL 17 | 5432 | 127.0.0.1 | Compose `db`; databases `carddemo` and `carddemo_test` |
| FastAPI backend | 8000 | 127.0.0.1 | `/health`, `/health/ready`, `/api/v1/*`, `/docs` (non-production) |
| Next.js frontend | 3000 | 127.0.0.1 | Entry route `/signon` |

## C. Key File Locations

| Area | Path |
|---|---|
| App factory and middleware | `backend/app/main.py`, `backend/app/core/middleware.py` |
| Settings and security | `backend/app/core/config.py`, `backend/app/core/security.py` |
| Error contract and literals | `backend/app/core/errors.py`, `backend/app/core/messages.py` |
| Online services, one per COBOL program | `backend/app/services/{auth,menu,accounts,cards,transactions,reports,billpay,users}/` |
| API routers | `backend/app/api/v1/` |
| ORM models and repositories | `backend/app/models/`, `backend/app/repositories/` |
| COBOL numeric and record codecs | `backend/app/utils/decimal_utils.py`, `backend/app/utils/record_layouts.py` |
| Migrations | `backend/alembic/versions/0001`–`0009` |
| Batch CLI and orchestration | `batch/cli.py`, `batch/orchestration/` (chain, window, GDG, run ledger) |
| Batch jobs and loaders | `batch/jobs/`, `batch/loaders/` |
| Reconciliation oracle and fixtures | `batch/tests/reconciliation/`, `batch/tests/fixtures/expected/` |
| Frontend pages, theme, shell | `frontend/src/app/**/page.tsx`, `frontend/src/app/theme.ts`, `frontend/src/components/` |
| API client and session | `frontend/src/lib/apiClient.ts`, `frontend/src/lib/session.tsx` |
| Code-standards gate | `backend/tests/test_code_standards.py`, `frontend/eslint.config.mjs` |
| Containers and env templates | `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`, `backend/.env.example`, `frontend/.env.local.example`, `frontend/.env.e2e.example` |
| Documentation | `docs/README.md`, `docs/api-reference.md`, `docs/architecture.md`, `docs/batch.md`, `docs/data-model.md`, `docs/traceability.md` |
| Legacy reference (unchanged) | `cbl/`, `cpy/`, `cpy-bms/`, `bms/`, `jcl/`, `proc/`, `data/EBCDIC/`, `data/ASCII/` |

## D. Technology Versions

| Layer | Component | Version |
|---|---|---|
| Runtime | Python / Node.js | 3.13.7 / 22.23.1 |
| Backend | FastAPI / Uvicorn / Pydantic | 0.136.1 / 0.34.0 / 2.10.4 |
| Backend | SQLAlchemy (asyncio) / Alembic | 2.0.36 / 1.14.0 |
| Backend | asyncpg / psycopg2-binary | 0.30.0 / 2.9.10 |
| Backend | PyJWT / cryptography / passlib / bcrypt | 2.13.0 / 50.0.2 / 1.7.4 / 4.0.1 |
| Batch and reports | Typer / reportlab / Jinja2 | 0.27.0 / 4.5.1 / 3.1.6 |
| Database | PostgreSQL | 17 |
| Frontend | Next.js / React | 16.2.11 / 19.2.7 |
| Frontend | @mui/material / @mui/material-nextjs / @emotion/react | 9.2.0 / 9.1.1 / 11.14.0 |
| Frontend | axios / TypeScript | 1.21.0 / 5.9.3 |
| Testing | pytest / Jest / Playwright | 9.1.1 / 30.5.2 / 1.64.1 |
| Tooling | ruff / ESLint / Prettier | 0.17.3 / 9.39.5 / 3.9.10 |

## E. Environment Variable Reference

| Variable | Scope | Template value | Notes |
|---|---|---|---|
| `APP_ENV` | Backend, batch | `local` | `local`, `test` or `production`. `production` makes the cookie Secure, turns docs off, skips the sample seed and refuses `init-users` |
| `DATABASE_URL` | Backend | `postgresql+asyncpg://carddemo:carddemo@127.0.0.1:5432/carddemo` | Async runtime engine |
| `SYNC_DATABASE_URL` | Backend, batch, Alembic | `postgresql+psycopg2://…/carddemo` | Required by the batch CLI |
| `TEST_DATABASE_URL` / `TEST_SYNC_DATABASE_URL` | Tests | (derived) `…/carddemo_test` | Optional overrides |
| `SECRET_KEY` | Backend | blank | Required, at least 32 characters; the backend fails closed |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Backend | `60` | Session lifetime |
| `SESSION_COOKIE_NAME` | Backend | `carddemo_session` | HttpOnly cookie |
| `BCRYPT_ROUNDS` | Backend, batch | `12` | Hash cost, 4–31 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | Backend | `5` / `900` | Sign-on throttle (429) |
| `BACKEND_CORS_ORIGINS` | Backend | `http://127.0.0.1:3000` | Comma-separated; also the CSRF `Origin` allow-list |
| `DB_CONNECT_TIMEOUT_SECONDS` / `DB_COMMAND_TIMEOUT_SECONDS` | Backend | `10` / `30` | Bounded database waits |
| `HEALTH_READY_TIMEOUT_SECONDS` | Backend | `2` | `SELECT 1` bound for `/health/ready` |
| `ENABLE_API_DOCS` | Backend | (unset) | Unset follows `APP_ENV` |
| `CARDDEMO_EBCDIC_DIR` / `CARDDEMO_ASCII_DIR` | Seed, batch | `data/EBCDIC` / `data/ASCII` | Seed image locations |
| `CARDDEMO_OUTPUT_DIR` | Batch | `var/batch-output` | Archive generations and statements |
| `NEXT_PUBLIC_API_URL` | Frontend | `http://127.0.0.1:8000` | Backend origin only; inlined at build |
| `CARDDEMO_E2E_*`, `CARDDEMO_BATCH_BIN` | End-to-end | blank credentials | Read from the uncommitted `frontend/.env.e2e` |

## F. Developer Tools Guide

- **API docs.** In non-production mode, browse `http://127.0.0.1:8000/docs`, `/redoc` and `/openapi.json`.
- **Database shell.** `PGPASSWORD=carddemo psql -h 127.0.0.1 -U carddemo -d carddemo`.
- **Migrations.** From `backend/`, run `alembic history` or `alembic current`. Every revision docstring names the copybooks and VSAM clusters it replaces.
- **Traceability.** `docs/traceability.md` maps every COBOL program, BMS map and JCL step to its module, page or job. The code-standards gate checks it against the tree.
- **Screens.** Each `page.tsx` opens with a header naming its BMS map and program. `useFunctionKeys` binds F3, F4, F5, F7, F8 and F12.

## G. Glossary

| Term | Meaning |
|---|---|
| BMS map | A CICS 3270 screen definition; each one becomes one Next.js page |
| COMMAREA | CICS state passed between screens; replaced by the session, `card_ref` and `screen_token` |
| `card_ref` | Fernet-encrypted opaque card key used in URLs and API bodies instead of the PAN |
| GDG | Generation Data Group: versioned batch archives, kept here as cataloged files with retention `LIMIT 5` |
| VSAM KSDS / AIX | Keyed mainframe file and its alternate index; replaced by PostgreSQL tables and indexes |
| `StoreInto` | Port of a COBOL MOVE/COMPUTE into a PIC field: truncates high-order and fractional digits, never rounds |
| Batch window | Advisory lock that fences online writes while a batch job runs |
| Legacy oracle | Independent re-derivation of expected batch outputs from the COBOL rules, used for reconciliation |
