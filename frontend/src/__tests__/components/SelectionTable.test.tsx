// Tests SelectionTable, which replaces the COCRDLI CRDSEL1-7, COTRN00 SEL0001-10 and COUSR00
// SEL0001-10 selection fields.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import type { ReactElement } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import {
    SelectionTable,
    type SelectionColumn,
    type SelectionRow,
} from "@/components/SelectionTable";
import { SCREEN_ENTER_ATTRIBUTE } from "@/lib/useFunctionKeys";

/** One list map: its replaced BMS selection fields, codes, rows and trimmed INITIAL headers. */
interface MapCase {
    mapName: string;
    fieldNames: string;
    allowedCodes: readonly string[];
    rowCount: number;
    columns: readonly SelectionColumn[];
    selectionHeader?: string;
}

interface CodeCase {
    mapName: string;
    fieldNames: string;
    code: string;
    mapCase: MapCase;
}

const COCRDLI_CASE: MapCase = {
    mapName: "COCRDLI",
    fieldNames: "CRDSEL1-7",
    allowedCodes: ["S", "U"],
    rowCount: 7,
    columns: [
        { field: "acct_id", header: "Account Number" },
        { field: "card_num_masked", header: "Card Number" },
        { field: "active_status", header: "Active" },
    ],
    selectionHeader: "Select",
};

const COTRN00_CASE: MapCase = {
    mapName: "COTRN00",
    fieldNames: "SEL0001-10",
    allowedCodes: ["S"],
    rowCount: 10,
    columns: [
        { field: "tran_id", header: "Transaction ID" },
        { field: "tran_date", header: "Date" },
        { field: "description", header: "Description" },
        { field: "amount", header: "Amount" },
    ],
};

const COUSR00_CASE: MapCase = {
    mapName: "COUSR00",
    fieldNames: "SEL0001-10",
    allowedCodes: ["U", "D"],
    rowCount: 10,
    columns: [
        { field: "user_id", header: "User ID" },
        { field: "first_name", header: "First Name" },
        { field: "last_name", header: "Last Name" },
        { field: "user_type", header: "Type" },
    ],
};

const MAP_CASES: readonly MapCase[] = [COCRDLI_CASE, COTRN00_CASE, COUSR00_CASE];

// One case per code a map accepts: S and U (COCRDLI), S (COTRN00), U and D (COUSR00).
const CODE_CASES: readonly CodeCase[] = MAP_CASES.flatMap((mapCase) =>
    mapCase.allowedCodes.map((code) => ({
        mapName: mapCase.mapName,
        fieldNames: mapCase.fieldNames,
        code,
        mapCase,
    })),
);

function RenderWithTheme(ui: ReactElement) {
    return render(<ThemeProvider theme={CARD_DEMO_THEME}>{ui}</ThemeProvider>);
}

// Row numbers count from 1, as CRDSEL1 and SEL0001 do.
function RowNumbersUpTo(rowCount: number): number[] {
    return Array.from(Array(rowCount).keys(), (index) => index + 1);
}

function SelectionFieldName(rowNumber: number): string {
    return `Selection for row ${rowNumber}`;
}

function BuildRow(columns: readonly SelectionColumn[], rowNumber: number): SelectionRow {
    const cells = Object.fromEntries(
        columns.map((column) => [column.field, `${column.field} ${rowNumber}`]),
    );
    return { rowKey: `KEY${rowNumber}`, cells, selectedCode: "" };
}

function BuildRows(columns: readonly SelectionColumn[], rowCount: number): SelectionRow[] {
    return RowNumbersUpTo(rowCount).map((rowNumber) => BuildRow(columns, rowNumber));
}

function RenderTable(mapCase: MapCase, onSelectionChange = jest.fn()) {
    RenderWithTheme(
        <SelectionTable
            columns={mapCase.columns}
            rows={BuildRows(mapCase.columns, mapCase.rowCount)}
            allowedCodes={mapCase.allowedCodes}
            onSelectionChange={onSelectionChange}
            selectionHeader={mapCase.selectionHeader}
        />,
    );
    return onSelectionChange;
}

function ColumnHeaderTexts(): (string | null)[] {
    return screen.getAllByRole("columnheader").map((header) => header.textContent);
}

