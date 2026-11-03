// Replaces: the selection-code list rows of COCRDLI (CRDSEL1-7),
// COTRN00 (SEL0001-0010) and COUSR00 (SEL0001-0010)
"use client";

import {
    MenuItem,
    Select,
    Table,
    TableBody,
    TableCell,
    TableContainer,
    TableHead,
    TableRow,
    type MenuProps,
    type SelectChangeEvent,
    type SelectProps,
    type SxProps,
    type Theme,
} from "@mui/material";
import { useState, type HTMLAttributes, type ReactElement, type SyntheticEvent } from "react";

import { IsScreenEnterKey, SCREEN_ENTER_PROPS } from "@/lib/useFunctionKeys";

import { ErrorMessageReference } from "@/components/MessageLine";

/** A protected display column of a list map; `field` is the key of `SelectionRow.cells`. */
export interface SelectionColumn {
    field: string;
    header: string;
}

/**
 * A list row: the page's stable key, its display texts, and the code entered for it. `isError`
 * marks a code the server named in `field_errors`, as the maps colour a flagged code field red.
 */
export interface SelectionRow {
    rowKey: string;
    cells: Record<string, string>;
    selectedCode: string;
    isError?: boolean;
}

export interface SelectionTableProps {
    columns: readonly SelectionColumn[];
    rows: readonly SelectionRow[];
    allowedCodes: readonly string[];
    onSelectionChange: (rowKey: string, code: string) => void;
    selectionHeader?: string;
}

type SelectionHandlers = Pick<SelectionTableProps, "allowedCodes" | "onSelectionChange">;

interface SelectionTableHeadProps {
    columns: readonly SelectionColumn[];
    selectionHeader: string;
}

interface SelectionTableRowProps {
    row: SelectionRow;
    rowNumber: number;
    columns: readonly SelectionColumn[];
    tableProps: SelectionHandlers;
}

type SelectionCellProps = Omit<SelectionTableRowProps, "columns">;

// The open state of one row's menu, spread onto its Select.
interface SelectionMenuState {
    open: boolean;
    onOpen: (event: SyntheticEvent) => void;
    onClose: () => void;
}

interface SelectionTableBodyProps {
    rows: readonly SelectionRow[];
    columns: readonly SelectionColumn[];
    tableProps: SelectionHandlers;
}

const DEFAULT_SELECTION_HEADER = "Sel";
const BLANK_SELECTION = "";
const BLANK_SELECTION_LABEL = "No selection";
// An empty MenuItem collapses to its padding; a zero-width space keeps one text line, the way
// MUI renders an empty Select value, so the blank option is as tall as the code options.
const ZERO_WIDTH_SPACE = "\u200b";
// Enter on a closed selection field is the screen's ENTER, as it was from the 3270 field holding
// the cursor; the marker rides on the combobox element MUI renders.
const SELECTION_DISPLAY_PROPS: HTMLAttributes<HTMLDivElement> & typeof SCREEN_ENTER_PROPS = {
    ...SCREEN_ENTER_PROPS,
};
// A content-sized field grows when a code replaces the blank, and the table's automatic layout
// then shifts every column, so the field keeps one width: MUI's select padding plus the widest
// glyph a one-character code can hold (code page 037), in theme spacing units of 4 dp each.
const SELECTION_FIELD_WIDTH_UNITS = 16;
const SELECTION_FIELD_STYLE: SxProps<Theme> = {
    width: (theme) => theme.spacing(SELECTION_FIELD_WIDTH_UNITS),
};
// The props every row's selection field shares: its size, the blank shown as a value, its fixed
// width and the screen-ENTER marker on its combobox.
const SELECTION_FIELD_PROPS: Pick<
    SelectProps<string>,
    "size" | "displayEmpty" | "sx" | "SelectDisplayProps"
> = {
    size: "small",
    displayEmpty: true,
    sx: SELECTION_FIELD_STYLE,
    SelectDisplayProps: SELECTION_DISPLAY_PROPS,
};

/** The accessible name of a row's selection field; `rowNumber` counts from 1, as CRDSEL1. */
export function SelectionLabelFor(rowNumber: number): string {
    return `Selection for row ${rowNumber}`;
}

// The server alone validates codes, so a code it echoes back outside allowedCodes is still
// offered and shown as entered. The blank option is rendered separately, so it is dropped here.
function OptionCodesFor(allowedCodes: readonly string[], selectedCode: string): string[] {
    const offeredCodes = new Set([...allowedCodes, selectedCode]);
    offeredCodes.delete(BLANK_SELECTION);
    return Array.from(offeredCodes);
}

