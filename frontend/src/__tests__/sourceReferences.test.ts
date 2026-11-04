/** @jest-environment node */
// Tests for: AAP 0.10.5 / 0.11.3 source-reference headers in frontend/src/app/**/page.tsx and
// frontend/src/components/*.tsx.
import { readdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";

interface ExpectedSource {
    route: string;
    map: string;
    program: string;
}

interface ExpectedComponentSource {
    fileName: string;
    identifiers: readonly string[];
}

const SOURCE_ROOT = resolve(__dirname, "..");
const APP_DIRECTORY = join(SOURCE_ROOT, "app");
const COMPONENTS_DIRECTORY = join(SOURCE_ROOT, "components");
const PAGE_FILE_NAME = "page.tsx";
const COMPONENT_EXTENSION = ".tsx";
const EXPECTED_PAGE_COUNT = 17;
const REPLACES_MARKER = "Replaces:";
const REPLACES_LINE_PREFIX = "// Replaces:";
const REPLACES_LINE_START_PATTERN = new RegExp(`^${REPLACES_LINE_PREFIX}`);
const USE_CLIENT_DIRECTIVE_PATTERN = /^["']use client["'];?$/;
const CODE_START_PATTERN = /^(import|export)\b/;
// Each component serves many maps, so it names BMS fields, maps and copybooks, never one program.
const EXPECTED_COMPONENT_SOURCES: readonly ExpectedComponentSource[] = [
    {
        fileName: "ConfirmField.tsx",
        identifiers: ["CONFIRM", "COTRN02", "COBIL00", "CORPT00"],
    },
    {
        fileName: "FunctionKeyBar.tsx",
        identifiers: ["ENTER", "F3", "F4", "F5", "F7", "F8", "F12"],
    },
    {
        fileName: "MessageLine.tsx",
        identifiers: ["ERRMSG", "INFOMSG", "CSMSG01Y", "CSMSG02Y"],
    },
    {
        fileName: "ScreenShell.tsx",
        identifiers: [
            "TITLE01",
            "TITLE02",
            "TRNNAME",
            "PGMNAME",
            "CURDATE",
            "CURTIME",
            "COTTL01Y",
            "CSDAT01Y",
        ],
    },
    {
        fileName: "SelectionTable.tsx",
        identifiers: ["COCRDLI", "COTRN00", "COUSR00", "CRDSEL1", "SEL0001"],
    },
];
const EXPECTED_SHARED_COMPONENTS = EXPECTED_COMPONENT_SOURCES.map((source) => source.fileName);
// Routes are relative to src/app, with "/" separators.
const EXPECTED_SOURCES: readonly ExpectedSource[] = [
    { route: "signon", map: "COSGN00", program: "COSGN00C" },
    { route: "menu", map: "COMEN01", program: "COMEN01C" },
    { route: "admin", map: "COADM01", program: "COADM01C" },
    { route: "accounts/view", map: "COACTVW", program: "COACTVWC" },
    { route: "accounts/update", map: "COACTUP", program: "COACTUPC" },
    { route: "cards", map: "COCRDLI", program: "COCRDLIC" },
    { route: "cards/view", map: "COCRDSL", program: "COCRDSLC" },
    { route: "cards/update", map: "COCRDUP", program: "COCRDUPC" },
    { route: "transactions", map: "COTRN00", program: "COTRN00C" },
    { route: "transactions/view", map: "COTRN01", program: "COTRN01C" },
    { route: "transactions/add", map: "COTRN02", program: "COTRN02C" },
    { route: "reports/transactions", map: "CORPT00", program: "CORPT00C" },
    { route: "billpay", map: "COBIL00", program: "COBIL00C" },
    { route: "admin/users", map: "COUSR00", program: "COUSR00C" },
    { route: "admin/users/add", map: "COUSR01", program: "COUSR01C" },
    { route: "admin/users/update", map: "COUSR02", program: "COUSR02C" },
    { route: "admin/users/delete", map: "COUSR03", program: "COUSR03C" },
];

// A root src/app/page.tsx yields route "." and fails the route comparison: "/" is a redirect.
function CollectPageRoutes(): string[] {
    const entries = readdirSync(APP_DIRECTORY, { recursive: true, encoding: "utf8" });
    return entries
        .filter((entry) => basename(entry) === PAGE_FILE_NAME)
        .map((entry) => dirname(entry).split(sep).join("/"))
        .sort();
}

// The header may sit above or below the "use client" directive; Next.js accepts both.
function ReadHeaderBlock(filePath: string): string[] {
    const lines = readFileSync(filePath, "utf8")
        .split(/\r?\n/)
        .map((line) => line.trim());
    const codeStart = lines.findIndex((line) => CODE_START_PATTERN.test(line));
    const headerLines = codeStart === -1 ? lines : lines.slice(0, codeStart);
    return headerLines.filter((line) => line !== "" && !USE_CLIENT_DIRECTIVE_PATTERN.test(line));
}

// The word boundary keeps the map COSGN00 from matching inside the program name COSGN00C.
function ContainsWord(text: string, word: string): boolean {
    return new RegExp(`\\b${word}\\b`).test(text);
}

function PagePath(route: string): string {
    return join(APP_DIRECTORY, ...route.split("/"), PAGE_FILE_NAME);
}

function ListComponentFiles(): string[] {
    return readdirSync(COMPONENTS_DIRECTORY, "utf8")
        .filter((fileName) => fileName.endsWith(COMPONENT_EXTENSION))
        .sort();
}

describe("one App Router page per in-scope BMS map (AAP 0.11.3)", () => {
    it("finds exactly 17 page.tsx files", () => {
        expect(CollectPageRoutes()).toHaveLength(EXPECTED_PAGE_COUNT);
    });

    it("maps every page route to exactly one BMS map", () => {
        const expectedRoutes = EXPECTED_SOURCES.map((source) => source.route).sort();
        expect(CollectPageRoutes()).toEqual(expectedRoutes);
    });

    it("lists 17 distinct maps and programs", () => {
        const maps = new Set(EXPECTED_SOURCES.map((source) => source.map));
        const programs = new Set(EXPECTED_SOURCES.map((source) => source.program));
        expect(maps.size).toBe(EXPECTED_PAGE_COUNT);
        expect(programs.size).toBe(EXPECTED_PAGE_COUNT);
    });
});

describe("page source-reference headers (AAP 0.10.5)", () => {
    it.each(EXPECTED_SOURCES)(
        "app/$route/page.tsx names BMS map $map and program $program",
        ({ route, map, program }) => {
            const headerText = ReadHeaderBlock(PagePath(route)).join("\n");
            const observed = {
                route,
                namesReplaces: headerText.includes(REPLACES_MARKER),
                namesMap: ContainsWord(headerText, map),
                namesProgram: ContainsWord(headerText, program),
            };
            expect(observed).toEqual({
                route,
                namesReplaces: true,
                namesMap: true,
                namesProgram: true,
            });
        },
    );
});

describe("shared component source-reference headers (AAP 0.10.5)", () => {
    it("contains the five shared components of AAP 0.7.4", () => {
        expect(ListComponentFiles()).toEqual(expect.arrayContaining(EXPECTED_SHARED_COMPONENTS));
    });

    it.each(ListComponentFiles())("components/%s opens with a // Replaces: header", (fileName) => {
        const firstHeaderLine = ReadHeaderBlock(join(COMPONENTS_DIRECTORY, fileName))[0] ?? "";
        expect(firstHeaderLine).toMatch(REPLACES_LINE_START_PATTERN);
    });

    it.each(EXPECTED_COMPONENT_SOURCES)(
        "components/$fileName header names its legacy source identifiers",
        ({ fileName, identifiers }) => {
            const headerText = ReadHeaderBlock(join(COMPONENTS_DIRECTORY, fileName)).join("\n");
            const missingIdentifiers = identifiers.filter(
                (identifier) => !ContainsWord(headerText, identifier),
            );
            expect({ fileName, missingIdentifiers }).toEqual({ fileName, missingIdentifiers: [] });
        },
    );
});