function HeadersOf(mapCase: MapCase): string[] {
    return mapCase.columns.map((column) => column.header);
}

// A body row holds its own selection field, named for its row number, and its cell texts.
function ExpectBodyRow(
    bodyRow: HTMLElement,
    columns: readonly SelectionColumn[],
    rowNumber: number,
) {
    const rowScope = within(bodyRow);
    expect(rowScope.getByRole("combobox", { name: SelectionFieldName(rowNumber) })).toBeVisible();
    for (const column of columns) {
        expect(rowScope.getByText(`${column.field} ${rowNumber}`)).toBeVisible();
    }
}

function ExpectSelectionFields(mapCase: MapCase) {
    const bodyRows = screen.getAllByRole("row").slice(1);
    for (const [index, bodyRow] of bodyRows.entries()) {
        ExpectBodyRow(bodyRow, mapCase.columns, index + 1);
    }
}

describe("SelectionTable list header", () => {
    it("COTRN00 SEL0001-10 header shows the default Sel and every column", () => {
        RenderTable(COTRN00_CASE);
        expect(screen.getByRole("columnheader", { name: "Sel" })).toBeVisible();
        expect(ColumnHeaderTexts()).toEqual(["Sel", ...HeadersOf(COTRN00_CASE)]);
    });

    it("COCRDLI CRDSEL1-7 selectionHeader Select overrides the default", () => {
        RenderTable(COCRDLI_CASE);
        expect(screen.getByRole("columnheader", { name: "Select" })).toBeVisible();
        expect(screen.queryByRole("columnheader", { name: "Sel" })).toBeNull();
        expect(ColumnHeaderTexts()).toEqual(["Select", ...HeadersOf(COCRDLI_CASE)]);
    });
});

describe("SelectionTable list rows", () => {
    it.each(MAP_CASES)("$mapName renders all $rowCount rows of $fieldNames", (mapCase) => {
        RenderTable(mapCase);
        expect(screen.getAllByRole("row")).toHaveLength(mapCase.rowCount + 1);
        ExpectSelectionFields(mapCase);
    });
});

describe("SelectionTable offered codes", () => {
    it.each(MAP_CASES)("$mapName $fieldNames offers blank plus its codes", async (mapCase) => {
        const user = userEvent.setup();
        RenderTable(mapCase);
        await user.click(screen.getByRole("combobox", { name: SelectionFieldName(1) }));
        const offeredValues = within(screen.getByRole("listbox"))
            .getAllByRole("option")
            .map((option) => option.getAttribute("data-value"));
        expect(offeredValues).toEqual(["", ...mapCase.allowedCodes]);
    });
});

describe("SelectionTable open list name", () => {
    it.each(MAP_CASES)("$mapName $fieldNames names the open list as its row", async (mapCase) => {
        const user = userEvent.setup();
        RenderTable(mapCase);
        const lastRowName = SelectionFieldName(mapCase.rowCount);
        await user.click(screen.getByRole("combobox", { name: lastRowName }));
        expect(screen.getByRole("listbox", { name: lastRowName })).toBeInTheDocument();
    });
});

describe("SelectionTable selection", () => {
    it.each(CODE_CASES)(
        "$mapName $fieldNames passes code $code unchanged",
        async ({ code, mapCase }) => {
            const user = userEvent.setup();
            const onSelectionChange = RenderTable(mapCase);
            await user.click(screen.getByRole("combobox", { name: SelectionFieldName(2) }));
            await user.click(screen.getByRole("option", { name: code }));
            expect(onSelectionChange).toHaveBeenCalledTimes(1);
            expect(onSelectionChange).toHaveBeenCalledWith("KEY2", code);
        },
    );
});

// A key pressed on the closed field of row 1, the first tab stop. Plain Enter is the screen's
// ENTER, so it leaves the menu closed; Space, the arrow keys and a chorded Enter open it.
const CLOSED_FIELD_KEY_CASES: readonly { press: string; keys: string; opens: boolean }[] = [
    { press: "Enter", keys: "{Enter}", opens: false },
    { press: "Space", keys: "[Space]", opens: true },
    { press: "ArrowDown", keys: "{ArrowDown}", opens: true },
    { press: "ArrowUp", keys: "{ArrowUp}", opens: true },
    { press: "Ctrl+Enter", keys: "{Control>}{Enter}{/Control}", opens: true },
];

