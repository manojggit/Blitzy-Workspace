# CardDemo REST API reference

This reference describes every endpoint of the CardDemo backend: the 28 endpoints under `/api/v1`
that replace the 17 base CICS transactions, and the two health probes. For each endpoint it gives
the legacy transaction, program and BMS map it replaces, the role it requires, its parameters, a
request sample, a response sample and its other outcomes with their exact message literals.

The backend reproduces the legacy programs' edits and messages. A message in this reference is the
program's literal, character for character: several end in `...`, some hold two consecutive spaces,
and one ends in a space. Copy them exactly when you match on them.

Related documents:

- [README.md](README.md): install, configuration and run instructions.
- [architecture.md](architecture.md): layers, the error contract, identity and trust, and the
    CAUP and CCUP screen state machine.
- [batch.md](batch.md): the batch chain and the batch window that closes files to online access.
- [data-model.md](data-model.md): the tables and the COBOL-to-Python type mapping behind the
    response fields.
- [traceability.md](traceability.md): each legacy artefact and its target path.

## Conventions

### Base URL and paths

The local base URL is `http://127.0.0.1:8000`. Every endpoint is served under the prefix `/api/v1`,
except the health probes `/health` and `/health/ready`.

Always use the host `127.0.0.1`, never `localhost`. A browser treats `localhost` and `127.0.0.1` as
different sites, so a session cookie set for one is not sent to the other, and `localhost` may
resolve to the IPv6 address `::1` first while the servers listen on IPv4.

### Names and formats

- **JSON bodies** use snake_case keys, such as `acct_id` and `screen_token`.
- **Path and query parameters** use camelCase names, such as `{acctId}`, `{cardNum}`, `{tranId}`,
    `{userId}`, `startKey`, `direction`, `searchTranId` and `searchUserId`.
- **Model names** are the backend's PascalCase Pydantic class names, such as
    `AccountUpdateRequest`. The generated OpenAPI document uses the same names.
