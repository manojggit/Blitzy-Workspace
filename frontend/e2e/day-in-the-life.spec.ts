// CardDemo day-in-the-life end-to-end flow (Technical Specification 0.9.2; success item 4 of
// 0.9.1: a regular user performs the online functions, and batch output reflects the online add).
//
// Exercises, in the order of 0.9.2: sign-on CC00/COSGN00C (map COSGN00), Account View
// CAVW/COACTVWC (map COACTVW), Transaction Add CT02/COTRN02C (map COTRN02), then the batch chain
// from POSTTRAN/CBTRN02C through INTCALC/CBACT04C, TRANBKP, COMBTRAN, CREASTMT/CBSTM03A and
// TRANIDX, and finally the statement CREASTMT writes.
// Sources: cbl/COTRN02C.cbl, bms/COTRN02.bms, cbl/COACTVWC.cbl, bms/COACTVW.bms, cpy/COMEN02Y.cpy,
// cbl/CBSTM03A.CBL, jcl/CREASTMT.JCL, jcl/POSTTRAN.jcl, jcl/INTCALC.jcl, data/ASCII/cardxref.txt.
//
// Global setup has run seed-all, so the sample data is loaded and transactions.transactions is
// empty. Expected values come from the committed reconciliation fixtures; every amount is compared
// as exact integer cents (bigint), never as a floating-point number.
import fs from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import {
    DescribeBatchRun,
    ExpectMessage,
    FIXTURE_DIR,
    MESSAGE_TIMEOUT_MS,
    OutputDir,
    RegularUser,
    RunBatchCli,
    SignOn,
    WaitForPath,
} from "./global-setup";

const ACCOUNT_ID = "00000000004";
// Account 00000000004's only card in the cross-reference (data/ASCII/cardxref.txt line 13,
// loader_rows/card_xref.csv). STATEMNT.PS never prints card numbers, so the card's statement
// block is found by its Account ID line.
const CARD_NUMBER = "2988091353094312";
// The first TRAN-ID COTRN02C allocates over an empty TRANSACT file.
const NEW_TRAN_ID = "0000000000000001";
const TRAN_DESCRIPTION = "E2E PURCHASE";
const TRAN_AMOUNT_CENTS = BigInt(5000);
// COTRN02C lines 728-731 STRING 'Transaction added successfully. ' with ' Your Tran ID is ', so
// two blanks follow the first period.
const ADD_SUCCESS_MESSAGE = `Transaction added successfully.  Your Tran ID is ${NEW_TRAN_ID}.`;

const AS_OF = "2022-07-18T00:00:00";
const RUN_ALL_ARGS = ["run-all", "--start-at", "POSTTRAN", "--as-of", AS_OF] as const;
// POSTTRAN rejects some sample daily records and ends with the legacy RC 4.
const EXIT_POSTTRAN_REJECTS = 4;

const MENU_ROUTE = "/menu";
// COMEN02Y option 1 and option 8.
const ACCOUNT_VIEW_OPTION = "Account View";
const ACCOUNT_VIEW_ROUTE = "/accounts/view";
const TRANSACTION_ADD_OPTION = "Transaction Add";
const TRANSACTION_ADD_ROUTE = "/transactions/add";

// The FunctionKeyBar names each button "<legend label> (<key>)": COACTVW's ENTER searches and
// COTRN02's legend reads ENTER=Continue.
const ACCOUNT_VIEW_ENTER_NAME = "Search (Enter)";
const TRANSACTION_ADD_ENTER_NAME = "Continue (Enter)";

// The BMS INITIAL texts the pages use as labels. Current Balance must not match Current Cycle.
const ACCOUNT_NUMBER_LABEL = /Account Number/i;
const CURRENT_BALANCE_LABEL = /^\s*Current Balance/i;
const TRANSACTION_ACCOUNT_LABEL = /Acct #/i;

