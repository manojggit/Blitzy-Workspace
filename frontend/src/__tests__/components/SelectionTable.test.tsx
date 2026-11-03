// Tests SelectionTable, which replaces the COCRDLI CRDSEL1-7, COTRN00 SEL0001-10 and COUSR00
// SEL0001-10 selection fields.
import { ThemeProvider } from "@mui/material/styles";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";

import { CARD_DEMO_THEME } from "@/app/theme";
import {
    SelectionTable,
    type SelectionColumn,
    type SelectionRow,
} from "@/components/SelectionTable";

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