- **Screen fields** are always JSON strings, sent exactly as a user would key them. The server runs
    the legacy edits on that raw text, so `"$1,234.567"` and `""` are valid requests that the edits
    judge (see [Input limits](#input-limits)).
- **Money** in responses is a JSON string holding a decimal with two places, such as `"40.00"`.
    No amount passes through a floating-point number.
- **Text from the database** is right-trimmed. Codes keep their legacy form, such as `"Y"`, `"01"`
    and zero-padded identifiers.

### Samples and placeholders

Samples use `curl` with a cookie jar: the login sample stores the session cookie with
`-c var/session.cookies`, and every later sample sends it with `-b var/session.cookies`. The jar
holds a live token, so it sits in the git-ignored `var/` directory, and the login sample's
`umask 077` subshell gives it mode `600`. Write samples also send the `Origin` header that the
[CSRF guard](#csrf-origin-guard-and-cors) requires. For example, this command fetches one account:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/accounts/ACCOUNT_ID
```

Replace the following placeholders wherever they appear in a sample:

- `USER_ID`: a user ID of up to 8 characters.
- `PASSWORD`: that user's password, of up to 8 characters.
- `ACCOUNT_ID`: an 11-digit account number, such as `00000000004`.
- `CARD_NUMBER`: a 16-digit card number.
- `CARD_REF`: the opaque `card_ref` token that a card list, card view or card selection returned.
- `TRAN_ID`: a 16-digit transaction ID.
- `SCREEN_TOKEN`: the opaque `screen_token` that the previous account or card update response
    returned.
- `START_KEY`: the `first_key` or `last_key` value of the list page shown.
- `SESSION_TOKEN`: the value of the session cookie, which the server issues. You never type it.

Response samples show the values the sample data produces. Opaque tokens in responses are shown as
the placeholders above. Where a sample shortens a list, the sentence that introduces it says so.

## Session cookie

`POST /api/v1/auth/login` is the only way to start a session. A successful sign-on sets an HttpOnly
cookie, and the response body never carries a token. Every other `/api/v1` endpoint except
`POST /api/v1/auth/logout` is protected: it reads the user from the cookie. Logout is public: it
reads no identity and needs no session, so repeating it is harmless, but as a `POST` it still needs
the `Origin` header that the [CSRF guard](#csrf-origin-guard-and-cors) requires.

| Property | Value |
|---|---|
| Name | `carddemo_session` (setting `SESSION_COOKIE_NAME`) |
| Value | An HS256 JWT signed with `SECRET_KEY` |
| Claims | `sub` (user ID), `typ` (user type), `iat` and `exp` |
| `HttpOnly` | Always set, so page scripts cannot read the cookie |
| `SameSite` | `lax` |
| `Path` | `/` |
| `Max-Age` | `ACCESS_TOKEN_EXPIRE_MINUTES` × 60 seconds; the local default is 60 minutes, `3600` |
| `Secure` | Set only when `APP_ENV` is `production` |

A sign-on with the default settings returns these cookie attributes in its `Set-Cookie` header,
joined by `; `:

```text
carddemo_session=SESSION_TOKEN
HttpOnly
Max-Age=3600
Path=/
SameSite=lax
```

Session rules:

- A protected endpoint refuses a missing, expired or invalid cookie with status 401 and an empty
    `message`.
- The user type in the token holds for the token's lifetime. A change to the user record, or its
    deletion, takes effect at that user's next sign-on.
- `POST /api/v1/auth/logout` clears the cookie: it sends the cookie again with an empty value and
    `Max-Age=0`.
- The CR00 CSV and PDF downloads are authenticated by the same cookie. The browser sends it on the
    download because the frontend and the API share the host `127.0.0.1`.

## CSRF Origin guard and CORS

Every `POST`, `PUT` and `DELETE` request must carry an `Origin` header that exactly equals one entry
of `BACKEND_CORS_ORIGINS`. The local default is `http://127.0.0.1:3000`, the frontend origin.
Matching is exact, so `http://localhost:3000` is a different origin. A write with a missing or
unlisted `Origin` is refused with status 403 before it reaches the endpoint, with this body:

```json
{
    "message": "",
    "info_message": "",
    "field_errors": [],
    "cursor_field": ""
}
```

`POST /api/v1/auth/login` is the only exemption, because no session exists before it. `GET`
requests and CORS preflight `OPTIONS` requests are never guarded. `POST /api/v1/auth/logout` is
guarded, so it too needs the `Origin` header.

CORS allows the `BACKEND_CORS_ORIGINS` origins with credentials, the methods `GET`, `POST`, `PUT`,
`DELETE` and `OPTIONS`, and the request headers `Content-Type` and `Accept`. It exposes the
`Content-Disposition` header, which names the report downloads. Every response to an allow-listed
`Origin` carries `Access-Control-Allow-Origin`, an unexpected 500 included, so the browser can
read it. The guard's 403 never does, because the guard refuses only an unlisted or missing
`Origin`.

## Roles

The role of a request comes only from the session cookie. No body field, query parameter or header
is read as a role.

- **Admin**: the stored user type is exactly `A`.
- **Regular**: every other user type, including `U`, lower-case `a` and `u`, and any other
    character that the user-add screen accepted.

| Role | Endpoints |
|---|---|
| Public | `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `/health`, `/health/ready` |
| Any signed-on user | `GET /api/v1/auth/me` |
| Admin | CA00 and CU00–CU03: the `/api/v1/menu/admin` and `/api/v1/admin/users` paths |
| Regular | CM00, CAVW, CAUP, CCLI, CCDL, CCUP, CT00, CT01, CT02, CR00 and CB00 |

A signed-on user calling an endpoint of the other role is refused with status 403:

- A regular user on an admin endpoint gets the message `No access - Admin Only option... `. The
    literal ends in a space.
- An admin on a regular endpoint gets an empty `message`.

The frontend answers both by returning to the user's own menu. There is no account ownership check:
any regular user may view or update any account, as in the legacy programs.

## Sensitive fields

- **Card numbers** are always masked in responses as twelve asterisks and the last four digits,
    such as `************4312`. The list and update screens identify a card by the opaque
    `card_ref` token instead, so a full card number never enters a frontend URL.
- **SSNs** are masked as `***-**-` and the last four digits (`ssn_masked`), except in
    `GET /api/v1/accounts/{acctId}/edit` and `PUT /api/v1/accounts/{acctId}`, whose screen edits the
    SSN in three parts.
- **The CVV** appears in no request or response.
- **Passwords and password hashes** appear in no response. Passwords are stored as bcrypt hashes.

## Input limits

Each screen field is a string limited only by two structural rules:

- its length is at most the BMS field `LENGTH`, which this reference gives for each field;
- every character is in the code-page-037 printable repertoire that a 3270 terminal can send.

A request that breaks either rule, or that is otherwise malformed, is refused with status 422
before any legacy edit runs. Malformed means invalid JSON, a wrong JSON type, an enum value outside
its list (such as `action`, `direction`, `reportType`, `format` or `screen`), a page number that is
not unsigned decimal digits, or a list or token over its maximum length.

All other validation, including type, sign, range and format checks, is the legacy program's edit,
and it answers with the program's own message.

The 422 body carries FastAPI's `detail` list beside the four standard keys. Each `detail` item keeps
only `loc`, `msg` and `type`, so the rejected value, which may be a password, card number or SSN,
is never echoed back. `field_errors` names the failing fields: the snake_case key of a body field;
for the key parameters `acctId`, `cardNum`, `tranId`, `userId`, `searchTranId` and `searchUserId`,
the matching screen field (`acct_id`, `card_num`, `tran_id`, `user_id`, `search_tran_id`,
`search_user_id`); for any other parameter, its wire name. A sign-on with a 13-character user ID is
refused with this body:

```json
{
    "detail": [
        {
            "loc": ["body", "user_id"],
            "msg": "String should have at most 8 characters",
            "type": "string_too_long"
        }
    ],
    "message": "",
    "info_message": "",
    "field_errors": ["user_id"],
    "cursor_field": "user_id"
}
```

## Error contract

Every refusal a legacy program makes is a non-2xx response whose body carries four keys:

| Key | Meaning |
|---|---|
| `message` | The program's error-line literal, verbatim; empty when the program sets none |
| `info_message` | The information line of CAVW, CAUP, CCLI, CCDL and CCUP; otherwise empty |
| `field_errors` | The snake_case names of the failing fields |
| `cursor_field` | The field that receives focus, replacing the BMS cursor; empty for none |

How many fields `field_errors` holds depends on the program's edit mode:

- **All-field mode** (CAVW, CAUP, CCLI, CCDL and CCUP): every edit runs. `message` is the first
    message an edit set, `field_errors` lists every failing field, and `cursor_field` is the first
    of them.
- **First-failure mode** (every other program): the first failing edit ends validation, so
    `field_errors` holds only the failing field.

For example, CT02 refuses an amount keyed as `50` with status 400 and this body:

```json
{
    "message": "Amount should be in format -99999999.99",
    "info_message": "",
    "field_errors": ["amount"],
    "cursor_field": "amount"
}
```

Some error bodies add keys beside the four:

- The CAUP and CCUP update screens add the screen's `fields`, `state` and `screen_token`, so the
    page can redisplay what was keyed. `AccountScreenErrorResponse` and `CardScreenErrorResponse`
    declare them on the 400, 404 and 503 of each `/edit` fetch and on the 400, 404, 409, 423 and
    503 of each `PUT`. Unlike a `CardScreen`, a CCUP error body carries no `card_ref`.
- The CCLI selection refusals add the redisplayed `rows`, `first_key` and `last_key`, and on
    `GET /api/v1/cards` only, `page`. `CardListErrorResponse` declares the 400 of that route, and
    `CardSelectionErrorResponse` the 400 of `POST /api/v1/cards/selection`.
- The CT00 invalid-selection refusal adds the browsed `rows`, `page`, `first_key`, `last_key` and
    `search_cleared` (`TransactionSelectionErrorResponse`).
- The CU00 invalid-selection refusal adds the browsed `rows`, `page`, `first_key` and `last_key`
    (`UserSelectionErrorResponse`).
- The 422 body adds `detail` ([Input limits](#input-limits)).

These named models extend `LegacyErrorResponse`, the model of the four keys, and every key they
add is optional: a CCLI filter-edit refusal and the CT00 non-numeric search refusal carry the four
keys alone. Every 401 and 403 is declared as `LegacyErrorResponse`.

| Status | Used for |
|---|---|
| 400 | Field-edit failures, and the no-change literal of CAUP, CCUP and CU02 |
| 401 | Sign-on failures; a missing, expired or invalid session cookie (empty `message`) |
| 403 | Role refusals; a write without an allow-listed `Origin` header (empty `message`) |
| 404 | Not-found literals |
| 409 | `Record changed by some one else. Please review`, and the duplicate-key literals |
| 422 | Structurally invalid requests ([Input limits](#input-limits)) |
| 423 | Lock literals, such as `Could not lock account record for update` |
| 429 | The sign-on lockout: `Too many failed attempts. Try again later ...` |
| 503 | `WHEN OTHER` file-error literals, including those returned while the batch window is held |
| 500 | An unexpected error; the frontend shows the legacy abend text `UNEXPECTED ABEND OCCURRED.` |

The duplicate-key literals are `Tran ID already exist...` (CT02, CB00) and
`User ID already exist...` (CU01). No catch-all handler is registered, so a 500 is FastAPI's
default response.

A success body carries `message` with the program's success literal, or an empty `message` where
the program shows none.

### Batch window

While a `carddemo-batch` command runs, it holds the batch window, which replaces the legacy
CLOSEFIL and OPENFIL jobs ([Batch window](batch.md#batch-window)). Requests that touch a closed
file are refused with the literal of that program's `WHEN OTHER` branch. Requests that touch no
closed file, such as CM00, CA00, CCLI, CCDL, CCUP and CR00, are unaffected.

| Transaction | Status | Literal while the window is held |
|---|---|---|
| CC00 | 503 | `Unable to verify the User ...` |
| CAVW, CAUP fetch | 503 | The CXACAIX file-error text below |
| CAUP save | 423 | `Could not lock account record for update` |
| CT00 | 503 | `Unable to lookup transaction...` |
| CT01 | 503 | `Unable to lookup Transaction...` |
| CT02 | 503 | `Unable to lookup Acct in XREF AIX file...` (account entered) |
| CT02 | 503 | `Unable to lookup Card # in XREF file...` (card entered) |
| CB00 | 503 | `Unable to lookup Account...` |
| CU00, CU02 and CU03 fetch | 503 | `Unable to lookup User...` |
| CU01 | 503 | `Unable to Add User...` |
| CU02 update, CU03 delete | 503 | `Unable to Update User...` |

The CAVW and CAUP fetch literal is this text:

```text
File Error: READ     on CXACAIX   returned RESP 000000019 ,RESP2 000000060
```

## API documentation endpoints

The backend serves its generated OpenAPI document and two viewers:

| Path | Content |
|---|---|
| `/openapi.json` | The OpenAPI document, the target of the schemathesis contract tests |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |

They are served when `ENABLE_API_DOCS` is true, or when it is unset and `APP_ENV` is not
`production`. Otherwise each path returns 404, so a production deployment discloses no API
surface by default. The document declares every status each operation can return, with the
`LegacyErrorResponse` and `LegacyValidationErrorResponse` error models. Some error statuses of
CAUP, CCLI, CCUP, CT00 and CU00 instead reference an endpoint-specific model that extends
`LegacyErrorResponse` ([Error contract](#error-contract)).

## Endpoint index

| Transaction | Method and path | Role |
|---|---|---|
| — | [GET /health](#get-health) | Public |
| — | [GET /health/ready](#get-healthready) | Public |
| CC00 | [POST /api/v1/auth/login](#post-apiv1authlogin) | Public |
| CC00 | [POST /api/v1/auth/logout](#post-apiv1authlogout) | Public |
| CC00 | [GET /api/v1/auth/me](#get-apiv1authme) | Any |
| CM00 | [GET /api/v1/menu/main](#get-apiv1menumain) | Regular |
| CM00 | [POST /api/v1/menu/main/selection](#post-apiv1menumainselection) | Regular |
| CA00 | [GET /api/v1/menu/admin](#get-apiv1menuadmin) | Admin |
| CA00 | [POST /api/v1/menu/admin/selection](#post-apiv1menuadminselection) | Admin |
| CAVW | [GET /api/v1/accounts/{acctId}](#get-apiv1accountsacctid) | Regular |
| CAUP | [GET /api/v1/accounts/{acctId}/edit](#get-apiv1accountsacctidedit) | Regular |
| CAUP | [PUT /api/v1/accounts/{acctId}](#put-apiv1accountsacctid) | Regular |
| CCLI | [GET /api/v1/cards](#get-apiv1cards) | Regular |
| CCLI | [POST /api/v1/cards/selection](#post-apiv1cardsselection) | Regular |
| CCDL | [GET /api/v1/cards/{cardNum}](#get-apiv1cardscardnum) | Regular |
| CCUP | [GET /api/v1/cards/{cardNum}/edit](#get-apiv1cardscardnumedit) | Regular |
| CCUP | [PUT /api/v1/cards/{cardNum}](#put-apiv1cardscardnum) | Regular |
| CT00 | [GET /api/v1/transactions](#get-apiv1transactions) | Regular |
| CT00 | [POST /api/v1/transactions/selection](#post-apiv1transactionsselection) | Regular |
| CT01 | [GET /api/v1/transactions/{tranId}](#get-apiv1transactionstranid) | Regular |
| CT02 | [POST /api/v1/transactions](#post-apiv1transactions) | Regular |
| CR00 | [GET /api/v1/reports/transactions](#get-apiv1reportstransactions) | Regular |
| CR00 | [GET /api/v1/reports/transactions/export](#get-apiv1reportstransactionsexport) | Regular |
| CB00 | [POST /api/v1/billpay](#post-apiv1billpay) | Regular |
| CU00 | [GET /api/v1/admin/users](#get-apiv1adminusers) | Admin |
| CU00 | [POST /api/v1/admin/users/selection](#post-apiv1adminusersselection) | Admin |
| CU01 | [POST /api/v1/admin/users](#post-apiv1adminusers) | Admin |
| CU02, CU03 | [GET /api/v1/admin/users/{userId}](#get-apiv1adminusersuserid) | Admin |
| CU02 | [PUT /api/v1/admin/users/{userId}](#put-apiv1adminusersuserid) | Admin |
| CU03 | [DELETE /api/v1/admin/users/{userId}](#delete-apiv1adminusersuserid) | Admin |

## Health probes

The probes need no session, take no batch-window lock and stay outside `/api/v1`.

### GET /health

Replaces no legacy transaction; role public. Reports that the process is serving requests, without
touching the database.

This command calls the liveness probe:

```bash
curl -s http://127.0.0.1:8000/health
```

The response has status 200 and this body:

```json
{"status": "ok"}
```

### GET /health/ready

Replaces no legacy transaction; role public. Runs `SELECT 1` on the database within
`HEALTH_READY_TIMEOUT_SECONDS`.

This command calls the readiness probe:

```bash
curl -s http://127.0.0.1:8000/health/ready
```

When the database answers in time, the response has status 200 and this body:

```json
{"status": "ready"}
```

When the query fails or times out, the response has status 503 and this body:

```json
{"status": "unavailable"}
```


## Sign-on (CC00)

### POST /api/v1/auth/login

Replaces CC00 / COSGN00C / COSGN00 (ENTER); role public. Signs a user on, sets the session cookie
and returns the user with the menu route to open. This is the only endpoint exempt from the
`Origin` requirement.

| Body field (`LoginRequest`) | Type | Description |
|---|---|---|
| `user_id` | string, at most 8 | The user ID as keyed; upper-cased before the lookup |
| `password` | string, at most 8 | The password as keyed; upper-cased before it is verified |

This command signs on and stores the session cookie in `var/session.cookies`:

```bash
mkdir -p var
(umask 077 && curl -s -c var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"user_id": "USER_ID", "password": "PASSWORD"}')
```

For a regular user, the response has status 200, sets the [session cookie](#session-cookie) and
carries this `LoginResponse` body:

```json
{
    "user_id": "USER0001",
    "user_type": "U",
    "first_name": "LAWRENCE",
    "last_name": "THOMAS",
    "next_route": "/menu"
}
```

`next_route` is `/admin` when the stored user type is exactly `A`, and `/menu` for every other
type. Because both inputs are upper-cased, a password stored with lower-case letters never
matches, as in COSGN00C.

Other outcomes, each with `field_errors` and `cursor_field` naming the field shown:

- 401 `Please enter User ID ...` (`user_id`): the user ID is blank.
- 401 `Please enter Password ...` (`password`): the password is blank.
- 401 `User not found. Try again ...` (`user_id`).
- 401 `Wrong Password. Try again ...` (`password`).
- 429 `Too many failed attempts. Try again later ...` (`user_id`): after `LOGIN_MAX_ATTEMPTS`
    (default 5) failures for the same upper-cased user ID, known or not, sign-on is refused for
    `LOGIN_LOCKOUT_SECONDS` (default 900). A successful sign-on resets the count. A blank user ID
    is never counted or refused with 429.
- 503 `Unable to verify the User ...` (`user_id`): the user file cannot be read, as while the
    batch window is held.

No cookie accompanies an error.

### POST /api/v1/auth/logout

Replaces CC00 / COSGN00C / COSGN00 (F3); role public. Clears the session cookie and returns the
sign-off literal. It needs no session, so repeating it is harmless, but as a `POST` it needs the
`Origin` header.

This command signs off:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/auth/logout \
    -H 'Origin: http://127.0.0.1:3000'
```

The response has status 200, clears the cookie and carries this `LogoutResponse` body:

```json
{"message": "Thank you for using CardDemo application..."}
```

Other outcomes:

- 403 with an empty `message`: the `Origin` header is missing or not allow-listed.

Logout does not revoke the token: the token in the jar stays valid until it expires,
`ACCESS_TOKEN_EXPIRE_MINUTES` after sign-on (60 minutes by default). To delete the jar when you
finish:

```bash
rm -f var/session.cookies
```

### GET /api/v1/auth/me

Replaces the COMMAREA user fields of CC00 / COSGN00C; role any signed-on user. Returns the user of
the session, which the frontend uses to choose the menu and fill the screen header.

This command reads the current user:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/auth/me
```

The response has status 200 and carries this `CurrentUser` body:

```json
{
    "user_id": "USER0001",
    "user_type": "U",
    "first_name": "LAWRENCE",
    "last_name": "THOMAS",
    "next_route": "/menu"
}
```

`user_id` and `user_type` come from the session token. The names are read from the user record,
and are empty when it cannot be read.

Other outcomes:

- 401 with an empty `message`: no valid session cookie.

## Menus (CM00, CA00)

The two menus share their models. A menu lists every option of its legacy table, including the
options whose programs belong to excluded modules: main-menu option 11 and admin options 5 and 6.
Selecting one of those answers the legacy "not installed" message instead of a route.

### GET /api/v1/menu/main

Replaces CM00 / COMEN01C / COMEN01; role regular. Lists the 11 options of `cpy/COMEN02Y.cpy` in
table order.

This command lists the main menu:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/menu/main
```

The response has status 200 and carries this `MenuResponse` body:

```json
{
    "options": [
        {"number": 1, "label": "Account View"},
        {"number": 2, "label": "Account Update"},
        {"number": 3, "label": "Credit Card List"},
        {"number": 4, "label": "Credit Card View"},
        {"number": 5, "label": "Credit Card Update"},
        {"number": 6, "label": "Transaction List"},
        {"number": 7, "label": "Transaction View"},
        {"number": 8, "label": "Transaction Add"},
        {"number": 9, "label": "Transaction Reports"},
        {"number": 10, "label": "Bill Payment"},
        {"number": 11, "label": "Pending Authorization View"}
    ]
}
```

Other outcomes:

- 401 with an empty `message`: no valid session cookie.
- 403 with an empty `message`: the user is an admin.

### POST /api/v1/menu/main/selection

Replaces CM00 / COMEN01C / COMEN01 (ENTER on the OPTION field); role regular. Runs the option edit
on the keyed text and returns the route of the chosen option, replacing the program's `XCTL`.

| Body field (`MenuSelectionRequest`) | Type | Description |
|---|---|---|
| `option` | string, at most 2 | The option number as keyed |

This command selects option 1:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/menu/main/selection \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"option": "1"}'
```

The response has status 200 and carries this `MenuSelectionResponse` body:

```json
{"route": "/accounts/view", "message": ""}
```

The routes of options 1 to 10 are `/accounts/view`, `/accounts/update`, `/cards`, `/cards/view`,
`/cards/update`, `/transactions`, `/transactions/view`, `/transactions/add`,
`/reports/transactions` and `/billpay`.

Other outcomes:

- 200 with `route` null: option 11, whose program COPAUS0C is in the excluded authorization
    module. `message` is `This option Pending Authorization View is not installed...`, the
    option name followed by the not-installed text.
- 400 `Please enter a valid option number...` (`option`): a blank, non-numeric, zero or
    out-of-range option.
- 401 or 403 as for [`GET /api/v1/menu/main`](#get-apiv1menumain).

The COMEN01C coming-soon builder, `This option ` followed by the option name's first word and
`is coming soon ...`, applies only to `DUMMY` table entries. No active COMEN02Y entry is one, so it
is never returned.

### GET /api/v1/menu/admin

Replaces CA00 / COADM01C / COADM01; role admin. Lists the 6 options of `cpy/COADM02Y.cpy` in table
order.

This command lists the admin menu:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/menu/admin
```

The response has status 200 and carries this `MenuResponse` body:

```json
{
    "options": [
        {"number": 1, "label": "User List (Security)"},
        {"number": 2, "label": "User Add (Security)"},
        {"number": 3, "label": "User Update (Security)"},
        {"number": 4, "label": "User Delete (Security)"},
        {"number": 5, "label": "Transaction Type List/Update (Db2)"},
        {"number": 6, "label": "Transaction Type Maintenance (Db2)"}
    ]
}
```

Other outcomes:

- 401 with an empty `message`: no valid session cookie.
- 403 `No access - Admin Only option... `: the user is not an admin. The literal ends in a space.

### POST /api/v1/menu/admin/selection

Replaces CA00 / COADM01C / COADM01 (ENTER on the OPTION field); role admin. Takes the same
`MenuSelectionRequest` as the main menu and answers a `MenuSelectionResponse`.

This command selects option 1:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/menu/admin/selection \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"option": "1"}'
```

The response has status 200 and this body:

```json
{"route": "/admin/users", "message": ""}
```

The routes of options 1 to 4 are `/admin/users`, `/admin/users/add`, `/admin/users/update` and
`/admin/users/delete`.

Other outcomes:

- 200 with `route` null and `message` `This option is not installed ...`: options 5 and 6, whose
    programs COTRTLIC and COTRTUPC are in the excluded Db2 module. COADM01C builds this literal
    without the option name, because the name is commented out of its `STRING` statement.
- 400 `Please enter a valid option number...` (`option`).
- 401 or 403 as for [`GET /api/v1/menu/admin`](#get-apiv1menuadmin).


## Accounts (CAVW, CAUP)

The account number arrives in the path exactly as keyed, at most 11 characters. The services edit
it: an account number must be 11 digits and not all zeros. A blank key, sent as `%20`, reaches the
edit as well.

### GET /api/v1/accounts/{acctId}

Replaces CAVW / COACTVWC / COACTVW; role regular. Edits the account number, reads the card
cross-reference, the account and the customer, and returns them for display with the SSN masked.

| Parameter | In | Type | Description |
|---|---|---|---|
| `acctId` | path | string, at most 11 | The account number as keyed |

This command views an account:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/accounts/ACCOUNT_ID
```

For account `00000000004`, the response has status 200 and carries this `AccountViewResponse`
body:

```json
{
    "message": "",
    "info_message": "Enter or update id of account to display",
    "field_errors": [],
    "cursor_field": "acct_id",
    "acct_id": "00000000004",
    "active_status": "Y",
    "open_date": "2012-11-17",
    "credit_limit": "3503.00",
    "expiration_date": "2023-12-16",
    "cash_credit_limit": "2789.00",
    "reissue_date": "2023-12-16",
    "curr_balance": "40.00",
    "curr_cycle_credit": "0.00",
    "group_id": "",
    "curr_cycle_debit": "0.00",
    "cust_id": "000000004",
    "ssn_masked": "***-**-4258",
    "date_of_birth": "1985-01-13",
    "fico_credit_score": 776,
    "first_name": "Delbert",
    "middle_name": "Kaia",
    "last_name": "Parisian",
    "addr_line_1": "638 Blanda Gateway",
    "addr_state_cd": "MI",
    "addr_line_2": "Apt. 076",
    "addr_zip": "39035-0455",
    "addr_line_3": "Lake Virginie",
    "addr_country_cd": "USA",
    "phone_num_1": "(801)603-4121",
    "govt_issued_id": "00000000000068579249",
    "phone_num_2": "(156)074-6837",
    "eft_account_id": "0040802739",
    "primary_holder_ind": "Y"
}
```

Dates are ISO text, or empty when the stored date is blank. Every error body also carries
`info_message` `Enter or update id of account to display`. Other outcomes, each naming `acct_id`:

- 400 `No input received`: the key is blank.
- 400 `Account Filter must  be a non-zero 11 digit number`: the key is not 11 digits, or is all
    zeros. The literal holds two spaces after `must`.
- 404, when the account has no cross-reference:

    ```text
    Account:00000000999 not found in Cross ref file.  Resp:000000013  Reas:0000
    ```

- 404, when the account record is missing:

    ```text
    Account:00000000999 not found in Acct Master file.Resp:000000013  Reas:0000
    ```

- 404, when the customer record is missing, naming the customer ID:

    ```text
    CustId:000000099 not found in customer master.Resp: 000000013  REAS:0000000
    ```

- 503 with the CXACAIX file-error text: the cross-reference cannot be read, as while the
    [batch window](#batch-window) is held.

The not-found texts are cut at 75 characters, the width of the program's message field, which is
why their reason codes are shortened. COACTVWC sets `Account number not provided` for a blank key
but then overwrites it with `No input received`, so the first literal is never returned.

### GET /api/v1/accounts/{acctId}/edit

Replaces CAUP / COACTUPC / COACTUP (ENTER with an account number); role regular. Edits the account
number and fetches the account and the customer into the update screen, in state `SHOW_DETAILS`.
The SSN is not masked, because this screen edits it.

| Parameter | In | Type | Description |
|---|---|---|---|
| `acctId` | path | string, at most 11 | The account number as keyed |

This command fetches an account for update:

```bash
curl -s -b var/session.cookies \
    http://127.0.0.1:8000/api/v1/accounts/ACCOUNT_ID/edit
```

The response has status 200 and carries an `AccountScreen` body. The update samples in this
section use account `00000000004` after its customer's state, ZIP code and phone numbers were
corrected to values that pass every COACTUPC edit. As shipped, every sample account fails at least
one edit, such as the FICO score range or a North American area code, so any change to it is
answered 400, as in the legacy program:

```json
{
    "message": "",
    "info_message": "Update account details presented above.",
    "field_errors": [],
    "cursor_field": "acct_id",
    "fields": {
        "acct_id": "00000000004",
        "active_status": "Y",
        "open_year": "2012",
        "open_month": "11",
        "open_day": "17",
        "credit_limit": "+      3,503.00",
        "expiration_year": "2023",
        "expiration_month": "12",
        "expiration_day": "16",
        "cash_credit_limit": "+      2,789.00",
        "reissue_year": "2023",
        "reissue_month": "12",
        "reissue_day": "16",
        "curr_balance": "+         40.00",
        "curr_cycle_credit": "+           .00",
        "group_id": "",
        "curr_cycle_debit": "+           .00",
        "cust_id": "000000004",
        "ssn_part_1": "660",
        "ssn_part_2": "35",
        "ssn_part_3": "4258",
        "dob_year": "1985",
        "dob_month": "01",
        "dob_day": "13",
        "fico_credit_score": "776",
        "first_name": "Delbert",
        "middle_name": "Kaia",
        "last_name": "Parisian",
        "addr_line_1": "638 Blanda Gateway",
        "addr_state_cd": "NY",
        "addr_line_2": "Apt. 076",
        "addr_zip": "10001",
        "addr_line_3": "Lake Virginie",
        "addr_country_cd": "USA",
        "phone_num_1_area": "212",
        "phone_num_1_prefix": "555",
        "phone_num_1_line": "0100",
        "govt_issued_id": "00000000000068579249",
        "phone_num_2_area": "212",
        "phone_num_2_prefix": "555",
        "phone_num_2_line": "0101",
        "eft_account_id": "0040802739",
        "primary_holder_ind": "Y"
    },
    "state": "SHOW_DETAILS",
    "screen_token": "SCREEN_TOKEN"
}
```

`fields` is the COACTUP map as the screen shows it: amounts in the map's edited picture, dates,
the SSN and the phone numbers in parts. Each field's maximum length is its BMS length:

| Fields (`AccountScreenFields`) | Length |
|---|---|
| `acct_id` | 11 |
| `active_status`, `primary_holder_ind` | 1 |
| `open_year`, `expiration_year`, `reissue_year`, `dob_year` | 4 |
| `open_month`, `open_day`, `expiration_month`, `expiration_day` | 2 |
| `reissue_month`, `reissue_day`, `dob_month`, `dob_day`, `addr_state_cd` | 2 |
| `credit_limit`, `cash_credit_limit`, `curr_balance` | 15 |
| `curr_cycle_credit`, `curr_cycle_debit` | 15 |
| `group_id`, `eft_account_id` | 10 |
| `cust_id` | 9 |
| `ssn_part_1`, `ssn_part_2`, `ssn_part_3` | 3, 2, 4 |
| `fico_credit_score`, `addr_country_cd` | 3 |
| `first_name`, `middle_name`, `last_name` | 25 |
| `addr_line_1`, `addr_line_2`, `addr_line_3` | 50 |
| `addr_zip` | 5 |
| `phone_num_1_area`, `phone_num_1_prefix`, `phone_num_1_line` (also `phone_num_2_*`) | 3, 3, 4 |
| `govt_issued_id` | 20 |

`cust_id` and `addr_country_cd` are display-only in every state
([Screen state machine](#screen-state-machine)).

Other outcomes, each an `AccountScreenErrorResponse` with `acct_id` named and, unless stated,
the key screen in state `DETAILS_NOT_FETCHED` with `info_message`
`Enter or update id of account to update`:

- 400 `No input received`: the key is blank.
- 400 `Account Number if supplied must be a 11 digit Non-Zero Number`.
- 404 with the three not-found texts of [`GET /api/v1/accounts/{acctId}`](#get-apiv1accountsacctid).
- 503 with the CXACAIX, ACCTDAT or CUSTDAT file-error text.
- When the account record is missing or its read fails, but the customer is read, the 404
    not-found text or the 503 ACCTDAT file-error text comes in state `SHOW_DETAILS`, with
    `info_message` `Update account details presented above.`, as in the legacy program. The
    customer fields are shown; the account fields other than `acct_id` are blank, and each
    amount reads `+           .00`. The `screen_token` holds these values, so ENTER with them
    unchanged answers 400 `No change detected with respect to values fetched.`

### PUT /api/v1/accounts/{acctId}

Replaces CAUP / COACTUPC / COACTUP (ENTER, F5 and F12); role regular. Runs one step of the update
screen's state machine on the state that `screen_token` carries, and returns the next screen.

| Parameter | In | Type | Description |
|---|---|---|---|
| `acctId` | path | string, at most 11 | The account number of the screen |

| Body field (`AccountUpdateRequest`) | Type | Description |
|---|---|---|
| `action` | `enter`, `save` or `cancel`; default `enter` | ENTER, F5 or F12 |
| `screen_token` | string, at most 16384 | The previous response's `screen_token`; empty at first |
| `fields` | `AccountScreenFields` | The screen's fields as keyed |

The response is an `AccountScreen`: the four standard keys, `fields`, `state` and a new
`screen_token`. Error bodies (`AccountScreenErrorResponse`) carry the same keys.

#### Screen state machine

`screen_token` replaces the program's COMMAREA. It is an encrypted token holding the state, the
account key, the values fetched at read time and, in `CHANGES_OK_NOT_CONFIRMED`, the validated new
values. It expires after `ACCESS_TOKEN_EXPIRE_MINUTES`. An empty, invalid or expired token, or one
issued for another account than `acctId`, is treated as `DETAILS_NOT_FETCHED`.

The server reads from `fields` only the fields that the state leaves editable. Every other value
comes from the token, as a protected 3270 field returns only what was sent to it. `save` is valid
only in `CHANGES_OK_NOT_CONFIRMED`, and `cancel` only once details are fetched. Any other use of
them is processed as `enter`.

| State before the request | Editable fields | `enter` | `save` | `cancel` |
|---|---|---|---|---|
| `DETAILS_NOT_FETCHED` | `acct_id` | Fetch | As `enter` | As `enter` |
| `SHOW_DETAILS`, `CHANGES_NOT_OK` | Most details, see below | Validate | As `enter` | Re-read |
| `CHANGES_OK_NOT_CONFIRMED` | None | Keep | Write | Re-read |
| `CHANGES_DONE`, `CHANGES_FAILED` | The key, when failed | Key screen | As `enter` | As `enter` |

In `SHOW_DETAILS` and `CHANGES_NOT_OK`, every detail field is editable except `cust_id` and
`addr_country_cd`, which stay protected like the key `acct_id`: their values always come from
`screen_token`, and replacements sent in `fields` are ignored.

The actions in the table do this:

- **Fetch** edits the key only and reads the record, giving `SHOW_DETAILS`. Detail fields are
    neither read nor edited.
- **Validate** is described below.
- **Keep** runs no edit and keeps the state and the token's validated values.
- **Write** writes the validated values held in the token, whatever `fields` carries.
- **Re-read** discards the edits and reads the record again, giving `SHOW_DETAILS`.
- **Key screen** discards the input and returns the empty key screen in `DETAILS_NOT_FETCHED`.

Validation compares the keyed fields with the values fetched first. With no change, the answer is
400 `No change detected with respect to values fetched.`, the state is kept and the cursor is on
`active_status`. Otherwise every detail edit runs. Any failure gives 400 and `CHANGES_NOT_OK`;
a pass gives 200, `CHANGES_OK_NOT_CONFIRMED` and the information line
`Changes validated.Press F5 to save`. Validation never writes.

The information line of each state:

| State | `info_message` |
|---|---|
| `DETAILS_NOT_FETCHED` | `Enter or update id of account to update` |
| `SHOW_DETAILS`, `CHANGES_NOT_OK` | `Update account details presented above.` |
| `CHANGES_OK_NOT_CONFIRMED` | `Changes validated.Press F5 to save` |
| `CHANGES_DONE` | `Changes committed to database` |
| `CHANGES_FAILED` | `Changes unsuccessful. Please try again` |

#### Validate a change

To validate a change, write the fetched `fields` to `enter.json` with the new value keyed, and
the fetched `screen_token`. This file changes the credit limit:

```json
{
    "action": "enter",
    "screen_token": "SCREEN_TOKEN",
    "fields": {
        "acct_id": "00000000004",
        "active_status": "Y",
        "open_year": "2012",
        "open_month": "11",
        "open_day": "17",
        "credit_limit": "5000.00",
        "expiration_year": "2023",
        "expiration_month": "12",
        "expiration_day": "16",
        "cash_credit_limit": "+      2,789.00",
        "reissue_year": "2023",
        "reissue_month": "12",
        "reissue_day": "16",
        "curr_balance": "+         40.00",
        "curr_cycle_credit": "+           .00",
        "group_id": "",
        "curr_cycle_debit": "+           .00",
        "cust_id": "000000004",
        "ssn_part_1": "660",
        "ssn_part_2": "35",
        "ssn_part_3": "4258",
        "dob_year": "1985",
        "dob_month": "01",
        "dob_day": "13",
        "fico_credit_score": "776",
        "first_name": "Delbert",
        "middle_name": "Kaia",
        "last_name": "Parisian",
        "addr_line_1": "638 Blanda Gateway",
        "addr_state_cd": "NY",
        "addr_line_2": "Apt. 076",
        "addr_zip": "10001",
        "addr_line_3": "Lake Virginie",
        "addr_country_cd": "USA",
        "phone_num_1_area": "212",
        "phone_num_1_prefix": "555",
        "phone_num_1_line": "0100",
        "govt_issued_id": "00000000000068579249",
        "phone_num_2_area": "212",
        "phone_num_2_prefix": "555",
        "phone_num_2_line": "0101",
        "eft_account_id": "0040802739",
        "primary_holder_ind": "Y"
    }
}
```

This command sends it:

```bash
curl -s -b var/session.cookies -X PUT \
    http://127.0.0.1:8000/api/v1/accounts/ACCOUNT_ID \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d @enter.json
```

The response has status 200 and this body, in state `CHANGES_OK_NOT_CONFIRMED`. Every field is now
protected, and the credit limit is shown in the edited picture:

```json
{
    "message": "",
    "info_message": "Changes validated.Press F5 to save",
    "field_errors": [],
    "cursor_field": "acct_id",
    "fields": {
        "acct_id": "00000000004",
        "active_status": "Y",
        "open_year": "2012",
        "open_month": "11",
        "open_day": "17",
        "credit_limit": "+      5,000.00",
        "expiration_year": "2023",
        "expiration_month": "12",
        "expiration_day": "16",
        "cash_credit_limit": "+      2,789.00",
        "reissue_year": "2023",
        "reissue_month": "12",
        "reissue_day": "16",
        "curr_balance": "+         40.00",
        "curr_cycle_credit": "+           .00",
        "group_id": "",
        "curr_cycle_debit": "+           .00",
        "cust_id": "000000004",
        "ssn_part_1": "660",
        "ssn_part_2": "35",
        "ssn_part_3": "4258",
        "dob_year": "1985",
        "dob_month": "01",
        "dob_day": "13",
        "fico_credit_score": "776",
        "first_name": "Delbert",
        "middle_name": "Kaia",
        "last_name": "Parisian",
        "addr_line_1": "638 Blanda Gateway",
        "addr_state_cd": "NY",
        "addr_line_2": "Apt. 076",
        "addr_zip": "10001",
        "addr_line_3": "Lake Virginie",
        "addr_country_cd": "USA",
        "phone_num_1_area": "212",
        "phone_num_1_prefix": "555",
        "phone_num_1_line": "0100",
        "govt_issued_id": "00000000000068579249",
        "phone_num_2_area": "212",
        "phone_num_2_prefix": "555",
        "phone_num_2_line": "0101",
        "eft_account_id": "0040802739",
        "primary_holder_ind": "Y"
    },
    "state": "CHANGES_OK_NOT_CONFIRMED",
    "screen_token": "SCREEN_TOKEN"
}
```

#### Save the change

To save, send `save` with the `screen_token` of the validated screen. The server writes the values
held in the token, so `fields` may be empty; any values it carries are ignored. This command saves:

```bash
curl -s -b var/session.cookies -X PUT \
    http://127.0.0.1:8000/api/v1/accounts/ACCOUNT_ID \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"action": "save", "screen_token": "SCREEN_TOKEN", "fields": {}}'
```

The response has status 200 and this body, in state `CHANGES_DONE`:

```json
{
    "message": "",
    "info_message": "Changes committed to database",
    "field_errors": [],
    "cursor_field": "acct_id",
    "fields": {
        "acct_id": "00000000004",
        "active_status": "Y",
        "open_year": "2012",
        "open_month": "11",
        "open_day": "17",
        "credit_limit": "+      5,000.00",
        "expiration_year": "2023",
        "expiration_month": "12",
        "expiration_day": "16",
        "cash_credit_limit": "+      2,789.00",
        "reissue_year": "2023",
        "reissue_month": "12",
        "reissue_day": "16",
        "curr_balance": "+         40.00",
        "curr_cycle_credit": "+           .00",
        "group_id": "",
        "curr_cycle_debit": "+           .00",
        "cust_id": "000000004",
        "ssn_part_1": "660",
        "ssn_part_2": "35",
        "ssn_part_3": "4258",
        "dob_year": "1985",
        "dob_month": "01",
        "dob_day": "13",
        "fico_credit_score": "776",
        "first_name": "Delbert",
        "middle_name": "Kaia",
        "last_name": "Parisian",
        "addr_line_1": "638 Blanda Gateway",
        "addr_state_cd": "NY",
        "addr_line_2": "Apt. 076",
        "addr_zip": "10001",
        "addr_line_3": "Lake Virginie",
        "addr_country_cd": "USA",
        "phone_num_1_area": "212",
        "phone_num_1_prefix": "555",
        "phone_num_1_line": "0100",
        "govt_issued_id": "00000000000068579249",
        "phone_num_2_area": "212",
        "phone_num_2_prefix": "555",
        "phone_num_2_line": "0101",
        "eft_account_id": "0040802739",
        "primary_holder_ind": "Y"
    },
    "state": "CHANGES_DONE",
    "screen_token": "SCREEN_TOKEN"
}
```

The next request with this token returns the empty key screen in `DETAILS_NOT_FETCHED`, whatever
its action.

Save outcomes other than success:

- 423 `Could not lock account record for update`, state `CHANGES_FAILED`: another request holds
    the account row, or the batch window is held.
- 423 `Could not lock customer record for update`, state `CHANGES_FAILED`.
- 409 `Record changed by some one else. Please review`, state `SHOW_DETAILS` with the current
    data: the account or customer changed after the fetch.
- 503 `Update of record failed`, state `CHANGES_FAILED`. When the customer rewrite fails after the
    account rewrite, the account change persists, as in the legacy program, whose files are not
    recoverable.

#### Edit failures

When edits fail, the response names every failing field. This sample keys `X` as the account
status and `12x` as the credit limit on the fetched screen. The response has status 400 and this
body, in state `CHANGES_NOT_OK`:

```json
{
    "message": "Account Status must be Y or N.",
    "info_message": "Update account details presented above.",
    "field_errors": ["active_status", "credit_limit"],
    "cursor_field": "active_status",
    "fields": {
        "acct_id": "00000000004",
        "active_status": "X",
        "open_year": "2012",
        "open_month": "11",
        "open_day": "17",
        "credit_limit": "12x",
        "expiration_year": "2023",
        "expiration_month": "12",
        "expiration_day": "16",
        "cash_credit_limit": "+      2,789.00",
        "reissue_year": "2023",
        "reissue_month": "12",
        "reissue_day": "16",
        "curr_balance": "+         40.00",
        "curr_cycle_credit": "+           .00",
        "group_id": "",
        "curr_cycle_debit": "+           .00",
        "cust_id": "000000004",
        "ssn_part_1": "660",
        "ssn_part_2": "35",
        "ssn_part_3": "4258",
        "dob_year": "1985",
        "dob_month": "01",
        "dob_day": "13",
        "fico_credit_score": "776",
        "first_name": "Delbert",
        "middle_name": "Kaia",
        "last_name": "Parisian",
        "addr_line_1": "638 Blanda Gateway",
        "addr_state_cd": "NY",
        "addr_line_2": "Apt. 076",
        "addr_zip": "10001",
        "addr_line_3": "Lake Virginie",
        "addr_country_cd": "USA",
        "phone_num_1_area": "212",
        "phone_num_1_prefix": "555",
        "phone_num_1_line": "0100",
        "govt_issued_id": "00000000000068579249",
        "phone_num_2_area": "212",
        "phone_num_2_prefix": "555",
        "phone_num_2_line": "0101",
        "eft_account_id": "0040802739",
        "primary_holder_ind": "Y"
    },
    "state": "CHANGES_NOT_OK",
    "screen_token": "SCREEN_TOKEN"
}
```

Each field message is the field's name followed by a suffix, such as these:

- `Account Status must be Y or N.`
- `Credit Limit must be supplied.`
- `Credit Limit is not valid`: the text fails the `NUMVAL-C` test. Currency text such as
    `$1,234.567` passes, and is stored truncated to `1234.56`.
- `FICO Score: should be between 300 and 850`
- `SSN: First 3 chars: should not be 000, 666, or between 900 and 999`
- `Phone Number 1: Area code cannot be zero`
- `Open Date : Century is not valid.`
- `Invalid zip code for state`, which names both `addr_state_cd` and `addr_zip`.

The edits run in this order, under these field names: `Account Status`, `Open Date`,
`Credit Limit`, `Expiry Date`, `Cash Credit Limit`, `Reissue Date`, `Current Balance`,
`Current Cycle Credit Limit`, `Current Cycle Debit Limit`, `SSN`, `Date of Birth`, `FICO Score`,
`First Name`, `Middle Name`, `Last Name`, `Address Line 1`, `State`, `Zip`, `City`, `Country`,
`Phone Number 1`, `Phone Number 2`, `EFT Account Id` and `Primary Card Holder`, then the
state-and-ZIP check. The SSN parts are named `SSN: First 3 chars`, `SSN 4th & 5th chars` and
`SSN Last 4 chars`. The suffixes are these:

| Edit | Suffixes |
|---|---|
| Presence, Y/N, text | ` must be supplied.`, ` must be Y or N.`, ` can have alphabets only.` |
| Numbers | ` must be all numeric.`, ` must not be zero.`, ` is not valid` |
| Phone area | `: Area code must be supplied.`, `: Area code must be A 3 digit number.` |
| Phone area | `: Area code cannot be zero` |
| Phone area | `: Not valid North America general purpose area code` |
| Phone prefix | `: Prefix code must be supplied.`, `: Prefix code must be A 3 digit number.` |
| Phone prefix | `: Prefix code cannot be zero` |
| Phone line | `: Line number code must be supplied.`, `: Line number code cannot be zero` |
| Phone line | `: Line number code must be A 4 digit number.` |
| Ranges | `: should not be 000, 666, or between 900 and 999`, `: should be between 300 and 850` |
| State | `: is not a valid state code` |
| Date year | ` : Year must be supplied.`, ` must be 4 digit number.`, ` : Century is not valid.` |
| Date month | ` : Month must be supplied.`, `: Month must be a number between 1 and 12.` |
| Date day | ` : Day must be supplied.`, `:day must be a number between 1 and 31.` |
| Day in month | `:Cannot have 31 days in this month.`, `:Cannot have 30 days in this month.` |
| Leap year | `:Not a leap year.Cannot have 29 days in this month.` |
| Date service | ` validation error Sev code: SEVERITY_CODE Message code: MESSAGE_CODE` |
| Date of birth | `:cannot be in the future ` (with a trailing space) |

In the date-service suffix, `SEVERITY_CODE` and `MESSAGE_CODE` stand for the severity and the
message number that the date service (CSUTLDTC's call to CEEDAYS) returned, each as four
zero-padded digits. Every date-service error has severity `0003`.

The leading and trailing spaces of each suffix are part of the literal. Every edit runs whenever
any field changes, so an account whose stored values fail an edit cannot be updated until that
value is corrected in the same request.


## Cards (CCLI, CCDL, CCUP)

Card responses carry masked card numbers and never the CVV. A card is identified across requests by
its `card_ref`: an encrypted token that holds the card number and expires after
`ACCESS_TOKEN_EXPIRE_MINUTES`. Where a path takes `{cardNum}`, it accepts either the card number as
keyed (at most 16 characters) or a `card_ref` (up to 1024 characters); the length tells them apart.

### GET /api/v1/cards

Replaces CCLI / COCRDLIC / COCRDLI; role regular. Lists at most 7 cards in card-number order,
filtered by account and card number, as the program's paging browse reads them.

| Parameter | In | Type | Description |
|---|---|---|---|
| `acctId` | query | string, at most 11 | Account filter as keyed; blank for all accounts |
| `cardNum` | query | string, at most 16 | Card-number filter as keyed; blank for none |
| `startKey` | query | string, at most 1024 | The `first_key` (ENTER, F7) or `last_key` (F8) |
| `direction` | query | `forward` or `backward` | F8 or F7; omit for ENTER |
| `page` | query | integer, 0 to 9 | The page number shown; default 0 |
| `selections` | query | repeated string, at most 1 | Each shown row's selection code, in order |

`first_key` and `last_key` are opaque tokens, never card numbers. A blank, expired or foreign
`startKey` reads the first page. `selections` serves only to redisplay a page with an invalid
selection: a single valid `S` or `U` is sent to
[`POST /api/v1/cards/selection`](#post-apiv1cardsselection) instead.

This command lists the first page:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/cards
```

The response has status 200 and carries a `CardListResponse` body. This sample is shortened to the
first two of the page's seven rows, and `CARD_REF_1` and `CARD_REF_2` stand for their `card_ref`
tokens:

```json
{
    "message": "",
    "info_message": "TYPE S FOR DETAIL, U TO UPDATE ANY RECORD",
    "field_errors": [],
    "cursor_field": "acct_id",
    "page": 1,
    "first_key": "START_KEY",
    "last_key": "START_KEY",
    "rows": [
        {
            "card_ref": "CARD_REF_1",
            "acct_id": "00000000050",
            "card_num_masked": "************5740",
            "active_status": "Y"
        },
        {
            "card_ref": "CARD_REF_2",
            "acct_id": "00000000027",
            "card_num_masked": "************1516",
            "active_status": "Y"
        }
    ]
}
```

This command reads the next page (F8) from the page shown:

```bash
curl -s -b var/session.cookies -G http://127.0.0.1:8000/api/v1/cards \
    --data-urlencode 'startKey=START_KEY' \
    -d direction=forward -d page=1
```

Other outcomes:

- 200 `NO PREVIOUS PAGES TO DISPLAY`: F7 on the first page.
- 200 `NO MORE PAGES TO DISPLAY`: F8 after the last page.
- 200 `NO MORE RECORDS TO SHOW`: the page shown holds the last matching card.
- 200 `NO RECORDS FOUND FOR THIS SEARCH CONDITION.`, with no rows: nothing matches the filters.
- 400 `ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER` (`acct_id`).
- 400 `CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER` (`card_num`).
- 400 `PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE`: more than one `S` or `U`.
    `field_errors` names each selected row, such as `rows[0].selection`, and the body
    (`CardListErrorResponse`) adds the redisplayed `rows`, `page`, `first_key` and `last_key`.
- 400 `INVALID ACTION CODE`: a selection code other than `S`, `U` or blank, with the same
    redisplayed page.

COCRDLIC's header comment promises admins every card, but the program only filters, and admins
cannot call this endpoint; it is ported as the code behaves.

### POST /api/v1/cards/selection

Replaces CCLI / COCRDLIC / COCRDLI (ENTER with a selection code) and its `XCTL` to COCRDSLC or
COCRDUPC; role regular. Edits the filters and the selection codes, then returns the selected row's
`card_ref` and the screen to open.

| Body field (`CardSelectionRequest`) | Type | Description |
|---|---|---|
| `acct_id` | string, at most 11 | The account filter shown |
| `card_num` | string, at most 16 | The card-number filter shown |
| `rows` | list of at most 7 `CardSelectionRow` | Every row shown, with its `card_ref` and code |
| `rows[].card_ref` | string, at most 1024 | The row's `card_ref` |
| `rows[].selection` | string, at most 1 | `S` (view), `U` (update) or blank |

This command selects the card of the account filter's single row for update:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/cards/selection \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"acct_id": "ACCOUNT_ID", "card_num": "",
        "rows": [{"card_ref": "CARD_REF", "selection": "U"}]}'
```

The response has status 200 and carries this `CardSelectionResponse` body:

```json
{
    "message": "",
    "info_message": "",
    "field_errors": [],
    "cursor_field": null,
    "action": "update",
    "card_ref": "CARD_REF"
}
```

`action` is `view` for `S` and `update` for `U`. The frontend then opens the card view or update
page with the returned `card_ref`.

Other outcomes:

- 400 with the filter literals, `PLEASE SELECT ONLY ONE RECORD TO VIEW OR UPDATE` or
    `INVALID ACTION CODE`, as for [`GET /api/v1/cards`](#get-apiv1cards). The two selection
    literals come with the redisplayed `rows`, `first_key` and `last_key`, but no `page`
    (`CardSelectionErrorResponse`).
- 404 `Did not find cards for this search condition`: the row's `card_ref` is invalid or expired.

### GET /api/v1/cards/{cardNum}

Replaces CCDL / COCRDSLC / COCRDSL; role regular. Edits both the account number and the card
number, then reads the card by card number only, as COCRDSLC does. A `card_ref` from the card list,
sent with its row's 11-digit account number, is read with those keys and no key edits.

| Parameter | In | Type | Description |
|---|---|---|---|
| `cardNum` | path | string, at most 1024 | A card number as keyed, or a `card_ref` |
| `acctId` | query | string, at most 11 | The account number as keyed |

This command views a card from the card list:

```bash
curl -s -b var/session.cookies \
    'http://127.0.0.1:8000/api/v1/cards/CARD_REF?acctId=ACCOUNT_ID'
```

To view a card by its number, replace `CARD_REF` with `CARD_NUMBER`. The response has status 200
and carries this `CardDetailResponse` body:

```json
{
    "message": "",
    "info_message": "   Displaying requested details",
    "field_errors": [],
    "cursor_field": "acct_id",
    "acct_id": "00000000004",
    "card_ref": "CARD_REF",
    "card_num_masked": "************4312",
    "embossed_name": "Delbert Parisian",
    "active_status": "Y",
    "expiry_month": "12",
    "expiry_year": "2023"
}
```

The information line starts with three spaces. Every error body carries `info_message`
`Please enter Account and Card Number`. Other outcomes:

- 400 `No input received` (`acct_id`, `card_num`): both keys are blank.
- 400 `Account number not provided` (`acct_id`).
- 400 `Card number not provided` (`card_num`).
- 400 `ACCOUNT FILTER,IF SUPPLIED MUST BE A 11 DIGIT NUMBER` (`acct_id`).
- 400 `CARD ID FILTER,IF SUPPLIED MUST BE A 16 DIGIT NUMBER` (`card_num`).
- 404 `Did not find cards for this search condition`: no such card, or an invalid or expired
    `card_ref`.
- 503 with the program's file-error text: the card file cannot be read.

### GET /api/v1/cards/{cardNum}/edit

Replaces CCUP / COCRDUPC / COCRDUP (entry from the card list, or ENTER with both keys); role
regular. Fetches the card into the update screen, in state `SHOW_DETAILS`. A `card_ref` with its
row's account number is read without key edits; a typed card number has both keys edited first.

| Parameter | In | Type | Description |
|---|---|---|---|
| `cardNum` | path | string, at most 1024 | A card number as keyed, or a `card_ref` |
| `acctId` | query | string, at most 11 | The account number as keyed |

This command fetches a card for update:

```bash
curl -s -b var/session.cookies \
    'http://127.0.0.1:8000/api/v1/cards/CARD_REF/edit?acctId=ACCOUNT_ID'
```

The response has status 200 and carries this `CardScreen` body:

```json
{
    "message": "",
    "info_message": "Details of selected card shown above",
    "field_errors": [],
    "cursor_field": "embossed_name",
    "fields": {
        "acct_id": "00000000004",
        "card_num": "************4312",
        "embossed_name": "DELBERT PARISIAN",
        "active_status": "Y",
        "expiry_month": "12",
        "expiry_year": "2023",
        "expiry_day": ""
    },
    "state": "SHOW_DETAILS",
    "screen_token": "SCREEN_TOKEN",
    "card_ref": "CARD_REF"
}
```

`fields.card_num` is always masked once details are fetched; the card key travels in
`screen_token`. `fields.embossed_name` is the stored name upper-cased; the card view shows it as
stored. `expiry_day` is always blank in responses and ignored in requests: the stored expiry day
travels only in the encrypted before-image inside `screen_token`, and a save keeps it.

| Fields (`CardScreenFields`) | Length |
|---|---|
| `acct_id` | 11 |
| `card_num` | 16 |
| `embossed_name` | 50 |
| `active_status` | 1 |
| `expiry_month`, `expiry_day` | 2 |
| `expiry_year` | 4 |

Other outcomes are those of [`GET /api/v1/cards/{cardNum}`](#get-apiv1cardscardnum), each with
the key screen in state `DETAILS_NOT_FETCHED` (`CardScreenErrorResponse`).

### PUT /api/v1/cards/{cardNum}

Replaces CCUP / COCRDUPC / COCRDUP (ENTER, F5 and F12); role regular. Runs one step of the same
state machine as the account update ([Screen state machine](#screen-state-machine)), on the card.

| Parameter | In | Type | Description |
|---|---|---|---|
| `cardNum` | path | string, at most 1024 | The card number or `card_ref` of the fetched screen |

| Body field (`CardUpdateRequest`) | Type | Description |
|---|---|---|
| `action` | `enter`, `save` or `cancel`; default `enter` | ENTER, F5 or F12 |
| `screen_token` | string, at most 16384 | The `screen_token` of the previous response |
| `fields` | `CardScreenFields` | The screen's fields as keyed |

The differences from the account update:

- Before a fetch, `acct_id` and `card_num` in `fields` are the editable keys, and the path is not
    read. Afterwards the keys come from `screen_token`, which holds only for the card the path
    names; a token for another card restarts the flow at `DETAILS_NOT_FETCHED` and writes nothing.
- With details shown, the editable fields are `embossed_name`, `active_status`, `expiry_month` and
    `expiry_year`.
- The information lines are `Please enter Account and Card Number` (`DETAILS_NOT_FETCHED`),
    `Details of selected card shown above` (`SHOW_DETAILS`) and
    `Update card details presented above.` (`CHANGES_NOT_OK`); the later states use the account
    update's lines.

To validate a new embossed name, write the fetched `fields` to `card-enter.json` with the new name
keyed, and the fetched `screen_token`. The name must differ from the fetched one in more than
letter case, because the no-change test compares both upper-cased:

```json
{
    "action": "enter",
    "screen_token": "SCREEN_TOKEN",
    "fields": {
        "acct_id": "00000000004",
        "card_num": "************4312",
        "embossed_name": "DELBERT K PARISIAN",
        "active_status": "Y",
        "expiry_month": "12",
        "expiry_year": "2023",
        "expiry_day": ""
    }
}
```

This command sends it:

```bash
curl -s -b var/session.cookies -X PUT \
    http://127.0.0.1:8000/api/v1/cards/CARD_REF \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d @card-enter.json
```

The response has status 200 and this body, in state `CHANGES_OK_NOT_CONFIRMED`:

```json
{
    "message": "",
    "info_message": "Changes validated.Press F5 to save",
    "field_errors": [],
    "cursor_field": "acct_id",
    "fields": {
        "acct_id": "00000000004",
        "card_num": "************4312",
        "embossed_name": "DELBERT K PARISIAN",
        "active_status": "Y",
        "expiry_month": "12",
        "expiry_year": "2023",
        "expiry_day": ""
    },
    "state": "CHANGES_OK_NOT_CONFIRMED",
    "screen_token": "SCREEN_TOKEN",
    "card_ref": "CARD_REF"
}
```

This command saves the validated change:

```bash
curl -s -b var/session.cookies -X PUT \
    http://127.0.0.1:8000/api/v1/cards/CARD_REF \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"action": "save", "screen_token": "SCREEN_TOKEN", "fields": {}}'
```

The response has status 200, state `CHANGES_DONE` and `info_message`
`Changes committed to database`, with the same `fields`, a new `screen_token` and the `card_ref`.
The card now holds the validated values, with the name as keyed, and keeps its stored expiry day.

Other outcomes:

- 400 `No change detected with respect to values fetched.`: `enter` with nothing changed but
    letter case, such as the fetched `fields` sent back as they are; the state is kept.
- 400 `Card name not provided` (`embossed_name`).
- 400 `Card name can only contain alphabets and spaces` (`embossed_name`).
- 400 `Card Active Status must be Y or N` (`active_status`).
- 400 `Card expiry month must be between 1 and 12` (`expiry_month`).
- 400 `Invalid card expiry year` (`expiry_year`): a year outside 1950 to 2099.
- 400 with the key literals of [`GET /api/v1/cards/{cardNum}`](#get-apiv1cardscardnum), before a
    fetch.
- 404 `Did not find cards for this search condition`: an invalid or expired `card_ref` path.
- 409 `Record changed by some one else. Please review`, state `SHOW_DETAILS` with fresh data.
- 423 `Could not lock record for update`, state `CHANGES_FAILED`.
- 503 `Update of record failed`, state `CHANGES_FAILED`.

In all-field mode one request can fail several edits. Keying `A1` as the name, `Q` as the status
and `13` as the month gives the name's message with
`"field_errors": ["embossed_name", "active_status", "expiry_month"]`.


## Transactions (CT00, CT01, CT02)

Transaction IDs are compared and ordered in EBCDIC order, as the VSAM key was. List and paging keys
are raw transaction IDs, sent back as the server returned them.

### GET /api/v1/transactions

Replaces CT00 / COTRN00C / COTRN00; role regular. Lists at most 10 transactions in transaction-ID
order.

| Parameter | In | Type | Description |
|---|---|---|---|
| `searchTranId` | query | string, at most 16 | Search key as keyed; blank starts at the lowest ID |
| `startKey` | query | string, at most 16 | The `first_key` (F7) or `last_key` (F8) shown |
| `firstKey` | query | string, at most 16 | The `first_key` shown, sent with `direction` `forward` |
| `direction` | query | `forward` or `backward`; default `forward` | F8 or F7 |
| `page` | query | integer, 0 to 99999999 | The page number shown; default 0 |

Without a `startKey`, the browse starts at the first transaction ID greater than or equal to
`searchTranId`, inclusive, as page 1. F8 (`direction=forward` with the `last_key`) continues after
the last ID shown, and F7 (`direction=backward` with the `first_key`) reads backward from the
first ID shown. `firstKey` lets the server show the same rows again when F8 finds no further page or
a search finds nothing.

This command lists the first page:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/transactions
```

The response has status 200 and carries this `TransactionListResponse` body:

```json
{
    "page": 1,
    "first_key": "0000000000000001",
    "last_key": "0000000000000002",
    "rows": [
        {
            "tran_id": "0000000000000001",
            "tran_date": "07/17/22",
            "tran_desc": "E2E PURCHASE",
            "tran_amt": "50.00"
        },
        {
            "tran_id": "0000000000000002",
            "tran_date": "11/04/26",
            "tran_desc": "BILL PAYMENT - ONLINE",
            "tran_amt": "345.00"
        }
    ],
    "message": "You have reached the bottom of the page...",
    "cursor_field": "search_tran_id",
    "search_cleared": true
}
```

`tran_date` is the original timestamp's date as `MM/DD/YY`. `search_cleared` is true when the
program blanked the search field, so the page shows it empty.

This command reads the next page (F8) after the page shown:

```bash
curl -s -b var/session.cookies -G http://127.0.0.1:8000/api/v1/transactions \
    -d startKey=START_KEY -d firstKey=0000000000000001 \
    -d direction=forward -d page=1
```

Every paging literal is a 200 list response:

- `You are already at the top of the page...`: F7 on the first page.
- `You are already at the bottom of the page...`: F8 with no further transaction.
- `You are at the top of the page...`: the browse found nothing at or after the search key.
- `You have reached the bottom of the page...`: the page holds the last transaction.
- `You have reached the top of the page...`: F7 reached the first transaction.

Other outcomes:

- 400 `Tran ID must be Numeric ...` (`search_tran_id`), with only the four standard keys: the
    search key is not numeric, and the list is left unchanged. On an empty file the answer is
    `You are at the top of the page...` instead.
- 503 `Unable to lookup transaction...`: the transaction file cannot be read, as while the batch
    window is held.

### POST /api/v1/transactions/selection

Replaces CT00 / COTRN00C / COTRN00 (ENTER with a selection code) and its `XCTL` to COTRN01C; role
regular. Only the first row with a non-blank selection code counts: blank codes and later codes
are ignored, and a blank `tran_id` on that row selects nothing. The search key is then applied.

| Body field (`TransactionSelectionRequest`) | Type | Description |
|---|---|---|
| `selections` | list of at most 10 `TransactionSelection` | The rows shown, in screen order |
| `selections[].tran_id` | string, at most 16 | The row's transaction ID |
| `selections[].selection` | string, at most 1 | `S`/`s` (view), another code, or blank |
| `search_tran_id` | string, at most 16 | The search key as keyed |
| `first_key` | string, at most 16 | The `first_key` shown |
| `page` | integer, 0 to 99999999 | The page number shown |

This command selects a transaction for display:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/transactions/selection \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"selections": [{"tran_id": "TRAN_ID", "selection": "S"}],
        "search_tran_id": "", "first_key": "TRAN_ID", "page": 1}'
```

The response has status 200 and carries this `TransactionSelectionResponse` body. `route` is the
Transaction View page for the selected ID:

```json
{
    "page": 0,
    "first_key": "",
    "last_key": "",
    "rows": [],
    "message": "",
    "cursor_field": null,
    "search_cleared": false,
    "route": "/transactions/view?tranId=0000000000000001"
}
```

Other outcomes:

- A non-blank code other than `S` or `s`, on a row whose `tran_id` is not blank, sets
    `Invalid selection. Valid value is S`, and the list is browsed again from `search_tran_id`. When
    that literal stays, the answer is 400 (`TransactionSelectionErrorResponse`) with the browsed
    `rows`, `page`, `first_key`, `last_key` and `search_cleared`. When the browse replaces it with
    a paging literal, the answer is a 200 list with `route` null.
- 400 `Tran ID must be Numeric ...` (`search_tran_id`): no row was routed by `S` or `s`, and the
    search key is not numeric. On an empty file the answer is `You are at the top of the page...`
    instead.
- 503 `Unable to lookup transaction...`.

### GET /api/v1/transactions/{tranId}

Replaces CT01 / COTRN01C / COTRN01; role regular. Reads one transaction for display.

| Parameter | In | Type | Description |
|---|---|---|---|
| `tranId` | path | string, at most 16 | The transaction ID as keyed |

This command views a transaction:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/transactions/TRAN_ID
```

The response has status 200 and carries this `TransactionDetailResponse` body:

```json
{
    "tran_id": "0000000000000001",
    "card_num_masked": "************4312",
    "tran_type_cd": "01",
    "tran_cat_cd": 1,
    "tran_source": "POS TERM",
    "tran_desc": "E2E PURCHASE",
    "tran_amt": "50.00",
    "orig_ts": "2022-07-17                ",
    "proc_ts": "2022-07-17                ",
    "merchant_id": "000000001",
    "merchant_name": "E2E MERCHANT",
    "merchant_city": "E2E CITY",
    "merchant_zip": "00000",
    "message": ""
}
```

`orig_ts` and `proc_ts` are the stored 26-character timestamp text, in the form its producer wrote.
A transaction added online holds its date followed by 16 spaces, as COTRN02C stored it; a posted
transaction holds a full timestamp such as `2022-06-10 19:27:53.000000`.

Other outcomes:

- 400 `Tran ID can NOT be empty...` (`tran_id`): the ID is blank, sent as `%20`.
- 404 `Transaction ID NOT found...` (`tran_id`).
- 503 `Unable to lookup Transaction...` (`tran_id`).

### POST /api/v1/transactions

Replaces CT02 / COTRN02C / COTRN02 (ENTER and F5); role regular. Edits the fields in screen order,
stopping at the first failure, and adds the transaction when `confirm` is `Y`. A new transaction
takes the highest existing transaction ID plus 1.

| Body field (`TransactionAddRequest`) | Length | Description |
|---|---|---|
| `action` | — | `enter` (ENTER, the default) or `copy_last` (F5) |
| `acct_id` | 11 | Account number; this or `card_num` is required |
| `card_num` | 16 | Card number |
| `type_cd` | 2 | Transaction type code |
| `cat_cd` | 4 | Transaction category code |
| `source` | 10 | Source |
| `desc` | 60 | Description |
| `amount` | 12 | Amount in the form `-99999999.99`, with a leading sign |
| `orig_date` | 10 | Original date, `YYYY-MM-DD` |
| `proc_date` | 10 | Processing date, `YYYY-MM-DD` |
| `merchant_id` | 9 | Merchant ID, numeric |
| `merchant_name` | 30 | Merchant name |
| `merchant_city` | 25 | Merchant city |
| `merchant_zip` | 10 | Merchant ZIP code |
| `confirm` | 1 | `Y` adds; blank or `N` asks for confirmation |

Every field is a string. To add a transaction, write the request to `add.json`:

```json
{
    "action": "enter",
    "acct_id": "00000000004",
    "card_num": "",
    "type_cd": "01",
    "cat_cd": "0001",
    "source": "POS TERM",
    "desc": "E2E PURCHASE",
    "amount": "+00000050.00",
    "orig_date": "2022-07-17",
    "proc_date": "2022-07-17",
    "merchant_id": "000000001",
    "merchant_name": "E2E MERCHANT",
    "merchant_city": "E2E CITY",
    "merchant_zip": "00000",
    "confirm": "Y"
}
```

This command sends it:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/transactions \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d @add.json
```

On an empty transaction table, the response has status 200 and carries this
`TransactionAddResponse` body, with the form cleared:

```json
{
    "message": "ADDED_MESSAGE",
    "cursor_field": "acct_id",
    "fields": {
        "acct_id": "",
        "card_num": "",
        "type_cd": "",
        "cat_cd": "",
        "source": "",
        "desc": "",
        "amount": "",
        "orig_date": "",
        "proc_date": "",
        "merchant_id": "",
        "merchant_name": "",
        "merchant_city": "",
        "merchant_zip": "",
        "confirm": ""
    },
    "tran_id": "0000000000000001"
}
```

`ADDED_MESSAGE` stands for the success literal, which is too long for a sample line. It holds the
new ID, and two spaces after `successfully.`:

```text
Transaction added successfully.  Your Tran ID is 0000000000000001.
```

With `confirm` blank or `N`, the same
request returns status 200, `message` `Confirm to add this transaction...`, `cursor_field`
`confirm`, `tran_id` null and `fields` holding the values as edited. `fields.card_num` is masked.

`copy_last` (F5) first edits the entered account or card number. It then copies the type,
category, source, amount, description, dates and merchant fields of the highest transaction into
the form, keeps the entered keys, and runs the same confirm flow with the submitted `confirm`.

Other outcomes, in edit order, each naming its field:

- 400 `Account or Card Number must be entered...`.
- 400 `Account ID must be Numeric...`, `Card Number must be Numeric...`.
- 404 `Account ID NOT found...`, `Card Number NOT found...`.
- 400 `FIELD_NAME can NOT be empty...`, where `FIELD_NAME` is the label of the first blank field,
    checked in this order: `Type CD`, `Category CD`, `Source`, `Description`, `Amount`,
    `Orig Date`, `Proc Date`, `Merchant ID`, `Merchant Name`, `Merchant City`, `Merchant Zip`.
- 400 `Type CD must be Numeric...`, `Category CD must be Numeric...`.
- 400 `Amount should be in format -99999999.99`.
- 400 `Orig Date should be in format YYYY-MM-DD`, `Proc Date should be in format YYYY-MM-DD`.
- 400 `Orig Date - Not a valid date...`, `Proc Date - Not a valid date...`.
- 400 `Merchant ID must be Numeric...`.
- 400 `Invalid value. Valid values are (Y/N)...` (`confirm`).
- 409 `Tran ID already exist...`: the next ID is taken.
- 503 `Unable to lookup Acct in XREF AIX file...`, `Unable to lookup Card # in XREF file...`,
    `Unable to lookup Transaction...` or `Unable to Add Transaction...`: a file cannot be read or
    written, as while the batch window is held.


## Transaction reports (CR00)

CORPT00C submitted a print job that ran CBTRN03C. Here the report is built online: the screen
endpoint returns the CBTRN03C report lines as JSON, and the export endpoint streams the same lines
as a CSV or PDF download. Both run the same edits in first-failure order, and both read committed
transactions without entering the batch window. In CR00 error bodies, `field_errors` and
`cursor_field` use the query parameter names, such as `reportType` and `startMm`, because they are
the report page's field names.

| Parameter | In | Type | Description |
|---|---|---|---|
| `reportType` | query | `monthly`, `yearly`, `custom` or empty; default empty | The report type |
| `startMm`, `startDd` | query | string, at most 2 | Start month and day, for `custom` |
| `startYyyy` | query | string, at most 4 | Start year, for `custom` |
| `endMm`, `endDd` | query | string, at most 2 | End month and day, for `custom` |
| `endYyyy` | query | string, at most 4 | End year, for `custom` |
| `confirm` | query | string, at most 1 | `Y` or `y` builds the report; `N` or `n` clears it |

`monthly` reports the current month, from its first to its last day, and `yearly` the current
year. `custom` reports the entered range; its date parts are edited only for that type.

### GET /api/v1/reports/transactions

Replaces CR00 / CORPT00C / CORPT00 (ENTER) and the CBTRN03C report; role regular. Returns the
report screen as a `TransactionReportResponse`.

This command builds a custom report for one day:

```bash
curl -s -b var/session.cookies -G \
    http://127.0.0.1:8000/api/v1/reports/transactions \
    -d reportType=custom -d startMm=07 -d startDd=17 -d startYyyy=2022 \
    -d endMm=07 -d endDd=17 -d endYyyy=2022 -d confirm=Y
```

The response has status 200. This sample is shortened to the detail line and the grand-total line
of the report's eight lines, and `PRINT_LINE` stands for each line's 133-character print text,
which is too long for a sample line:

```json
{
    "start_date": "2022-07-17",
    "end_date": "2022-07-17",
    "message": "Custom report submitted for printing ...",
    "cursor_field": "reportType",
    "lines": [
        {
            "line_kind": "detail",
            "tran_id": "0000000000000001",
            "acct_id": "00000000004",
            "type_cd": "01",
            "type_desc": "Purchase",
            "cat_cd": 1,
            "cat_desc": "Regular Sales Draft",
            "source": "POS TERM",
            "amount": "50.00",
            "text": "PRINT_LINE"
        },
        {
            "line_kind": "grand_total",
            "tran_id": "",
            "acct_id": "",
            "type_cd": "",
            "type_desc": "",
            "cat_cd": null,
            "cat_desc": "",
            "source": "",
            "amount": "100.00",
            "text": "PRINT_LINE"
        }
    ]
}
```

The report holds transactions whose processing date falls in the range, in card-number order and
then transaction-ID order. `line_kind` is one of these:

| `line_kind` | Content |
|---|---|
| `header` | Title and date range, a blank line, column headings, a rule; the rule after a total |
| `detail` | One transaction: ID, account, type, category, source and amount |
| `account_total` | `Account Total` of the previous card, written when the card number changes |
| `page_total` | `Page Total` and a rule, then the headers again at a page break; also at the end |
| `grand_total` | `Grand Total`, at the end, after the last `page_total` and its rule |

Every line but `grand_total` is counted: 4 per set of headers, 2 per total with its rule and 1 per
detail. Only just before a detail does the program test the count; when it is a multiple of 20,
the page breaks there with `page_total`, its rule and the headers. An `account_total` adds 2 lines
between two tests, so the count can pass a multiple of 20 untested and the page runs on: two cards
of 15 transactions give 39 lines and one `page_total`, at the end. That last `page_total` is
followed by its rule and `grand_total`, never by headers.

The totals reproduce CBTRN03C exactly. At end of file the program adds the last amount to the page
and grand totals a second time and never writes the final account total, so a report with one
50.00 transaction ends with a page total and a grand total of 100.00 and no `account_total` line.

`text` is the 133-column TRANREPT print line. A detail line places the transaction ID in columns
1–16, the account in 18–28, the type code and description in 30–47, the category code and
description in 49–82, the source in 84–93 and the amount, edited as `-ZZZ,ZZZ,ZZZ.ZZ`, in 98–112.
Total lines end with the amount edited as `+ZZZ,ZZZ,ZZZ.ZZ` in the same columns.

Other outcomes:

- 200 with `lines` empty and `message` `Please confirm to print the Custom report...`: `confirm`
    is blank. The name is `Monthly`, `Yearly` or `Custom`.
- 200 with the screen cleared, empty dates and an empty `message`: `confirm` is `N`.
- 400 `Select a report type to print report...` (`reportType`).
- 400 `Start Date - Month can NOT be empty...`, and the same form for `Day` and `Year` and for
    `End Date`.
- 400 `Start Date - Not a valid Month...` (`startMm`) for a month above 12, and
    `Start Date - Not a valid Day...` (`startDd`) for a day above 31, both after normalisation;
    the `End Date` forms name `endMm` and `endDd`.
- 400 `Start Date - Not a valid date...` (`startMm`), `End Date - Not a valid date...` (`endMm`):
    the normalised parts form no calendar date, such as 30 February or a month or day of `00`.
- 400 `"X" is not a valid value to confirm...` (`confirm`): the keyed value, here `X`, in double
    quotes.
- 503 `Unable to Write TDQ (JOBS)...` (`reportType`): the transactions cannot be read. The literal
    is the program's only file-error message.

CORPT00C normalises each date part before these checks: NUMVAL-C reads the text into an unsigned
two- or four-digit field, which drops any sign and fraction, and unreadable text becomes zero. So
`-5` becomes `05`, `1.` becomes `01`, `AB` becomes `00` and `ABCD` becomes `0000`. A part longer
than its field, such as the year `-2022`, is refused with 422 first ([Input limits](#input-limits)).
A normalised year is always four digits, so `Start Date - Not a valid Year...` and
`End Date - Not a valid Year...` are assigned in the source but never returned. A date that exists
in the calendar but falls before 15 October 1582, year `0000` included, passes the calendar-date
check, because CORPT00C accepts CEEDAYS message 2513: `startYyyy=ABCD` with month `07` and day
`17` returns 200 with `start_date` `0000-07-17`. A month of `00` or a day the month lacks still
fails in any year: `startYyyy=0000` with month `02` and day `30` returns 400
`Start Date - Not a valid date...` (`startMm`). A `0000` start precedes every stored date, and a
`0000` end selects no transaction.

### GET /api/v1/reports/transactions/export

Replaces the TRANREPT print file of CR00 / CORPT00C and CBTRN03C; role regular. Runs the same edits
and streams the same lines as an attachment named `TRANREPT_START_DATE_END_DATE.FORMAT`, built
in memory. `START_DATE` and `END_DATE` stand for the report's first and last days as `YYYY-MM-DD`,
and `FORMAT` for the `format` parameter, `csv` or `pdf`. Only `confirm` `Y` downloads.

| Parameter | In | Type | Description |
|---|---|---|---|
| The report parameters | query | As above | The same parameters as the on-screen report |
| `format` | query | `csv` or `pdf`; required | The download format |

This command downloads the one-day report as CSV:

```bash
curl -s -b var/session.cookies -G -OJ \
    http://127.0.0.1:8000/api/v1/reports/transactions/export \
    -d reportType=custom -d startMm=07 -d startDd=17 -d startYyyy=2022 \
    -d endMm=07 -d endDd=17 -d endYyyy=2022 -d confirm=Y -d format=csv
```

The response has status 200 with these headers:

```http
HTTP/1.1 200 OK
content-type: text/csv; charset=utf-8
content-disposition: attachment; filename="TRANREPT_2022-07-17_2022-07-17.csv"
# Other headers omitted.
```

The CSV file has one row per report line, under this header row:

```text
line_kind,tran_id,acct_id,type_cd,type_desc,cat_cd,cat_desc,source,amount
```

The detail row of the sample report holds these values:

| Column | Value |
|---|---|
| `line_kind` | `detail` |
| `tran_id` | `0000000000000001` |
| `acct_id` | `00000000004` |
| `type_cd` | `01` |
| `type_desc` | `Purchase` |
| `cat_cd` | `0001` |
| `cat_desc` | `Regular Sales Draft` |
| `source` | `POS TERM` |
| `amount` | `50.00` |

Header rows leave every column but `line_kind` empty, and total rows fill only `amount`. Amounts
are plain decimals. A text cell starting with `=`, `+`, `-` or `@` is prefixed with `'`, so a
spreadsheet never runs it as a formula.

With `format=pdf`, the response is `application/pdf`: the 133-column print lines drawn in Courier,
reproducing the TRANREPT print file.

Other outcomes:

- 400 `Please confirm to print the Custom report...` (`confirm`): `confirm` is blank. A download
    needs `Y`.
- 400 with an empty `message` (`reportType`): `confirm` is `N`.
- 400, 503: the edit and file-error literals of
    [`GET /api/v1/reports/transactions`](#get-apiv1reportstransactions).
- 422 (`format`): `format` is missing or not `csv` or `pdf`.

## Bill payment (CB00)

### POST /api/v1/billpay

Replaces CB00 / COBIL00C / COBIL00 (ENTER); role regular. Shows the account's current balance and
asks for confirmation; on `Y`, records a payment transaction for the full balance and reduces the
balance by it.

| Body field (`BillPayRequest`) | Type | Description |
|---|---|---|
| `acct_id` | string, at most 11 | The account number as keyed |
| `confirm` | string, at most 1 | `Y` pays; `N` clears the screen; blank asks for confirmation |

This command shows the balance and asks for confirmation:

```bash
curl -s -b var/session.cookies -X POST http://127.0.0.1:8000/api/v1/billpay \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"acct_id": "ACCOUNT_ID", "confirm": ""}'
```

For account `00000000005`, the response has status 200 and carries this `BillPayResponse` body:

```json
{
    "current_balance": "345.00",
    "message": "Confirm to make a bill payment...",
    "cursor_field": "confirm",
    "tran_id": null
}
```

This command confirms the payment:

```bash
curl -s -b var/session.cookies -X POST http://127.0.0.1:8000/api/v1/billpay \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"acct_id": "ACCOUNT_ID", "confirm": "Y"}'
```

The response has status 200 and this body:

```json
{
    "current_balance": null,
    "message": "Payment successful.  Your Transaction ID is 0000000000000002.",
    "cursor_field": "acct_id",
    "tran_id": "0000000000000002"
}
```

The success literal holds two spaces after `successful.`. The payment transaction has type `02`,
category 2, source `POS TERM`, description `BILL PAYMENT - ONLINE`, the account's cross-referenced
card, merchant ID `999999999`, merchant name `BILL PAYMENT`, and city and ZIP code `N/A`. Its
amount is the balance moved into the 9-digit transaction amount, so a balance of 1,000,000,000.00
or more loses its high-order digit, as in COBIL00C.

Other outcomes, each naming its field:

- 200 with `current_balance` null and an empty `message` (`acct_id`): `confirm` is `N`.
- 400 `Acct ID can NOT be empty...` (`acct_id`).
- 404 `Account ID NOT found...` (`acct_id`).
- 400 `You have nothing to pay...` (`acct_id`): the balance is zero or negative.
- 400 `Invalid value. Valid values are (Y/N)...` (`confirm`).
- 409 `Tran ID already exist...`: the next transaction ID is taken.
- 503 `Unable to lookup Account...`, `Unable to lookup XREF AIX file...`,
    `Unable to lookup Transaction...`, `Unable to Add Bill pay Transaction...` or
    `Unable to Update Account...`: a file cannot be read or written, as while the batch window is
    held.

After a confirmed payment, COBIL00C runs every step in sequence whatever the earlier steps
returned, and shows the last literal set. So a payment whose transaction write fails as a duplicate
still reduces the balance, and a payment whose balance rewrite fails keeps its transaction.

## User administration (CU00–CU03)

Every endpoint in this section is admin-only. User IDs are ordered in EBCDIC order: lower case
before upper case, and both before digits. Request samples use the placeholders `NEW_USER_ID`, a
user ID of up to 8 characters to create, and `NEW_PASSWORD`, its password of up to 8 characters.

### GET /api/v1/admin/users

Replaces CU00 / COUSR00C / COUSR00; role admin. Lists at most 10 users in user-ID order.

| Parameter | In | Type | Description |
|---|---|---|---|
| `searchUserId` | query | string, at most 8 | Search key; blank starts at the lowest user ID |
| `startKey` | query | string, at most 8 | With `direction`: `first_key` (F7) or `last_key` (F8) |
| `direction` | query | `forward` or `backward` | F8 or F7; omit for ENTER |
| `page` | query | integer, 0 to 99999999 | The page number shown; default 0 |

Without a `direction`, the browse starts at the first user ID greater than or equal to
`searchUserId`, inclusive, as page 1.

This command lists the first page:

```bash
curl -s -b var/session.cookies http://127.0.0.1:8000/api/v1/admin/users
```

The response has status 200 and carries a `UserListResponse` body. This sample is shortened to the
first two of the page's ten rows:

```json
{
    "page": 1,
    "first_key": "ADMIN001",
    "last_key": "USER0005",
    "rows": [
        {
            "user_id": "ADMIN001",
            "first_name": "MARGARET",
            "last_name": "GOLD",
            "user_type": "A"
        },
        {
            "user_id": "ADMIN002",
            "first_name": "RUSSELL",
            "last_name": "RUSSELL",
            "user_type": "A"
        }
    ],
    "message": "You have reached the bottom of the page...",
    "cursor_field": "search_user_id"
}
```

This command reads the next page (F8):

```bash
curl -s -b var/session.cookies -G http://127.0.0.1:8000/api/v1/admin/users \
    -d startKey=START_KEY -d direction=forward -d page=1
```

Other outcomes:

- 200 `You are at the top of the page...`, page 1 with no rows and a blank `last_key`: the search
    found no user ID at or after its key.
- 200 with a page and `You have reached the bottom of the page...` or
    `You have reached the top of the page...`: the page holds the last or the first user.
- 400 `You are already at the top of the page...` (`search_user_id`): F7 on page 1.
- 400 `You are already at the bottom of the page...` (`search_user_id`): F8 with no further user,
    or with a blank `startKey`.
- 400 `You are at the top of the page...` or `You have reached the top of the page...`
    (`search_user_id`): F7 reads no row before the first key shown.
- 503 `Unable to lookup User...`.

Unlike CT00, a CU00 key press that reads no row is a 400 refusal with the four standard keys, and
the page shown stays as it was.

### POST /api/v1/admin/users/selection

Replaces CU00 / COUSR00C / COUSR00 (ENTER with a selection code) and its `XCTL` to COUSR02C or
COUSR03C; role admin. Only the first row with a non-blank selection code counts: blank codes and
later codes are ignored, and a blank `user_id` on that row selects nothing. The search key is then
applied.

| Body field (`UserSelectionRequest`) | Type | Description |
|---|---|---|
| `selections` | list of at most 10 `UserSelection` | Every row shown, in screen order |
| `selections[].user_id` | string, at most 8 | The row's user ID |
| `selections[].selection` | string, at most 1 | `U`/`u` update, `D`/`d` delete, other, or blank |
| `search_user_id` | string, at most 8 | The search key as keyed |

This command selects a user for update:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/admin/users/selection \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"selections": [{"user_id": "USER_ID", "selection": "U"}],
        "search_user_id": ""}'
```

The response has status 200 and carries this `UserSelectionResponse` body:

```json
{
    "page": 0,
    "first_key": "",
    "last_key": "",
    "rows": [],
    "message": "",
    "cursor_field": null,
    "route": "/admin/users/update?userId=USER0002"
}
```

`u` routes as `U` does, and `D` or `d` routes to `/admin/users/delete?userId=` and the user ID.

Other outcomes:

- A non-blank code other than `U`, `u`, `D` or `d`, on a row whose `user_id` is not blank, sets
    `Invalid selection. Valid values are U and D`, and the list is browsed again from
    `search_user_id`. When that literal stays, the answer is 400
    (`UserSelectionErrorResponse`) with the browsed `rows`, `page`, `first_key` and `last_key`.
    When the browse replaces it with a paging literal, the answer is a 200 list with `route` null.
- 503 `Unable to lookup User...`.

### POST /api/v1/admin/users

Replaces CU01 / COUSR01C / COUSR01 (ENTER); role admin. Checks that every field is non-blank, in
screen order, and adds the user. Any user type is accepted, as in COUSR01C; only `A` gives admin
access. The password is stored as a bcrypt hash of the text as entered, case kept.

| Body field (`UserCreateRequest`) | Type | Description |
|---|---|---|
| `first_name` | string, at most 20 | First name |
| `last_name` | string, at most 20 | Last name |
| `user_id` | string, at most 8 | The new user ID |
| `password` | string, at most 8 | The new password |
| `user_type` | string, at most 1 | The user type: `A` for an admin |

This command adds a regular user:

```bash
curl -s -b var/session.cookies -X POST \
    http://127.0.0.1:8000/api/v1/admin/users \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"first_name": "Ada", "last_name": "Lovelace",
        "user_id": "NEW_USER_ID", "password": "NEW_PASSWORD", "user_type": "U"}'
```

For the user ID `TEMP0001`, the response has status 200 and carries this `UserMessageResponse`
body:

```json
{"message": "User TEMP0001 has been added ...", "cursor_field": "first_name"}
```

Sign-on upper-cases the keyed password before verifying it, so a password added with lower-case
letters can never sign on, as in the legacy programs.

Other outcomes, each naming its field:

- 400 `First Name can NOT be empty...`, and the same form for `Last Name`, `User ID`, `Password`
    and `User Type`.
- 409 `User ID already exist...` (`user_id`).
- 503 `Unable to Add User...`.

F12 on COUSR01 has no endpoint. The program answers it without reading any field, with the
literal `Invalid key pressed. Please see below...`, and the page shows that literal itself.

### GET /api/v1/admin/users/{userId}

Replaces CU02 / COUSR02C / COUSR02 and CU03 / COUSR03C / COUSR03 (ENTER with a user ID); role
admin. Fetches a user for the update or the delete screen. No password or password hash is ever
returned.

| Parameter | In | Type | Description |
|---|---|---|---|
| `userId` | path | string, at most 8 | The user ID as keyed |
| `screen` | query | `update` or `delete`; default `update` | The screen that fetches |

This command fetches a user for the delete screen:

```bash
curl -s -b var/session.cookies \
    'http://127.0.0.1:8000/api/v1/admin/users/USER_ID?screen=delete'
```

The response has status 200 and carries this `UserDetailResponse` body:

```json
{
    "user_id": "TEMP0001",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "user_type": "U",
    "message": "Press PF5 key to delete this user ..."
}
```

With `screen=update`, `message` is `Press PF5 key to save your updates ...`.

Other outcomes:

- 400 `User ID can NOT be empty...` (`user_id`): the ID is blank, sent as `%20`.
- 404 `User ID NOT found...` (`user_id`).
- 503 `Unable to lookup User...`.

### PUT /api/v1/admin/users/{userId}

Replaces CU02 / COUSR02C / COUSR02 (F5, and F3, which saves before returning); role admin.
Rewrites the user. F12 returns to the admin menu without a request.

| Parameter | In | Type | Description |
|---|---|---|---|
| `userId` | path | string, at most 8 | The fetched user ID |

| Body field (`UserUpdateRequest`) | Type | Description |
|---|---|---|
| `first_name` | string, at most 20 | First name |
| `last_name` | string, at most 20 | Last name |
| `password` | string, at most 8 | A new password, or blank to keep the stored one |
| `user_type` | string, at most 1 | User type |

COUSR02C showed the stored password and compared it with the keyed one. A hash supplies neither,
so the password is handled this way:

- A blank `password` means unchanged, and the stored hash is kept.
- A non-blank `password` is a change only when it does not verify against the stored hash. It then
    replaces the hash with a hash of the text as entered.
- When no field differs from the stored user, the answer is 400 `Please modify to update ...`.

This command changes a user's last name and keeps the password:

```bash
curl -s -b var/session.cookies -X PUT \
    http://127.0.0.1:8000/api/v1/admin/users/USER_ID \
    -H 'Origin: http://127.0.0.1:3000' \
    -H 'Content-Type: application/json' \
    -d '{"first_name": "Ada", "last_name": "King", "password": "",
        "user_type": "U"}'
```

For the user ID `TEMP0001`, the response has status 200 and this body:

```json
{"message": "User TEMP0001 has been updated ...", "cursor_field": "first_name"}
```

Other outcomes, each naming its field:

- 400 `User ID can NOT be empty...`, `First Name can NOT be empty...`,
    `Last Name can NOT be empty...` or `User Type can NOT be empty...`. The password is not
    required here.
- 400 `Please modify to update ...` (`first_name`): nothing changed.
- 404 `User ID NOT found...` (`user_id`).
- 503 `Unable to Update User...` (`first_name`): the batch window is held, another request
    holds the user's row past the lock timeout, or the user file cannot be read or rewritten.

### DELETE /api/v1/admin/users/{userId}

Replaces CU03 / COUSR03C / COUSR03 (F5); role admin. Deletes the user.

| Parameter | In | Type | Description |
|---|---|---|---|
| `userId` | path | string, at most 8 | The fetched user ID |

This command deletes a user:

```bash
curl -s -b var/session.cookies -X DELETE \
    http://127.0.0.1:8000/api/v1/admin/users/USER_ID \
    -H 'Origin: http://127.0.0.1:3000'
```

For the user ID `TEMP0001`, the response has status 200 and this body:

```json
{"message": "User TEMP0001 has been deleted ...", "cursor_field": "user_id"}
```

Other outcomes:

- 400 `User ID can NOT be empty...` (`user_id`).
- 404 `User ID NOT found...` (`user_id`): nothing is deleted.
- 503 `Unable to Update User...`.