// COTRN02 in map order. Card # stays blank: COTRN02C accepts the account or the card.
const TRANSACTION_FIELDS: ReadonlyArray<{ label: RegExp; value: string }> = [
    { label: TRANSACTION_ACCOUNT_LABEL, value: ACCOUNT_ID },
    { label: /Type CD/i, value: "01" },
    { label: /Category CD/i, value: "0001" },
    { label: /^\s*Source/i, value: "POS TERM" },
    { label: /Description/i, value: TRAN_DESCRIPTION },
    { label: /^\s*Amount/i, value: "+00000050.00" },
    { label: /Orig Date/i, value: "2022-07-17" },
    { label: /Proc Date/i, value: "2022-07-17" },
    { label: /Merchant ID/i, value: "000000001" },
    { label: /Merchant Name/i, value: "E2E MERCHANT" },
    { label: /Merchant City/i, value: "E2E CITY" },
    { label: /Merchant Zip/i, value: "00000" },
    { label: /confirm/i, value: "Y" },
];

const SEED_ACCOUNTS_CSV = path.join(FIXTURE_DIR, "loader_rows", "accounts.csv");
const FINAL_ACCOUNTS_CSV = path.join(FIXTURE_DIR, "intcalc_accounts.csv");
const EXPECTED_STATEMENT = path.join(FIXTURE_DIR, "creastmt_STATEMNT.PS");
const STATEMENTS_DIRECTORY = "statements";
const STATEMENT_FILE_NAME = "STATEMNT.PS";
const ACCT_ID_COLUMN = "acct_id";
const CURR_BALANCE_COLUMN = "curr_balance";

// CBSTM03A STATEMENT-LINES, 80 columns (cbl/CBSTM03A.CBL lines 86-146).
// ST-LINE0 (line 86) and ST-LINE15 (line 143) open and close each statement.
const STATEMENT_START_MARKER = "START OF STATEMENT";
const STATEMENT_END_MARKER = "END OF STATEMENT";
// ST-LINE7 (line 107): the X(20) literal, then ST-ACCT-ID X(20).
const ACCOUNT_ID_PREFIX = "Account ID         :";
// ST-LINE8 (line 111): the X(20) literal, then ST-CURR-BAL 9(9).99- in columns 20-32.
const CURRENT_BALANCE_PREFIX = "Current Balance    :";
const BALANCE_START = 20;
const BALANCE_END = 33;
// ST-LINE14A (line 138): the literal, 56 blanks, "$", then ST-TOTAL-TRAMT Z(9).99-.
const TOTAL_PREFIX = "Total EXP:";
// ST-LINE14 (line 132): ST-TRANID X(16) at 0-15, a blank, ST-TRANDT X(49) at 17-65, "$" at 66,
// then ST-TRANAMT Z(9).99- at 67-79. ST-LINE14A puts its "$" and amount in the same columns.
const DESCRIPTION_START = 17;
const DESCRIPTION_END = 66;
const DOLLAR_COLUMN = 66;
const AMOUNT_START = 67;
const DOLLAR_SIGN = "$";

// The batch step alone runs chain steps 13-18.
const DAY_TIMEOUT_MS = 360_000;

const STEP_TITLES = {
    signOn: `Sign on as the regular user and reach ${MENU_ROUTE}`,
    seededBalance: `Account View shows the seeded balance of ${ACCOUNT_ID}`,
    addTransaction: `Transaction Add (option 8) writes ${TRAN_DESCRIPTION} as ${NEW_TRAN_ID}`,
    endOfDayBatch: `${RUN_ALL_ARGS.join(" ")} exits ${EXIT_POSTTRAN_REJECTS}`,
    finalBalance: "Account View shows the post-INTCALC balance",
    statement: `STATEMNT.PS for card ${CARD_NUMBER} lists the new transaction and totals`,
} as const;

