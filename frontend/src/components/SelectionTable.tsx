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
    type SxProps,
    type Theme,
} from "@mui/material";
import type { ReactElement } from "react";

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
// A content-sized field grows when a code replaces the blank, and the table's automatic layout
// then shifts every column, so the field keeps one width: MUI's select padding plus the widest
// glyph a one-character code can hold (code page 037), in theme spacing units of 4 dp each.
const SELECTION_FIELD_WIDTH_UNITS = 16;
const SELECTION_FIELD_STYLE: SxProps<Theme> = {
    width: (theme) => theme.spacing(SELECTION_FIELD_WIDTH_UNITS),
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

// Select labels its open listbox only through a labelId, so the list takes its field's name.
function SelectionMenuPropsFor(rowNumber: number): Partial<MenuProps> {
    return { slotProps: { list: { "aria-label": SelectionLabelFor(rowNumber) } } };
}

function SelectionCell({ row, rowNumber, tableProps }: SelectionCellProps) {
    const { allowedCodes, onSelectionChange } = tableProps;
    return (
        <Select
            size="small"
            displayEmpty
            sx={SELECTION_FIELD_STYLE}
            error={row.isError ?? false}
            value={row.selectedCode}
            inputProps={{ "aria-label": SelectionLabelFor(rowNumber) }}
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