async function PressOnClosedField({ keys, opens }: { keys: string; opens: boolean }) {
    const user = userEvent.setup();
    const onSelectionChange = RenderTable(COCRDLI_CASE);
    await user.tab();
    const field = screen.getByRole("combobox", { name: SelectionFieldName(1) });
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute(SCREEN_ENTER_ATTRIBUTE);
    await user.keyboard(keys);
    expect(field).toHaveAttribute("aria-expanded", String(opens));
    expect(screen.queryByRole("listbox") !== null).toBe(opens);
    expect(onSelectionChange).not.toHaveBeenCalled();
}

describe("SelectionTable keyboard", () => {
    it.each(CLOSED_FIELD_KEY_CASES)(
        "COCRDLI CRDSEL1 $press on the closed field opens its menu: $opens",
        PressOnClosedField,
    );

    it("COCRDLI CRDSEL1 Enter in the open menu picks the highlighted code and closes it", async () => {
        const user = userEvent.setup();
        const onSelectionChange = RenderTable(COCRDLI_CASE);
        await user.tab();
        await user.keyboard("{ArrowDown}");
        await user.keyboard("{ArrowDown}{Enter}");
        expect(onSelectionChange.mock.calls).toEqual([["KEY1", "S"]]);
        await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    });
});

// The input root carries MUI's error state, which colours the field red as DFHRED did.
function IsMarkedAsError(rowNumber: number): boolean {
    const field = screen.getByRole("combobox", { name: SelectionFieldName(rowNumber) });
    return field.closest(".MuiInputBase-root")?.classList.contains("Mui-error") ?? false;
}

// Rows 2 and 3 of COCRDLI carry the server's flag; the other five do not.
function RenderFlaggedCardRows() {
    const rows = BuildRows(COCRDLI_CASE.columns, COCRDLI_CASE.rowCount).map((row, index) => ({
        ...row,
        isError: index === 1 || index === 2,
    }));
    RenderWithTheme(
        <SelectionTable
            columns={COCRDLI_CASE.columns}
            rows={rows}
            allowedCodes={COCRDLI_CASE.allowedCodes}
            onSelectionChange={jest.fn()}
        />,
    );
}

// The id MessageLine gives the ERRMSG Alert, which a flagged code field is described by.
const ERRMSG_ID = "message-line-errmsg";

function DescriptionOf(rowNumber: number): string | null {
    const field = screen.getByRole("combobox", { name: SelectionFieldName(rowNumber) });
    return field.getAttribute("aria-describedby");
}

describe("SelectionTable field errors", () => {
    it("COCRDLI CRDSEL1-7 marks only the rows flagged by the server", () => {
        RenderFlaggedCardRows();
        const marked = RowNumbersUpTo(COCRDLI_CASE.rowCount).map(IsMarkedAsError);
        expect(marked).toEqual([false, true, true, false, false, false, false]);
    });

    it("COCRDLI CRDSEL1-7 ties only the flagged rows to the ERRMSG line", () => {
        RenderFlaggedCardRows();
        const descriptions = RowNumbersUpTo(COCRDLI_CASE.rowCount).map(DescriptionOf);
        expect(descriptions).toEqual([null, ERRMSG_ID, ERRMSG_ID, null, null, null, null]);
    });
});

const CONTROLLED_ROW_NUMBER = 2;
const CONTROLLED_ROW_KEY = "KEY2";
const UNLISTED_CODE = "X";
const BLANK_OPTION_NAME = "No selection";
const ZERO_WIDTH_SPACE = "\u200b";

type SelectionChangeMock = jest.Mock<void, [string, string]>;

let consoleWarningSpies: jest.SpyInstance[] = [];

function SpyOnConsoleWarnings() {
    consoleWarningSpies = [jest.spyOn(console, "error"), jest.spyOn(console, "warn")];
}