// The whole trimmed amount text, matched before any digit is read. At most one sign: a leading
// "+" or "-", which blanks may separate from the digits, or a trailing "-" directly after the
// decimals, as the 9(9).99- and Z(9).99- pictures print a negative value. Those pictures print
// a blank, never "+", for a positive value, so a trailing "+" is rejected, and the lookahead
// rejects a leading sign together with a trailing "-". Then contiguous integer digits, blank
// when Z(9).99- prints a zero integer part (".00"), one decimal point and exactly two decimals.
// Groups: leading sign, integer digits, decimals, trailing sign.
const AMOUNT_PATTERN = /^(?![+-].*-$)(?:([+-]) *)?(\d*)\.(\d{2})(-)?$/;
const NEGATIVE_SIGN = "-";
const CENTS_PER_UNIT = BigInt(100);
const TRAILING_CR_PATTERN = /\r$/;
const QUOTED_CELL_PATTERN = /^"(.*)"$/;

interface CsvTable {
    csvPath: string;
    header: readonly string[];
    rows: readonly (readonly string[])[];
}

interface MenuOptionTarget {
    label: string;
    route: string;
}

interface StatementTotals {
    balanceCents: bigint;
    totalCents: bigint;
}

/**
 * Exact cents of an amount text, once whitespace around it is trimmed: a CSV or Account View
 * decimal (40.00, -12.34), a sign-led screen amount (+         40.00), or a statement picture,
 * 9(9).99- (000001945.87 , 000000040.00-) or Z(9).99- (       50.00 ,        47.88-, and
 *          .00  for zero). AMOUNT_PATTERN gives the grammar; any other text, including a
 * currency sign, grouping commas, embedded blanks or a repeated sign, is rejected.
 */
function ParseAmountCents(text: string): bigint {
    const match = AMOUNT_PATTERN.exec(text.trim());
    if (match === null) {
        throw new Error(`Unrecognised amount text ${JSON.stringify(text)}`);
    }
    const [, leadingSign, integerDigits = "", decimalDigits = "", trailingSign] = match;
    const cents = BigInt(integerDigits || "0") * CENTS_PER_UNIT + BigInt(decimalDigits);
    const isNegative = leadingSign === NEGATIVE_SIGN || trailingSign === NEGATIVE_SIGN;
    return isNegative ? -cents : cents;
}

// The file's records: CRLF or LF terminated, with the empty text after the last terminator dropped.
function SplitRecords(text: string): string[] {
    const records = text.split("\n").map((record) => record.replace(TRAILING_CR_PATTERN, ""));
    if (records[records.length - 1] === "") {
        records.pop();
    }
    return records;
}

// The fixture CSVs quote no cell that holds a comma, so a plain split is exact.
function SplitCsvCells(line: string): string[] {
    return line.split(",").map((cell) => cell.replace(QUOTED_CELL_PATTERN, "$1"));
}

function ReadCsvTable(csvPath: string): CsvTable {
    const [headerLine = "", ...rowLines] = SplitRecords(fs.readFileSync(csvPath, "utf8"));
    return { csvPath, header: SplitCsvCells(headerLine), rows: rowLines.map(SplitCsvCells) };
}

function ColumnIndex(table: CsvTable, column: string): number {
    const index = table.header.indexOf(column);
    if (index < 0) {
        throw new Error(`Column ${column} is missing from ${table.csvPath}`);
    }
    return index;
}

function FindCsvRow(table: CsvTable, keyColumn: string, keyValue: string): readonly string[] {
    const keyIndex = ColumnIndex(table, keyColumn);
    const row = table.rows.find((cells) => cells[keyIndex] === keyValue);
    if (row === undefined) {
        throw new Error(`No row with ${keyColumn} ${keyValue} in ${table.csvPath}`);
    }
    return row;
}

/** The fixture's curr_balance of ACCOUNT_ID, in cents. */
function ReadAccountsCsvBalance(csvPath: string): bigint {
    const table = ReadCsvTable(csvPath);
    const row = FindCsvRow(table, ACCT_ID_COLUMN, ACCOUNT_ID);
    return ParseAmountCents(row[ColumnIndex(table, CURR_BALANCE_COLUMN)] ?? "");
}