// Select reads its MenuItems as direct children (never a Fragment), so they are built as an array.
function SelectionOptionsFor(
    allowedCodes: readonly string[],
    selectedCode: string,
): ReactElement[] {
    const blankOption = (
        <MenuItem key={BLANK_SELECTION} value={BLANK_SELECTION} aria-label={BLANK_SELECTION_LABEL}>
            <span aria-hidden="true">{ZERO_WIDTH_SPACE}</span>
        </MenuItem>
    );
    const codeOptions = OptionCodesFor(allowedCodes, selectedCode).map((code) => (
        <MenuItem key={code} value={code}>
            {code}
        </MenuItem>
    ));
    return [blankOption, ...codeOptions];
}

function IsScreenEnterPress(event: SyntheticEvent): boolean {
    return event.nativeEvent instanceof KeyboardEvent && IsScreenEnterKey(event.nativeEvent);
}

// The menu opens on Space, the arrow keys and the pointer, and stays closed on the Enter the
// screen takes as its ENTER, so one press is one action; inside the open menu Enter picks an
// option. React recognises hooks only by the camelCase "use" prefix, so this name is a
// framework-mandated exception to the MK_Modernization Rule's PascalCase function naming.
function useSelectionMenu(): SelectionMenuState {
    const [isOpen, setIsOpen] = useState(false);
    return {
        open: isOpen,
        onOpen: (event) => {
            if (!IsScreenEnterPress(event)) {
                setIsOpen(true);
            }
        },
        onClose: () => setIsOpen(false),
    };
}

// A flagged code field is tied to the ERRMSG line, which MUI's SelectInput passes on to the
// combobox, so assistive technology reads the literal when the cursor lands on the row.
function SelectionInputProps(row: SelectionRow, rowNumber: number) {
    return {
        "aria-label": SelectionLabelFor(rowNumber),
        ...ErrorMessageReference(row.isError ?? false),
    };
}

// Select labels its open listbox only through a labelId, so the list takes its field's name.
function SelectionMenuPropsFor(rowNumber: number): Partial<MenuProps> {
    return { slotProps: { list: { "aria-label": SelectionLabelFor(rowNumber) } } };
}

function SelectionCell({ row, rowNumber, tableProps }: SelectionCellProps) {
    const { allowedCodes, onSelectionChange } = tableProps;
    const menu = useSelectionMenu();
    return (
        <Select
            {...SELECTION_FIELD_PROPS}
            error={row.isError ?? false}
            value={row.selectedCode}
            {...menu}
            inputProps={SelectionInputProps(row, rowNumber)}
            MenuProps={SelectionMenuPropsFor(rowNumber)}
            onChange={(event: SelectChangeEvent<string>) =>
                onSelectionChange(row.rowKey, event.target.value)
            }
        >
            {SelectionOptionsFor(allowedCodes, row.selectedCode)}
        </Select>
    );
}

function SelectionTableRow({ row, rowNumber, columns, tableProps }: SelectionTableRowProps) {
    return (
        <TableRow>
            <TableCell>
                <SelectionCell row={row} rowNumber={rowNumber} tableProps={tableProps} />
            </TableCell>
            {columns.map((column) => (
                <TableCell key={column.field}>{row.cells[column.field] ?? ""}</TableCell>
            ))}
        </TableRow>
    );
}

function SelectionTableBody({ rows, columns, tableProps }: SelectionTableBodyProps) {
    return (
        <TableBody>
            {rows.map((row, index) => (
                <SelectionTableRow
                    key={row.rowKey}
                    row={row}
                    rowNumber={index + 1}
                    columns={columns}
                    tableProps={tableProps}
                />
            ))}
        </TableBody>
    );
}

function SelectionTableHead({ columns, selectionHeader }: SelectionTableHeadProps) {
    return (
        <TableHead>
            <TableRow>
                <TableCell>{selectionHeader}</TableCell>
                {columns.map((column) => (
                    <TableCell key={column.field}>{column.header}</TableCell>
                ))}
            </TableRow>
        </TableHead>
    );
}

/**
 * The rows of a list map with a one-character selection field each. Codes are forwarded to
 * `onSelectionChange` unchanged: the page posts them and the server returns the legacy messages.
 */
export function SelectionTable(props: SelectionTableProps) {
    const { columns, rows, allowedCodes, onSelectionChange, selectionHeader } = props;
    const tableProps: SelectionHandlers = { allowedCodes, onSelectionChange };
    return (
        <TableContainer>
            <Table size="small">
                <SelectionTableHead
                    columns={columns}
                    selectionHeader={selectionHeader ?? DEFAULT_SELECTION_HEADER}
                />
                <SelectionTableBody rows={rows} columns={columns} tableProps={tableProps} />
            </Table>
        </TableContainer>
    );
}