// React reports a duplicate option key through console.error, and MUI a value that matches no
// option through console.warn.
function ExpectNoConsoleWarnings() {
    const warningCalls = consoleWarningSpies.flatMap((spy) => spy.mock.calls);
    for (const spy of consoleWarningSpies) {
        spy.mockRestore();
    }
    expect(warningCalls).toEqual([]);
}

function RowsWithCode(mapCase: MapCase, rowKey: string, code: string): SelectionRow[] {
    return BuildRows(mapCase.columns, mapCase.rowCount).map((row) =>
        row.rowKey === rowKey ? { ...row, selectedCode: code } : row,
    );
}

function TableTree(
    mapCase: MapCase,
    rows: readonly SelectionRow[],
    onSelectionChange: SelectionChangeMock,
): ReactElement {
    return (
        <ThemeProvider theme={CARD_DEMO_THEME}>
            <SelectionTable
                columns={mapCase.columns}
                rows={rows}
                allowedCodes={mapCase.allowedCodes}
                onSelectionChange={onSelectionChange}
                selectionHeader={mapCase.selectionHeader}
            />
        </ThemeProvider>
    );
}

// The blank option, like an empty Select, renders only a zero-width space and never a code.
function DisplayedCode(rowNumber: number): string {
    const field = screen.getByRole("combobox", { name: SelectionFieldName(rowNumber) });
    return (field.textContent ?? "").replaceAll(ZERO_WIDTH_SPACE, "");
}

async function OfferedValuesOf(user: UserEvent, rowNumber: number): Promise<(string | null)[]> {
    await user.click(screen.getByRole("combobox", { name: SelectionFieldName(rowNumber) }));
    return within(screen.getByRole("listbox"))
        .getAllByRole("option")
        .map((option) => option.getAttribute("data-value"));
}

async function ChooseOption(user: UserEvent, optionName: string) {
    const field = screen.getByRole("combobox", { name: SelectionFieldName(CONTROLLED_ROW_NUMBER) });
    await user.click(field);
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: optionName }));
}

// The page's part: the code reported for a row comes back as that row's selectedCode.
function EchoReportedCode(
    rerender: (ui: ReactElement) => void,
    mapCase: MapCase,
    onSelectionChange: SelectionChangeMock,
) {
    const [rowKey, code] = onSelectionChange.mock.lastCall ?? [];
    if (rowKey === undefined || code === undefined) {
        throw new Error("SelectionTable reported no selection to echo back");
    }
    rerender(TableTree(mapCase, RowsWithCode(mapCase, rowKey, code), onSelectionChange));
}

async function ShowsRetainedCode({ code, mapCase }: CodeCase) {
    const user = userEvent.setup();
    render(TableTree(mapCase, RowsWithCode(mapCase, CONTROLLED_ROW_KEY, code), jest.fn()));
    const expectedCodes = RowNumbersUpTo(mapCase.rowCount).map((rowNumber) =>
        rowNumber === CONTROLLED_ROW_NUMBER ? code : "",
    );
    expect(RowNumbersUpTo(mapCase.rowCount).map(DisplayedCode)).toEqual(expectedCodes);
    const offeredValues = await OfferedValuesOf(user, CONTROLLED_ROW_NUMBER);
    expect(offeredValues).toEqual(["", ...mapCase.allowedCodes]);
}

async function ClearsEchoedCodeThroughBlank({ code, mapCase }: CodeCase) {
    const user = userEvent.setup();
    const onSelectionChange: SelectionChangeMock = jest.fn();
    const rows = BuildRows(mapCase.columns, mapCase.rowCount);
    const { rerender } = render(TableTree(mapCase, rows, onSelectionChange));
    await ChooseOption(user, code);
    expect(onSelectionChange.mock.calls).toEqual([[CONTROLLED_ROW_KEY, code]]);
    EchoReportedCode(rerender, mapCase, onSelectionChange);
    expect(DisplayedCode(CONTROLLED_ROW_NUMBER)).toBe(code);
    await ChooseOption(user, BLANK_OPTION_NAME);
    expect(onSelectionChange).toHaveBeenCalledTimes(2);
    expect(onSelectionChange).toHaveBeenLastCalledWith(CONTROLLED_ROW_KEY, "");
    EchoReportedCode(rerender, mapCase, onSelectionChange);
    expect(DisplayedCode(CONTROLLED_ROW_NUMBER)).toBe("");
}