// A fresh main menu, then the listed option's ListItemButton, which posts its option number.
async function OpenMenuOption(page: Page, option: MenuOptionTarget): Promise<void> {
    await page.goto(MENU_ROUTE);
    await WaitForPath(page, MENU_ROUTE);
    await page.getByRole("button", { name: option.label }).click();
    await WaitForPath(page, option.route);
}

// The read-only ACURBAL field; blank until the lookup has answered.
async function ReadDisplayedBalanceCents(page: Page): Promise<bigint | null> {
    const shownText = await page.getByLabel(CURRENT_BALANCE_LABEL).inputValue();
    if (shownText.trim() === "") {
        return null;
    }
    return ParseAmountCents(shownText);
}

// Typing waits until the screen has put the cursor on its first field, as the map's IC
// attribute did, so no keystroke reaches a screen that is not interactive yet.
async function WaitForCursor(page: Page, label: RegExp): Promise<void> {
    await expect(page.getByLabel(label)).toBeFocused({ timeout: MESSAGE_TIMEOUT_MS });
}

/** CAVW: ENTER on ACCOUNT_ID shows the balance the fixture holds for it. */
async function VerifyAccountBalance(page: Page, csvPath: string): Promise<void> {
    const expectedCents = ReadAccountsCsvBalance(csvPath);
    await OpenMenuOption(page, { label: ACCOUNT_VIEW_OPTION, route: ACCOUNT_VIEW_ROUTE });
    await WaitForCursor(page, ACCOUNT_NUMBER_LABEL);
    await page.getByLabel(ACCOUNT_NUMBER_LABEL).fill(ACCOUNT_ID);
    await page.getByRole("button", { name: ACCOUNT_VIEW_ENTER_NAME, exact: true }).click();
    await expect
        .poll(() => ReadDisplayedBalanceCents(page), {
            message: `Account View should show ${ACCOUNT_ID}'s ${path.basename(csvPath)} balance`,
            timeout: MESSAGE_TIMEOUT_MS,
        })
        .toBe(expectedCents);
}

/**
 * CT02: one ENTER with confirm Y adds the transaction, because COTRN02C evaluates CONFIRMI once
 * the key and data edits have passed.
 */
async function AddTransaction(page: Page): Promise<void> {
    await OpenMenuOption(page, { label: TRANSACTION_ADD_OPTION, route: TRANSACTION_ADD_ROUTE });
    await WaitForCursor(page, TRANSACTION_ACCOUNT_LABEL);
    for (const field of TRANSACTION_FIELDS) {
        await page.getByLabel(field.label).fill(field.value);
    }
    await page.getByRole("button", { name: TRANSACTION_ADD_ENTER_NAME, exact: true }).click();
    await ExpectMessage(page, ADD_SUCCESS_MESSAGE);
}

async function SignOnAsRegularUser(page: Page): Promise<void> {
    await SignOn(page, RegularUser());
    await WaitForPath(page, MENU_ROUTE);
}

/**
 * Resumes the cycle seed-all opened and runs chain steps 13-18: POSTTRAN, INTCALC, TRANBKP
 * (occurrence 2), COMBTRAN, CREASTMT and TRANIDX. Starting at POSTTRAN skips the first TRANBKP
 * (step 6), which in the legacy order would archive and delete the transaction just added. The
 * exit status 4 is POSTTRAN's rejects, the legacy RC 4.
 */
function RunEndOfDayBatch(): void {
    const run = RunBatchCli(RUN_ALL_ARGS);
    expect(run.status, DescribeBatchRun(RUN_ALL_ARGS, run)).toBe(EXIT_POSTTRAN_REJECTS);
}

function IsFileNotFound(error: unknown): boolean {
    return error instanceof Error && (error as NodeJS.ErrnoException).code === "ENOENT";
}

// latin1 maps each byte to one character, so every column offset is a byte offset.
function ReadStatementLines(filePath: string): string[] {
    try {
        return SplitRecords(fs.readFileSync(filePath, "latin1"));
    } catch (error) {
        if (IsFileNotFound(error)) {
            throw new Error(
                `Statement file not found at ${filePath}; CREASTMT (step 17) did not write it`,
                { cause: error },
            );
        }
        throw error;
    }
}