async function OffersEchoedUnlistedCodeLast(mapCase: MapCase) {
    const user = userEvent.setup();
    render(TableTree(mapCase, RowsWithCode(mapCase, CONTROLLED_ROW_KEY, UNLISTED_CODE), jest.fn()));
    expect(DisplayedCode(CONTROLLED_ROW_NUMBER)).toBe(UNLISTED_CODE);
    const offeredValues = await OfferedValuesOf(user, CONTROLLED_ROW_NUMBER);
    expect(offeredValues).toEqual(["", ...mapCase.allowedCodes, UNLISTED_CODE]);
}

async function OffersOneBlankAndEachCodeOnce(mapCase: MapCase) {
    const user = userEvent.setup();
    const repeatedCodes = [...mapCase.allowedCodes, "", ...mapCase.allowedCodes];
    const repeatedCase: MapCase = { ...mapCase, allowedCodes: repeatedCodes };
    render(TableTree(repeatedCase, BuildRows(mapCase.columns, mapCase.rowCount), jest.fn()));
    const offeredValues = await OfferedValuesOf(user, 1);
    expect(offeredValues).toEqual(["", ...mapCase.allowedCodes]);
}

// Every field keeps this width in theme spacing units, so a chosen code never shifts the columns.
const SELECTION_FIELD_WIDTH_UNITS = 16;

function FieldWidth(rowNumber: number): string {
    const field = screen.getByRole("combobox", { name: SelectionFieldName(rowNumber) });
    const fieldRoot = field.closest(".MuiInputBase-root");
    if (fieldRoot === null) {
        throw new Error(`${SelectionFieldName(rowNumber)} has no input root`);
    }
    return getComputedStyle(fieldRoot).width;
}

function ExpectFixedFieldWidths(rowCount: number) {
    const fixedWidth = CARD_DEMO_THEME.spacing(SELECTION_FIELD_WIDTH_UNITS);
    const rowNumbers = RowNumbersUpTo(rowCount);
    expect(rowNumbers.map(FieldWidth)).toEqual(rowNumbers.map(() => fixedWidth));
}

async function KeepsFieldWidthWithEchoedCode({ code, mapCase }: CodeCase) {
    const user = userEvent.setup();
    const onSelectionChange: SelectionChangeMock = jest.fn();
    const rows = BuildRows(mapCase.columns, mapCase.rowCount);
    const { rerender } = render(TableTree(mapCase, rows, onSelectionChange));
    ExpectFixedFieldWidths(mapCase.rowCount);
    await ChooseOption(user, code);
    EchoReportedCode(rerender, mapCase, onSelectionChange);
    expect(DisplayedCode(CONTROLLED_ROW_NUMBER)).toBe(code);
    ExpectFixedFieldWidths(mapCase.rowCount);
}

describe("SelectionTable controlled selectedCode", () => {
    beforeEach(SpyOnConsoleWarnings);
    afterEach(ExpectNoConsoleWarnings);

    it.each(CODE_CASES)(
        "$mapName $fieldNames row 2 shows retained code $code, offers it once, other rows none",
        ShowsRetainedCode,
    );
    it.each(CODE_CASES)(
        "$mapName $fieldNames row 2 shows echoed code $code, then clears it through No selection",
        ClearsEchoedCodeThroughBlank,
    );
    it.each(MAP_CASES)(
        "$mapName $fieldNames shows an echoed code outside its codes and offers it last",
        OffersEchoedUnlistedCodeLast,
    );
    it.each(MAP_CASES)(
        "$mapName $fieldNames offers one blank and each code once from repeated and blank codes",
        OffersOneBlankAndEachCodeOnce,
    );
});

describe("SelectionTable field width", () => {
    beforeEach(SpyOnConsoleWarnings);
    afterEach(ExpectNoConsoleWarnings);

    it.each(CODE_CASES)(
        "$mapName $fieldNames keeps every field one width, blank and with echoed code $code",
        KeepsFieldWidthWithEchoedCode,
    );
});