// One block per statement, from its START OF STATEMENT line to its END OF STATEMENT line.
function SplitStatementBlocks(lines: readonly string[]): string[][] {
    const blocks: string[][] = [];
    let current: string[] | null = null;
    for (const line of lines) {
        if (line.includes(STATEMENT_START_MARKER)) {
            current = [];
        }
        if (current !== null) {
            current.push(line);
        }
        if (current !== null && line.includes(STATEMENT_END_MARKER)) {
            blocks.push(current);
            current = null;
        }
    }
    return blocks;
}

// ST-ACCT-ID is X(20), so the 11-digit account is followed by a blank.
function FindAccountBlock(
    blocks: readonly string[][],
    accountId: string,
    source: string,
): string[] {
    const accountLine = `${ACCOUNT_ID_PREFIX}${accountId} `;
    const matches = blocks.filter((block) => block.some((line) => line.startsWith(accountLine)));
    const [block] = matches;
    if (matches.length !== 1 || block === undefined) {
        throw new Error(
            `Expected one statement for account ${accountId} in ${source}, found ${matches.length}`,
        );
    }
    return block;
}

function ReadAccountBlock(filePath: string): string[] {
    return FindAccountBlock(
        SplitStatementBlocks(ReadStatementLines(filePath)),
        ACCOUNT_ID,
        filePath,
    );
}

function FindLineStartingWith(block: readonly string[], prefix: string): string {
    const line = block.find((candidate) => candidate.startsWith(prefix));
    if (line === undefined) {
        throw new Error(`No statement line starts with ${JSON.stringify(prefix)}`);
    }
    return line;
}

function ReadStatementTotals(block: readonly string[]): StatementTotals {
    const balanceLine = FindLineStartingWith(block, CURRENT_BALANCE_PREFIX);
    const totalLine = FindLineStartingWith(block, TOTAL_PREFIX);
    expect(totalLine.charAt(DOLLAR_COLUMN), `"$" column of ${JSON.stringify(totalLine)}`).toBe(
        DOLLAR_SIGN,
    );
    return {
        balanceCents: ParseAmountCents(balanceLine.slice(BALANCE_START, BALANCE_END)),
        totalCents: ParseAmountCents(totalLine.slice(AMOUNT_START)),
    };
}

// The ST-LINE14 that 6000-WRITE-TRANS writes for the online add.
function ExpectNewTransactionLine(block: readonly string[]): void {
    const line = FindLineStartingWith(block, `${NEW_TRAN_ID} `);
    expect(line.slice(DESCRIPTION_START, DESCRIPTION_END).trimEnd()).toBe(TRAN_DESCRIPTION);
    expect(line.charAt(DOLLAR_COLUMN)).toBe(DOLLAR_SIGN);
    expect(ParseAmountCents(line.slice(AMOUNT_START))).toBe(TRAN_AMOUNT_CENTS);
}

/**
 * The fixture statement was built without the online add, so Total EXP exceeds the fixture's by
 * exactly the added amount (CBSTM03A line 429, ADD TRNX-AMT TO WS-TOTAL-AMT), while Current
 * Balance is the post-INTCALC balance, which a CT02 add never changes.
 */
function VerifyStatement(): void {
    const actualBlock = ReadAccountBlock(
        path.join(OutputDir(), STATEMENTS_DIRECTORY, STATEMENT_FILE_NAME),
    );
    const actualTotals = ReadStatementTotals(actualBlock);
    const expectedTotals = ReadStatementTotals(ReadAccountBlock(EXPECTED_STATEMENT));
    ExpectNewTransactionLine(actualBlock);
    expect(actualTotals.balanceCents, "Current Balance").toBe(
        ReadAccountsCsvBalance(FINAL_ACCOUNTS_CSV),
    );
    expect(actualTotals.totalCents, "Total EXP").toBe(
        expectedTotals.totalCents + TRAN_AMOUNT_CENTS,
    );
}

interface AmountCase {
    text: string;
    cents: bigint;
}

// Every amount form the flow reads, with its exact cents: the fixture CSV and Account View
// decimals, the sign-led screen amount, the ST-CURR-BAL 9(9).99- slice and the ST-TRANAMT and
// ST-TOTAL-TRAMT Z(9).99- slice, with negatives in leading and trailing position and both zeros.
const ACCEPTED_AMOUNT_CASES: readonly AmountCase[] = [
    { text: "40.00", cents: BigInt(4000) },
    { text: "1686.98", cents: BigInt(168698) },
    { text: "-12.34", cents: BigInt(-1234) },
    { text: "0.00", cents: BigInt(0) },
    { text: "+         40.00", cents: BigInt(4000) },
    { text: "-         40.00", cents: BigInt(-4000) },
    { text: "+           .00", cents: BigInt(0) },
    { text: "000001945.87 ", cents: BigInt(194587) },
    { text: "000000040.00-", cents: BigInt(-4000) },
    { text: "     1472.64 ", cents: BigInt(147264) },
    { text: "       50.00 ", cents: BigInt(5000) },
    { text: "       47.88-", cents: BigInt(-4788) },
    { text: "         .05-", cents: BigInt(-5) },
    { text: "         .00 ", cents: BigInt(0) },
    { text: ".00", cents: BigInt(0) },
];

// Embedded, repeated, doubled or detached signs, a trailing "+", split digit groups, the wrong
// number of decimals, no decimal point, blank text, a currency sign and a grouping comma.
const REJECTED_AMOUNT_TEXTS: readonly string[] = [
    "5-0.00",
    "5 0.00",
    "--50.00",
    "+-50.00",
    "+50.00-",
    "-50.00-",
    "50.00 -",
    "50.00+",
    "50 .00",
    "50. 00",
    "50.0",
    "50.000",
    "50",
    ".",
    "",
    "   ",
    "$50.00",
    "5,0.00",
];

function ExpectAcceptedAmount(amountCase: AmountCase): void {
    expect(ParseAmountCents(amountCase.text)).toBe(amountCase.cents);
}

function ExpectRejectedAmount(text: string): void {
    expect(() => ParseAmountCents(text)).toThrow(
        `Unrecognised amount text ${JSON.stringify(text)}`,
    );
}

// Parsing cases with no page or browser. They run ahead of the flow, which this serial file
// skips when a case fails, so the statement and balance checks never rest on a broken parser.
function DeclareParseAmountCentsTests(): void {
    for (const amountCase of ACCEPTED_AMOUNT_CASES) {
        test(`accepts ${JSON.stringify(amountCase.text)} as ${amountCase.cents} cents`, () =>
            ExpectAcceptedAmount(amountCase));
    }
    for (const text of REJECTED_AMOUNT_TEXTS) {
        test(`rejects ${JSON.stringify(text)}`, () => ExpectRejectedAmount(text));
    }
}

test.describe("ParseAmountCents", DeclareParseAmountCentsTests);

// The flow changes the shared database and is not repeatable: a retry would be given TRAN-ID
// 0000000000000002, and global setup reseeds only once per run.
test.describe.configure({ mode: "serial", retries: 0 });

test("regular user: sign on, view account, add transaction, run batch, read statement", async ({
    page,
}) => {
    test.setTimeout(DAY_TIMEOUT_MS);
    await test.step(STEP_TITLES.signOn, () => SignOnAsRegularUser(page));
    await test.step(STEP_TITLES.seededBalance, () => VerifyAccountBalance(page, SEED_ACCOUNTS_CSV));
    await test.step(STEP_TITLES.addTransaction, () => AddTransaction(page));
    await test.step(STEP_TITLES.endOfDayBatch, async () => RunEndOfDayBatch());
    // A CT02 add changes no balance (COTRN02C ADD-TRANSACTION), so the INTCALC fixture applies.
    await test.step(STEP_TITLES.finalBalance, () => VerifyAccountBalance(page, FINAL_ACCOUNTS_CSV));
    await test.step(STEP_TITLES.statement, async () => VerifyStatement());
});
